"""Jakobs Masher - brings BL3's Masher revolvers to Borderlands 4.

A Masher is a Jakobs revolver whose barrel fires a burst of projectiles instead
of a single slug, each one hitting for a fraction of the card damage. BL4 ships
no such part: every JAK_PS barrel in `Nexus-Data-inv4.ncs` leaves
`projectilespershot` at its default of one, and the shotgun barrels that do set
it (`bor_sg`, `dad_sg`, ...) drive it from a data table row that has no pistol
equivalent. Part behaviour is native C++ and the NCS records cannot be extended
without a full re-encoder, so the variant is applied at runtime instead.

What happens in game: whenever a Jakobs pistol starts firing, its
`WeaponBehavior_FireProjectile` is inspected. Guns whose part roll marks them as
Mashers get `ProjectilesPerShot` raised and `Spread` widened, with `Damage`
scaled down per projectile so the total stays in BL3 territory (6 x 0.4 = 2.4x
card damage at the default settings). Everything is reverted when the mod is
disabled, and the per-gun decision comes from the gun's own parts, so a given
revolver is a Masher in every session or in none.
"""

from __future__ import annotations

import re
import traceback
from typing import Any

import unrealsdk
from mods_base import (
    BoolOption,
    CoopSupport,
    SliderOption,
    build_mod,
    command,
    get_pc,
    hook,
    keybind,
)
from unrealsdk.hooks import Type
from unrealsdk.unreal import BoundFunction, UObject, WeakPointer, WrappedStruct

__version__ = "1.0"
__author__ = "Claude"

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

# The runtime behaviour object that owns ProjectilesPerShot. The game's own
# attribute table maps `weapon_projectile_per_shot` to
# /Script/OakGame.WeaponBehavior_FireProjectile -> ProjectilesPerShot.
FIRE_BEHAVIOUR_CLASS = "WeaponBehavior_FireProjectile"

# Jakobs pistols, as named throughout the NCS inventory data (`inv: JAK_PS`,
# `bodyanimtag: JAK_PS`, `Body_JAK_PS`, ...).
TARGET_WEAPON_TAG = "JAK_PS"

# Every manufacturer/class pair the game uses, for sniffing out which properties
# on a weapon actually identify it.
WEAPON_TAG_RE = re.compile(
    r"(?:BOR|DAD|JAK|MAL|ORD|TED|TOR|VLA)_(?:PS|SG|AR|SM|SR|HW)",
    re.IGNORECASE,
)

# How many part slots to probe through OakWeapon.GetPartValue.
MAX_PART_SLOTS = 16

LOG_PREFIX = "[jakobs_masher]"


def log(msg: str) -> None:
    print(f"{LOG_PREFIX} {msg}")


def debug(msg: str) -> None:
    if verbose_logging.value:
        print(f"{LOG_PREFIX} {msg}")


# --------------------------------------------------------------------------- #
# Options
# --------------------------------------------------------------------------- #

projectiles = SliderOption(
    "Projectiles Per Shot",
    6,
    2,
    12,
    display_name="Projectiles Per Shot",
    description=(
        "How many projectiles a Masher fires per trigger pull. BL3's Masher"
        " barrel fired 6."
    ),
)

damage_scale = SliderOption(
    "Damage Per Projectile",
    0.40,
    0.05,
    1.0,
    step=0.05,
    is_integer=False,
    display_name="Damage Per Projectile",
    description=(
        "Each projectile deals this fraction of the gun's normal damage."
        " 6 projectiles at 0.40 gives 2.4x total, matching BL3."
    ),
)

spread_scale = SliderOption(
    "Spread Multiplier",
    3.0,
    1.0,
    10.0,
    step=0.5,
    is_integer=False,
    display_name="Spread Multiplier",
    description=(
        "How much wider a Masher shoots than the same gun would normally."
        " Higher means more shotgun, less revolver."
    ),
)

masher_frequency = SliderOption(
    "Masher Frequency",
    1,
    0,
    4,
    display_name="Masher Frequency (in 4)",
    description=(
        "Roughly how many Jakobs revolvers out of every four are Mashers."
        " The roll comes from the gun's own parts, so a given revolver is"
        " always a Masher or never one - but changing this setting reshuffles"
        " which guns qualify. 0 disables, 4 converts every Jakobs pistol."
    ),
)

player_weapons_only = BoolOption(
    "Player Weapons Only",
    True,
    "On",
    "Off",
    display_name="Player Weapons Only",
    description=(
        "Only convert guns you are carrying. The sweep sees every weapon actor"
        " in the level, enemies included, and a Masher in enemy hands is a"
        " 2.4x damage enemy. Turn off to convert every Jakobs revolver in the"
        " world."
    ),
)

verbose_logging = BoolOption(
    "Verbose Logging",
    False,
    "On",
    "Off",
    display_name="Verbose Logging",
    description="Log every weapon the mod inspects. Useful for diagnosing.",
)


