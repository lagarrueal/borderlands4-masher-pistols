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

### Why not a `.pak` mod, like `bl4-xp-mod`?

The right question, and it took three attempts to answer correctly. NCS
**writing exists** — `scripts/ncs_multipatch.py` appends strings to the
`value_strings` pool and rewrites a cell's bit-packed index, which is exactly
how the XP mod works. Saying this "needs an NCS re-encoder first" was wrong.

The actual limit is narrower: repointing rewrites the value of a cell that
**already exists**. It cannot add a key to a record, because that changes
`key_strings`, the type-code matrix, and the bit offset of every later cell.

So the question becomes: does a Jakobs pistol have a `projectilespershot` cell
anywhere in its resolution chain? Checked at all three levels, and the answer
is no at each:

| Level | Finding |
|---|---|
| **Part** | `jak_ps` has only 3 parts with a fire aspect — `part_barrel_01_phantom_flame`, `part_barrel_02_kingsgambit`, `part_barrel_quickdraw`. The common barrels (`part_barrel_01`, `part_barrel_02`) have **no fire aspect at all**; they inherit wholesale. |
| **Aspect** | `jak_ps_fire_projectile` sets exactly one key: `recoil`. Its parent `ps_fire_projectile` has 15 keys, none of them `projectilespershot`. **No base fire aspect in the game has it** — it is always set per part. |
| **Data table** | `part_barrel_quickdraw` *does* reference `Unique_PS_Barrel_Init` → `ProjectilesPerShot_Value`, but the `JAK_Quickdraw` row has no such cell (rows store only the fields they set, the way `TOR_Linebacker` in `unique_sg_barrel_init` carries `projectilespershot_value: 4.000000`). Nothing to repoint. |

