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

import datetime
import json
import re
import time
import traceback
from pathlib import Path
from typing import Any

import unrealsdk
from mods_base import (
    BoolOption,
    CoopSupport,
    EInputEvent,
    SliderOption,
    build_mod,
    command,
    get_pc,
    hook,
    keybind,
)
from unrealsdk.hooks import Type
from unrealsdk.unreal import BoundFunction, UObject, WeakPointer, WrappedStruct

__version__ = "1.3"
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

def _on_setting_change(_option: Any, _value: Any) -> None:
    """Re-evaluate the guns you are holding soon after a setting moves.

    mods_base calls this *before* the new value is stored, so sweeping here
    would still see the old one. Opening the fast window instead makes the
    next key press - closing the menu is enough - sweep with the new value.
    """
    request_follow_up()


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
    on_change_while_enabled=_on_setting_change,
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
    on_change_while_enabled=_on_setting_change,
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
    on_change_while_enabled=_on_setting_change,
)

masher_chance = SliderOption(
    "Masher Chance",
    25,
    0,
    100,
    display_name="Masher Chance (%)",
    description=(
        "What share of newly found Jakobs revolvers are Mashers - 17 is about"
        " one in six. Each gun is judged once, the first time it is in your"
        " hands, and the verdict is remembered across restarts. Changing this"
        " only affects guns you find afterwards. 'masher forget' re-judges"
        " everything at the current chance."
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
    on_change_while_enabled=_on_setting_change,
)

auto_scan = BoolOption(
    "Automatic Scanning",
    True,
    "On",
    "Off",
    display_name="Automatic Scanning",
    description=(
        "Convert new guns by itself, instead of needing the keybind. None of"
        " the weapon-side hooks fire in solo play, so this rides on events"
        " that do - equipping, interacting, and a throttled heartbeat."
    ),
)

scan_interval = SliderOption(
    "Scan Interval",
    3,
    1,
    30,
    display_name="Scan Interval (seconds)",
    description=(
        "How often the heartbeat may re-scan. Equipping or interacting scans"
        " immediately regardless. Raise it if you ever see a hitch."
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

# Weapons already logged as "not ours", so re-checking every pass - which is
# now the point - does not also mean logging every pass.
_not_ours_reported: set[int] = set()


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
    """Fallback identity, from the rolled numbers on the fire behaviour.

    Reads through attribute structs like everything else here - these fields
    are `GbxAttribute*` values, not plain floats, so a naive `float(getattr(...))`
    yields nothing at all and the roll then always says "not a Masher".
    """
    values: list[int] = []
    for behaviour in behaviours:
        for field in ("FireRate", "Spread", "ShotAmmoCost", "AutomaticBurstCount"):
            subfields = _probe_property(behaviour, field)
            if subfields is None:
                continue
            value = _read_effective(behaviour, field, subfields)
            if value is None:
                continue
            values.append(int(round(value * 1000)))
    return tuple(values)


# --------------------------------------------------------------------------- #
# Deciding which revolvers are Mashers
# --------------------------------------------------------------------------- #


def roll_identity(weapon: UObject, behaviours: list[UObject]) -> tuple[str, tuple[int, ...]]:
    """What the roll is computed from, and where it came from."""
    parts = part_values(weapon)
    if parts:
        return "parts", parts
    return "stat fingerprint", stat_fingerprint(behaviours)


# The roll lands on 0.00 .. 99.99, in hundredths of a percent.
ROLL_BUCKETS = 100 * 100


def masher_roll(weapon: UObject, behaviours: list[UObject]) -> float | None:
    """Where this particular revolver sits on 0..100. None if it has no identity.

    BL3 made Masher a barrel part, so Masher-ness here is likewise a fixed
    property of the gun rather than a per-shot coin flip: the roll is derived
    from the weapon's part indices, the same numbers its serial encodes. The
    same revolver therefore rolls the same in every session - it is decided
    when the gun drops, even though it can only be applied once there is a
    live weapon to apply it to.

    The roll is a position, not a verdict: a gun is a Masher when its roll is
    below `Masher Chance` at the moment it is first judged (see `decide`).

    (The part slots are read as an opaque list - the game does not tell us
    which index is the barrel - so this is a hash over the whole roll rather
    than a literal barrel-variant check.)
    """
    _source, identity = roll_identity(weapon, behaviours)
    return roll_of(identity)


def roll_of(identity: tuple[int, ...]) -> float | None:
    """The 0..100 roll for an identity tuple. None for an empty one."""
    if not identity:
        return None

    # A cheap, stable spread of the part roll.
    mixed = 0
    for index, value in enumerate(identity):
        mixed = (mixed * 31 + value * (index + 7)) & 0xFFFFFFFF
    mixed ^= mixed >> 16
    mixed = (mixed * 0x45D9F3B) & 0xFFFFFFFF
    mixed ^= mixed >> 16

    return (mixed % ROLL_BUCKETS) / 100.0


def is_masher_roll(roll: float | None) -> bool:
    """Does a roll qualify under the current `Masher Chance`?"""
    chance = float(masher_chance.value)
    if chance >= 100:
        return True  # "every Jakobs pistol" - even one we could not roll
    return roll is not None and roll < chance


def rolls_masher(weapon: UObject, behaviours: list[UObject]) -> bool:
    """Would this revolver be a Masher if it were judged now?"""
    return is_masher_roll(masher_roll(weapon, behaviours))


# --------------------------------------------------------------------------- #
# Remembering the verdict, per save
#
# A gun is judged once, the first time the mod sees it in your hands, against
# the chance at that moment, and the verdict is written down. Later changes to
# `Masher Chance` only affect guns found afterwards, and neither a restart nor
# a mod update can flip a gun you already own. Nothing goes into the game's
# save: these are the mod's own files, beside its settings.
#
# Each save gets its own record, named after the character GUID the save
# stores (`char_guid` in a decrypted .sav), so characters never share verdicts
# and each file only holds one character's guns.
#
# Guns are known by their part values, the numbers the item serial stores.
# Two revolvers built from identical parts are the same gun as far as anything
# here can tell, so they share a verdict - the way a BL3 Masher barrel made
# every gun carrying it a Masher.
# --------------------------------------------------------------------------- #

try:
    from mods_base import SETTINGS_DIR as _SETTINGS_DIR
except Exception:  # pragma: no cover - depends on the mods_base build
    _SETTINGS_DIR = Path(__file__).resolve().parent.parent / "settings"

REGISTRY_DIR_NAME = "jakobs_masher_guns"
REGISTRY_VERSION = 2

# Before 1.3 every save shared one file. It is adopted by the first save that
# is identified, since that is the character the verdicts came from.
LEGACY_REGISTRY_FILE_NAME = "jakobs_masher_guns.json"

# The record used when the loaded character cannot be identified.
UNKNOWN_SAVE_ID = "unknown-character"

# Keys with this prefix are judged once per session but never written: they
# come from the stat fingerprint, which temporary buffs can move, so a
# remembered verdict could not be found again reliably.
SESSION_KEY_PREFIX = "session:"

# Save id -> (key -> {"masher", "roll", "chance", "decided"}), loaded lazily.
_registries: dict[str, dict[str, dict[str, Any]]] = {}

# Saves whose file exists but cannot be read: never overwritten, so a
# hand-edit gone wrong loses nothing. `masher forget` clears the flag.
_unreadable_saves: set[str] = set()


# --- which save is loaded ------------------------------------------------- #
#
# The GUID is read from the live game. Neither the class that holds it nor the
# exact property is known from offline data - only that the names
# `ActiveCharGuid` and `CharacterGuid` exist in the binary - so every plausible
# holder is tried once and the one that answers is remembered, the same way the
# ownership link is discovered.

SAVE_ID_FIELDS = ("ActiveCharGuid", "CharacterGuid")

# How long to wait before searching again after no holder answered. A search
# walks the object list, so it must not run on every sweep in the main menu.
SAVE_LOOKUP_RETRY_SECONDS = 10.0

# (holder, field, description) that answered last time.
_save_source: tuple[WeakPointer, str, str] | None = None
_current_save: str | None = None
_next_save_lookup = 0.0
_save_fallback_reported = False
# Holders already announced, so a new PlayerState per map is not re-logged.
_save_labels_reported: set[str] = set()


def _format_guid(value: Any) -> str | None:
    """A GUID as the save file writes it: 32 upper-case hex digits.

    None for anything that is not a GUID, or for the all-zero GUID the game
    holds while no character is loaded.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = re.sub(r"[^0-9A-Fa-f]", "", value).upper()
        return text if len(text) == 32 and text.strip("0") else None
    try:
        parts = [int(getattr(value, name)) & 0xFFFFFFFF for name in ("A", "B", "C", "D")]
    except Exception:
        return None
    if not any(parts):
        return None
    return "".join(f"{part:08X}" for part in parts)


def _save_id_holders(search: bool = True) -> list[tuple[str, UObject]]:
    """Every object that might hold the loaded character's GUID.

    Measured in game: `PlayerState.ActiveCharGuid` holds it, matching the
    save's `char_guid`. The player's own objects come first because reaching
    them needs no object-list walk; a new level brings a new PlayerState, so
    this runs after every map load. The profile classes are only walked, with
    `search`, if none of those answers.
    """
    holders: list[tuple[str, UObject]] = []
    try:
        pc = get_pc()
    except Exception:
        pc = None
    if pc is not None:
        for field, label in (("PlayerState", "PlayerState"), ("Player", "LocalPlayer")):
            try:
                linked = getattr(pc, field)
            except Exception:
                continue
            if linked is not None:
                holders.append((label, linked))
        holders.append(("PlayerController", pc))
    if search:
        for class_name in ("OakActiveProfile", "GbxActiveProfile"):
            try:
                for obj in unrealsdk.find_all(class_name, False):
                    if obj != obj.Class.ClassDefaultObject:
                        holders.append((class_name, obj))
            except Exception:
                continue
    return holders


def _read_save_guid() -> str | None:
    global _save_source
    if _save_source is not None:
        pointer, field, _label = _save_source
        holder = pointer()
        if holder is not None:
            try:
                guid = _format_guid(getattr(holder, field))
            except Exception:
                guid = None
            if guid:
                return guid
        _save_source = None

    for search in (False, True):
        for label, holder in _save_id_holders(search):
            for field in SAVE_ID_FIELDS:
                try:
                    guid = _format_guid(getattr(holder, field))
                except Exception:
                    continue
                if guid:
                    _save_source = (WeakPointer(holder), field, f"{label}.{field}")
                    if label not in _save_labels_reported:
                        _save_labels_reported.add(label)
                        log(f"saves told apart by {label}.{field} (character {guid})")
                    return guid
    return None


def refresh_save_id(force: bool = False) -> str:
    """Work out which save is loaded. Cheap once the GUID's holder is known."""
    global _current_save, _next_save_lookup, _save_fallback_reported
    now = time.monotonic()
    if _save_source is None and not force and now < _next_save_lookup:
        return _current_save or UNKNOWN_SAVE_ID

    guid = _read_save_guid()
    if guid is None:
        _next_save_lookup = now + SAVE_LOOKUP_RETRY_SECONDS
        if _current_save is None and not _save_fallback_reported:
            _save_fallback_reported = True
            log(
                "cannot tell which character is loaded yet - verdicts go to a"
                f" shared '{UNKNOWN_SAVE_ID}' record until it can ('masher save' shows why)"
            )
        return _current_save or UNKNOWN_SAVE_ID

    if guid != _current_save:
        if _current_save is not None:
            log(f"character changed: {_current_save} -> {guid}")
        _current_save = guid
    return guid


def current_save_id() -> str:
    return _current_save or refresh_save_id()


# --- the record files ----------------------------------------------------- #


def registry_dir() -> Path:
    return Path(_SETTINGS_DIR) / REGISTRY_DIR_NAME


def registry_path(save_id: str | None = None) -> Path:
    return registry_dir() / f"{save_id or current_save_id()}.json"


def _read_guns(path: Path) -> dict[str, dict[str, Any]]:
    """Parse a record file. Raises on anything malformed."""
    data = json.loads(path.read_text(encoding="utf-8"))
    guns = data["guns"]
    if not isinstance(guns, dict):
        raise ValueError("'guns' is not an object")
    return {
        key: entry
        for key, entry in guns.items()
        if isinstance(entry, dict) and isinstance(entry.get("masher"), bool)
    }


def _adopt_legacy_record(save_id: str, registry: dict[str, dict[str, Any]]) -> None:
    """Move the pre-1.3 shared record into the first save that is identified."""
    legacy = Path(_SETTINGS_DIR) / LEGACY_REGISTRY_FILE_NAME
    if save_id == UNKNOWN_SAVE_ID or not legacy.exists():
        return
    try:
        registry.update(_read_guns(legacy))
        legacy.replace(legacy.with_name(legacy.name + ".migrated"))
    except Exception as exc:
        log(f"could not adopt the old shared record {legacy}: {exc}")
        return
    log(f"adopted {len(registry)} verdict(s) from the old shared record into {save_id}")
    save_registry(save_id)


def load_registry(save_id: str | None = None) -> dict[str, dict[str, Any]]:
    save_id = save_id or current_save_id()
    registry = _registries.get(save_id)
    if registry is not None:
        return registry
    registry = _registries[save_id] = {}
    path = registry_path(save_id)
    try:
        registry.update(_read_guns(path))
    except FileNotFoundError:
        _adopt_legacy_record(save_id, registry)
        return registry
    except Exception as exc:
        _unreadable_saves.add(save_id)
        log(
            f"could not read {path} ({exc}). It will not be overwritten;"
            " guns are judged for this session only. 'masher forget' resets it."
        )
        return registry
    debug(f"remembered verdicts loaded for {save_id}: {len(registry)}")
    return registry


def save_registry(save_id: str | None = None) -> None:
    save_id = save_id or current_save_id()
    registry = _registries.get(save_id)
    if registry is None or save_id in _unreadable_saves:
        return
    path = registry_path(save_id)
    persisted = {k: v for k, v in registry.items() if not k.startswith(SESSION_KEY_PREFIX)}
    body = json.dumps(
        {"version": REGISTRY_VERSION, "character_guid": save_id, "guns": persisted},
        indent=1,
        sort_keys=True,
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(body, encoding="utf-8")
        temp.replace(path)
    except Exception as exc:
        log(f"could not save {path}: {exc}")


def remembered_count(save_id: str | None = None) -> int:
    return sum(1 for k in load_registry(save_id) if not k.startswith(SESSION_KEY_PREFIX))


def gun_key(source: str, identity: tuple[int, ...]) -> str | None:
    """How a gun is known in the record. None if it has no identity at all."""
    if not identity:
        return None
    text = ",".join(map(str, identity))
    if source == "parts":
        return f"parts:{text}"
    return f"{SESSION_KEY_PREFIX}{source}:{text}"


def decide(key: str | None, roll: float | None) -> tuple[bool, bool]:
    """Is this gun a Masher? Returns (verdict, judged just now).

    The first call for a key judges it against the current chance and records
    the answer; every later call - this session or any other - returns that
    record untouched.
    """
    registry = load_registry()
    if key is not None:
        entry = registry.get(key)
        if entry is not None:
            return bool(entry["masher"]), False
    masher = is_masher_roll(roll)
    if key is not None:
        registry[key] = {
            "masher": masher,
            "roll": None if roll is None else round(roll, 2),
            "chance": int(masher_chance.value),
            "decided": datetime.date.today().isoformat(),
        }
        if not key.startswith(SESSION_KEY_PREFIX):
            save_registry()
    return masher, True


def forget_all() -> int:
    """Drop the loaded save's remembered verdicts. Returns how many were written."""
    save_id = current_save_id()
    count = remembered_count(save_id)
    load_registry(save_id).clear()
    _unreadable_saves.discard(save_id)
    save_registry(save_id)
    return count


def describe_decision(key: str | None, roll: float | None) -> str:
    chance = int(masher_chance.value)
    rolled = "no roll" if roll is None else f"roll {roll:.2f}"
    entry = load_registry().get(key) if key is not None else None
    if entry is None:
        would = "Masher" if is_masher_roll(roll) else "plain"
        return f"{rolled}, not judged yet (would be {would} at {chance}%)"
    verdict = "Masher" if entry["masher"] else "plain"
    scope = " - this session only" if key.startswith(SESSION_KEY_PREFIX) else ""
    return f"{rolled}, judged {verdict} at {entry['chance']}% on {entry['decided']}{scope}"


# --------------------------------------------------------------------------- #
# Applying the variant
# --------------------------------------------------------------------------- #

# Behaviour address -> what we last wrote and what it was before us. Tracking
# both lets the game keep recalculating damage (level ups, skills, anointments)
# without us either fighting it or compounding our own multiplier.
_touched: dict[int, dict[str, Any]] = {}

# Weapon address -> (path name, behaviour pointers, record key, roll, is a
# Jakobs pistol). The path name guards against the engine recycling an address
# for a different weapon.
_weapon_cache: dict[int, tuple[str, list[WeakPointer], str | None, float | None, bool]] = {}


def cached_masher(weapon: UObject) -> bool | None:
    """The remembered verdict for this weapon. None if not judged yet."""
    cached = _weapon_cache.get(weapon._get_address())
    if cached is None:
        return None
    _path, _pointers, key, _roll, jakobs = cached
    if not jakobs:
        return False
    entry = load_registry().get(key) if key is not None else None
    return None if entry is None else bool(entry["masher"])


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

# A Gbx attribute struct carries BOTH a base and an effective value:
#
#   untouched Torgue shotgun     modified Jakobs pistol
#     BaseValue = 3                BaseValue = 6   <- we wrote this
#     Value     = 3                Value     = 1   <- the game reads this
#
# On an untouched weapon the two agree. Writing only `BaseValue` therefore
# changes nothing observable, which is what five test sessions saw. Every
# numeric member is written, and scaled rather than assigned where a ratio
# matters: `Damage` runs BaseValue 82 / Value 241, so the two have to move
# together or the modifier chain is destroyed.
STRUCT_SCALARS = ("Value", "BaseValue", "Constant", "BaseValueConstant")

# knob -> (property name, tuple of struct subfields); the tuple is empty for a
# plain scalar. None once we know the knob is unreachable.
_resolved_fields = {}

_missing_reported = set()
_shape_reported = set()
_drift_reported = set()


def _field_names(obj):
    """Every field name on an object or struct, for diagnostics."""
    try:
        return sorted(n for n in dir(obj) if not n.startswith("_"))
    except Exception:
        return []


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _report_shape(field, struct):
    """Log a struct we cannot find a number in, once per property."""
    if field in _shape_reported:
        return
    _shape_reported.add(field)

    log(f"{field} is a struct with no numeric field; it holds:")
    for name in _field_names(struct):
        try:
            log(f"    {name} = {_describe(getattr(struct, name))[:120]}")
        except Exception as exc:
            log(f"    {name} = <unreadable: {exc}>")


def _scalar_subfields(struct):
    """The numeric members of an attribute struct, effective value first."""
    found = []
    for name in STRUCT_SCALARS:
        try:
            if _is_number(getattr(struct, name)):
                found.append(name)
        except Exception:
            continue
    return tuple(found)


def _probe_property(obj, field):
    """How to reach numbers at obj.field.

    Returns () for a plain scalar, the struct subfields to use, or None when
    there is no number there at all.
    """
    try:
        raw = getattr(obj, field)
    except Exception:
        return None

    if _is_number(raw):
        return ()

    subfields = _scalar_subfields(raw)
    if subfields:
        return subfields

    _report_shape(field, raw)
    return None


def _read_effective(obj, field, subfields):
    """The value the game actually uses - the first subfield, or the scalar."""
    try:
        raw = getattr(obj, field)
        return float(raw if not subfields else getattr(raw, subfields[0]))
    except Exception:
        return None


def _read_member(obj, field, subfield):
    try:
        raw = getattr(obj, field)
        return float(raw if subfield is None else getattr(raw, subfield))
    except Exception:
        return None


def _match_type(current, wanted):
    """Coerce `wanted` to the numeric type the member already holds.

    `ProjectilesPerShot` is a GbxAttributeInteger - writing a float to it fails
    with "Unable to cast ... to C++ type 'int'", while Damage and Spread are
    floats. That difference is why spread worked and the projectile count did
    not, so the existing value decides the type rather than a guess.
    """
    if isinstance(current, bool):
        return bool(wanted)
    if isinstance(current, int):
        return int(round(wanted))
    return float(wanted)


def _apply_numbers(obj, field, subfields, values):
    """Write one number to a plain property, or per-member values to a struct."""
    try:
        if not subfields:
            setattr(obj, field, _match_type(getattr(obj, field), values[None]))
            return True
        struct = getattr(obj, field)
        for name in subfields:
            setattr(struct, name, _match_type(getattr(struct, name), values[name]))
        # A struct read from a property may be a copy, so assign it back.
        setattr(obj, field, struct)
        return True
    except Exception as exc:
        log(f"could not write {field}: {exc}")
        return False


def _resolve_field(behaviour, knob):
    """The first candidate property for this knob that yields a number."""
    if knob in _resolved_fields:
        return _resolved_fields[knob]

    for candidate in FIELD_CANDIDATES[knob]:
        subfields = _probe_property(behaviour, candidate)
        if subfields is None:
            continue
        _resolved_fields[knob] = (candidate, subfields)
        where = candidate + (f" ({', '.join(subfields)})" if subfields else "")
        log(f"'{knob}' resolved to {where}")
        return _resolved_fields[knob]

    _resolved_fields[knob] = None
    if knob not in _missing_reported:
        _missing_reported.add(knob)
        try:
            class_name = behaviour.Class.Name
        except Exception:
            class_name = "?"
        log(
            f"no usable property for '{knob}' on {class_name}"
            f" (tried {', '.join(FIELD_CANDIDATES[knob])})"
        )
        log(f"  {class_name} exposes: {', '.join(_field_names(behaviour)) or 'nothing'}")
    return None


def _state_key(field, subfield):
    return field if subfield is None else f"{field}.{subfield}"


def _members(subfields):
    """Iterate struct members, or the single (None,) for a plain scalar."""
    return subfields if subfields else (None,)


# Which member of an attribute struct this mod owns.
#
# The engine derives `Value` from `BaseValue` times whatever modifiers are
# active - measured: a Masher's damage re-read as 69.1113 against our 53.1625,
# exactly 1.300x. So the mod owns `BaseValue` and leaves `Value` to the engine,
# writing `Value` only when (re)applying, to kick it into place straight away
# (the engine does not recompute it just because `BaseValue` changed). Writing
# `Value` on every pass instead would strip every buff and debuff each time the
# heartbeat ran.
OWNED_MEMBER = "BaseValue"


def _owned(subfields):
    """The member to enforce: BaseValue if present, else the only one there."""
    if not subfields:
        return None
    return OWNED_MEMBER if OWNED_MEMBER in subfields else subfields[0]


def _write_knob(behaviour, knob, want):
    """Apply a knob, where `want(member, anchor)` gives each member's target.

    Anchors are the values first seen, per member. Every member is written on
    first touch and whenever the owned member has left our value - an engine
    re-init, or the option being changed. Otherwise nothing is written, so the
    engine keeps layering modifiers onto the base we set.
    """
    resolved = _resolve_field(behaviour, knob)
    if resolved is None:
        return None
    field, subfields = resolved
    owned = _owned(subfields)

    state = _touched.setdefault(behaviour._get_address(), {})
    anchors = {}
    for name in _members(subfields):
        record = state.get(_state_key(field, name))
        if record is not None:
            anchors[name] = record["original"]
            continue
        current = _read_member(behaviour, field, name)
        if current is None:
            return None
        anchors[name] = current

    targets = {name: want(name, anchors[name]) for name in _members(subfields)}

    current_owned = _read_member(behaviour, field, owned)
    first_touch = _state_key(field, owned) not in state
    if (
        not first_touch
        and current_owned is not None
        and abs(current_owned - targets[owned]) < 1e-3
    ):
        # Still ours. Leave Value to the engine.
        return _state_key(field, owned)

    if not _apply_numbers(behaviour, field, subfields, targets):
        return None

    for name in _members(subfields):
        state[_state_key(field, name)] = {
            "original": anchors[name],
            "applied": targets[name],
            "field": field,
            "subfield": name,
        }
    return _state_key(field, owned)


def _scale_field(behaviour, knob, scale):
    """Scale every member from its anchor. See _write_knob."""
    return _write_knob(behaviour, knob, lambda _name, anchor: anchor * scale)


def _set_field(behaviour, knob, value):
    """Set every member to a fixed value. See _write_knob."""
    return _write_knob(behaviour, knob, lambda _name, _anchor: float(value))


def _verify(behaviour, knob):
    """Compare the owned member with what we last wrote to it.

    Only the owned member counts: `Value` moving is the engine applying a buff,
    which is supposed to happen.
    """
    resolved = _resolved_fields.get(knob)
    if not resolved:
        return None
    field, subfields = resolved
    owned = _owned(subfields)
    record = _touched.get(behaviour._get_address(), {}).get(_state_key(field, owned))
    if record is None:
        return None
    actual = _read_member(behaviour, field, owned)
    if actual is None:
        return None
    return float(record["applied"]), actual


def check_drift(behaviour):
    """Which knobs no longer hold the effective value we wrote?"""
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
        debug(f"the engine reset a base value, re-applying: {'; '.join(reverted)}")

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


def restore_behaviour(behaviour: UObject) -> bool:
    """Put one fire behaviour back the way we found it. False if untouched.

    Its bookkeeping goes with it, so converting it again later anchors afresh
    on the restored values.
    """
    address = behaviour._get_address()
    state = _touched.pop(address, None)
    if not state:
        return False
    # Records are keyed per struct member, so group them back per property
    # and restore every member in one write.
    by_property: dict[str, dict[str | None, float]] = {}
    for record in state.values():
        by_property.setdefault(record["field"], {})[record["subfield"]] = record[
            "original"
        ]
    for field, originals in by_property.items():
        subfields = tuple(n for n in originals if n is not None)
        _apply_numbers(behaviour, field, subfields, originals)
    _drift_reported.discard(address)
    return True


def restore_all() -> None:
    """Undo every change we made to behaviours that still exist."""
    restored = 0
    for behaviour in unrealsdk.find_all(FIRE_BEHAVIOUR_CLASS, False):
        if restore_behaviour(behaviour):
            restored += 1
    _touched.clear()
    _weapon_cache.clear()
    _identity_cache.clear()
    _resolved_fields.clear()
    _missing_reported.clear()
    _shape_reported.clear()
    _drift_reported.clear()
    _not_ours_reported.clear()
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
    refresh_save_id()
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

    behaviours: list[UObject] | None = None
    if cached is not None and cached[0] == path:
        _, pointers, key, roll, jakobs = cached
        if not jakobs:
            return
        behaviours = [b for b in (p() for p in pointers) if b is not None]
        if len(behaviours) != len(pointers):
            behaviours = None  # something was garbage collected - rescan

    if behaviours is None:
        if not is_jakobs_pistol(weapon):
            _weapon_cache[address] = (path, [], None, None, False)
            return
        behaviours = known if known is not None else fire_behaviours_of(weapon)
        if not behaviours:
            debug(f"no {FIRE_BEHAVIOUR_CLASS} found under {path}")
            return
        source, identity = roll_identity(weapon, behaviours)
        key = gun_key(source, identity)
        roll = roll_of(identity)
        _weapon_cache[address] = (path, [WeakPointer(b) for b in behaviours], key, roll, True)

    # Ownership first: a gun is only judged once it is in your hands (or, with
    # 'Player Weapons Only' off, in anyone's), so enemy guns and guns
    # mid-pickup never use up a verdict.
    if _eligible_owner(weapon, address, path):
        wanted, judged_now = decide(key, roll)
        if judged_now:
            log(
                f"judged {path}: {'Masher' if wanted else 'plain revolver'}"
                f" ({describe_decision(key, roll)})"
            )
    else:
        wanted = False
    converted = any(b._get_address() in _touched for b in behaviours)

    if wanted:
        applied: list[str] = []
        for behaviour in behaviours:
            applied = make_masher(behaviour)
        if not converted:
            log(
                f"Masher: {path} -> {int(projectiles.value)} projectiles"
                f" ({', '.join(applied) or 'nothing applied'})"
            )
        return

    if converted:
        # No longer eligible (not yours once 'Player Weapons Only' is on), or
        # its verdict was forgotten and re-judged plain.
        for behaviour in behaviours:
            restore_behaviour(behaviour)
        log(f"no longer a Masher, restored: {path}")


def _eligible_owner(weapon: UObject, address: int, path: str) -> bool:
    """Does `Player Weapons Only` allow this weapon? Re-checked every pass.

    Never cached. Picking a gun up fires the use event before the game hands
    the weapon over, so the first look sees a revolver owned by nobody.
    Caching that verdict made every picked-up gun permanently ineligible until
    a swap respawned it as a new actor. Identity and the roll are what get
    cached; ownership is cheap to re-check.
    """
    global _ownership_warned
    if not player_weapons_only.value:
        return True
    owned = is_player_weapon(weapon)
    if owned is False:
        if address not in _not_ours_reported:
            _not_ours_reported.add(address)
            debug(f"{path} is a Jakobs revolver, but not ours (yet) - will re-check")
        return False
    if owned is None and not _ownership_warned:
        _ownership_warned = True
        log(
            "cannot tell who owns a weapon - converting all of them,"
            " enemies included. Turn off 'Player Weapons Only' to silence"
            " this, or report it."
        )
    return True


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


# When the last automatic sweep ran, so a high-frequency trigger cannot turn
# into a per-frame object scan.
_last_scan = 0.0

# Even an "immediate" trigger is not allowed to sweep more often than this;
# equipping can fire several events in a burst.
IMMEDIATE_FLOOR_SECONDS = 0.25


# After a pickup or equip, the weapon actor and its ownership arrive a moment
# after the event that announced them. For this long afterwards the heartbeat
# may sweep much more often, so the conversion lands within a keypress or two.
FAST_WINDOW_SECONDS = 6.0
FAST_INTERVAL_SECONDS = 0.5
_fast_until = 0.0


def request_follow_up() -> None:
    """Open the fast window: something is about to arrive."""
    global _fast_until
    _fast_until = time.monotonic() + FAST_WINDOW_SECONDS


def maybe_scan(reason: str, immediate: bool = False, force: bool = False) -> int:
    """Sweep, unless one ran too recently. Returns weapons processed."""
    global _last_scan

    if not force and not auto_scan.value:
        return 0

    now = time.monotonic()
    if not force:
        if immediate:
            interval = IMMEDIATE_FLOOR_SECONDS
        elif now < _fast_until:
            interval = FAST_INTERVAL_SECONDS
        else:
            interval = float(scan_interval.value)
        if now - _last_scan < interval:
            return 0
    _last_scan = now

    try:
        return scan_all()
    except Exception:
        log(f"error during automatic scan from {reason}:")
        traceback.print_exc()
        return 0


# --------------------------------------------------------------------------- #
# Triggers.
#
# The five weapon-side hooks below have NEVER been observed firing in solo play
# - BL4 resolves those paths natively, where unrealsdk's ProcessEvent hook
# cannot see them. They are kept because they cost nothing and their counters
# are evidence, but nothing may depend on them.
#
# The triggers that follow are ones other working mods prove are reachable:
# trashSeller hooks OakPlayerController:ServerUseJunkObject, and music_watch
# hooks the Gbx audio library. Each one only asks for a sweep; the throttle
# decides whether it happens.
# --------------------------------------------------------------------------- #


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
    _guard("ClientSetActiveWeaponEquipSlot", lambda: _scan_and_follow_up("weapon swap"))


@hook("/Script/OakGame.OakUIDataCollector_Weapon:OnWeaponEquipped", Type.POST)
def on_ui_weapon_equipped(
    obj: UObject,
    args: WrappedStruct,
    ret: Any,
    func: BoundFunction,
) -> None:
    _guard("OnWeaponEquipped", lambda: _scan_and_follow_up("weapon equipped"))


@hook("/Script/OakGame.OakPlayerController:ServerUseObject", Type.POST)
def on_use_object(
    obj: UObject,
    args: WrappedStruct,
    ret: Any,
    func: BoundFunction,
) -> None:
    # Interacting with the world - which is how a dropped gun gets picked up.
    _guard("ServerUseObject", lambda: _scan_and_follow_up("used an object"))


@hook("/Script/OakGame.OakPlayerController:ServerUseJunkObject", Type.POST)
def on_use_junk(
    obj: UObject,
    args: WrappedStruct,
    ret: Any,
    func: BoundFunction,
) -> None:
    # Proven reachable: trashSeller hooks exactly this.
    _guard("ServerUseJunkObject", lambda: _scan_and_follow_up("used a junk object"))


@hook("/Script/OakGame.OakPlayerController:OnEquipSlotsReadyForInventory", Type.POST)
def on_slots_ready(
    obj: UObject,
    args: WrappedStruct,
    ret: Any,
    func: BoundFunction,
) -> None:
    _guard("OnEquipSlotsReadyForInventory", lambda: _scan_and_follow_up("equip slots ready"))


@hook("/Script/GbxAudio.GbxAudioBlueprintFunctionLibrary:PostEventInWorld", Type.POST)
def on_audio_event(
    obj: UObject,
    args: WrappedStruct,
    ret: Any,
    func: BoundFunction,
) -> None:
    # The heartbeat. Fires constantly - footsteps, gunfire, UI - so it is
    # throttled to `Scan Interval` and does nothing most of the time. Proven
    # reachable: music_watch hooks this library.
    _guard("PostEventInWorld", lambda: maybe_scan("audio heartbeat"))


def _scan_and_follow_up(reason: str) -> None:
    """Sweep now, and keep sweeping briefly for whatever arrives late."""
    maybe_scan(reason, immediate=True)
    request_follow_up()


# --------------------------------------------------------------------------- #
# The real heartbeat: every key press.
#
# `PostEventInWorld` and `OnEquipSlotsReadyForInventory` bound and never fired,
# so the audio "heartbeat" never beat. What demonstrably does reach Python on
# this install is input - the SDK detours UGbxEnhancedPlayerInput::InputKey,
# which is how keybinds work at all. The keybinds stub documents `None` as the
# key meaning "any key", but the compiled module shipped with this install
# rejects it (measured: "incompatible function arguments ... key: str"). So
# `None` is tried first, for builds where it works, and otherwise the heartbeat
# registers one raw keybind per key in HEARTBEAT_KEYS. Moving, firing,
# swapping and pressing E all drive the sweep. It is throttled like everything
# else and never blocks the key.
# --------------------------------------------------------------------------- #

try:
    from keybinds.keybinds import (  # type: ignore[import-not-found]
        deregister_keybind as _deregister_raw_key,
        register_keybind as _register_raw_key,
    )
except Exception:  # pragma: no cover - depends on the SDK build
    _register_raw_key = None
    _deregister_raw_key = None

# Handles of the registered raw keybinds; empty while the heartbeat is off.
_input_handles: list[Any] = []
INPUT_HEARTBEAT = "any key press"

# The fallback when "any key" is not accepted: every key that plausibly gets
# pressed while playing, keyboard and mouse and gamepad. All 26 letters rather
# than a movement set, because the letter a key produces depends on the
# keyboard layout (ZQSD on AZERTY is WASD on QWERTY). Names are Unreal FKeys.
HEARTBEAT_KEYS: tuple[str, ...] = (
    *(chr(c) for c in range(ord("A"), ord("Z") + 1)),
    "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Zero",
    "SpaceBar", "LeftShift", "LeftControl", "LeftAlt", "Tab", "Escape", "Enter",
    "LeftMouseButton", "RightMouseButton", "MiddleMouseButton",
    "ThumbMouseButton", "ThumbMouseButton2", "MouseScrollUp", "MouseScrollDown",
    "Gamepad_FaceButton_Bottom", "Gamepad_FaceButton_Right",
    "Gamepad_FaceButton_Left", "Gamepad_FaceButton_Top",
    "Gamepad_LeftShoulder", "Gamepad_RightShoulder",
    "Gamepad_LeftTrigger", "Gamepad_RightTrigger",
    "Gamepad_LeftThumbstick", "Gamepad_RightThumbstick",
    "Gamepad_DPad_Up", "Gamepad_DPad_Down", "Gamepad_DPad_Left", "Gamepad_DPad_Right",
    "Gamepad_Special_Left", "Gamepad_Special_Right",
)


def _on_any_key() -> None:
    """Called for every key press in the game: must stay cheap, never block."""
    _guard(INPUT_HEARTBEAT, lambda: maybe_scan("input"))
    return None


def start_input_heartbeat() -> bool:
    if _input_handles:
        return True
    if _register_raw_key is None:
        log("no raw keybind support in this SDK build - heartbeat unavailable")
        return False

    try:
        _input_handles.append(_register_raw_key(None, EInputEvent.IE_Pressed, _on_any_key))
        debug("input heartbeat: any key")
        return True
    except Exception:
        pass  # this build wants a real key name - register them one by one

    failed: list[str] = []
    for key in HEARTBEAT_KEYS:
        try:
            _input_handles.append(_register_raw_key(key, EInputEvent.IE_Pressed, _on_any_key))
        except Exception:
            failed.append(key)
    if not _input_handles:
        log(f"could not start the input heartbeat: no key would register ({', '.join(failed[:5])}...)")
        return False
    debug(f"input heartbeat: {len(_input_handles)} keys")
    if failed:
        log(f"input heartbeat: these keys would not register: {', '.join(failed)}")
    return True


def stop_input_heartbeat() -> None:
    if _deregister_raw_key is not None:
        for handle in _input_handles:
            try:
                _deregister_raw_key(handle)
            except Exception:
                pass
    _input_handles.clear()


HOOKS = (
    on_start_using,
    on_equip,
    on_reload,
    on_play_effects,
    on_weapon_swap,
    on_ui_weapon_equipped,
    on_use_object,
    on_use_junk,
    on_slots_ready,
    on_audio_event,
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
        f"{int(masher_chance.value)}% of newly found Jakobs revolvers,"
        " one record per save")
    log(f"hooks bound: {', '.join(bound) if bound else 'NONE'}")
    if unbound:
        log(f"hooks NOT bound: {', '.join(unbound)}")
    heartbeat = start_input_heartbeat()
    if auto_scan.value:
        log(
            "automatic scanning on - equipping and interacting scan immediately,"
            f" key presses at most every {int(scan_interval.value)}s"
            + ("" if heartbeat else " (INPUT HEARTBEAT UNAVAILABLE)")
        )
    else:
        log("automatic scanning off - use the 'Scan Weapons Now' keybind")
    log("'masher mine' shows what your guns are actually doing")


@keybind("Scan Weapons Now", description="Apply the Masher variant to every loaded weapon now.")
def scan_keybind() -> None:
    """Hook-independent trigger, for when the automatic ones do not fire."""
    found = maybe_scan("keybind", force=True)
    log(f"scanned {found} weapon(s)")


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #



# --------------------------------------------------------------------------- #
# Item card research probe
#
# The card does not read the live fire behaviour (see CLAUDE.md). Offline data
# narrows what it does read to a handful of structs - InventoryStatsContainer,
# WeaponStatsContainer, InventoryItem - carried by objects nobody has looked
# inside yet: the weapon actor itself, ground pickups, the inventory owner, and
# the card/backpack UI collectors. `masher card` dumps all of them to a file,
# so the next step is chosen from evidence rather than guessed.
# --------------------------------------------------------------------------- #

CARD_PROBE_FILE_NAME = "jakobs_masher_card_probe.txt"

# Field names worth following one level further down.
CARD_FIELD_RE = re.compile(
    r"(?i)stat|item|inventor|serial|name|card|part|def|container|balance|rarity|level|title|damage|projectile"
)

# Classes to dump every field of. Each is a real class (it has a CDO); the
# stats containers themselves are structs and can only be reached through one.
CARD_PROBE_CLASSES = (
    "OakUIDataCollector_ItemCard",
    "OakUIDataCollector_Backpack",
    "OakUIDataCollector_ItemIcon",
    "OakInventoryOwner",
    "InventoryPickup",
    "InteractiveItemContainer",
)

CARD_PROBE_MAX_LINES = 6000
CARD_PROBE_MAX_FIELDS = 300


class _ProbeOut:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.truncated = False

    def add(self, text: str) -> bool:
        if len(self.lines) >= CARD_PROBE_MAX_LINES:
            self.truncated = True
            return False
        self.lines.append(text)
        return True


def _probe_dump(out: _ProbeOut, obj: Any, indent: str, depth: int, seen: set[int], only_matching: bool) -> None:
    """Every field of `obj`, following matching struct/object fields `depth` deeper."""
    try:
        address = obj._get_address()
    except Exception:
        address = None
    if address is not None:
        if address in seen:
            out.add(f"{indent}(already shown)")
            return
        seen.add(address)

    # Filter before capping: capping first cut the pawn off at "Inventory...",
    # alphabetically, and hid every field after it.
    names = [n for n in _field_names(obj) if not only_matching or CARD_FIELD_RE.search(n)]
    for name in names[:CARD_PROBE_MAX_FIELDS]:
        try:
            value = getattr(obj, name)
        except Exception as exc:
            if not out.add(f"{indent}{name}: <unreadable {type(exc).__name__}>"):
                return
            continue
        if isinstance(value, BoundFunction):
            continue
        if not out.add(f"{indent}{name} = {_describe(value)[:200]}"):
            return
        if depth <= 0 or not CARD_FIELD_RE.search(name):
            continue
        if isinstance(value, (str, bytes, bool, int, float)) or value is None:
            continue
        children = _elements(value)
        is_array = not (len(children) == 1 and children[0] is value)
        if is_array:
            out.add(f"{indent}  [{len(children)} element(s) shown]")
        for index, child in enumerate(children[:3]):
            if _as_object(child) is None:
                continue
            if is_array:
                out.add(f"{indent}  [{index}]")
            _probe_dump(out, child, indent + "    ", depth - 1, seen, only_matching=False)


def _outer_chain(obj: Any) -> str:
    parts = []
    node = obj
    for _ in range(8):
        try:
            node = node.Outer
        except Exception:
            break
        if node is None:
            break
        try:
            parts.append(node.Class.Name)
        except Exception:
            parts.append("?")
    return " < ".join(parts) or "(no outer)"


def probe_card() -> Path:
    out = _ProbeOut()
    out.add(f"Jakobs Masher card probe, {datetime.datetime.now().isoformat(timespec='seconds')}")
    out.add(f"save: {current_save_id()}")

    # 1. Every fire behaviour, grouped by what it lives under. Behaviours that
    # do not sit under a weapon actor are the card's candidates.
    out.add("")
    out.add("== 1. fire behaviours, by outer chain ==")
    groups: dict[str, list[UObject]] = {}
    for behaviour in unrealsdk.find_all(FIRE_BEHAVIOUR_CLASS, False):
        try:
            if behaviour == behaviour.Class.ClassDefaultObject:
                continue
        except Exception:
            continue
        groups.setdefault(_outer_chain(behaviour), []).append(behaviour)
    for chain, members in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        out.add(f"{len(members):4d} x  {chain}")
        for behaviour in members[:3]:
            numbers = []
            for field in ("ProjectilesPerShot", "Damage", "Spread"):
                subfields = _probe_property(behaviour, field)
                value = None if subfields is None else _read_effective(behaviour, field, subfields)
                numbers.append(f"{field}={value}")
            out.add(f"        {behaviour._path_name()}  {'  '.join(numbers)}")

    # 2. The Jakobs pistols you carry: the whole actor, one level into anything
    # that looks like stats, item data, parts or a name.
    out.add("")
    out.add("== 2. your Jakobs pistols (weapon actors) ==")
    seen: set[int] = set()
    for weapon, behaviours in live_weapons().values():
        if is_player_weapon(weapon) is False or not is_jakobs_pistol(weapon):
            continue
        out.add(f"--- {weapon.Class.Name} {weapon._path_name()}  masher={cached_masher(weapon)}")
        _probe_dump(out, weapon, "    ", 2, seen, only_matching=False)

    # 3. The player: inventory, items, containers.
    out.add("")
    out.add("== 3. player controller and pawn (inventory-like fields) ==")
    pc = get_pc()
    for label, obj in (("controller", pc), ("pawn", getattr(pc, "Pawn", None) if pc else None)):
        if obj is None:
            out.add(f"--- {label}: none")
            continue
        out.add(f"--- {label}: {obj.Class.Name} {obj._path_name()}")
        _probe_dump(out, obj, "    ", 2, seen, only_matching=True)

    # 4. Everything that might build or hold a card.
    out.add("")
    out.add("== 4. card / backpack / pickup / inventory classes ==")
    for class_name in CARD_PROBE_CLASSES:
        try:
            instances = [o for o in unrealsdk.find_all(class_name, False) if o != o.Class.ClassDefaultObject]
        except Exception as exc:
            out.add(f"--- {class_name}: cannot enumerate ({exc})")
            continue
        out.add(f"--- {class_name}: {len(instances)} instance(s)")
        for obj in instances[:3]:
            out.add(f"  * {obj._path_name()}")
            _probe_dump(out, obj, "      ", 2, seen, only_matching=False)

    if out.truncated:
        out.lines.append(f"... truncated at {CARD_PROBE_MAX_LINES} lines")
    path = Path(_SETTINGS_DIR) / CARD_PROBE_FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out.lines), encoding="utf-8")
    return path


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
                field, subfields = resolved
                where = field + (f" ({', '.join(subfields)})" if subfields else "")
                log(f"  {knob:<12} -> {where}")
        log("hook activity:")
        for hook_obj in HOOKS:
            for func_name, _hook_type in hook_obj.hook_funcs:
                short = func_name.rsplit(":", 1)[-1]
                log(
                    f"  {short:<34} bound={bool(hook_obj.get_active_count())}"
                    f"  fired={_fires.get(short, 0)}"
                )
        log(
            f"  {INPUT_HEARTBEAT:<34} bound={len(_input_handles)} key(s)"
            f"  fired={_fires.get(INPUT_HEARTBEAT, 0)}"
        )
        log(f"weapons seen: {len(_weapon_cache)}, behaviours modified: {len(_touched)}")
        for path, pointers, key, roll, jakobs in _weapon_cache.values():
            if not jakobs:
                log(f"  not a Jakobs pistol  {path}")
                continue
            log(f"  {describe_decision(key, roll)}  behaviours={len(pointers)}  {path}")
        save_id = refresh_save_id(force=True)
        log(
            f"loaded save: {save_id}"
            + (f" (via {_save_source[2]})" if _save_source else " (not identified)")
        )
        log(
            f"remembered verdicts: {remembered_count(save_id)} in {registry_path(save_id)}"
            + ("  (UNREADABLE - not being saved)" if save_id in _unreadable_saves else "")
        )
        return

    if args.action == "scan":
        found = maybe_scan("console", force=True)
        log(f"scanned {found} weapon(s)")
        return

    if args.action == "mine":
        refresh_save_id(force=True)
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
            log(
                f"  {'/'.join(tags) or 'untagged':<10}"
                f" jakobs_pistol={jakobs} masher={cached_masher(weapon)}"
            )
            if jakobs:
                # What the per-gun decision is made from. Two different
                # revolvers must show different numbers here, or every gun
                # rolls alike and the chance is all-or-nothing.
                source, identity = roll_identity(weapon, behaviours)
                log(f"      {source} = {', '.join(map(str, identity)) or 'NONE'}")
                # The key as judged, from the cache: a stat fingerprint read
                # now would include the Masher's own widened spread.
                cached = _weapon_cache.get(weapon._get_address())
                if cached is not None and cached[4]:
                    key, roll = cached[2], cached[3]
                else:
                    key, roll = gun_key(source, identity), roll_of(identity)
                log(f"      {describe_decision(key, roll)}")

            # The item card is built from the item's own stats container, not
            # from the live behaviour this mod writes to, so it keeps showing
            # the unmodified numbers. Report the real maths here instead.
            for behaviour in behaviours:
                numbers = {}
                for knob in ("projectiles", "damage", "spread"):
                    resolved = _resolved_fields.get(knob)
                    if not resolved:
                        continue
                    field, subfields = resolved
                    value = _read_effective(behaviour, field, subfields)
                    if value is not None:
                        numbers[knob] = value

                if "projectiles" not in numbers or "damage" not in numbers:
                    continue

                count = numbers["projectiles"]
                per_shot = numbers["damage"]
                spread_now = numbers.get("spread")
                log(
                    f"      {per_shot:.0f} x {count:.0f}"
                    f"  =  {per_shot * count:.0f} per trigger pull"
                    + (f"   (spread {spread_now:.2f})" if spread_now else "")
                )

                record = _touched.get(behaviour._get_address(), {})
                anchor = None
                for key, entry in record.items():
                    if entry["field"] == (_resolved_fields.get("damage") or ("",))[0]:
                        anchor = entry["original"]
                        break
                if anchor:
                    log(
                        f"      unmodified it would be {anchor:.0f} x 1"
                        f"  =  {anchor:.0f}, so this is {per_shot * count / anchor:.2f}x"
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
            log(
                f"--- {'/'.join(tags) or 'untagged'}"
                f" jakobs_pistol={is_jakobs_pistol(weapon)}"
                f" masher={cached_masher(weapon)}"
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

    if args.action == "card":
        path = probe_card()
        log(f"card probe written to {path}")
        return

    if args.action == "save":
        # Which save is loaded, and every candidate that was asked. When the
        # saves cannot be told apart, this is the output to report.
        save_id = refresh_save_id(force=True)
        log(f"loaded save: {save_id}")
        log(f"record file: {registry_path(save_id)}  ({remembered_count(save_id)} gun(s))")
        for label, holder in _save_id_holders():
            try:
                where = f"{holder.Class.Name} {holder._path_name()}"
            except Exception:
                where = "?"
            log(f"  {label}: {where}")
            for field in SAVE_ID_FIELDS:
                try:
                    raw = getattr(holder, field)
                except Exception as exc:
                    log(f"      {field}: not a property ({type(exc).__name__})")
                    continue
                log(f"      {field} = {_describe(raw)[:120]} -> {_format_guid(raw)}")
        try:
            others = sorted(p.stem for p in registry_dir().glob("*.json"))
        except Exception:
            others = []
        log(f"records on disk: {', '.join(others) or 'none'}")
        return

    if args.action == "forget":
        # Re-judge everything at the current chance: revert, forget, sweep.
        refresh_save_id(force=True)
        count = forget_all()
        restore_all()
        maybe_scan("forget", force=True)
        log(
            f"forgot {count} remembered gun(s) for this save; what you carry was judged again"
            f" at {int(masher_chance.value)}%"
        )
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
    choices=("dump", "status", "scan", "mine", "probe", "restore", "forget", "save", "card"),
    help=(
        "dump the live weapon data, show mod status and hook activity,"
        " scan every loaded weapon now, list the guns you are carrying,"
        " probe them in full, undo all changes, forget this save's remembered"
        " verdicts and judge your guns again, show which save is loaded, or"
        " dump what the item card might read to a file"
    ),
)


# --------------------------------------------------------------------------- #

def on_mod_disabled() -> None:
    stop_input_heartbeat()
    restore_all()


build_mod(
    coop_support=CoopSupport.HostOnly,
    on_enable=on_mod_enabled,
    on_disable=on_mod_disabled,
)