# --------------------------------------------------------------------------- #
# Runtime discovery
#
# BL4's weapon definitions live in Nexus `.ncs` data, not in UObjects, so a
# weapon actor does not necessarily carry its type name as a directly readable
# property - the first in-game run found none at all. What it does carry are
# references to UE *assets* whose paths are named after the weapon:
# `Body_JAK_PS`, `TriggerFB_JAK_PS`, `/Game/Gear/Weapons/Pistols/JAK/...`.
# Those sit one or two hops away, so identity is gathered by walking the
# weapon's object graph rather than by reading one known property.
# --------------------------------------------------------------------------- #

# How far to walk, and how many objects to touch, when identifying a weapon.
# Generous enough to reach a mesh or a data asset, bounded so a stray reference
# into the world cannot turn this into a full object-graph crawl.
IDENTITY_MAX_DEPTH = 3
IDENTITY_MAX_NODES = 250

# Links that lead *away* from the weapon - up to its owner, the world, the
# player. Following them would escape the weapon entirely.
IDENTITY_SKIP_FIELDS = frozenset(
    {
        "Outer",
        "Class",
        "Owner",
        "Instigator",
        "World",
        "Level",
        "GameInstance",
        "PlayerState",
        "Pawn",
        "Controller",
        "WeaponUser",
        "AttachParent",
        "Parent",
    }
)

# Vehicle turrets are weapons too, and are not what this mod is about.
IGNORED_WEAPON_CLASSES = ("VehicleWeapon",)

# True once we know whether OakWeapon.GetPartValue can be called.
_part_values_work: bool | None = None

# Weapon address -> the strings its object graph yielded. Walking the graph is
# not free, so it happens once per weapon.
_identity_cache: dict[int, list[str]] = {}


def _describe(value: Any) -> str:
    """Stringify an unreal value without blowing up on exotic types."""
    try:
        return str(value)
    except Exception:
        return ""


def _as_object(value: Any) -> UObject | None:
    """Return the value if it is walkable - an unreal object or struct.

    Both answer `_get_address`; only objects answer `_path_name`, which the
    caller handles.
    """
    if isinstance(value, (str, bytes, bool, int, float)) or value is None:
        return None
    try:
        value._get_address()
    except Exception:
        return None
    return value


# Arrays of parts, components or materials are exactly where a weapon's assets
# tend to live, so they get walked too - bounded, like everything else here.
MAX_ARRAY_ELEMENTS = 32


def _elements(value: Any) -> list[Any]:
    """The items of an unreal array, or the value itself if it is not one."""
    if isinstance(value, (str, bytes)) or value is None:
        return [value]
    try:
        length = len(value)
    except Exception:
        return [value]
    try:
        return [value[i] for i in range(min(length, MAX_ARRAY_ELEMENTS))]
    except Exception:
        return [value]


def collect_identity(weapon: UObject) -> list[str]:
    """Walk the weapon's object graph, collecting every string it reaches.

    Breadth-first so the closest references - the ones most likely to name the
    weapon - are seen first, and bounded on both depth and total objects.
    """
    cached = _identity_cache.get(weapon._get_address())
    if cached is not None:
        return cached

    strings: list[str] = []
    seen: set[int] = set()
    queue: list[tuple[UObject, int]] = [(weapon, 0)]

    while queue and len(seen) < IDENTITY_MAX_NODES:
        obj, depth = queue.pop(0)

        try:
            address = obj._get_address()
        except Exception:
            continue
        if address in seen:
            continue
        seen.add(address)

        try:
            strings.append(obj._path_name())
        except Exception:
            pass

        if depth >= IDENTITY_MAX_DEPTH:
            continue

        try:
            fields = dir(obj)
        except Exception:
            continue

        for name in fields:
            if name.startswith("_") or name in IDENTITY_SKIP_FIELDS:
                continue
            try:
                value = getattr(obj, name)
            except Exception:
                continue
            if isinstance(value, BoundFunction):
                continue

            child = _as_object(value)
            if child is not None:
                # Objects and structs both answer `_get_address`, and both can
                # be walked into - a struct field is as likely to hold the
                # asset reference as an object property is.
                queue.append((child, depth + 1))
                continue

            for element in _elements(value):
                nested = _as_object(element)
                if nested is not None:
                    queue.append((nested, depth + 1))
                else:
                    text = _describe(element)
                    if text:
                        strings.append(text)

    _identity_cache[weapon._get_address()] = strings
    return strings


def weapon_identity(weapon: UObject) -> str:
    """Everything the weapon's object graph says about what it is."""
    return " ".join(collect_identity(weapon))


def is_ignored_weapon(weapon: UObject) -> bool:
    try:
        name = weapon.Class.Name
    except Exception:
        return False
    return any(ignored in name for ignored in IGNORED_WEAPON_CLASSES)


