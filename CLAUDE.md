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

Finds the `WeaponBehavior_FireProjectile` belonging to a weapon and sets
`ProjectilesPerShot`, scaling `Damage` and `Spread`.

`WeaponBehavior` instances are **per weapon**, not shared per weapon type —
they carry replicated state (`OnRep_ChargeState`, `ServerSyncedLoadedAmmo`,
`StoredAmmo`), so writing to one affects that gun only.

### Which triggers actually fire — measured

Of the five hooks, **only `Weapon:PlayEffects` has been observed firing**
(12 times in one session). All three `Server*` RPCs and
`OakCharacter:ClientSetActiveWeaponEquipSlot` bound successfully and fired
**zero** times:

```
ServerStartUsing                   bound=True  fired=0
ServerEquipInterruptible           bound=True  fired=0
ServerStartReloading               bound=True  fired=0
PlayEffects                        bound=True  fired=12
ClientSetActiveWeaponEquipSlot     bound=True  fired=0
```

`bound=True fired=0` means BL4 resolves that path in native C++ rather than
through the script VM, so unrealsdk's ProcessEvent hook never sees it. Do not
assume a reflection-resolved function name implies a reachable hook — the name
being real is necessary, not sufficient.

This is why `scan_all()` exists: a hook-independent sweep, reachable from the
**Scan Weapons Now** keybind and `masher scan`, that walks every live fire
behaviour up to its weapon via `owning_weapon()`.

### Identity is not a property — it is a graph walk

The first run reported `could not find any property naming a weapon's type`
and `identity properties: []`. Scanning the weapon's own properties for a
`JAK_PS`-style tag finds **nothing**, because weapon definitions are NCS data,
not UObjects. The weapon actor holds no type name; what it holds are
references to *assets* named after the weapon, one or more hops away —
`Body_JAK_PS`, `TriggerFB_JAK_PS`, `/Game/Gear/Weapons/Pistols/JAK/...`.

`collect_identity()` therefore walks the weapon's object graph breadth-first,
bounded at `IDENTITY_MAX_DEPTH` (3) and `IDENTITY_MAX_NODES` (250), following
object *and struct* fields (both answer `_get_address`; only objects answer
`_path_name`) plus array elements, and skipping `IDENTITY_SKIP_FIELDS` — the
upward links (`Outer`, `Owner`, `World`, `Pawn`, …) that would otherwise escape
into the whole level.

`is_jakobs_pistol()` then prefers the **explicit tag** whenever the graph
carries any `MANUFACTURER_CLASS` tag at all, and only falls back to the
`weapons/pistols/jak` directory when there is no tag to judge by. A weapon can
legitimately reference another weapon's assets — shared or licensed parts — so
OR-ing the two signals misidentifies Jakobs shotguns. Tests cover that case.

**Bug this replaced:** identity discovery used to cache the winning property
names globally on first use. The live scan hits `OakVehicleWeapon` turrets
first, which legitimately have no tag, so the negative result was cached
permanently and every later weapon failed. Discovery must never cache a
negative derived from one sample.

### Enemies hold weapons too

`scan_all()` sees every weapon actor in the level. Converting them all makes
enemy Jakobs revolvers 2.4× damage, so `player_weapons_only` (default on)
filters by ownership, trying `WeaponUser`, `Owner`, `Instigator`, `BodyOwner`
and following a few `Owner` hops to the pawn. `is_player_weapon()` returns
`None` — not `False` — when no owner link exists at all, so an unknown is not
silently treated as an enemy; the caller converts anyway and warns once.

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

Everything the mod does is self-reporting, because guessing has cost two
sessions now:

- `on_enable` prints the active settings and which hooks bound
- every hook counts itself and announces its first call
- `masher status` prints the bound/fired table, the ownership link, and
  how many weapons have been identified
- `masher dump` prints, per live weapon: its class and path, how many strings
  its graph yielded, every `MANUFACTURER_CLASS` tag found, every
  `/Game/Gear/` asset path, its owner links, and — when neither turns up —
  a raw sample of what the graph *did* contain

## Settled, and still open

66 passing tests against a fake engine, plus two in-game sessions.

**Settled in game:**

| Question | Answer |
|---|---|
| Does the mod load and bind? | Yes — all five hooks bind |
| Which trigger fires? | `PlayEffects` only; the `Server*` RPCs never do |
| Does `scan_all()` reach weapons? | Yes — 6–10 `OakWeapon` actors per sweep |
| Does a weapon carry its type as a property? | **No** — hence the graph walk |
| Does `owning_weapon()` work? | Yes — behaviours resolve to their actor |

**Still open:**

- **Whether the graph walk finds the tag.** It is the fix for the one confirmed
  blocker, but the depth, node cap and skip-list are reasoned, not measured.
  `masher dump` prints the graph's tags, gear paths, and a raw sample when
  neither turns up — that names the fix in one run.
- **Which property links a weapon to its holder.** Four candidates are tried;
  the winner is logged and shown in `masher status`. Until one matches,
  ownership is undecidable and everything converts, with a warning.
- **Whether `Damage` stays where it is written** between shots, or is recomputed
  fast enough that six full-damage projectiles come out. Visible immediately if
  wrong.
- `PlayEffects` has only been observed firing for `OakVehicleWeapon` turrets, so
  it is not yet confirmed to fire for player guns. The keybind covers this.