Counting properly — `175` part records set `projectilespershot`, `30` of them
JAK, `6` on `jak_ps`. But those six are five underbarrels (a separate fire mode,
not the revolver's own shot) plus QuickDraw, whose cell is absent from the row
it points at.

> An earlier pass here reported "63 records, 0 JAK". That was wrong: the script
> labelled each hit by its JSON path, which never contained the part name, so
> the JAK filter matched nothing. The conclusion happened to survive; the
> evidence for it did not.

So the SDK route is not a shortcut, it is the only route — but the trade is
real: a pak mod would persist without Python and work on a vanilla install,
where this one needs the SDK and is host-only.

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

This is why `scan_all()` exists: a hook-independent sweep that walks every live
fire behaviour up to its weapon via `owning_weapon()`.

**Automatic application therefore rides on events that are reachable.** The
first attempt picked triggers by copying what other mods hook; measured in the
next session, two of those never fired either:

| Trigger | Measured | Role |
|---|---|---|
| `OakPlayerController:ServerUseObject` | **fires** | pickup / interact — sweep now, open fast window |
| `OakPlayerController:ServerUseJunkObject` | **fires** | same (trashSeller hooks it) |
| `OakUIDataCollector_Weapon:OnWeaponEquipped` | **fires** | inventory swap — sweep now, open fast window |
| `OakPlayerController:OnEquipSlotsReadyForInventory` | never fired | kept, costs nothing |
| `GbxAudio...:PostEventInWorld` | **never fired** | the intended heartbeat; kept, costs nothing |
| any key press (raw keybind) | fires by construction | **the real heartbeat** |

"music_watch hooks this library" proved only that the hooks *bind* — its log
says `hook OK`, which is registration, not a call. Bound is not fired.

**The heartbeat is input.** The SDK detours `UGbxEnhancedPlayerInput::InputKey`
(visible in `unrealsdk.log`), which is how keybinds work at all.
`keybinds.pyi` documents `None` as the key meaning *any* key, **but the
compiled `keybinds.pyd` shipped here rejects it**. Measured:
`register_keybind(): incompatible function arguments ... (key: str, event:
SupportsInt | None, callback)`, so the heartbeat never started and loaded guns
waited 25s for a pickup event to convert. `start_input_heartbeat()` now tries
`None` first and falls back to one registration per `HEARTBEAT_KEYS` entry: all
26 letters (AZERTY vs QWERTY), the number row, mouse buttons and scroll,
modifiers, and the gamepad buttons. Registered on `IE_Pressed` only — axis events (mouse look) would be far too
frequent — it fires on moving, firing, swapping, and pressing E to pick up:
exactly while you are playing. `mods_base`'s `@keybind` cannot do this; its
`enable_keybind` returns early when `key is None`, so the module is called
directly and deregistered in `on_mod_disabled`. The callback returns `None`
so the key is never blocked.

`maybe_scan()` holds the throttle: `force` for the keybind and console (always
works, even with automatic scanning off), `immediate` for pickup/equip events
(floored at 0.25s so a burst collapses to one sweep), and the interval for the
heartbeat. Pickup/equip events also open a **fast window** — 6s during which the
heartbeat may sweep every 0.5s — because the weapon actor and its ownership
arrive a moment *after* the event that announced them. A sweep that raises is
contained rather than escaping into the engine.

### Verdicts are judged once and written down

History, because it went through three designs in a day:

1. **1.0** cached `is_masher` per weapon *actor*, rolled `(hash & 3) <
   frequency`. Frequency changes silently did nothing to seen guns, "0
   disables" disabled nothing, and the description wrongly said changes
   "reshuffle" guns (a threshold on a fixed hash is nested).
2. **1.1** cached the roll and re-compared it to `Masher Chance` every pass,
   so changes applied live, reverting guns included. The user did not want
   guns re-checked against the config: a gun should be decided once.
3. **Now:** `decide(key, roll)` judges a gun the first time it is eligible
   (ownership passes) and records `{masher, roll, chance, decided}` in
   `SETTINGS_DIR/jakobs_masher_guns.json`. Every later pass is a dict lookup.
   `Masher Chance` therefore only affects guns found afterwards, and it no
   longer has an `on_change` callback. `masher forget` clears the record,
   reverts, and re-sweeps.

Keys are `parts:<GetPartValue slots 0..15>`. **Measured**, 2026-09-29: two
different JAK_PS read `1,0,2,2,0,0,0,0,5,0,...` and `3,3,1,1,0,...`, each
identical across five `masher mine` calls. Identical parts share a verdict,
which is BL3-faithful (a Masher barrel). Stat-fingerprint keys (the fallback
when `GetPartValue` fails) are prefixed `session:` and never written, because
buffs and the Masher's own spread move the stats they hash. `masher mine` reads
the key from `_weapon_cache` rather than recomputing it, for the same reason.

An unreadable record is never overwritten (`_registry_unreadable`); guns are
judged for the session only until `masher forget`.

**Measured across a full restart** (13:34 vs 13:55, 2026-09-29): the same two
revolvers read the same parts and rolls (`23.43`, `37.89`) in the new session,
and the verdicts were loaded from the record (`remembered verdicts loaded: 3`).
The per-key heartbeat registered 66 keys and converted the equipped Masher on
the first key press after loading.

### One record per save (1.3)

Asked for by the user, 2026-09-29. Saves are
`Documents/My Games/Borderlands 4/Saved/SaveGames/<steamid>/Profiles/client/N.sav`.
Decrypting one with `tools/bl4.exe save N.sav -s <steamid> decrypt` shows
`state.char_guid: 3B67116641774DAB955F2AB41769A961` (32 hex digits) and
`char_name`, so the GUID is the stable per-save identity. Slot numbers and file
times were rejected: nothing at runtime names the slot, and the game writes the
file on quit, so mtime points at the previous character.

The binary contains the property names `ActiveCharGuid` and `CharacterGuid`,
but offline data does not say which class carries them (`GetSaveSlotName` is
stock UE `LocalPlayerSaveGame`, which BL4 does not appear to use).
`_read_save_guid()` therefore tries both fields on `OakActiveProfile`,
`GbxActiveProfile`, the player controller, its `PlayerState` and its
`LocalPlayer`, keeps a weak pointer to whichever answers, and re-reads only
that field on later sweeps, so a character switch is seen without searching
again. Until something answers, a search runs at most every 10s, and verdicts
go to `unknown-character.json`.

**Measured in game** (`masher save`, 2026-09-29): only
`OakPlayerState.ActiveCharGuid` exists, reading
`{A: 996610406, B: 1098337707, C: -1788925260, D: 392800609}`. That formats to
`3B67116641774DAB955F2AB41769A961`, exactly slot 11's `char_guid`.
`OakActiveProfile` (under `OakGameInstance...OakProfileProgressVault`), the
controller and `OakLocalPlayer` have neither field. `C` is negative, so the
unsigned mask matters. The player's own objects are therefore tried before
the profile classes are walked: a new level brings a new PlayerState, and this
way re-finding it costs no object-list walk.

`FGuid` members are read as `A, B, C, D` and formatted `%08X` each (unsigned),
which is how the save writes `char_guid`. An all-zero GUID means no character
is loaded. Files: `settings/jakobs_masher_guns/<GUID>.json`, carrying
`character_guid`. The pre-1.3 shared file is adopted by the first identified
save and renamed `.migrated`.

### Ownership must never be cached as a no

Measured: picking up a Jakobs revolver logged

```
12:04:58  OakWeapon_2147406817 is a Jakobs revolver, but not ours
12:06:38  Masher: OakWeapon_2147402983        <- only after an inventory swap
```

`ServerUseObject` fires before the game hands the weapon over, so the sweep it
triggers sees a revolver owned by nobody. The old code cached that verdict in
`_weapon_cache`, making the gun permanently ineligible; an inventory swap
"fixed" it only because it respawned the weapon as a new actor at a new
address. Ownership is now re-checked every pass. Identity (the graph walk) and
the Masher roll are what get cached — both are fixed properties of the gun, and
ownership is not.

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

### The behaviour reflects almost nothing

Third in-game run: identification worked, two Mashers were found, and the mod
applied **nothing** — `-> 6 projectiles (nothing applied)`.

The binary's property-name table explains it. `ProjectilesPerShot` sits alone
between `WeaponBehavior_Charge`'s members (`ChargeState`, `NumStackCharges`)
and the accuracy behaviour's (`MovementAccuracyMaxValue`, …), which means
`WeaponBehavior_FireProjectile` reflects roughly **one** property. `Damage` and
`Spread` are configured on the *Def* and resolved through the attribute system;
they are not fields on the live behaviour.

So each knob now has a candidate list (`FIELD_CANDIDATES`), resolves to the
first property the object actually has, and a knob with no candidate logs the
behaviour's class and its complete field list rather than failing silently. The
old code could not distinguish "write failed" from "property absent", which is
why one line of log took a whole session to explain.

`_touched` also used to book an entry *before* attempting the write, so
`behaviours modified: 2` was reported while nothing had been modified. It now
records only successful writes.

### `BaseValue` is not the value — `Value` is

Five test sessions died on this. `masher probe` settled it in one:

```
modified Jakobs pistol        untouched Torgue shotgun
  ProjectilesPerShot            ProjectilesPerShot
    BaseValue = 6  <- we wrote    BaseValue = 3
    Value     = 1  <- game reads  Value     = 3   <- equal when untouched
```

The struct exposes `Value` **and** `BaseValue`. On an untouched weapon they
agree, so nothing in the data hints that they differ — you only see it after
writing one of them. `drift: none` was correct and useless: the write held
perfectly, the game simply reads the other member.

`GbxAttributeBase`'s reflected members are `OldValue` and `BaseValue`, which is
why `BaseValue` looked authoritative in the binary. `Value` lives on the
derived type and does not appear in that block. **The binary's property table
is not a reliable guide to which member matters.**

Both members are now written. They are *scaled*, not assigned, wherever a ratio
exists — `Damage` runs `BaseValue 82 / Value 241`, a roughly 2.9x modifier
chain (manufacturer scaling, level, skills), and flattening the two to one
number would destroy it.

### ...and the members are not all the same type

`ProjectilesPerShot` is a **`GbxAttributeInteger`**; `Damage` and `Spread` are
floats. Writing a float to the int member fails outright:

```
could not write ProjectilesPerShot:
  Unable to cast Python instance of type <class 'float'> to C++ type 'int'
```

That single difference is why spread visibly widened in game while the
projectile count did nothing — the two knobs go through identical code. Writes
now coerce to the type the member already holds (`_match_type`).

This was a **self-inflicted regression**: the earlier writer tried the value,
then `int`, then `float`, and the rewrite that added `Value` support replaced
that with a hard `float()`. A retry loop was doing real work and its removal
went unnoticed because the tests only ever used float members.

### Own `BaseValue`; leave `Value` to the engine

The engine derives `Value` from `BaseValue` times whatever modifiers are active.
Measured on a Masher: damage re-read as `69.1113` against the `53.1625` written —
exactly **1.300×**, a buff layered on our base; another read `114.068` against
`117.933`, 0.967×.

Two wrong answers came first. Re-basing on the current value each pass scaled
the engine's output again, so damage shrank every scan. Anchoring and
re-writing *both* members every pass fixed the shrink but stripped every buff —
harmless while no heartbeat fired, and a real regression the moment one did.

So `_write_knob` anchors each member to the value first seen and then:

- **first touch**: writes every member — `BaseValue` for real, `Value` as a kick,
  since the engine does not recompute `Value` just because `BaseValue` changed;
- **later passes**: writes *nothing* while `BaseValue` still holds our value, so
  buffs and debuffs on `Value` survive;
- **`BaseValue` moved** (engine re-init) **or the option changed**: re-applies
  every member from the anchors.

`check_drift` watches `BaseValue` only. `Value` moving is the engine doing its
job; `BaseValue` moving is the only thing that means our change was undone.

The general lesson, now paid for several times over: **a property is not a
number, a number is not the number, and the numbers are not the same type.**
Probe the live object before writing.

### The item card does NOT follow — it reads a different object

Confirmed in game: with `ProjectilesPerShot` working and the gun visibly
firing six projectiles, **the card still shows the unmodified numbers**.

The card is not built from the live `WeaponBehavior_FireProjectile`. It comes
from the item's own stats container — `InventoryStatsContainer`,
`InventoryStatsDef`, `NexusConfigStoreInventoryStats`,
`InventoryStatsContainerValueResolver`. That has to be true structurally: the
backpack draws cards for items you are *not* holding, which have no live weapon
actor at all.

So the `uistat_damage_and_projectile_count` analysis below is correct about
*how the card decides* — but the attribute it reads resolves against the item,
not against the behaviour this mod writes to. Changing the card means reaching
the item's stats container, which is separate work.

`masher mine` reports the real maths instead: per-projectile damage, the count,
the total per trigger pull, and the multiplier against the unmodified anchor.

**Measured with `masher card`** (2026-09-29, a Masher equipped, a Masher on the
ground with its HUD card up, a backpack card shown just before):

- All 10 `WeaponBehavior_FireProjectile` objects sit under `OakWeapon` actors.
  There is no separate card or item copy of the behaviour. A ground pickup
  (`InventoryPickup`) comes with its own `OakWeapon`, which is why ground guns
  already fire as Mashers.
- Every item-side object carries only a handle:
  `Item = {data: {InstanceId: 1011, Identity: {}}, State: {Quantity, Flags}}` on
  the weapon and on the pickup, and `SourceItemHandle: {Handle: 909}` in the
  pawn's `EquippedInventorySlots`. `Identity`, where parts, serial and stats
  would live, has **no reflected members**.
- `OakUIDataCollector_ItemCard`, `_Backpack` and `_ItemIcon` exist (outer: the
  player controller) and expose **no properties**. The UI is Coherent (HTML),
  fed natively, and no UFunction returns card rows or stat values.
- There is no scriptable HUD message API. `PlayerController.ClientMessage` only
  reaches the console.

So the numbers on the card are built in native code from data that reflection
cannot see. Reaching them would mean reverse-engineering native struct layouts
and writing raw memory, which is fragile across patches and risks crashes. The
first probe also capped fields before filtering, which cut the pawn off at
"Inventory..."; that is fixed, but equipped-slot handles make a reflected stats
container on the pawn unlikely.

### How the card decides (for reference)

`Nexus-Data-ui_stat0.ncs` defines two damage rows whose `displaycondition` is a
straight comparison on the attribute `weapon_projectile_per_shot`:

| Row | Condition | `statvalue` |
|---|---|---|
| `uistat_damage` | `LessOrEqual 1.0` | `$VALUE$` of `weapon_damage` |
| `uistat_damage_and_projectile_count` | `GreaterThan 1.0` | `{dmg} x {proj}` |

And **every** weapon base type in `inv0` lists both — `weapon_ar`, `weapon_ps`,
`weapon_sg`, `weapon_sm`, `weapon_sr`:

```
weapon_ps  uistats: [uistat_damage, uistat_damage_and_projectile_count, uistat_typeline_ps]
```

So the pistol card already knows how to render `{dmg} x {proj}`; it simply never
fires because no stock pistol exceeds one projectile. Since
`weapon_projectile_per_shot` resolves *from* the very property this mod writes,
the card flips by itself. **No UI work is needed, and the card is the
verification signal.**

(I had claimed the opposite — that no pistol had a projectile row, so the card
could never show it. Wrong on both counts, and checkable in the data the whole
time.)

### The values are attribute structs, not numbers

Fourth in-game run, with the property names finally resolving:

```
'projectiles' resolved to property 'ProjectilesPerShot'
could not write ProjectilesPerShot = 6: Unable to cast Python instance of
  type <class 'int'> to C++ type 'unrealsdk::unreal::WrappedStruct'
could not read Damage: float() argument must be ... not 'WrappedStruct'
```

All three properties **exist**. They are not scalars. That matches the NCS,
where the fire aspect stores `projectilespershot: {constant: 0.0,
datatablevalue: {...}}` — a value that can come from a constant, an attribute,
or a data-table cell. The binary names the type: `GbxAttributeFloat` and
`GbxAttributeInteger`, both deriving `GbxAttributeBase`, whose scalar member is
**`BaseValue`**.

So reads and writes go through `_read_scalar` / `_write_scalar`, which accept a
plain number or reach into the struct (`BaseValue`, then `Value`, `Constant`,
`BaseValueConstant`). Writes assign the struct back after mutating it, since a
struct read from a property may be a copy. A struct with no numeric field logs
its entire field list rather than failing silently.

**Note the shape of this bug:** three rounds of "the property is missing" were
actually "the property is a different type than assumed". Reflection tells you a
name exists; it does not tell you the type. Read one before writing it.

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
| Does the graph walk find the tag? | **Yes** — Jakobs pistols identified |
| Which property links weapon to holder? | `WeaponUser` |
| Is `GetPartValue` callable? | Yes — 16 slots |
| Are `Damage`/`Spread` on the behaviour? | **Yes** — as `GbxAttribute*` structs |
| What type are the values? | Structs; scalar is `BaseValue` |
| Does the card show `{dmg} x {proj}`? | Yes, keyed on `weapon_projectile_per_shot` |

**Still open:**

- ~~Whether writing `BaseValue` takes effect.~~ **Settled — it does not.** The
  struct carries two numbers and `Value` is the one the game reads. See below.
- ~~Whether the item card reflects any of it.~~ **Settled — it does.** See
  below.
- `PlayEffects` has only been observed firing for `OakVehicleWeapon` turrets, so
  it is still not confirmed to fire for player guns. The keybind covers this.

## Pak route, revisited (2026-09-29, in test)

The SDK route cannot reach the item card (see above), so the data route was
re-examined, and an earlier claim here turned out wrong. **Every common JAK_PS
barrel does carry an inline fire aspect** (`parent:
inv_aspect'jak_ps_fire_projectile'`) with spread, damage, fire rate and
`automaticburstcount: "1"`. Only `projectilespershot` is missing.

The NCS writer cannot add a field, but it can **rename** one. Struct field
names are indices into the payload's `key_strings` pool (inv4: 3785 keys at 12
bits; `automaticburstcount`=582, `projectilespershot`=302). The instrumented
trace logs values, not keys, but a field's key sits in the gap before its
traced value: 9 bits in, 12 wide, then 4 type bits. That was checked on
`automaticburstcount`, `bautoburst` and `firefeedback`.