def is_jakobs_pistol(weapon: UObject) -> bool:
    """Does this weapon's graph name it as a Jakobs pistol?

    Two spellings appear in the data: the explicit `JAK_PS` tag, used by the
    NCS records and by asset names like `Body_JAK_PS` or `TriggerFB_JAK_PS`,
    and the directory the assets live in, `/Game/Gear/Weapons/Pistols/JAK/`.

    The tag wins whenever one is present. A weapon can legitimately reference
    assets belonging to another weapon - a shared or licensed part - so the
    directory is only trusted when the graph carries no tag at all to judge by.
    """
    if is_ignored_weapon(weapon):
        return False

    identity = collect_identity(weapon)

    tags = {
        match.group(0).upper()
        for text in identity
        for match in WEAPON_TAG_RE.finditer(text)
    }
    if tags:
        return TARGET_WEAPON_TAG in tags

    return any("weapons/pistols/jak" in text.lower() for text in identity)


# Properties that might link a weapon back to whoever is holding it. Which one
# BL4 actually uses is unconfirmed, so all are tried and the winner is logged.
OWNER_FIELDS = ("WeaponUser", "Owner", "Instigator", "BodyOwner")

_ownership_field: str | None = None
_ownership_warned = False


def _player_objects() -> set[int]:
    """Addresses that count as 'the local player'."""
    addresses: set[int] = set()
    try:
        pc = get_pc()
    except Exception:
        return addresses
    if pc is None:
        return addresses

    for candidate in (pc,):
        try:
            addresses.add(candidate._get_address())
        except Exception:
            pass
    for field in ("Pawn", "AcknowledgedPawn", "Character", "PlayerState"):
        try:
            obj = getattr(pc, field)
        except Exception:
            continue
        if obj is None:
            continue
        try:
            addresses.add(obj._get_address())
        except Exception:
            continue
    return addresses


def is_player_weapon(weapon: UObject) -> bool | None:
    """Is this weapon held by the local player?

    Returns None when it cannot be determined - none of the candidate owner
    links exist - so the caller can decide what to do rather than silently
    treating an unknown as a no.
    """
    global _ownership_field

    targets = _player_objects()
    if not targets:
        return None

    saw_a_link = False
    for field in OWNER_FIELDS:
        try:
            owner = getattr(weapon, field)
        except Exception:
            continue
        if owner is None:
            continue
        saw_a_link = True

        # The holder may be a component or body rather than the pawn itself,
        # so follow a couple of Owner hops upward.
        node = owner
        for _ in range(4):
            try:
                if node._get_address() in targets:
                    if _ownership_field != field:
                        _ownership_field = field
                        log(f"player weapons identified via '{field}'")
                    return True
                node = node.Owner
            except Exception:
                break
            if node is None:
                break

    # A link existed but led somewhere else: definitely not ours. No link at
    # all: we genuinely cannot tell.
    return False if saw_a_link else None


def part_values(weapon: UObject) -> tuple[int, ...]:
    """Read the weapon's part indices through OakWeapon.GetPartValue.

    These are the numbers the serial format stores, so they are identical every
    time the same gun is loaded - exactly what a stable per-gun decision needs.
    Returns an empty tuple if the function is unavailable.
    """
    global _part_values_work

    if _part_values_work is False:
        return ()

    try:
        get_part_value = weapon.GetPartValue
    except Exception:
        _part_values_work = False
        return ()

    values: list[int] = []
    for slot in range(MAX_PART_SLOTS):
        try:
            values.append(int(get_part_value(slot)))
        except Exception:
            break

    if not values:
        if _part_values_work is None:
            log("OakWeapon.GetPartValue is not callable - using a stat fingerprint")
        _part_values_work = False
        return ()

    if _part_values_work is None:
        log(f"reading part slots via GetPartValue ({len(values)} slots)")
        _part_values_work = True
    return tuple(values)


def stat_fingerprint(behaviours: list[UObject]) -> tuple[int, ...]:
    """Fallback identity, from the rolled numbers on the fire behaviour."""
    values: list[int] = []
    for behaviour in behaviours:
        for field in ("FireRate", "Spread", "ShotAmmoCost", "AutomaticBurstCount"):
            try:
                values.append(int(round(float(getattr(behaviour, field)) * 1000)))
            except Exception:
                continue
    return tuple(values)


# --------------------------------------------------------------------------- #
# Deciding which revolvers are Mashers
# --------------------------------------------------------------------------- #


def rolls_masher(weapon: UObject, behaviours: list[UObject]) -> bool:
    """Is this particular revolver a Masher?

    BL3 made Masher a barrel part, so Masher-ness here is likewise a fixed
    property of the gun rather than a per-shot coin flip: the decision is
    derived from the weapon's part indices, the same numbers its serial
    encodes. The same revolver therefore answers the same way in every
    session, and only a fraction of drops qualify.

    (The part slots are read as an opaque list - the game does not tell us
    which index is the barrel - so this is a hash over the whole roll rather
    than a literal barrel-variant check.)
    """
    identity = part_values(weapon) or stat_fingerprint(behaviours)
    if not identity:
        return False

    # A cheap, stable spread of the part roll over 0..3.
    mixed = 0
    for index, value in enumerate(identity):
        mixed = (mixed * 31 + value * (index + 7)) & 0xFFFFFFFF
    mixed ^= mixed >> 16
    mixed = (mixed * 0x45D9F3B) & 0xFFFFFFFF
    mixed ^= mixed >> 16

    return (mixed & 3) < masher_frequency.value


