# BL4 Masher Pistols

Adds BL3-style **Masher** revolvers: a fraction of Jakobs pistols fire multiple
projectiles per shot, each for a fraction of card damage.

> This is an **SDK Python mod**, not a `.pak` mod. The parent `../CLAUDE.md`'s
> packaging rules (repak, stub `.utoc`, priority numbers, uncompressed `.ncs`)
> do **not** apply here. Its `.ncs` *reading* notes do — that is how the design
> was worked out.

Unlike `bl4-xp-mod`, a game patch does not silently corrupt this mod. It either
loads or it does not: the SDK itself breaks on patches (sigscan), and the hook
paths are reflection names, which fail loudly.

## Where weapon data actually lives

Not where you would look first.

| What | Where |
|---|---|
| Weapon types, parts, firing stats | `Nexus-Data-inv4.ncs`, in **pakchunk4** (1.48 MB decompressed) |
| Smaller sibling set | `Nexus-Data-inv6.ncs`, pakchunk6 |
| Generic item framework, `Weapon_PS`/`Weapon_SG` base types | `Nexus-Data-inv0.ncs`, pakchunk0 |

`scripts/extract_ncs.py` from the parent repo **silently returns 0 files** for
pakchunk4/6 — their index layout differs. Use repak instead:

```bash
repak unpack -i Engine/Content/_NCS -o out/ pakchunk4-Windows_0_P.pak
python ../scripts/ncs_decomp.py out/Engine/Content/_NCS dec/
bl4 ncs show out/Engine/Content/_NCS/Nexus-Data-inv4.ncs > inv4.txt
```

Each weapon type is one record:

```
jak_ps = {
  basetype: inv'Weapon_PS'
  inv: JAK_PS
  manufacturer: manufacturer'Jakobs'
  namingstrategydef: inv_name_strategy'NameStrat_JAK'
  serialindex: { index: 3 }
  parttypes: [body, body_acc, barrel, barrel_acc, ...]
}
    part_barrel_01 (dep: barrel)
    part_barrel_01_a .. _d (dep: barrel_acc)
    comp_01_common .. comp_05_legendary_seventh_sense (dep: inv_comp)
```

`bl4 ncs show` prints child part **names but not their contents**.

## The finding

The fire aspect carries a `projectilespershot` field, as a `constant` or a
`datatablevalue`:

```
behavior: {
  projectilespershot: { constant: 0.0
                        datatablevalue: { datatable: 'Weapon_SG_Underbarrel_Init'
                                          columnname: ProjectilesperShot_Value_81_E681C3A5...
                                          rowname: DAD_Microrocket } }
  spread: { constant: 4.0 }
  damage: { attribute: attribute'...' }
  firerate, recoil, crosshair, lightprojectile
}
parent: inv_aspect'dad_sg_fire_projectile'
```

`bor_sg`'s barrel 01 is a flat `4.000000`. **No `JAK_PS` barrel sets it at
all** — that absence is precisely why BL4 has no Masher.

### Why a real part is a dead end

1. Adding one needs **new NCS records**. Existing values can be repointed
   (parent `CLAUDE.md`); new keys cannot be added without a full re-encoder.
2. Part behaviour is **native C++** in the game binary. The NCS is only the
   registry naming parts and wiring them together — an injected record would
   have nothing behind it.

Hence the runtime approach.

## Two offline reflection tricks

Both far faster than probing in game, and they turn guesses into certainties.
Everything the mod hooks was found this way.

**1. Fully-qualified names for every UClass and UFunction.**

```bash
retoc print-script-objects global.utoc > script_objects.txt   # 61,425 entries
```

Each entry is `Name: / global_index / outer_index`. Resolve `outer_index`
recursively to rebuild full paths. That is how
`/Script/GbxWeapon.Weapon:ServerStartUsing`, `ServerEquipInterruptible`,
`ServerStartReloading` and `OakWeapon.GetPartValue` were confirmed rather than
guessed. Script objects only — no properties.

**2. Property names: grep the 808 MB `OakGame/Binaries/Win64/Borderlands4.exe`.**