`pak/build_masher_pak.py` makes barrel 02 (name part `Muki`) the Masher
barrel. It renames that field, repoints its value `1` to `6.000000`, decodes
the result, and requires it to equal the original except for that one field.
Output: `JakobsMasher_9600_P.pak/.ucas/.utoc`. No other installed mod ships
inv4.

Planned next, once the card is confirmed:
- `gbx_ue_data_table4` row `JAK_Barrel_02`: `damage_scale` 3.4 -> 1.36 (x0.4)
  and `spread_value` 1.05 -> 3.15 (x3).
- `inv_name_part4` `np_weap_JAK_PS_B02`: partname
  `"WeaponNamingStrategies, 4A32D085..., Muki"` -> a new string ending
  `Masher`.

A Masher is then whatever rolls barrel 02, so the frequency is fixed by the
game's barrel roll. It applies to guns already owned and is stored in the
serial. The SDK mod was disabled for the test so the two do not stack.

### Test result: the card works, and the pak destroys items (2026-09-29)

In game, a "Panicking Muki" card read **`227 x 6`**, the Cuca was unchanged,
and the gun fired multiple projectiles, with an occasional double shot.
Probably `AutomaticBurstCount` fell to the engine default once the field was
renamed away; no parent aspect defines it.

**But with the pak installed, the character lost items.** Comparing the
decrypted `11.sav` (Loveless, `Char_CorpoHacker`) before and after:
- the **equipped class mod** (`slot_8`, item type 402) was deleted;
- **26 of 40 Lost Loot items** were deleted, of every kind (guns, shields,
  Gravitar and Dark Siren class mods), with no Lost Loot interaction;