# --------------------------------------------------------------------------- #
# Applying the variant
# --------------------------------------------------------------------------- #

# Behaviour address -> what we last wrote and what it was before us. Tracking
# both lets the game keep recalculating damage (level ups, skills, anointments)
# without us either fighting it or compounding our own multiplier.
_touched: dict[int, dict[str, Any]] = {}

# Weapon address -> (path name, behaviour pointers, is a masher). The path name
# guards against the engine recycling an address for a different weapon.
_weapon_cache: dict[int, tuple[str, list[WeakPointer], bool]] = {}


# Which property carries each knob is not obvious, and neither is its type.
# In game these are NOT plain floats: reading one yields a WrappedStruct, and
# writing an int fails with "Unable to cast ... to WrappedStruct". That matches
# the NCS, where the fire aspect stores `projectilespershot: {constant: 0.0,
# datatablevalue: {...}}` - a Gbx attribute value, not a number. The binary
# names the type: GbxAttributeFloat / GbxAttributeInteger, both deriving
# GbxAttributeBase, whose scalar member is `BaseValue`.
FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "projectiles": (
        "ProjectilesPerShot",
        "ProjectilesperShot",
        "ProjectileCount",
        "NumProjectiles",
    ),
    "damage": ("Damage", "BaseDamage", "DamageScale", "DamageScalar"),
    "spread": ("Spread", "BaseSpread", "SpreadScale", "SpreadScalar"),
}

# The scalar inside a Gbx attribute struct, most likely first.
STRUCT_SCALAR_CANDIDATES = ("BaseValue", "Value", "Constant", "BaseValueConstant")

# knob -> (property name, struct subfield or None), or None once we know the
# knob is unreachable.
_resolved_fields: dict[str, tuple[str, str | None] | None] = {}

# Knobs and struct shapes we have already complained about.
_missing_reported: set[str] = set()
_shape_reported: set[str] = set()
_drift_reported: set[int] = set()


def _field_names(obj: Any) -> list[str]:
    """Every field name on an object or struct, for diagnostics."""
    try:
        return sorted(n for n in dir(obj) if not n.startswith("_"))
    except Exception:
        return []


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _report_shape(field: str, struct: Any) -> None:
    """Log a struct we cannot find a scalar in, once per property."""
    if field in _shape_reported:
        return
    _shape_reported.add(field)

    log(f"{field} is a struct with no obvious scalar; its fields are:")
    for name in _field_names(struct):
        try:
            value = getattr(struct, name)
        except Exception as exc:
            log(f"    {name} = <unreadable: {exc}>")
            continue
        log(f"    {name} = {_describe(value)[:120]}")


def _read_scalar(obj: UObject, field: str) -> tuple[float, str | None] | None:
    """Read a number at `obj.field`, reaching into a struct if need be.

    Returns (value, subfield) where subfield is None for a plain scalar, or
    None if no number is reachable.
    """
    try:
        raw = getattr(obj, field)
    except Exception:
        return None

    if _is_number(raw):
        return float(raw), None

    for candidate in STRUCT_SCALAR_CANDIDATES:
        try:
            inner = getattr(raw, candidate)
        except Exception:
            continue
        if _is_number(inner):
            return float(inner), candidate

    _report_shape(field, raw)
    return None


def _write_scalar(obj: UObject, field: str, subfield: str | None, value: float) -> bool:
    """Write a number, through the struct when there is one.

    A struct read back from a property may be a copy, so it is assigned back
    after being modified.
    """
    for attempt in (value, float(value), int(value)):
        try:
            if subfield is None:
                setattr(obj, field, attempt)
            else:
                struct = getattr(obj, field)
                setattr(struct, subfield, attempt)
                setattr(obj, field, struct)
            return True
        except Exception as exc:
            last = exc
    where = field if subfield is None else f"{field}.{subfield}"
    log(f"could not write {where} = {value!r}: {last}")
    return False


def _resolve_field(behaviour: UObject, knob: str) -> tuple[str, str | None] | None:
    """The first candidate property for this knob that yields a number."""
    if knob in _resolved_fields:
        return _resolved_fields[knob]

    for candidate in FIELD_CANDIDATES[knob]:
        found = _read_scalar(behaviour, candidate)
        if found is None:
            continue
        _, subfield = found
        _resolved_fields[knob] = (candidate, subfield)
        where = candidate if subfield is None else f"{candidate}.{subfield}"
        log(f"'{knob}' resolved to '{where}'")
        return _resolved_fields[knob]

    _resolved_fields[knob] = None
    if knob not in _missing_reported:
        _missing_reported.add(knob)
        try:
            class_name = behaviour.Class.Name
        except Exception:
            class_name = "?"
        names = _field_names(behaviour)
        log(
            f"no usable property for '{knob}' on {class_name}"
            f" (tried {', '.join(FIELD_CANDIDATES[knob])})"
        )
        log(f"  {class_name} exposes: {', '.join(names) or 'nothing'}")
    return None


