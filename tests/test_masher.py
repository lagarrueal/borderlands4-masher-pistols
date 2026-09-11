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


def make_weapon(tag: str, parts: tuple[int, ...], damage=100.0, spread=1.0):
    """A weapon actor plus the fire behaviour hanging off it."""
    weapon = env.FakeObject(
        "OakWeapon",
        path=f"World.{tag}_{env._NEXT_ADDR[0]}",
        BodyData=f"/Game/Gear/Weapons/Pistols/JAK/Body_{tag}.Body_{tag}",
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
    jm._identity_props = None
    jm._part_values_work = None
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
    "identity property discovered",
    jm._identity_props == ["BodyData"],
    str(jm._identity_props),
)

jak_sg, _ = make_weapon("JAK_SG", (0, 1, 2, 3))
check("JAK_SG rejected", not jm.is_jakobs_pistol(jak_sg))
ted_ps, _ = make_weapon("TED_PS", (0, 1, 2, 3))
check("TED_PS rejected", not jm.is_jakobs_pistol(ted_ps))

# --------------------------------------------------------------------------- #
print("\n== part values are read through GetPartValue ==")
reset_mod()
w, b = make_weapon("JAK_PS", (5, 2, 9))
check("part values read", jm.part_values(w) == (5, 2, 9), str(jm.part_values(w)))
check("GetPartValue marked usable", jm._part_values_work is True)

# --------------------------------------------------------------------------- #
print("\n== the masher roll is stable and proportioned ==")
reset_mod()
jm.masher_frequency.value = 2  # 2 of 4 barrel variants

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

# Distribution: roughly masher_rarity/4 of guns.
hits = 0
total = 2000
for i in range(total):
    parts = (i % 17, (i // 17) % 19, (i // 323) % 7, (i * 7) % 23)
    w, b = make_weapon("JAK_PS", parts)
    if jm.rolls_masher(w, [b]):
        hits += 1
ratio = hits / total
check(
    "about half are mashers at frequency 2",
    0.35 < ratio < 0.65,
    f"{ratio:.1%}",
)

jm.masher_frequency.value = 1
hits = sum(
    jm.rolls_masher(*(lambda p: (lambda wb: (wb[0], [wb[1]]))(make_weapon("JAK_PS", p)))(
        (i % 17, (i // 17) % 19, (i // 323) % 7, (i * 7) % 23)
    ))
    for i in range(total)
)
ratio1 = hits / total
check("about a quarter at frequency 1", 0.13 < ratio1 < 0.37, f"{ratio1:.1%}")
jm.masher_frequency.value = 2

# --------------------------------------------------------------------------- #
print("\n== applying and restoring ==")
reset_mod()
w, b = make_weapon("JAK_PS", (1, 1, 1, 1), damage=100.0, spread=1.0)
# Force it to be a masher regardless of the roll.
jm.masher_frequency.value = 4

jm.process_weapon(w)
check("projectiles raised", b.ProjectilesPerShot == 6, str(b.ProjectilesPerShot))
check("damage scaled to 40", abs(b.Damage - 40.0) < 1e-6, str(b.Damage))
check("spread widened to 3", abs(b.Spread - 3.0) < 1e-6, str(b.Spread))

# Idempotent: firing again must not compound.
for _ in range(10):
    jm.process_weapon(w)
check("damage does not compound", abs(b.Damage - 40.0) < 1e-6, str(b.Damage))
check("spread does not compound", abs(b.Spread - 3.0) < 1e-6, str(b.Spread))

# The game recalculating damage (a buff) must survive.
b.Damage = 200.0  # engine recalculated, e.g. a skill kicked in
jm.process_weapon(w)
check("buffed damage is rescaled, not clobbered", abs(b.Damage - 80.0) < 1e-6, str(b.Damage))

# Changing the option takes effect without compounding.
jm.damage_scale.value = 0.5
jm.process_weapon(w)
check("option change re-bases", abs(b.Damage - 100.0) < 1e-6, str(b.Damage))
jm.damage_scale.value = 0.40

jm.restore_all()
check("projectiles restored", b.ProjectilesPerShot == 1, str(b.ProjectilesPerShot))
check("damage restored to the live base", abs(b.Damage - 200.0) < 1e-6, str(b.Damage))
check("spread restored", abs(b.Spread - 1.0) < 1e-6, str(b.Spread))
check("bookkeeping cleared", not jm._touched and not jm._weapon_cache)

# --------------------------------------------------------------------------- #
print("\n== non-mashers and non-jakobs are left alone ==")
reset_mod()
jm.masher_frequency.value = 0  # nothing rolls a masher
w, b = make_weapon("JAK_PS", (1, 2, 3, 4))
jm.process_weapon(w)
check("plain revolver untouched", b.ProjectilesPerShot == 1 and b.Damage == 100.0)

jm.masher_frequency.value = 4
sg, sgb = make_weapon("JAK_SG", (1, 2, 3, 4))
jm.process_weapon(sg)
check("jakobs shotgun untouched", sgb.ProjectilesPerShot == 1 and sgb.Damage == 100.0)

# --------------------------------------------------------------------------- #
print("\n== caching ==")
reset_mod()
jm.masher_frequency.value = 4
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
path, pointers, is_masher = jm._weapon_cache[w._get_address()]
pointers[0].kill()
try:
    jm.process_weapon(w)
    check("survives a collected behaviour", True)
except Exception as exc:  # noqa: BLE001
    check("survives a collected behaviour", False, repr(exc))

# --------------------------------------------------------------------------- #
print("\n== missing properties degrade gracefully ==")
reset_mod()
jm.masher_frequency.value = 4
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
jm.masher_frequency.value = 4
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
print("\n== hooks, keybind and commands registered ==")
hooks = env.sys.modules["mods_base"].REGISTERED["hooks"]
targets = [h.hook_funcs[0][0] for h in hooks]
check(
    "five triggers registered",
    targets
    == [
        "/Script/GbxWeapon.Weapon:ServerStartUsing",
        "/Script/GbxWeapon.Weapon:ServerEquipInterruptible",
        "/Script/GbxWeapon.Weapon:ServerStartReloading",
        "/Script/GbxWeapon.Weapon:PlayEffects",
        "/Script/OakGame.OakCharacter:ClientSetActiveWeaponEquipSlot",
    ],
    str(targets),
)
check("HOOKS tuple matches what was registered", list(jm.HOOKS) == hooks)

# Firing a hook must count itself and announce the first call.
reset_mod()
jm._fires.clear()
jm.masher_frequency.value = 4
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
for action in ("dump", "status", "scan", "restore"):
    try:
        cmds[0](action)
        check(f"'masher {action}' runs", True)
    except Exception as exc:  # noqa: BLE001
        check(f"'masher {action}' runs", False, repr(exc))

mod_kwargs = env.sys.modules["mods_base"].REGISTERED["mod"]
check("restore wired to disable", mod_kwargs.get("on_disable") is jm.restore_all)
check("report wired to enable", mod_kwargs.get("on_enable") is jm.on_mod_enabled)

try:
    jm.on_mod_enabled()
    check("enable report runs", True)
except Exception as exc:  # noqa: BLE001
    check("enable report runs", False, repr(exc))

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILURE(S): {FAILS}"))
sys.exit(1 if FAILS else 0)