- the **backpack was byte-identical**, 65 serials including 12 type-402 class
  mods. Those were only hidden in game and came back once the pak was
  removed.

**Cause: the build read a stale inv4.** A later worry that this was only
part of it came from a flawed test; see "Resolved" below. repak panics on `pakchunk4-Windows_20_P.pak`, so the build took
Windows_18 as newest. `classmod_corpohacker`, the class mods of the newest
class (serial type 402), exists only in W20's inv4. The mod pak shadowed W20,
so those items could not resolve. Even the unchanged repack deleted them.
`build_masher_pak.py` now finds sources by grepping raw pak bytes and carving
them (`../scripts/carve_ncs.py`). Rebuilt from W20, every position moved: key
index 532 -> 244, not 582 -> 302.

Original notes, from before the cause was known: type 402 has no top-level
definition in inv0, inv4 or inv6. It
appears in inv4 only as a nested `_scope: Sub` serialindex inside every class
mod's `passive_points` entries. The decode check proved the file differs by
one field *as bl4.exe reads it*. The game evidently reads it differently, or
rejects the modified file for some content. The XP mod replaces attribute0
and data_table0 the same way with no such effect, so the difference is
specific to inv4 or to the edit.

Backups: `SaveGames/CLAUDE_BACKUP_20260929_before_masher_pak` (pre-install)
and `..._after_masher_pak_test`.