def _scale_field(behaviour: UObject, knob: str, scale: float) -> str | None:
    """Multiply a value, re-basing if the game changed it under us."""
    resolved = _resolve_field(behaviour, knob)
    if resolved is None:
        return None
    field, subfield = resolved

    found = _read_scalar(behaviour, field)
    if found is None:
        return None
    current, _ = found

    state = _touched.setdefault(behaviour._get_address(), {})
    previous = state.get(field)
    if previous is not None and abs(current - previous["applied"]) < 1e-4:
        # Still holding our value, and the scale may have changed in the menu.
        base = previous["original"]
    else:
        # First touch, or the game recalculated it - treat what is there as the
        # new baseline so buffs and debuffs still come through.
        base = current

    wanted = base * scale
    if not _write_scalar(behaviour, field, subfield, wanted):
        return None

    state[field] = {"original": base, "applied": wanted, "subfield": subfield}
    return field if subfield is None else f"{field}.{subfield}"


def _set_field(behaviour: UObject, knob: str, value: float) -> str | None:
    """Set a value outright."""
    resolved = _resolve_field(behaviour, knob)
    if resolved is None:
        return None
    field, subfield = resolved

    found = _read_scalar(behaviour, field)
    if found is None:
        return None
    current, _ = found

    if not _write_scalar(behaviour, field, subfield, value):
        return None

    state = _touched.setdefault(behaviour._get_address(), {})
    if field not in state:
        state[field] = {"original": current, "applied": value, "subfield": subfield}
    else:
        state[field]["applied"] = value
    return field if subfield is None else f"{field}.{subfield}"


def _verify(behaviour: UObject, knob: str) -> tuple[float, float] | None:
    """Read a knob back after writing. Returns (expected, actual)."""
    resolved = _resolved_fields.get(knob)
    if not resolved:
        return None
    field, _ = resolved
    state = _touched.get(behaviour._get_address(), {}).get(field)
    if state is None:
        return None
    found = _read_scalar(behaviour, field)
    if found is None:
        return None
    return float(state["applied"]), found[0]


def check_drift(behaviour: UObject) -> list[str]:
    """Which knobs no longer hold the value we wrote?

    Distinguishes the two ways this can fail silently: the write not sticking
    (the game re-resolves the value from its data-table source) versus the
    write sticking but the game reading the number from somewhere else.
    """
    drifted = []
    for knob in FIELD_CANDIDATES:
        pair = _verify(behaviour, knob)
        if pair is None:
            continue
        expected, actual = pair
        if abs(expected - actual) > 1e-3:
            drifted.append(f"{knob} expected {expected:g} but reads {actual:g}")
    return drifted


def make_masher(behaviour: UObject) -> list[str]:
    """Turn one fire behaviour into a Masher. Returns what actually stuck.

    Every write is read back: a setattr that does not raise is not proof the
    value took, and assuming it was cost a test session.
    """
    # Before writing, notice if what we wrote last time has since been undone.
    reverted = check_drift(behaviour)
    if reverted and behaviour._get_address() not in _drift_reported:
        _drift_reported.add(behaviour._get_address())
        log(f"value(s) did not persist since the last pass: {'; '.join(reverted)}")
        log("  the game is re-resolving these - writing BaseValue is not enough")

    applied: list[str] = []
    for field in (
        _set_field(behaviour, "projectiles", int(projectiles.value)),
        _scale_field(behaviour, "damage", float(damage_scale.value)),
        _scale_field(behaviour, "spread", float(spread_scale.value)),
    ):
        if field is not None:
            applied.append(field)

    # And confirm the writes we just made actually read back.
    rejected = check_drift(behaviour)
    if rejected:
        log(f"write did not take: {'; '.join(rejected)}")

    return applied


def restore_all() -> None:
    """Undo every change we made to behaviours that still exist."""
    restored = 0
    for behaviour in unrealsdk.find_all(FIRE_BEHAVIOUR_CLASS, False):
        state = _touched.get(behaviour._get_address())
        if not state:
            continue
        for field, record in state.items():
            _write_scalar(behaviour, field, record.get("subfield"), record["original"])
        restored += 1
    _touched.clear()
    _weapon_cache.clear()
    _identity_cache.clear()
    _resolved_fields.clear()
    _missing_reported.clear()
    _shape_reported.clear()
    _drift_reported.clear()
    if restored:
        log(f"restored {restored} weapon(s)")


# --------------------------------------------------------------------------- #
# Wiring it to the game
# --------------------------------------------------------------------------- #


def _owns(weapon: UObject, behaviour: UObject) -> bool:
    """Is this behaviour attached to this weapon?"""
    outer = behaviour
    for _ in range(8):
        try:
            outer = outer.Outer
        except Exception:
            return False
        if outer is None:
            return False
        if outer._get_address() == weapon._get_address():
            return True
    return False