UE's generated registration leaves each class's property names as adjacent
NUL-separated strings, so searching `\x00PropName\x00` and printing a ±1–3 KB
window gives the whole class's property list. Confirmed `ProjectilesPerShot`,
`Spread`, `Damage`, `Crosshair`, `FiringPattern`, and the Weapon class's
`CurrentUseModeIndex`, `NotifyEquipped`, …

**Also useful:** `Nexus-Data-attribute0.ncs` is ASCII and greppable, and maps
attribute → class → property directly:

| Attribute | Resolves to |
|---|---|
| `weapon_projectile_per_shot` | `/Script/OakGame.WeaponBehavior_FireProjectile` → `ProjectilesPerShot` |
| `weapon_part_barrel_value`, `_grip_`, `_scope_`, … | `/Script/OakGame.WeaponPartValueResolver` |

**Gotcha:** `retoc manifest <utoc>` writes `pakstore.json` **into the CWD**, never
to a named output, and only works on `pakchunk0-Windows_0_P` (patch chunks fail
with "FPackageId … has no path name entry"). Run it from a scratch directory —
the game root's own 5.4 MB `pakstore.json` is a maps/DLC index and must not be
clobbered.

## How the mod works

Hooks `Weapon:ServerStartUsing` (plus equip and reload), finds the
`WeaponBehavior_FireProjectile` under the weapon's `Outer` chain, and sets
`ProjectilesPerShot`, scaling `Damage` and `Spread`.

`WeaponBehavior` instances are **per weapon**, not shared per weapon type —
they carry replicated state (`OnRep_ChargeState`, `ServerSyncedLoadedAmmo`,
`StoredAmmo`), so writing to one affects that gun only.

Two things are discovered at runtime rather than hardcoded, because they could
not be confirmed offline. Both log what they picked, and `masher dump` shows
everything if they fail:

- **Which property identifies a gun's type.** Scans the weapon's properties for
  a `MANUFACTURER_CLASS` tag (`JAK_PS`, `BOR_SG`, …) once, then caches the names.
- **Whether `OakWeapon.GetPartValue` is callable.** It supplies the part indices
  the Masher roll is keyed on. Falls back to a fingerprint of rolled stats.

### Re-basing, not pinning

`Damage` and `Spread` are scaled through `_scale_field`, which stores both the
original and what it last wrote. If the current value still matches what it
wrote, it rescales from the stored original; if the game changed it underneath
(a skill, a level-up, an anointment), it treats the new value as the baseline.

That is what keeps repeated shots from compounding the multiplier while still
letting buffs through. It is the one piece of logic worth not breaking — tests
cover all three paths.

## Diagnosing a silent mod

Hit once already: **a freshly installed mod starts disabled**, and
`Mod.enable()` is what binds hooks, keybinds *and* commands. So a disabled mod
is perfectly silent — no logs, and `masher` is not even a command. Check
`sdk_mods/settings/jakobs_masher.json` for `"enabled": true`, or grep the log
for `(Disabled)`:

```bash
grep -i masher OakGame/Binaries/Win64/Plugins/unrealsdk.log
```

Log timestamps are **UTC**; local here is UTC+2, so an entry that looks two
hours stale is current.

Everything the mod does is now self-reporting, because guessing cost a session:

- `on_enable` prints the active settings and which hooks bound
- every hook counts itself and announces its first call
- `masher status` prints the bound/fired table
- `masher scan` and the **Scan Weapons Now** keybind sweep every loaded weapon
  without any hook, via `scan_all()` → `owning_weapon()`

`bound=True fired=0` after real gameplay means BL4 resolves that path natively
rather than through the script VM, and unrealsdk's ProcessEvent hook never sees
it. That is why there are five triggers and a manual sweep rather than one hook.

## What is not verified

The logic has 26 passing tests against a fake engine. The **live object graph
has not been confirmed in game**. Specifically:

- whether a weapon exposes a property naming its type (the `BodyData` the tests
  assume is a plausible stand-in, not an observed value)
- whether `Damage` stays where it is written between shots, or is recomputed
  fast enough that six full-damage projectiles come out

Both fail visibly rather than silently, and `masher dump` was written to answer
them in one run.