**Do not reinstall until the cause is known.** Next discriminating test: a
null pak (inv4 repacked unchanged), on a throwaway character, with the save
restored afterwards. If it also deletes items, overriding inv4 is unsafe as
such; if not, the key rename is at fault.

### Resolved: the stale source was the cause (2026-09-29)

The "still open" worry was a testing mistake. Throwaway Amon's three class
mods were in **Lost Loot**, never in the backpack, so "no class mods in the
backpack" was his normal state and every Amon test was blind to class mods.
The decrypted saves settle it:
- **W18-based pak on Loveless:** deleted her equipped type-402 class mod and
  26 Lost Loot items. Type 402 exists only in W20.
- **W20-based pak on Amon:** Muki showed `dmg x 6`, and nothing was deleted,
  **including the class mods in Lost Loot**.
- **Hotfix path** (`OakGame/_PATCH/PAK/_NCS/Nexus-Data-inv.ncs` in the mod
  pak): no effect at all, no `x 6`. Online patch files only apply from the
  game's own patch pak. Kept behind `--patch-route`.

**Lesson:** a test result is only meaningful if the thing tested is present
in the test subject. Check the save's sections *before* choosing a test
character.

Full auto ("double shots") came from renaming away `automaticburstcount: 1`,
which is what keeps a Jakobs pistol semi-auto. The build now renames
`bautoburst` (`false`, already the default) instead.

### Working (2026-09-29)

The full pak (`pak/build_masher_pak.py`, three files from the W20 sources)
was verified in game on Loveless:
- Mukis read **"… Masher"**, so a new localization key with unknown GUID falls
  back to the source text;
- the card damage dropped to 0.4× and reads `N x 6`;
- the spread is visibly wider;
- it fires semi-auto;
- all 13 type-402 class mods are present;
- the decrypted save is identical before and after (backpack 65, equipped 9,
  Lost Loot 6).

The pak is now the primary version; the SDK mod is kept and disabled.