def fire_behaviours_of(weapon: UObject) -> list[UObject]:
    found: list[UObject] = []
    for behaviour in unrealsdk.find_all(FIRE_BEHAVIOUR_CLASS, False):
        try:
            if behaviour == behaviour.Class.ClassDefaultObject:
                continue
        except Exception:
            continue
        if _owns(weapon, behaviour):
            found.append(behaviour)
    return found


def owning_weapon(behaviour: UObject) -> UObject | None:
    """Walk up from a fire behaviour to the weapon it belongs to.

    Behaviour classes are all named `WeaponBehavior_*`, so requiring the name
    to *end* in Weapon picks the actor without matching a sibling behaviour.
    """
    outer = behaviour
    for _ in range(8):
        try:
            outer = outer.Outer
        except Exception:
            return None
        if outer is None:
            return None
        try:
            if outer.Class.Name.endswith("Weapon"):
                return outer
        except Exception:
            return None
    return None


def live_weapons() -> dict[int, tuple[UObject, list[UObject]]]:
    """Every live weapon that owns a fire behaviour, keyed by address.

    Walks up from the behaviours rather than down from a weapon class name, so
    it does not care what the weapon actor is actually called.
    """
    by_weapon: dict[int, tuple[UObject, list[UObject]]] = {}

    for behaviour in unrealsdk.find_all(FIRE_BEHAVIOUR_CLASS, False):
        try:
            if behaviour == behaviour.Class.ClassDefaultObject:
                continue
        except Exception:
            continue
        weapon = owning_weapon(behaviour)
        if weapon is None:
            continue
        entry = by_weapon.setdefault(weapon._get_address(), (weapon, []))
        entry[1].append(behaviour)

    return by_weapon


def scan_all() -> int:
    """Process every live weapon, without relying on any hook firing.

    This is what the keybind and `masher scan` use.
    """
    by_weapon = live_weapons()

    for weapon, behaviours in by_weapon.values():
        try:
            process_weapon(weapon, behaviours)
        except Exception:
            log(f"error while processing {weapon._path_name()}:")
            traceback.print_exc()

    return len(by_weapon)


def process_weapon(weapon: UObject, known: list[UObject] | None = None) -> None:
    """Bring one weapon up to date. Cheap on the common path.

    `known` lets a caller that already enumerated the behaviours skip the
    object-list scan - `scan_all` groups them once instead of once per weapon.
    """
    address = weapon._get_address()
    path = weapon._path_name()
    cached = _weapon_cache.get(address)

    if cached is not None and cached[0] == path:
        _, pointers, is_masher = cached
        behaviours = [b for b in (p() for p in pointers) if b is not None]
        if len(behaviours) == len(pointers):
            if is_masher:
                for behaviour in behaviours:
                    make_masher(behaviour)
            return
        # Something was garbage collected - fall through and rescan.

    if not is_jakobs_pistol(weapon):
        _weapon_cache[address] = (path, [], False)
        return

    if player_weapons_only.value:
        global _ownership_warned
        owned = is_player_weapon(weapon)
        if owned is False:
            debug(f"{path} is a Jakobs revolver, but not ours")
            _weapon_cache[address] = (path, [], False)
            return
        if owned is None and not _ownership_warned:
            _ownership_warned = True
            log(
                "cannot tell who owns a weapon - converting all of them,"
                " enemies included. Turn off 'Player Weapons Only' to silence"
                " this, or report it."
            )

    behaviours = known if known is not None else fire_behaviours_of(weapon)
    if not behaviours:
        debug(f"no {FIRE_BEHAVIOUR_CLASS} found under {path}")
        return

    is_masher = rolls_masher(weapon, behaviours)
    _weapon_cache[address] = (path, [WeakPointer(b) for b in behaviours], is_masher)

    if not is_masher:
        debug(f"{weapon._path_name()} is a plain Jakobs revolver")
        return

    applied: list[str] = []
    for behaviour in behaviours:
        applied = make_masher(behaviour)
    log(
        f"Masher: {weapon._path_name()} -> "
        f"{int(projectiles.value)} projectiles ({', '.join(applied) or 'nothing applied'})"
    )


# Which hooks have actually fired, and how often. BL4 may resolve a shot
# without going through the path we expect, so rather than assume, every hook
# counts itself and announces its first call. `masher status` prints the table.
_fires: dict[str, int] = {}


def _fired(name: str) -> None:
    count = _fires.get(name, 0) + 1
    _fires[name] = count
    if count == 1:
        log(f"hook '{name}' fired for the first time")


def _guard(name: str, run) -> None:
    _fired(name)
    try:
        run()
    except Exception:
        log(f"error in hook '{name}':")
        traceback.print_exc()


@hook("/Script/GbxWeapon.Weapon:ServerStartUsing", Type.PRE)
def on_start_using(
    obj: UObject,
    args: WrappedStruct,
    ret: Any,
    func: BoundFunction,
) -> None:
    _guard("ServerStartUsing", lambda: process_weapon(obj))


