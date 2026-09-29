"""Exercise jakobs_masher against the fake engine."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import fake_engine as env  # noqa: E402

env.install()

# The mod lives one level up, beside this test directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

jm = importlib.import_module("jakobs_masher")

FAILS: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}" + (f"  -- {extra}" if extra else ""))
    if not cond:
        FAILS.append(label)


# The game lays assets out by weapon class, then manufacturer.
CLASS_DIRS = {
    "PS": "Pistols",
    "SG": "Shotguns",
    "AR": "AssaultRifles",
    "SM": "SMG",
    "SR": "Sniper",
    "HW": "Heavy",
}


def make_weapon(tag: str, parts: tuple[int, ...], damage=100.0, spread=1.0):
    """A weapon actor plus the fire behaviour hanging off it."""
    manufacturer, weapon_class = tag.split("_")
    directory = f"{CLASS_DIRS[weapon_class]}/{manufacturer}"
    weapon = env.FakeObject(
        "OakWeapon",
        path=f"World.{tag}_{env._NEXT_ADDR[0]}",
        BodyData=f"/Game/Gear/Weapons/{directory}/Body_{tag}.Body_{tag}",
        CurrentUseModeIndex=0,
    )
    # GetPartValue: the engine exposes it as a callable property.
    part_list = list(parts)

    def get_part_value(slot: int) -> int:
        if slot >= len(part_list):
            raise RuntimeError("slot out of range")
        return part_list[slot]

    weapon._props["GetPartValue"] = env.BoundFunction(get_part_value)

    behaviour = env.FakeObject(
        "WeaponBehavior_FireProjectile",
        path=f"{weapon._path_name()}.FireProjectile",
        outer=weapon,
        ProjectilesPerShot=1,
        Damage=damage,
        Spread=spread,
        FireRate=2.5,
    )
    return weapon, behaviour


def reset_mod() -> None:
    jm._touched.clear()
    jm._weapon_cache.clear()
    jm._identity_cache.clear()
    jm._part_values_work = None
    jm._ownership_field = None
    jm._ownership_warned = False
    jm._resolved_fields.clear()
    jm._missing_reported.clear()
    jm._shape_reported.clear()
    jm._drift_reported.clear()
    jm._not_ours_reported.clear()
    jm._registry = None
    jm._registry_unreadable = False
    jm.registry_path().unlink(missing_ok=True)
    env.reset_world()


# --------------------------------------------------------------------------- #
print("\n== options are legal ==")
check("6 projectiles default", jm.projectiles.value == 6)
check("damage scale 0.40", abs(jm.damage_scale.value - 0.40) < 1e-9)
check("net damage is 2.4x", abs(jm.projectiles.value * jm.damage_scale.value - 2.4) < 1e-9)

# --------------------------------------------------------------------------- #
print("\n== identifies Jakobs pistols, ignores everything else ==")
reset_mod()
jak, jak_beh = make_weapon("JAK_PS", (0, 1, 2, 3))
check("JAK_PS recognised", jm.is_jakobs_pistol(jak))
check(
    "identity found in the graph",
    any("JAK_PS" in text for text in jm.collect_identity(jak)),
    str(jm.collect_identity(jak))[:160],
)

jak_sg, _ = make_weapon("JAK_SG", (0, 1, 2, 3))
check("JAK_SG rejected", not jm.is_jakobs_pistol(jak_sg))
ted_ps, _ = make_weapon("TED_PS", (0, 1, 2, 3))
check("TED_PS rejected", not jm.is_jakobs_pistol(ted_ps))

# --------------------------------------------------------------------------- #
# The first in-game run found no type name on the weapon at all: BL4's weapon
# definitions are NCS data, not UObjects, so the only readable clue is an asset
# reference sitting one or more hops away. These cover that shape.
print("\n== identity is found through the object graph, not one property ==")
reset_mod()

# Nested: the name is on a sub-object, not on the weapon.
nested = env.FakeObject("OakWeapon", path="World.Unnamed_1", Tag="nothing useful")
body = env.FakeObject(
    "OakWeaponBody",
    path="World.Unnamed_1.Body",
    Mesh="/Game/Gear/Weapons/Pistols/JAK/Model/SK_JAK_PS.SK_JAK_PS",
)
nested._props["BodyOwner"] = body
check("identity reachable one hop away", jm.is_jakobs_pistol(nested))

# Two hops, via the directory layout rather than the JAK_PS tag.
reset_mod()
deep_leaf = env.FakeObject(
    "MaterialInstance",
    path="World.Deep.Mat",
    Asset="/Game/Gear/Weapons/Pistols/JAK/Materials/DA_MD_JAK.DA_MD_JAK",
)
deep_mid = env.FakeObject("MeshComponent", path="World.Deep.Mesh")
deep_mid._props["Material"] = deep_leaf
deep = env.FakeObject("OakWeapon", path="World.Deep")
deep._props["Mesh1P"] = deep_mid
check("identity reachable two hops away", jm.is_jakobs_pistol(deep))

# Through an array, which is where parts and components live.
reset_mod()
arrayed = env.FakeObject("OakWeapon", path="World.Arrayed")
arrayed._props["Parts"] = [
    env.FakeObject("Part", path="World.Arrayed.P0", Asset="/Game/Whatever/Thing"),
    env.FakeObject("Part", path="World.Arrayed.P1", Asset="Body_JAK_PS"),
]
check("identity reachable through an array", jm.is_jakobs_pistol(arrayed))

# An explicit tag beats the directory heuristic: a weapon that borrows a shared
# or licensed asset from the Jakobs pistol folder is still whatever its own tag
# says it is.
reset_mod()
borrower = env.FakeObject(
    "OakWeapon",
    path="World.Borrower",
    BodyData="/Game/Gear/Weapons/Shotguns/JAK/Body_JAK_SG.Body_JAK_SG",
    Licensed="/Game/Gear/Weapons/Pistols/JAK/Shared/SomeSharedThing",
)
check("an explicit tag outranks the directory", not jm.is_jakobs_pistol(borrower))

# ... but with no tag anywhere, the directory is all there is to go on.
reset_mod()
untagged = env.FakeObject(
    "OakWeapon",
    path="World.Untagged",
    Mesh="/Game/Gear/Weapons/Pistols/JAK/Model/SK_Revolver.SK_Revolver",
)
check("the directory is used when nothing is tagged", jm.is_jakobs_pistol(untagged))

# A reference cycle must terminate.
reset_mod()
a = env.FakeObject("OakWeapon", path="World.Cycle.A")
bb = env.FakeObject("Thing", path="World.Cycle.B")
a._props["Link"] = bb
bb._props["Back"] = a
try:
    check("a reference cycle terminates", jm.is_jakobs_pistol(a) is False)
except RecursionError as exc:  # noqa: BLE001
    check("a reference cycle terminates", False, repr(exc))

# Upward links are not followed - otherwise one weapon would see the whole map.
reset_mod()
world_owner = env.FakeObject("World", path="World", Name="Body_JAK_PS")
owned = env.FakeObject("OakWeapon", path="World.Owned")
owned._props["Owner"] = world_owner
check("upward links are not followed", not jm.is_jakobs_pistol(owned))

# Vehicle turrets are weapons too, and were the first things the live scan hit.
reset_mod()
turret, _ = make_weapon("JAK_PS", (1, 2, 3, 4))
turret._class_name = "OakVehicleWeapon"
check("vehicle weapons are ignored", not jm.is_jakobs_pistol(turret))

# Identity is cached per weapon, and cleared on restore.
reset_mod()
w, b = make_weapon("JAK_PS", (1, 2, 3, 4))
jm.is_jakobs_pistol(w)
check("identity cached", len(jm._identity_cache) == 1, str(len(jm._identity_cache)))
jm.restore_all()
check("restore clears the identity cache", not jm._identity_cache)

# --------------------------------------------------------------------------- #
# The sweep sees every weapon actor in the level, so enemy guns have to be
# filtered out or they become 2.4x damage enemies.
print("\n== only the player's weapons are converted ==")


def make_player():
    pawn = env.FakeObject("OakCharacter", path="World.PlayerPawn")
    pc = env.FakeObject("OakPlayerController", path="World.PC")
    pc._props["Pawn"] = pawn
    env.set_player(pc)
    return pc, pawn


reset_mod()
jm.masher_chance.value = 100
pc, pawn = make_player()

mine, mine_beh = make_weapon("JAK_PS", (1, 2, 3, 4))
mine._props["WeaponUser"] = pawn
check("my own weapon is recognised", jm.is_player_weapon(mine) is True)

enemy_pawn = env.FakeObject("OakCharacter", path="World.EnemyPawn")
theirs, theirs_beh = make_weapon("JAK_PS", (1, 2, 3, 4))
theirs._props["WeaponUser"] = enemy_pawn
check("an enemy weapon is rejected", jm.is_player_weapon(theirs) is False)

orphan, orphan_beh = make_weapon("JAK_PS", (1, 2, 3, 4))
check("no owner link at all is undecidable", jm.is_player_weapon(orphan) is None)

jm.scan_all()
check("my revolver converted", mine_beh.ProjectilesPerShot == 6)
check("the enemy's revolver left alone", theirs_beh.ProjectilesPerShot == 1)
check(
    "undecidable ownership still converts, and warns once",
    orphan_beh.ProjectilesPerShot == 6 and jm._ownership_warned,
)

# Ownership through a held component rather than the pawn directly.
reset_mod()
pc, pawn = make_player()
held = env.FakeObject("WeaponBody", path="World.Held")
held._props["Owner"] = pawn
indirect, indirect_beh = make_weapon("JAK_PS", (1, 2, 3, 4))
indirect._props["BodyOwner"] = held
check("ownership found through one hop", jm.is_player_weapon(indirect) is True)

# With the option off, everything converts.
reset_mod()
pc, pawn = make_player()
jm.player_weapons_only.value = False
theirs, theirs_beh = make_weapon("JAK_PS", (1, 2, 3, 4))
theirs._props["WeaponUser"] = env.FakeObject("OakCharacter", path="World.Enemy2")
jm.scan_all()
check("option off converts enemy weapons too", theirs_beh.ProjectilesPerShot == 6)
jm.player_weapons_only.value = True
env.set_player(None)

# --------------------------------------------------------------------------- #
print("\n== part values are read through GetPartValue ==")
reset_mod()
w, b = make_weapon("JAK_PS", (5, 2, 9))
check("part values read", jm.part_values(w) == (5, 2, 9), str(jm.part_values(w)))
check("GetPartValue marked usable", jm._part_values_work is True)

# --------------------------------------------------------------------------- #
print("\n== the masher roll is stable and proportioned ==")
reset_mod()
jm.masher_chance.value = 50

# Deterministic: same parts -> same answer, every time.
sample = [(3, 1, 4, 1), (0, 0, 0, 0), (9, 9, 9, 9), (2, 7, 1, 8)]
stable = True
for parts in sample:
    w, b = make_weapon("JAK_PS", parts)
    first = jm.rolls_masher(w, [b])
    for _ in range(5):
        w2, b2 = make_weapon("JAK_PS", parts)
        if jm.rolls_masher(w2, [b2]) != first:
            stable = False
check("same parts always give the same answer", stable)

# Distribution: each chance setting converts about that share of guns.
def roll_many(total=2000):
    rolls = []
    for i in range(total):
        parts = (i % 17, (i // 17) % 19, (i // 323) % 7, (i * 7) % 23)
        w, b = make_weapon("JAK_PS", parts)
        rolls.append((parts, jm.masher_roll(w, [b])))
    return rolls


many = roll_many()
check("every roll lands on 0..100", all(r is not None and 0 <= r < 100 for _, r in many))
for chance, low, high in ((50, 0.43, 0.57), (25, 0.19, 0.31), (17, 0.12, 0.22), (5, 0.02, 0.08)):
    jm.masher_chance.value = chance
    ratio = sum(jm.is_masher_roll(r) for _, r in many) / len(many)
    check(f"about {chance}% are mashers at chance {chance}", low < ratio < high, f"{ratio:.1%}")

# Nested: every Masher at a low chance is still one at any higher chance, so
# moving the slider up never takes a Masher away from you.
nested = True
for lower, higher in ((5, 17), (17, 25), (25, 50), (50, 99)):
    for _, roll in many:
        jm.masher_chance.value = lower
        was = jm.is_masher_roll(roll)
        jm.masher_chance.value = higher
        if was and not jm.is_masher_roll(roll):
            nested = False
check("raising the chance only adds mashers", nested)

jm.masher_chance.value = 0
check("0 makes nothing a masher", not any(jm.is_masher_roll(r) for _, r in many))
jm.masher_chance.value = 100
check("100 makes everything a masher", all(jm.is_masher_roll(r) for _, r in many))
check("100 includes a gun with no identity to roll", jm.is_masher_roll(None))
jm.masher_chance.value = 99
check("below 100 a gun with no identity is not one", not jm.is_masher_roll(None))
jm.masher_chance.value = 50

# --------------------------------------------------------------------------- #
# The second in-game run applied nothing and could not say why: a failed write
# was indistinguishable from an absent property. Each knob now resolves to
# whichever candidate property the object actually has.
print("\n== knobs resolve to whatever property exists ==")
reset_mod()
jm.masher_chance.value = 100

w, b = make_weapon("JAK_PS", (1, 2, 3, 4))
applied = jm.make_masher(b)
check(
    "standard names resolve",
    applied == ["ProjectilesPerShot", "Damage", "Spread"],
    str(applied),
)
check(
    "resolution is remembered",
    jm._resolved_fields["projectiles"] == ("ProjectilesPerShot", ()),
    str(jm._resolved_fields),
)

# An engine that spells it differently still works.
reset_mod()
alt_weapon = env.FakeObject("OakWeapon", path="World.Alt", BodyData="Body_JAK_PS")
alt = env.FakeObject(
    "WeaponBehavior_FireProjectile",
    path="World.Alt.Fire",
    outer=alt_weapon,
    ProjectileCount=1,
    BaseDamage=100.0,
    SpreadScale=1.0,
)
applied = jm.make_masher(alt)
check(
    "alternate spellings resolve",
    applied == ["ProjectileCount", "BaseDamage", "SpreadScale"],
    str(applied),
)
check("alternate projectile count applied", alt.ProjectileCount == 6)
check("alternate damage scaled", abs(alt.BaseDamage - 40.0) < 1e-6, str(alt.BaseDamage))

# A behaviour with none of the candidates reports rather than failing mute.
reset_mod()
bare_weapon = env.FakeObject("OakWeapon", path="World.Bare", BodyData="Body_JAK_PS")
bare = env.FakeObject(
    "WeaponBehavior_FireProjectile",
    path="World.Bare.Fire",
    outer=bare_weapon,
    SomethingElse=1,
)
applied = jm.make_masher(bare)
check("nothing resolves on a bare behaviour", applied == [], str(applied))
check(
    "every missing knob is recorded",
    jm._missing_reported == {"projectiles", "damage", "spread"},
    str(jm._missing_reported),
)
check(
    "and nothing is recorded as touched",
    not jm._touched,
    str(jm._touched),
)

# Only successful writes count as touched - the old code booked an entry before
# it knew whether the write would work, which made 'behaviours modified' lie.
reset_mod()
half_weapon = env.FakeObject("OakWeapon", path="World.Half", BodyData="Body_JAK_PS")
half = env.FakeObject(
    "WeaponBehavior_FireProjectile",
    path="World.Half.Fire",
    outer=half_weapon,
    ProjectilesPerShot=1,
)
applied = jm.make_masher(half)
check("the one present knob applies", applied == ["ProjectilesPerShot"], str(applied))
check(
    "touched records only what was written",
    list(jm._touched.values())[0].keys() == {"ProjectilesPerShot"},
    str(jm._touched),
)
jm.restore_all()
check("partial application still restores", half.ProjectilesPerShot == 1)

# --------------------------------------------------------------------------- #
# In game these properties are not numbers. Reading one yields a WrappedStruct
# (a Gbx attribute value), and writing an int fails outright. This is the shape
# the mod actually meets.
print("\n== values behind attribute structs ==")


def make_struct_weapon(scalar_name=None, projectiles=1, damage=100.0, spread=1.0):
    """A weapon whose values are attribute structs, as the game really has them.

    By default both members are present and equal, which is what an untouched
    weapon looks like in game.
    """

    def attr(value):
        if scalar_name is not None:
            return env.WrappedStruct(**{scalar_name: value})
        return env.WrappedStruct(Value=value, BaseValue=value)

    weapon = env.FakeObject(
        "OakWeapon",
        path=f"World.Struct_{env._NEXT_ADDR[0]}",
        BodyData="/Game/Gear/Weapons/Pistols/JAK/Body_JAK_PS.Body_JAK_PS",
    )
    weapon._props["GetPartValue"] = env.BoundFunction(lambda s: (1, 2, 3, 4)[s % 4])
    behaviour = env.FakeObject(
        "WeaponBehavior_FireProjectile",
        path=f"{weapon._path_name()}.Fire",
        outer=weapon,
        ProjectilesPerShot=attr(projectiles),
        Damage=attr(damage),
        Spread=attr(spread),
    )
    return weapon, behaviour


reset_mod()
jm.masher_chance.value = 100
sw, sb = make_struct_weapon()

applied = jm.make_masher(sb)
check(
    "struct-valued knobs report the member the mod owns",
    applied
    == ["ProjectilesPerShot.BaseValue", "Damage.BaseValue", "Spread.BaseValue"],
    str(applied),
)
check(
    "resolution records every numeric member",
    jm._resolved_fields["projectiles"]
    == ("ProjectilesPerShot", ("Value", "BaseValue")),
    str(jm._resolved_fields["projectiles"]),
)

# The bug that cost five sessions: only BaseValue was written, and the game
# reads Value. Both must move.
check("effective Value written", sb.ProjectilesPerShot.Value == 6)
check("BaseValue written too", sb.ProjectilesPerShot.BaseValue == 6)
check(
    "damage scaled on both members",
    abs(sb.Damage.Value - 40.0) < 1e-6 and abs(sb.Damage.BaseValue - 40.0) < 1e-6,
    f"Value={sb.Damage.Value} BaseValue={sb.Damage.BaseValue}",
)
check(
    "spread widened on both members",
    abs(sb.Spread.Value - 3.0) < 1e-6 and abs(sb.Spread.BaseValue - 3.0) < 1e-6,
    f"Value={sb.Spread.Value} BaseValue={sb.Spread.BaseValue}",
)

# Repeated application must not compound through the struct either.
for _ in range(5):
    jm.make_masher(sb)
check(
    "struct damage does not compound",
    abs(sb.Damage.Value - 40.0) < 1e-6,
    str(sb.Damage.Value),
)

jm.restore_all()
check(
    "struct projectiles restored on both members",
    sb.ProjectilesPerShot.Value == 1 and sb.ProjectilesPerShot.BaseValue == 1,
    f"Value={sb.ProjectilesPerShot.Value} BaseValue={sb.ProjectilesPerShot.BaseValue}",
)
check(
    "struct damage restored on both members",
    abs(sb.Damage.Value - 100.0) < 1e-6 and abs(sb.Damage.BaseValue - 100.0) < 1e-6,
    f"Value={sb.Damage.Value} BaseValue={sb.Damage.BaseValue}",
)

# A differently-named scalar still resolves.
reset_mod()
jm.masher_chance.value = 100
sw2, sb2 = make_struct_weapon(scalar_name="Constant")
applied = jm.make_masher(sb2)
check(
    "an alternate struct scalar resolves",
    applied and applied[0] == "ProjectilesPerShot.Constant",
    str(applied),
)
check("alternate scalar written", sb2.ProjectilesPerShot.Constant == 6)

# A ratio between the members must survive scaling - Damage really runs
# BaseValue 82 / Value 241, and flattening the two would wreck the modifiers.
reset_mod()
jm.masher_chance.value = 100
ratio_w = env.FakeObject("OakWeapon", path="World.Ratio", BodyData="Body_JAK_PS")
ratio_b = env.FakeObject(
    "WeaponBehavior_FireProjectile",
    path="World.Ratio.Fire",
    outer=ratio_w,
    Damage=env.WrappedStruct(Value=240.0, BaseValue=80.0),
)
jm.make_masher(ratio_b)
check(
    "scaling preserves the base/effective ratio",
    abs(ratio_b.Damage.Value - 96.0) < 1e-6
    and abs(ratio_b.Damage.BaseValue - 32.0) < 1e-6,
    f"Value={ratio_b.Damage.Value} BaseValue={ratio_b.Damage.BaseValue}",
)

# A struct with no numeric field is reported, not silently skipped.
reset_mod()
opaque_weapon = env.FakeObject(
    "OakWeapon", path="World.Opaque", BodyData="Body_JAK_PS"
)
opaque = env.FakeObject(
    "WeaponBehavior_FireProjectile",
    path="World.Opaque.Fire",
    outer=opaque_weapon,
    ProjectilesPerShot=env.WrappedStruct(SomeHandle="not a number", Flags="x"),
)
applied = jm.make_masher(opaque)
check("an opaque struct applies nothing", applied == [], str(applied))
check(
    "and its shape is reported",
    "ProjectilesPerShot" in jm._shape_reported,
    str(jm._shape_reported),
)

# A write that reports success but does not survive must be caught. This is the
# difference between "the mod applied it" and "the game kept it".
reset_mod()
jm.masher_chance.value = 100
dw, db = make_struct_weapon()
jm.make_masher(db)
check("no drift right after writing", jm.check_drift(db) == [], str(jm.check_drift(db)))

# Simulate the engine re-initialising the weapon: the owned member resets.
db.ProjectilesPerShot.BaseValue = 1
db.ProjectilesPerShot.Value = 1
drift = jm.check_drift(db)
check(
    "a reset base value is detected",
    any("projectiles" in d for d in drift),
    str(drift),
)

# A write that silently does nothing is caught too.
reset_mod()
jm.masher_chance.value = 100
sticky_w = env.FakeObject(
    "OakWeapon", path="World.Sticky", BodyData="Body_JAK_PS"
)


class StubbornStruct(env.WrappedStruct):
    """Accepts writes and discards them, like a value the engine recomputes."""

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        # swallow it


stubborn = env.FakeObject(
    "WeaponBehavior_FireProjectile",
    path="World.Sticky.Fire",
    outer=sticky_w,
    ProjectilesPerShot=StubbornStruct(Value=1, BaseValue=1),
)
jm.make_masher(stubborn)
check(
    "a write that does not take is reported",
    any("projectiles" in d for d in jm.check_drift(stubborn)),
    str(jm.check_drift(stubborn)),
)

# ProjectilesPerShot is a GbxAttributeInteger: writing a float to it fails with
# "Unable to cast ... to C++ type 'int'". Damage and Spread are floats, which is
# why spread worked in game while the projectile count silently did not.
reset_mod()
jm.masher_chance.value = 100
int_w = env.FakeObject("OakWeapon", path="World.Ints", BodyData="Body_JAK_PS")


class TypedStruct(env.WrappedStruct):
    """Rejects a float written to a member that currently holds an int."""

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        fields = object.__getattribute__(self, "_fields")
        if name not in fields:
            raise AttributeError(f"no such struct field {name}")
        if isinstance(fields[name], int) and not isinstance(value, int):
            raise TypeError(
                "Unable to cast Python instance of type "
                f"{type(value)} to C++ type 'int'"
            )
        fields[name] = value


int_b = env.FakeObject(
    "WeaponBehavior_FireProjectile",
    path="World.Ints.Fire",
    outer=int_w,
    ProjectilesPerShot=TypedStruct(Value=1, BaseValue=1),
    Damage=env.WrappedStruct(Value=100.0, BaseValue=100.0),
)
applied = jm.make_masher(int_b)
check(
    "an integer member is written as an int",
    int_b.ProjectilesPerShot.Value == 6 and int_b.ProjectilesPerShot.BaseValue == 6,
    f"Value={int_b.ProjectilesPerShot.Value} BaseValue={int_b.ProjectilesPerShot.BaseValue}",
)
check(
    "the integer write is reported as applied",
    any("ProjectilesPerShot" in a for a in applied),
    str(applied),
)
check(
    "float members are still written as floats",
    abs(int_b.Damage.Value - 40.0) < 1e-6,
    str(int_b.Damage.Value),
)
check("no drift on mixed types", jm.check_drift(int_b) == [], str(jm.check_drift(int_b)))

# Scaling an integer member rounds rather than failing.
reset_mod()
jm.masher_chance.value = 100
round_w = env.FakeObject("OakWeapon", path="World.Round", BodyData="Body_JAK_PS")
round_b = env.FakeObject(
    "WeaponBehavior_FireProjectile",
    path="World.Round.Fire",
    outer=round_w,
    Spread=TypedStruct(Value=3, BaseValue=3),
)
jm.make_masher(round_b)
check(
    "scaling an int member rounds to an int",
    round_b.Spread.Value == 9 and isinstance(round_b.Spread.Value, int),
    f"{round_b.Spread.Value!r}",
)

# --------------------------------------------------------------------------- #
# In game, picking a gun up fires the use event BEFORE the game hands the weapon
# over. The scan it triggered saw a Jakobs revolver owned by nobody, and the old
# code cached "not ours" permanently - so picked-up guns only converted after a
# swap respawned them as new actors.
print("\n== ownership is re-checked, never cached as a no ==")
reset_mod()
jm.masher_chance.value = 100
pc, pawn = make_player()

ground_w, ground_b = make_struct_weapon()
nobody = env.FakeObject("OakCharacter", path="World.Nobody")
ground_w._props["WeaponUser"] = nobody  # mid-pickup: not handed over yet

jm.maybe_scan("used an object", force=True)
check("mid-pickup the gun is not converted", ground_b.ProjectilesPerShot.Value == 1)
check(
    "the gun is remembered as a Jakobs revolver, but not as 'not ours'",
    jm._weapon_cache.get(ground_w._get_address(), (None,) * 5)[-1] is True,
    str(jm._weapon_cache.get(ground_w._get_address())),
)

ground_w._props["WeaponUser"] = pawn  # the game finishes the handover
jm.maybe_scan("next pass", force=True)
check("once it is ours, the next pass converts it", ground_b.ProjectilesPerShot.Value == 6)

# Re-checking every pass must not mean logging every pass.
reset_mod()
jm.masher_chance.value = 100
pc, pawn = make_player()
enemy_w, enemy_b = make_struct_weapon()
enemy_w._props["WeaponUser"] = env.FakeObject("OakCharacter", path="World.Enemy")
jm.verbose_logging.value = True
import io as _io
import contextlib as _contextlib

_buf = _io.StringIO()
with _contextlib.redirect_stdout(_buf):
    for _ in range(5):
        jm.maybe_scan("pass", force=True)
jm.verbose_logging.value = False
check(
    "an enemy's revolver is reported once, not every pass",
    _buf.getvalue().count("not ours") == 1,
    f"{_buf.getvalue().count('not ours')} reports",
)
check("and never converted", enemy_b.ProjectilesPerShot.Value == 1)
env.set_player(None)

# --------------------------------------------------------------------------- #
# The audio heartbeat never fired. Input does: the keybinds module accepts None
# as "any key", riding the InputKey detour keybinds already depend on.
print("\n== the input heartbeat ==")
reset_mod()
env.RAW_KEYBINDS.clear()
jm.stop_input_heartbeat()

# The shipped build rejects key=None: the heartbeat falls back to a key list.
check("heartbeat starts on enable", jm.start_input_heartbeat() is True)
registered = list(env.RAW_KEYBINDS.values())
check(
    "without 'any key' it registers every heartbeat key, on press",
    len(registered) == len(jm.HEARTBEAT_KEYS)
    and all(e.name == "IE_Pressed" for _, e, _ in registered)
    and {k for k, _, _ in registered} == set(jm.HEARTBEAT_KEYS),
    str(len(registered)),
)
check(
    "covers every letter, whatever the keyboard layout",
    all(chr(c) in jm.HEARTBEAT_KEYS for c in range(ord("A"), ord("Z") + 1)),
)
check("and firing and gamepad", {"LeftMouseButton", "Gamepad_RightTrigger"} <= set(jm.HEARTBEAT_KEYS))
check(
    "starting twice does not register twice",
    jm.start_input_heartbeat() and len(env.RAW_KEYBINDS) == len(jm.HEARTBEAT_KEYS),
)
jm.stop_input_heartbeat()
check("stopping removes every key", len(env.RAW_KEYBINDS) == 0, str(len(env.RAW_KEYBINDS)))

# A build that does accept "any key" gets exactly one registration.
env.RAW_KEY_ACCEPTS_NONE["value"] = True
jm.start_input_heartbeat()
registered = list(env.RAW_KEYBINDS.values())
check(
    "with 'any key' it registers once",
    len(registered) == 1 and registered[0][0] is None,
    str([k for k, _, _ in registered]),
)
jm.stop_input_heartbeat()
env.RAW_KEY_ACCEPTS_NONE["value"] = False
jm.start_input_heartbeat()

class _HeartbeatClock:
    def __init__(self):
        self.now = 5000.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


clock2 = _HeartbeatClock()
real_time2 = jm.time
jm.time = clock2
jm._last_scan = 0.0
jm._fast_until = 0.0
jm.auto_scan.value = True
jm.scan_interval.value = 3

sweeps = {"n": 0}
real_scan_all2 = jm.scan_all


def counting_scan2():
    sweeps["n"] += 1
    return 0


jm.scan_all = counting_scan2

results = env.press_any_key()
check("a key press sweeps", sweeps["n"] == 1, str(sweeps["n"]))
check("and never blocks the key", results == [None], str(results))

for _ in range(20):
    env.press_any_key()
check("held-down typing is throttled", sweeps["n"] == 1, str(sweeps["n"]))
clock2.advance(3.5)
env.press_any_key()
check("the next press after the interval sweeps", sweeps["n"] == 2, str(sweeps["n"]))

# After a pickup the heartbeat briefly runs fast, so the late-arriving weapon
# converts within a keypress or two rather than waiting out the interval.
jm._scan_and_follow_up("used an object")
after_pickup = sweeps["n"]
clock2.advance(0.6)
env.press_any_key()
check("inside the fast window a press sweeps sooner", sweeps["n"] == after_pickup + 1, str(sweeps["n"]))
clock2.advance(jm.FAST_WINDOW_SECONDS + 1)
env.press_any_key()
before = sweeps["n"]
clock2.advance(0.6)
env.press_any_key()
check("after the window it slows back down", sweeps["n"] == before, str(sweeps["n"]))

jm.scan_all = real_scan_all2
jm.time = real_time2
jm._last_scan = 0.0
jm._fast_until = 0.0

jm.stop_input_heartbeat()
check("heartbeat stops on disable", len(env.RAW_KEYBINDS) == 0, str(len(env.RAW_KEYBINDS)))

jm.on_mod_enabled()
check("enabling the mod starts it", len(env.RAW_KEYBINDS) == len(jm.HEARTBEAT_KEYS))
jm.on_mod_disabled()
check("disabling the mod stops it", len(env.RAW_KEYBINDS) == 0)

# --------------------------------------------------------------------------- #
# The engine derives Value from BaseValue x modifiers (measured 1.300x exactly).
# With a heartbeat that actually runs, re-writing Value every pass would strip
# every buff every few seconds. The mod owns BaseValue; Value is only kicked.
print("\n== BaseValue is owned, Value is left to the engine ==")
reset_mod()
jm.masher_chance.value = 100
bw, bb = make_struct_weapon(damage=100.0)
jm.make_masher(bb)
check(
    "first touch sets both members",
    abs(bb.Damage.BaseValue - 40.0) < 1e-6 and abs(bb.Damage.Value - 40.0) < 1e-6,
    f"Base={bb.Damage.BaseValue} Value={bb.Damage.Value}",
)

bb.Damage.Value = 52.0  # a +30% damage buff, applied by the engine
for _ in range(5):
    jm.make_masher(bb)
check("a buff on Value survives repeated passes", abs(bb.Damage.Value - 52.0) < 1e-6, str(bb.Damage.Value))
check("and is not reported as drift", jm.check_drift(bb) == [], str(jm.check_drift(bb)))

bb.Damage.BaseValue = 100.0  # the engine re-initialised the weapon
bb.Damage.Value = 100.0
check("a reset base IS drift", any("damage" in d for d in jm.check_drift(bb)))
jm.make_masher(bb)
check(
    "and is re-applied from the anchor",
    abs(bb.Damage.BaseValue - 40.0) < 1e-6 and abs(bb.Damage.Value - 40.0) < 1e-6,
    f"Base={bb.Damage.BaseValue} Value={bb.Damage.Value}",
)

jm.damage_scale.value = 0.5
jm.make_masher(bb)
check(
    "changing the option re-applies both members",
    abs(bb.Damage.BaseValue - 50.0) < 1e-6 and abs(bb.Damage.Value - 50.0) < 1e-6,
    f"Base={bb.Damage.BaseValue} Value={bb.Damage.Value}",
)
jm.damage_scale.value = 0.40

jm.restore_all()
check(
    "restore puts both anchors back",
    abs(bb.Damage.BaseValue - 100.0) < 1e-6 and abs(bb.Damage.Value - 100.0) < 1e-6,
    f"Base={bb.Damage.BaseValue} Value={bb.Damage.Value}",
)

# End to end through the sweep, with the realistic shape.
reset_mod()
jm.masher_chance.value = 100
pc, pawn = make_player()
e2e_weapon, e2e_beh = make_struct_weapon()
e2e_weapon._props["WeaponUser"] = pawn
found = jm.scan_all()
check("sweep converts a struct-valued weapon", e2e_beh.ProjectilesPerShot.Value == 6)
env.set_player(None)

# --------------------------------------------------------------------------- #
print("\n== applying and restoring ==")
reset_mod()
w, b = make_weapon("JAK_PS", (1, 1, 1, 1), damage=100.0, spread=1.0)
# Force it to be a masher regardless of the roll.
jm.masher_chance.value = 100

jm.process_weapon(w)
check("projectiles raised", b.ProjectilesPerShot == 6, str(b.ProjectilesPerShot))
check("damage scaled to 40", abs(b.Damage - 40.0) < 1e-6, str(b.Damage))
check("spread widened to 3", abs(b.Spread - 3.0) < 1e-6, str(b.Spread))

# Idempotent: firing again must not compound.
for _ in range(10):
    jm.process_weapon(w)
check("damage does not compound", abs(b.Damage - 40.0) < 1e-6, str(b.Damage))
check("spread does not compound", abs(b.Spread - 3.0) < 1e-6, str(b.Spread))

# Scaling anchors to the value first seen and always writes anchor * scale.
# Re-basing on whatever is there now looks friendlier, but the engine
# recomputes Damage from BaseValue, so each pass would scale its own output and
# the number would shrink every scan.
b.Damage = 200.0  # engine recalculated, e.g. a skill kicked in
jm.process_weapon(w)
check(
    "an engine recompute does not drag the value along",
    abs(b.Damage - 40.0) < 1e-6,
    str(b.Damage),
)

# Repeated passes after an engine recompute stay put rather than shrinking.
for _ in range(5):
    b.Damage = 200.0
    jm.process_weapon(w)
check("no downward drift across passes", abs(b.Damage - 40.0) < 1e-6, str(b.Damage))

# Changing the option rescales from the anchor, not from the last result.
jm.damage_scale.value = 0.5
jm.process_weapon(w)
check("option change rescales from the anchor", abs(b.Damage - 50.0) < 1e-6, str(b.Damage))
jm.damage_scale.value = 0.40

jm.restore_all()
check("projectiles restored", b.ProjectilesPerShot == 1, str(b.ProjectilesPerShot))
check("damage restored to the anchor", abs(b.Damage - 100.0) < 1e-6, str(b.Damage))
check("spread restored", abs(b.Spread - 1.0) < 1e-6, str(b.Spread))
check("bookkeeping cleared", not jm._touched and not jm._weapon_cache)

# --------------------------------------------------------------------------- #
print("\n== non-mashers and non-jakobs are left alone ==")
reset_mod()
jm.masher_chance.value = 0  # nothing rolls a masher
w, b = make_weapon("JAK_PS", (1, 2, 3, 4))
jm.process_weapon(w)
check("plain revolver untouched", b.ProjectilesPerShot == 1 and b.Damage == 100.0)

jm.masher_chance.value = 100
sg, sgb = make_weapon("JAK_SG", (1, 2, 3, 4))
jm.process_weapon(sg)
check("jakobs shotgun untouched", sgb.ProjectilesPerShot == 1 and sgb.Damage == 100.0)

# --------------------------------------------------------------------------- #
print("\n== caching ==")
reset_mod()
jm.masher_chance.value = 100
w, b = make_weapon("JAK_PS", (1, 2, 3, 4))
jm.process_weapon(w)
calls = {"n": 0}
real_find_all = jm.unrealsdk.find_all


def counting_find_all(*a, **kw):
    calls["n"] += 1
    return real_find_all(*a, **kw)


jm.unrealsdk.find_all = counting_find_all
for _ in range(20):
    jm.process_weapon(w)
check("cached path does not rescan the object list", calls["n"] == 0, f"{calls['n']} scans")
jm.unrealsdk.find_all = real_find_all

# A garbage-collected behaviour forces a rescan rather than crashing.
path, pointers, key, roll, jakobs = jm._weapon_cache[w._get_address()]
pointers[0].kill()
try:
    jm.process_weapon(w)
    check("survives a collected behaviour", True)
except Exception as exc:  # noqa: BLE001
    check("survives a collected behaviour", False, repr(exc))

# --------------------------------------------------------------------------- #
# A gun is judged once, the first time it is in your hands, and the verdict is
# written down: chance changes only affect guns found afterwards, and a restart
# cannot flip a gun you own.
print("\n== a gun is judged once, and remembered ==")
reset_mod()
jm.masher_chance.value = 50
POOL_PARTS = [(i, i * 3 % 11, 7, i % 5) for i in range(40)]


def make_pool():
    made = []
    for parts in POOL_PARTS:
        made.append(make_weapon("JAK_PS", parts))
    for cw, _ in made:
        jm.process_weapon(cw)
    return made


def mashers_in(pool):
    return [cb.ProjectilesPerShot == 6 for _, cb in pool]


pool = make_pool()
at_half = mashers_in(pool)
check("some but not all convert at 50%", 0 < sum(at_half) < len(pool), f"{sum(at_half)}/{len(pool)}")
check(
    "each verdict matches the roll at the time",
    all(m == jm.rolls_masher(cw, [cb]) for (cw, cb), m in zip(pool, at_half)),
)

saved = __import__("json").loads(jm.registry_path().read_text(encoding="utf-8"))
check("every gun is written down", len(saved["guns"]) == len(set(POOL_PARTS)), str(len(saved["guns"])))
entry = saved["guns"]["parts:" + ",".join(map(str, POOL_PARTS[0]))]
check(
    "with its verdict, roll and the chance it was judged at",
    set(entry) == {"masher", "roll", "chance", "decided"} and entry["chance"] == 50,
    str(entry),
)

for chance in (0, 100, 17):
    jm.masher_chance.value = chance
    for cw, _ in pool:
        jm.process_weapon(cw)
    check(
        f"moving the chance to {chance}% changes no gun already judged",
        mashers_in(pool) == at_half,
    )

# A restart: nothing in memory, the same guns come back as new actors.
jm.restore_all()
jm._registry = None
jm.masher_chance.value = 0
pool = make_pool()
check("after a restart every gun keeps its verdict", mashers_in(pool) == at_half)

# New guns are judged at the chance in force when they are found.
jm.masher_chance.value = 100
fresh_w, fresh_b = make_weapon("JAK_PS", (90, 91, 92, 93))
jm.process_weapon(fresh_w)
check("a new gun found at 100% is a Masher", fresh_b.ProjectilesPerShot == 6)
jm.masher_chance.value = 0
plain_w, plain_b = make_weapon("JAK_PS", (80, 81, 82, 83))
jm.process_weapon(plain_w)
check("a new gun found at 0% is not", plain_b.ProjectilesPerShot == 1)
jm.process_weapon(fresh_w)
check("and the earlier one stays a Masher", fresh_b.ProjectilesPerShot == 6)

# Identical parts are the same gun, as a BL3 Masher barrel would be.
twin_w, twin_b = make_weapon("JAK_PS", (90, 91, 92, 93))
jm.process_weapon(twin_w)
check("a gun with identical parts shares the verdict", twin_b.ProjectilesPerShot == 6)

# The per-pass path is a lookup, not a re-judgement.
judged = {"n": 0}
real_is_masher_roll = jm.is_masher_roll
jm.is_masher_roll = lambda roll: judged.__setitem__("n", judged["n"] + 1) or real_is_masher_roll(roll)
for _ in range(10):
    for cw, _ in pool:
        jm.process_weapon(cw)
jm.is_masher_roll = real_is_masher_roll
check("remembered guns are never compared to the chance again", judged["n"] == 0, str(judged["n"]))

# 'masher forget' judges everything again at the current chance.
jm.masher_chance.value = 0
buf_f = __import__("io").StringIO()
with __import__("contextlib").redirect_stdout(buf_f):
    env.sys.modules["mods_base"].REGISTERED["commands"][0]("forget")
check("forget re-judges held guns at the current chance", not any(mashers_in(pool)), str(sum(mashers_in(pool))))
check("and says how many it forgot", "forgot" in buf_f.getvalue(), buf_f.getvalue()[-200:])
saved = __import__("json").loads(jm.registry_path().read_text(encoding="utf-8"))
check(
    "the record now holds the new verdicts",
    saved["guns"] and not any(e["masher"] for e in saved["guns"].values()),
    str(len(saved["guns"])),
)

# A record that cannot be read is never overwritten.
reset_mod()
jm.registry_path().write_text("{not json", encoding="utf-8")
jm.masher_chance.value = 100
bad_w, bad_b = make_weapon("JAK_PS", (5, 5, 5, 5))
buf_b = __import__("io").StringIO()
with __import__("contextlib").redirect_stdout(buf_b):
    jm.process_weapon(bad_w)
check("an unreadable record still lets guns convert", bad_b.ProjectilesPerShot == 6)
check("it is reported", "could not read" in buf_b.getvalue(), buf_b.getvalue()[-200:])
check(
    "and left untouched on disk",
    jm.registry_path().read_text(encoding="utf-8") == "{not json",
)

# Fingerprint identities are judged for the session but never written: buffs
# can move the stats they come from, so the record could not find them again.
reset_mod()
jm.masher_chance.value = 100
fp_w = env.FakeObject("OakWeapon", path="World.Fp_JAK_PS", BodyData="Body_JAK_PS")
fp_w._props["GetPartValue"] = env.BoundFunction(lambda s: (_ for _ in ()).throw(RuntimeError()))
fp_b = env.FakeObject(
    "WeaponBehavior_FireProjectile",
    path="World.Fp_JAK_PS.Fire",
    outer=fp_w,
    ProjectilesPerShot=1,
    Damage=100.0,
    Spread=1.0,
    FireRate=2.5,
)
jm.process_weapon(fp_w)
check("a fingerprinted gun still converts", fp_b.ProjectilesPerShot == 6)
check(
    "but is not written down",
    not jm.registry_path().exists()
    or not __import__("json").loads(jm.registry_path().read_text(encoding="utf-8"))["guns"],
)
check("it is remembered for the session", jm.cached_masher(fp_w) is True)

# Turning 'Player Weapons Only' on reverts revolvers that are not yours.
reset_mod()
jm.masher_chance.value = 100
jm.player_weapons_only.value = False
pc, pawn = make_player()
foe_w, foe_b = make_struct_weapon()
foe_w._props["WeaponUser"] = env.FakeObject("OakCharacter", path="World.Foe")
jm.process_weapon(foe_w)
check("with the option off an enemy revolver converts", foe_b.ProjectilesPerShot.Value == 6)
jm.player_weapons_only.value = True
jm.process_weapon(foe_w)
check("turning it on reverts it", foe_b.ProjectilesPerShot.Value == 1, str(foe_b.ProjectilesPerShot.Value))
env.set_player(None)

# A setting change is noticed promptly. mods_base calls back *before* storing
# the new value, so the callback must not sweep - it opens the fast window.
jm._fast_until = 0.0
swept = {"n": 0}
real_scan_all3 = jm.scan_all
jm.scan_all = lambda: swept.__setitem__("n", swept["n"] + 1) or 0
jm._on_setting_change(jm.masher_chance, 17)
check("a setting change does not sweep with the stale value", swept["n"] == 0)
check("it opens the fast window instead", jm._fast_until > jm.time.monotonic())
jm.scan_all = real_scan_all3
jm._fast_until = 0.0
check(
    "the knob options carry the callback",
    all(
        o.on_change_while_enabled is jm._on_setting_change
        for o in (jm.projectiles, jm.damage_scale, jm.spread_scale, jm.player_weapons_only)
    ),
)
check(
    "the chance does not - it only affects guns found afterwards",
    jm.masher_chance.on_change_while_enabled is None,
)

# --------------------------------------------------------------------------- #
print("\n== missing properties degrade gracefully ==")
reset_mod()
jm.masher_chance.value = 100
weapon = env.FakeObject(
    "OakWeapon",
    path="World.Odd_JAK_PS",
    BodyData="Body_JAK_PS",
)
weapon._props["GetPartValue"] = env.BoundFunction(lambda s: (_ for _ in ()).throw(RuntimeError()))
beh = env.FakeObject(
    "WeaponBehavior_FireProjectile",
    path="World.Odd_JAK_PS.Fire",
    outer=weapon,
    ProjectilesPerShot=1,
    FireRate=3.0,
    # no Damage, no Spread
)
try:
    jm.process_weapon(weapon)
    check("no Damage/Spread property does not crash", True)
    check("projectiles still applied", beh.ProjectilesPerShot == 6, str(beh.ProjectilesPerShot))
except Exception as exc:  # noqa: BLE001
    check("no Damage/Spread property does not crash", False, repr(exc))

# --------------------------------------------------------------------------- #
print("\n== hook-independent scan ==")
reset_mod()
jm.masher_chance.value = 100
w1, b1 = make_weapon("JAK_PS", (1, 2, 3, 4))
w2, b2 = make_weapon("JAK_PS", (5, 6, 7, 8))
sg, sgb = make_weapon("JAK_SG", (1, 2, 3, 4))

check("owning_weapon walks up to the actor", jm.owning_weapon(b1) is w1)

found = jm.scan_all()
check("scan visits every weapon", found == 3, f"{found} weapons")
check("both revolvers converted", b1.ProjectilesPerShot == 6 and b2.ProjectilesPerShot == 6)
check("shotgun still untouched", sgb.ProjectilesPerShot == 1)

# scan_all groups behaviours once, so process_weapon must not rescan per weapon.
calls = {"n": 0}
real_find_all = jm.unrealsdk.find_all


def counting(*a, **kw):
    calls["n"] += 1
    return real_find_all(*a, **kw)


reset_mod()
jm.unrealsdk.find_all = counting
jm.scan_all()
jm.unrealsdk.find_all = real_find_all
check("scan_all enumerates the object list once", calls["n"] == 1, f"{calls['n']} scans")

# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# The heartbeat trigger fires constantly (every sound in the game), so the
# throttle is what keeps it from becoming a per-frame object scan.
print("\n== automatic scanning is throttled ==")


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


clock = FakeClock()
real_time = jm.time
jm.time = clock

reset_mod()
jm.masher_chance.value = 100
jm._last_scan = 0.0
jm.auto_scan.value = True
jm.scan_interval.value = 3

scans = {"n": 0}
real_scan_all = jm.scan_all


def counting_scan():
    scans["n"] += 1
    return real_scan_all()


jm.scan_all = counting_scan

jm.maybe_scan("first")
check("the first heartbeat scans", scans["n"] == 1, str(scans["n"]))

jm.maybe_scan("too soon")
jm.maybe_scan("still too soon")
check("further heartbeats inside the interval do not", scans["n"] == 1, str(scans["n"]))

clock.advance(3.5)
jm.maybe_scan("after the interval")
check("a heartbeat after the interval scans", scans["n"] == 2, str(scans["n"]))

# Equipping should not wait for the heartbeat.
clock.advance(0.5)
jm.maybe_scan("equipped", immediate=True)
check("an immediate trigger scans straight away", scans["n"] == 3, str(scans["n"]))

# ... but a burst of equip events still collapses to one sweep.
jm.maybe_scan("equipped", immediate=True)
jm.maybe_scan("equipped", immediate=True)
check("a burst of immediate triggers collapses", scans["n"] == 3, str(scans["n"]))

# The keybind and console must always work, throttle or not.
jm.maybe_scan("keybind", force=True)
check("force bypasses the throttle", scans["n"] == 4, str(scans["n"]))

# And the option must actually disable it.
jm.auto_scan.value = False
clock.advance(100)
jm.maybe_scan("heartbeat while disabled")
check("automatic scanning can be turned off", scans["n"] == 4, str(scans["n"]))
jm.maybe_scan("keybind while disabled", force=True)
check("the keybind still works when it is off", scans["n"] == 5, str(scans["n"]))

# A sweep that raises must not escape into the engine.
jm.auto_scan.value = True
clock.advance(100)


def exploding_scan():
    raise RuntimeError("boom")


jm.scan_all = exploding_scan
try:
    jm.maybe_scan("broken")
    check("a failing sweep is contained", True)
except Exception as exc:  # noqa: BLE001
    check("a failing sweep is contained", False, repr(exc))

jm.scan_all = real_scan_all
jm.time = real_time
jm._last_scan = 0.0

# --------------------------------------------------------------------------- #
print("\n== hooks, keybind and commands registered ==")
hooks = env.sys.modules["mods_base"].REGISTERED["hooks"]
targets = [h.hook_funcs[0][0] for h in hooks]
check(
    "every trigger registered",
    targets
    == [
        # Weapon-side: never observed firing in solo play, kept as evidence.
        "/Script/GbxWeapon.Weapon:ServerStartUsing",
        "/Script/GbxWeapon.Weapon:ServerEquipInterruptible",
        "/Script/GbxWeapon.Weapon:ServerStartReloading",
        "/Script/GbxWeapon.Weapon:PlayEffects",
        "/Script/OakGame.OakCharacter:ClientSetActiveWeaponEquipSlot",
        # Reachable ones, which is what automatic scanning actually rides on.
        "/Script/OakGame.OakUIDataCollector_Weapon:OnWeaponEquipped",
        "/Script/OakGame.OakPlayerController:ServerUseObject",
        "/Script/OakGame.OakPlayerController:ServerUseJunkObject",
        "/Script/OakGame.OakPlayerController:OnEquipSlotsReadyForInventory",
        "/Script/GbxAudio.GbxAudioBlueprintFunctionLibrary:PostEventInWorld",
    ],
    str(targets),
)
check("HOOKS tuple matches what was registered", list(jm.HOOKS) == hooks)

# Firing a hook must count itself and announce the first call.
reset_mod()
jm._fires.clear()
jm.masher_chance.value = 100
w, b = make_weapon("JAK_PS", (1, 2, 3, 4))
jm.on_start_using(w, None, None, None)
jm.on_start_using(w, None, None, None)
check("hook fires are counted", jm._fires.get("ServerStartUsing") == 2, str(jm._fires))
check("the hook did its job", b.ProjectilesPerShot == 6)

# A hook that raises must not propagate into the engine.
jm._fires.clear()
broken = env.FakeObject("OakWeapon", path="World.Broken")  # no properties at all
try:
    jm.on_start_using(broken, None, None, None)
    check("a failing hook is swallowed", True)
except Exception as exc:  # noqa: BLE001
    check("a failing hook is swallowed", False, repr(exc))

kbs = env.sys.modules["mods_base"].REGISTERED["keybinds"]
check("scan keybind registered", len(kbs) == 1 and kbs[0].name == "Scan Weapons Now")

cmds = env.sys.modules["mods_base"].REGISTERED["commands"]
check("masher command registered", len(cmds) == 1 and cmds[0].cmd == "masher")
for action in ("dump", "status", "scan", "mine", "probe", "restore", "forget"):
    try:
        cmds[0](action)
        check(f"'masher {action}' runs", True)
    except Exception as exc:  # noqa: BLE001
        check(f"'masher {action}' runs", False, repr(exc))

# The item card is built from the item's stats container, not the live
# behaviour, so it keeps showing unmodified numbers. `masher mine` has to
# answer "what is my damage now" instead.
print("\n== masher mine reports the damage maths ==")
reset_mod()
jm.masher_chance.value = 100
pc, pawn = make_player()
rep_w = env.FakeObject(
    "OakWeapon", path="World.Report", BodyData="Body_JAK_PS"
)
rep_w._props["WeaponUser"] = pawn
rep_b = env.FakeObject(
    "WeaponBehavior_FireProjectile",
    path="World.Report.Fire",
    outer=rep_w,
    ProjectilesPerShot=env.WrappedStruct(Value=1, BaseValue=1),
    Damage=env.WrappedStruct(Value=150.0, BaseValue=150.0),
    Spread=env.WrappedStruct(Value=1.0, BaseValue=1.0),
)
jm.scan_all()

import io
import contextlib

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    cmds[0]("mine")
out = buf.getvalue()

check("mine reports per-projectile x count", "60 x 6" in out, out.strip()[-200:])
check("mine reports the total per trigger pull", "360 per trigger pull" in out, "")
check("mine reports the multiplier vs unmodified", "2.40x" in out, "")
env.set_player(None)

# --------------------------------------------------------------------------- #
mod_kwargs = env.sys.modules["mods_base"].REGISTERED["mod"]
check("disable stops the heartbeat and restores", mod_kwargs.get("on_disable") is jm.on_mod_disabled)
check("report wired to enable", mod_kwargs.get("on_enable") is jm.on_mod_enabled)

try:
    jm.on_mod_enabled()
    check("enable report runs", True)
except Exception as exc:  # noqa: BLE001
    check("enable report runs", False, repr(exc))

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILURE(S): {FAILS}"))
sys.exit(1 if FAILS else 0)
