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
# The exact property that carries a weapon's identity is not documented
# anywhere, so it is discovered once from the first weapon seen and cached.
# --------------------------------------------------------------------------- #

# Property names on a weapon whose value mentions a MANUFACTURER_CLASS tag.
_identity_props: list[str] | None = None

# True once we know whether OakWeapon.GetPartValue can be called.
_part_values_work: bool | None = None


def _describe(value: Any) -> str:
    """Stringify an unreal value without blowing up on exotic types."""
    try:
        return str(value)
    except Exception:
        return ""


def _discover_identity_props(weapon: UObject) -> list[str]:
    """Find the properties on a weapon that name its manufacturer and class."""
    found: list[str] = []
    for name in dir(weapon):
        if name.startswith("_"):
            continue
        try:
            value = getattr(weapon, name)
        except Exception:
            continue
        # Skip callables - we only want data.
        if isinstance(value, BoundFunction):
            continue
        if WEAPON_TAG_RE.search(_describe(value)):
            found.append(name)
    return found


def weapon_identity(weapon: UObject) -> str:
    """A string naming this weapon's manufacturer and class, e.g. 'JAK_PS'."""
    global _identity_props

    if _identity_props is None:
        _identity_props = _discover_identity_props(weapon)
        if _identity_props:
            log(f"identifying weapons via {', '.join(_identity_props)}")
        else:
            log(
                "could not find any property naming a weapon's type -"
                " falling back to the object path"
            )

    chunks = [weapon._path_name()]
    for name in _identity_props:
        try:
            chunks.append(_describe(getattr(weapon, name)))
        except Exception:
            continue
    return " ".join(chunks)


def is_jakobs_pistol(weapon: UObject) -> bool:
    return TARGET_WEAPON_TAG.lower() in weapon_identity(weapon).lower()


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


def _scale_field(behaviour: UObject, field: str, scale: float) -> bool:
    """Multiply a float field, re-basing if the game changed it underneath us."""
    address = behaviour._get_address()
    state = _touched.setdefault(address, {})

    try:
        current = float(getattr(behaviour, field))
    except Exception:
        return False

    previous = state.get(field)
    if previous is not None and abs(current - previous["applied"]) < 1e-4:
        # Still holding our value, and the scale may have changed in the menu.
        base = previous["original"]
    else:
        # First touch, or the game recalculated it - treat what is there as the
        # new baseline so buffs and debuffs still come through.
        base = current

    wanted = base * scale
    try:
        setattr(behaviour, field, wanted)
    except Exception:
        return False

    state[field] = {"original": base, "applied": wanted}
    return True


def _set_field(behaviour: UObject, field: str, value: Any) -> bool:
    address = behaviour._get_address()
    state = _touched.setdefault(address, {})
    try:
        current = getattr(behaviour, field)
    except Exception:
        return False

    if field not in state:
        state[field] = {"original": current, "applied": value}
    else:
        state[field]["applied"] = value

    try:
        setattr(behaviour, field, value)
    except Exception:
        return False
    return True


def make_masher(behaviour: UObject) -> list[str]:
    """Turn one fire behaviour into a Masher. Returns the fields that stuck."""
    applied: list[str] = []

    if _set_field(behaviour, "ProjectilesPerShot", int(projectiles.value)):
        applied.append("ProjectilesPerShot")
    if _scale_field(behaviour, "Damage", float(damage_scale.value)):
        applied.append("Damage")
    if _scale_field(behaviour, "Spread", float(spread_scale.value)):
        applied.append("Spread")

    return applied


def restore_all() -> None:
    """Undo every change we made to behaviours that still exist."""
    restored = 0
    for behaviour in unrealsdk.find_all(FIRE_BEHAVIOUR_CLASS, False):
        state = _touched.get(behaviour._get_address())
        if not state:
            continue
        for field, record in state.items():
            try:
                setattr(behaviour, field, record["original"])
            except Exception:
                continue
        restored += 1
    _touched.clear()
    _weapon_cache.clear()
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


def scan_all() -> int:
    """Process every live weapon, without relying on any hook firing.

    This is the fallback the keybind and `masher scan` use. It walks from the
    fire behaviours rather than from a weapon class name, so it does not care
    what the weapon actor is actually called.
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
        log(f"identity properties: {_identity_props}")
        log(f"GetPartValue usable: {_part_values_work}")
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

    if args.action == "restore":
        restore_all()
        return

    # dump - everything we can see about the currently held weapon.
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

        outer = behaviour
        for depth in range(6):
            try:
                outer = outer.Outer
            except Exception:
                break
            if outer is None:
                break
            log(f"      outer[{depth}] = {outer.Class.Name} {outer._path_name()}")
            if outer.Class.Name.endswith("Weapon"):
                log(f"      identity = {weapon_identity(outer)[:400]}")
                log(f"      parts    = {part_values(outer)}")
                break


masher_command.add_argument(
    "action",
    nargs="?",
    default="dump",
    choices=("dump", "status", "scan", "restore"),
    help=(
        "dump the live weapon data, show mod status and hook activity,"
        " scan every loaded weapon now, or undo all changes"
    ),
)


# --------------------------------------------------------------------------- #

build_mod(
    coop_support=CoopSupport.HostOnly,
    on_enable=on_mod_enabled,
    on_disable=restore_all,
)