@hook("/Script/GbxWeapon.Weapon:ServerEquipInterruptible", Type.PRE)
def on_equip(
    obj: UObject,
    args: WrappedStruct,
    ret: Any,
    func: BoundFunction,
) -> None:
    _guard("ServerEquipInterruptible", lambda: process_weapon(obj))


@hook("/Script/GbxWeapon.Weapon:ServerStartReloading", Type.PRE)
def on_reload(
    obj: UObject,
    args: WrappedStruct,
    ret: Any,
    func: BoundFunction,
) -> None:
    _guard("ServerStartReloading", lambda: process_weapon(obj))


@hook("/Script/GbxWeapon.Weapon:PlayEffects", Type.PRE)
def on_play_effects(
    obj: UObject,
    args: WrappedStruct,
    ret: Any,
    func: BoundFunction,
) -> None:
    _guard("PlayEffects", lambda: process_weapon(obj))


@hook("/Script/OakGame.OakCharacter:ClientSetActiveWeaponEquipSlot", Type.POST)
def on_weapon_swap(
    obj: UObject,
    args: WrappedStruct,
    ret: Any,
    func: BoundFunction,
) -> None:
    # This one is on the character, not the weapon, so it cannot name a weapon
    # directly - sweep instead.
    _guard("ClientSetActiveWeaponEquipSlot", scan_all)


HOOKS = (
    on_start_using,
    on_equip,
    on_reload,
    on_play_effects,
    on_weapon_swap,
)


def on_mod_enabled() -> None:
    """Report what actually bound, so a silent mod is diagnosable."""
    bound = []
    unbound = []
    for hook_obj in HOOKS:
        for func_name, _hook_type in hook_obj.hook_funcs:
            short = func_name.rsplit(":", 1)[-1]
            (bound if hook_obj.get_active_count() else unbound).append(short)

    log(f"enabled - {int(projectiles.value)} projectiles at "
        f"{float(damage_scale.value):.2f}x damage, "
        f"{int(masher_frequency.value)} in 4 revolvers")
    log(f"hooks bound: {', '.join(bound) if bound else 'NONE'}")
    if unbound:
        log(f"hooks NOT bound: {', '.join(unbound)}")
    log("press the 'Scan Weapons Now' keybind or run 'masher scan' if nothing happens")


@keybind("Scan Weapons Now", description="Apply the Masher variant to every loaded weapon now.")
def scan_keybind() -> None:
    """Hook-independent trigger, for when the automatic ones do not fire."""
    found = scan_all()
    log(f"scanned {found} weapon(s)")


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #


@command("masher", description="Inspect what Jakobs Masher is doing.")
def masher_command(args: Any) -> None:
    if args.action == "status":
        log(f"weapons identified: {len(_identity_cache)}")
        log(f"GetPartValue usable: {_part_values_work}")
        log(f"ownership link: {_ownership_field or 'not established'}")
        for knob in FIELD_CANDIDATES:
            if knob not in _resolved_fields:
                log(f"  {knob:<12} -> not looked up yet")
                continue
            resolved = _resolved_fields[knob]
            if resolved is None:
                log(f"  {knob:<12} -> NO USABLE PROPERTY")
            else:
                field, subfield = resolved
                log(f"  {knob:<12} -> {field}{'.' + subfield if subfield else ''}")
        log("hook activity:")
        for hook_obj in HOOKS:
            for func_name, _hook_type in hook_obj.hook_funcs:
                short = func_name.rsplit(":", 1)[-1]
                log(
                    f"  {short:<34} bound={bool(hook_obj.get_active_count())}"
                    f"  fired={_fires.get(short, 0)}"
                )
        log(f"weapons seen: {len(_weapon_cache)}, behaviours modified: {len(_touched)}")
        for path, pointers, is_masher in _weapon_cache.values():
            log(f"  masher={is_masher}  behaviours={len(pointers)}  {path}")
        return

    if args.action == "scan":
        found = scan_all()
        log(f"scanned {found} weapon(s)")
        return

    if args.action == "mine":
        # Weapons have no readable display name, so the next best answer to
        # "which of my guns is a Masher" is to list them with their live
        # projectile count - that is the number the effect actually changes.
        mine = [
            (weapon, behaviours)
            for weapon, behaviours in live_weapons().values()
            if is_player_weapon(weapon) is not False
        ]
        if not mine:
            log("you are not carrying anything with a fire behaviour")
            return

        log(f"{len(mine)} weapon(s) you are carrying:")
        for weapon, behaviours in mine:
            jakobs = is_jakobs_pistol(weapon)
            tags = sorted(
                {
                    match.group(0).upper()
                    for text in collect_identity(weapon)
                    for match in WEAPON_TAG_RE.finditer(text)
                }
            )
            cached = _weapon_cache.get(weapon._get_address())
            masher = cached[2] if cached else None

            counts = []
            field = _resolved_fields.get("projectiles")
            for behaviour in behaviours:
                try:
                    counts.append(str(getattr(behaviour, field or "ProjectilesPerShot")))
                except Exception:
                    counts.append("?")

            log(
                f"  {'/'.join(tags) or 'untagged':<10}"
                f" jakobs_pistol={jakobs}"
                f" masher={masher}"
                f" projectiles={','.join(counts)}"
            )
        return

    if args.action == "probe":
        # Everything about the guns you are carrying, in full: the behaviour's
        # fields, and every field of every attribute struct with its value.
        # This is what answers "the write says it worked but nothing changed".
        mine = [
            (weapon, behaviours)
            for weapon, behaviours in live_weapons().values()
            if is_player_weapon(weapon) is not False
        ]
        log(f"probing {len(mine)} carried weapon(s)")

        for weapon, behaviours in mine:
            tags = sorted(
                {
                    match.group(0).upper()
                    for text in collect_identity(weapon)
                    for match in WEAPON_TAG_RE.finditer(text)
                }
            )
            cached = _weapon_cache.get(weapon._get_address())
            log(
                f"--- {'/'.join(tags) or 'untagged'}"
                f" jakobs_pistol={is_jakobs_pistol(weapon)}"
                f" masher={cached[2] if cached else None}"
            )

            for behaviour in behaviours:
                try:
                    log(f"    behaviour {behaviour.Class.Name}")
                except Exception:
                    log("    behaviour <unnamed>")
                log(f"      fields: {', '.join(_field_names(behaviour))}")

                for candidate in (
                    "ProjectilesPerShot",
                    "Damage",
                    "Spread",
                    "FireRate",
                ):
                    try:
                        raw = getattr(behaviour, candidate)
                    except Exception as exc:
                        log(f"      {candidate}: unreadable ({exc})")
                        continue
                    if _is_number(raw):
                        log(f"      {candidate} = {raw} (plain number)")
                        continue
                    log(f"      {candidate} = struct:")
                    for name in _field_names(raw):
                        try:
                            log(f"          {name} = {_describe(getattr(raw, name))[:120]}")
                        except Exception as exc:
                            log(f"          {name} = <unreadable: {exc}>")

                drift = check_drift(behaviour)
                log(f"      drift: {'; '.join(drift) if drift else 'none - values held'}")
        return

    if args.action == "restore":
        restore_all()
        return

    # dump - everything we can see about every live weapon, with the strings
    # its object graph yielded. When identification fails this is the evidence.
    behaviours = [
        b
        for b in unrealsdk.find_all(FIRE_BEHAVIOUR_CLASS, False)
        if b != b.Class.ClassDefaultObject
    ]
    log(f"{len(behaviours)} live {FIRE_BEHAVIOUR_CLASS} object(s)")

    for behaviour in behaviours[:8]:
        log(f"--- {behaviour._path_name()}")
        for field in (
            "ProjectilesPerShot",
            "Damage",
            "Spread",
            "FireRate",
            "Crosshair",
            "FiringPattern",
        ):
            try:
                log(f"      {field} = {getattr(behaviour, field)}")
            except Exception as exc:
                log(f"      {field} unreadable ({exc})")

        weapon = owning_weapon(behaviour)
        if weapon is None:
            log("      no owning weapon found")
            continue

        try:
            class_name = weapon.Class.Name
        except Exception:
            class_name = "?"
        log(f"      weapon   = {class_name} {weapon._path_name()}")

        identity = collect_identity(weapon)
        log(f"      graph    = {len(identity)} string(s)")

        tagged = [s for s in identity if WEAPON_TAG_RE.search(s)]
        gear = [s for s in identity if "/game/gear/" in s.lower()]

        if tagged:
            log("      MANUFACTURER/CLASS TAGS:")
            for text in tagged[:10]:
                log(f"        {text[:200]}")
        else:
            log("      no MANUFACTURER_CLASS tag anywhere in the graph")

        if gear:
            log("      gear asset paths:")
            for text in gear[:10]:
                log(f"        {text[:200]}")

        if not tagged and not gear:
            log("      sample of what the graph did contain:")
            for text in identity[:25]:
                log(f"        {text[:160]}")

        owner_links = []
        for field in OWNER_FIELDS:
            try:
                owner = getattr(weapon, field)
            except Exception:
                continue
            if owner is None:
                continue
            try:
                owner_links.append(f"{field}={owner.Class.Name} {owner._path_name()}")
            except Exception:
                owner_links.append(f"{field}={_describe(owner)[:120]}")
        log(f"      owner    = {'; '.join(owner_links) or 'no owner link found'}")
        log(f"      mine     = {is_player_weapon(weapon)}")
        log(f"      jakobs pistol = {is_jakobs_pistol(weapon)}")
        log(f"      parts    = {part_values(weapon)}")


masher_command.add_argument(
    "action",
    nargs="?",
    default="dump",
    choices=("dump", "status", "scan", "mine", "probe", "restore"),
    help=(
        "dump the live weapon data, show mod status and hook activity,"
        " scan every loaded weapon now, list the guns you are carrying,"
        " probe them in full, or undo all changes"
    ),
)


# --------------------------------------------------------------------------- #

build_mod(
    coop_support=CoopSupport.HostOnly,
    on_enable=on_mod_enabled,
    on_disable=restore_all,
)
