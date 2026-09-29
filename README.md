# Borderlands 4 — Masher Pistols

Brings BL3's **Masher** revolvers to Borderlands 4.

A Masher is a Jakobs revolver whose barrel fires a burst of projectiles instead
of a single slug, each one landing for a fraction of the card damage. BL3 had
them; BL4 does not. This adds them back.

No new art and no new legendary — it reuses the Jakobs pistols already in the
game, so a Masher looks exactly like the revolver it is.

There are two versions. **Use the pak.**

| | Pak (`pak/`) | SDK mod (`jakobs_masher/`) |
|---|---|---|
| Item card | **`91 x 6`**, name **"… Masher"** | unchanged; use `masher mine` |
| Which guns | every Jakobs pistol with **barrel 02** (formerly "… Muki") | a configurable share, judged per save |
| Decided | by the game, at drop; stored in the serial | by the mod, on first sight |
| Needs | nothing but the pak | the Oak2 SDK |
| After a game patch | **rebuild required** (see below) | keeps working |

## The pak

Barrel 02 of the Jakobs pistol becomes a Masher barrel, like BL3's: 6
projectiles, each at 0.4× the barrel's damage (2.4× per trigger pull), 3×
the spread, and the name part "Muki" becomes "Masher". The card shows it,
because the card is built from exactly the data this changes.

```bash
python pak/build_masher_pak.py --install     # build against the installed game, then install
python pak/build_masher_pak.py --uninstall   # remove it
```

It replaces three game data files in `Paks/JakobsMasher_9600_P.*`:

| File | Change |
|---|---|
| `Nexus-Data-inv4.ncs` | `part_barrel_02`'s fire behaviour: the field `bautoburst` (`false`, already the default) is renamed `projectilespershot` and set to 6 |
| `Nexus-Data-gbx_ue_data_table4.ncs` | `Weapon_PS_Barrel_Init / JAK_Barrel_02`: `damage_scale` ×0.4, `spread_value` ×3 |
| `Nexus-Data-inv_name_part4.ncs` | `np_weap_JAK_PS_B02`'s name: "Muki" → "Masher" |

Only barrel 02 reads that row and that name part, and every other entry in
the three files is left exactly as the game ships it. The build checks this:
it decodes each patched file and requires it to equal the original except for
those cells.

> **Rebuild after every game patch, before playing.** A mod pak replaces whole
> files, so a stale build silently undoes whatever the patch changed in them.
> A build from an out-of-date inv4 made the game **delete items** (see
> [CLAUDE.md](CLAUDE.md)). The build always reads the newest copy the game has
> installed, so after a patch: uninstall, rebuild, reinstall.

Needs the NCS toolkit from the parent repo (`../scripts`, `../tools/bl4.exe`),
plus repak and retoc.

## The SDK mod (older)

Kept for reference and for setups without paks. It changes the live weapon,
so it works across patches, but the card never shows it.

### Install

Requires the [Oak2 mod manager](https://github.com/bl-sdk/oak2-mod-manager)
(BL4 PythonSDK). Either:

```bash
python scripts/install.py
```

or drop `dist/JakobsMasher.sdkmod` (from `python scripts/install.py --package`)
into `Borderlands 4/sdk_mods/`.

Mods load at **startup only** — fully exit and relaunch, not just back to menu.

> **A freshly installed mod starts disabled.** `mods_base` only auto-enables a
> mod that was enabled last time, and a first install has no saved settings.
> Open the console (**F10**), type `mods`, and enable **Jakobs Masher**. The
> choice persists from then on.
>
> While a mod is disabled its hooks, keybinds and console commands are all
> unbound, so it is completely silent — no logs, and `masher` does nothing.
> When it enables it announces itself:
>
> ```
> [jakobs_masher] enabled - 6 projectiles at 0.40x damage, 25% of Jakobs revolvers
> [jakobs_masher] hooks bound: ServerStartUsing, ServerEquipInterruptible, ...
> ```

## SDK mod: settings

All are live-adjustable from the mods menu.

| Setting | Default | Effect |
|---|---|---|
| Projectiles Per Shot | 6 | what BL3's Masher barrel fired |
| Damage Per Projectile | 0.40 | 6 × 0.40 = **2.4× card damage** on a full hit |
| Spread Multiplier | 3.0 | wide enough to read as a Masher, tight enough to aim |
| Masher Chance (%) | 25 | share of **newly found** Jakobs revolvers that are Mashers; 17 is about 1 in 6 |
| Player Weapons Only | On | do not turn enemy revolvers into Mashers too |
| Automatic Scanning | On | convert new guns without pressing anything |
| Scan Interval | 3s | how often the background heartbeat may re-scan |

### Telling which gun is a Masher

**The item card does not tell you** — see Caveats. Run `masher mine`, which
reports what the card omits:

```
  JAK_PS     jakobs_pistol=True masher=True
      62 x 6  =  372 per trigger pull   (spread 3.75)
      unmodified it would be 154 x 1  =  154, so this is 2.40x
```

For reference, the card *would* pick the right row if it read the live weapon —
every weapon class declares two damage rows and chooses between them on the
`weapon_projectile_per_shot` attribute:

| Row | Condition | Renders |
|---|---|---|
| `uistat_damage` | `weapon_projectile_per_shot` ≤ 1 | `Damage: 847` |
| `uistat_damage_and_projectile_count` | `weapon_projectile_per_shot` > 1 | `Damage: 847 x 6` |

— but it resolves that attribute against the **item**, not against the live
weapon actor, so writing the behaviour does not move it. The backpack has to
work that way: it draws cards for guns you are not holding.

```
2 weapon(s) you are carrying:
  JAK_PS     jakobs_pistol=True   masher=True   projectiles=6
  TED_AR     jakobs_pistol=False  masher=None   projectiles=1
```

### Which revolvers become Mashers

Each gun is **judged once**: the first time the mod sees it in your hands, it
is a Masher or not according to **Masher Chance** at that moment, and the
verdict is written down. From then on it never changes:

- **changing Masher Chance only affects guns you find afterwards**; the ones
  you already own keep their verdict;
- **restarts don't flip anything**: the verdict is read back from the record,
  not rolled again;
- **mod updates don't either**, even if they change how rolls are computed.

**Each save has its own record**, in
`sdk_mods/settings/jakobs_masher_guns/<character GUID>.json`. The GUID is the
`char_guid` stored in the save itself, read from the running game, so
characters never share verdicts. **Nothing is written to your save.** Delete a
character's file, or run `masher forget` while playing it, to judge that
character's guns again at the current chance. `masher save` shows which
character is loaded and which file it uses.

If the loaded character cannot be identified, verdicts go to a shared
`unknown-character.json` and the log says so once. Upgrading from 1.2 moves the
old single `jakobs_masher_guns.json` into the first character you load; the old
file is kept as `.migrated`.

Guns are recognised by their **part values**, the numbers the game's item
serial stores. Measured in game, two different revolvers read
`1, 0, 2, 2, 0, 0, 0, 0, 5, 0, ...` and `3, 3, 1, 1, 0, ...`, and each read the
same every time. Two revolvers built from identical parts count as the same
gun and share a verdict, the way BL3's Masher barrel made every gun carrying it
a Masher.

A gun on the ground or in your backpack has no live weapon to change, so a
Masher is *applied* when you equip it. Guns in enemy hands, and guns
mid-pickup, are never judged.

`masher mine` shows each Jakobs pistol's parts and verdict:

```
  JAK_PS     jakobs_pistol=True masher=True
      parts = 1, 0, 2, 2, 0, 0, 0, 0, 5, 0, 0, 0, 0, 0, 0, 0
      roll 23.43, judged Masher at 25% on 2026-09-29
```

> Upgrading from 1.0: the old **Masher Frequency (in 4)** setting is not
> carried over. The new option starts at 25%.

## SDK mod: how it applies itself

Picking a gun up, interacting, or swapping weapons scans immediately. Any
key press also drives a throttled sweep, and for a few seconds after a pickup
it sweeps every half-second — the game hands a picked-up weapon over a moment
*after* the pickup event, and this is what catches it. You should not need to
press anything.

A gun in your **backpack** has no weapon actor, so there is nothing to convert
until it is equipped; it converts the moment you equip it.

This is deliberately not built on the weapon's own events. Measured in game,
**none** of `ServerStartUsing`, `ServerEquipInterruptible`,
`ServerStartReloading`, `PlayEffects` or `ClientSetActiveWeaponEquipSlot` ever
fire in solo play, and neither do the audio library or
`OnEquipSlotsReadyForInventory` — BL4 resolves those natively, where the SDK
cannot see them. They stay registered, and `masher status` shows the tally:

```
hook activity:
  ServerStartUsing                   bound=True  fired=0
  OnWeaponEquipped                   bound=True  fired=1
  ServerUseObject                    bound=True  fired=...
  PostEventInWorld                   bound=True  fired=0
  any key press                      bound=True  fired=...
```

If something is ever missed, the **Scan Weapons Now** keybind and `masher scan`
force a sweep regardless of throttle or settings. If a sweep produces no
Mashers, `masher dump` prints what the mod can see about each weapon — that
output is the thing to report.

## SDK mod: console commands

The console key on this install is **F10**.

```
masher dump      # print the live fire behaviours, their properties and owners
masher status    # what the mod found: hooks, ownership link, resolved properties
masher scan      # apply to every loaded weapon now, ignoring hooks
masher mine      # list the guns you are carrying, and which are Mashers
masher probe     # full dump of those guns: every struct field and its value
masher restore   # undo every change without disabling the mod
masher forget    # forget this save's verdicts; judge your guns again
masher save      # which character is loaded, and its record file
```

## How the pak adds a field the data never had

This README used to say a pak could not do it. That was wrong, and the reason
is worth recording.

Weapon parts live in `Nexus-Data-inv4.ncs`. No common Jakobs pistol barrel sets
`projectilespershot`, which is why BL4 has no Masher. The NCS writer can
repoint existing values but cannot add fields. However, **every common barrel
already carries an inline fire behaviour** (spread, damage, fire rate,
`automaticburstcount`, `bautoburst`), and a field's *name* is just an index
into the file's key pool. Rewriting that one index renames `bautoburst` to
`projectilespershot`, and its value is then repointed to `6`. The game reads
it like any shotgun barrel's pellet count.

`automaticburstcount` would have worked too, but its `1` is what keeps a
Jakobs pistol semi-auto; without it the gun fires full auto. See
[CLAUDE.md](CLAUDE.md) for the bit layout and the tests.

## Tests

```bash
python tests/test_masher.py
```

208 checks against a fake engine (`tests/fake_engine.py`) that models the parts
of the SDK the mod touches — unreal objects with properties, structs, arrays
and an `Outer` chain, class default objects, `find_all`, weak pointers, and the
`mods_base` decorators.

Covers Jakobs-pistol detection through nested objects, arrays and reference
cycles; explicit tags outranking the directory heuristic; ownership filtering;
knobs resolving to whichever property the engine actually exposes, including
values hidden behind attribute structs; roll stability and distribution at any chance; verdicts judged once and remembered across chance changes and restarts; one record per save, found through whichever object holds the character GUID; the old shared record being adopted; `masher forget` touching only the loaded save; an unreadable record never being overwritten; the input heartbeat falling back to a key list; damage not compounding across repeated shots; buffs
surviving a rescale; clean restore; caching; hook fire counting; and graceful
degradation when properties are missing.

These cover the mod's logic. They cannot cover the live object graph — see
**Caveats**.

## Caveats

- **Host only.** The change is applied where the shot is resolved, so in co-op
  it affects the host's own guns.
- **The item card still reads "Jakobs Pistol"** — weapon names come from the
  `inv_name_part` / naming-strategy system keyed on attribute thresholds, and
  renaming means driving that subsystem separately. The **damage row does**
  change, though: see below.
- **The values are attribute structs, not numbers.** `ProjectilesPerShot`,
  `Damage` and `Spread` read back as structs carrying both a `Value` (what the
  game uses) and a `BaseValue`, and they are not all the same type —
  `ProjectilesPerShot` is an integer attribute while `Damage` and `Spread` are
  floats. The mod owns `BaseValue`, kicks `Value` into place when
  it first applies, and then leaves `Value` to the engine so buffs and debuffs
  still stack on top. `masher probe` dumps
  them in full.
- Disabling the mod restores every gun it touched. Nothing is ever written
  to your save or to the item itself, only to the live weapon, so
  disabling or uninstalling leaves your guns exactly as they were.

## Licence

The mod is original work. `monokrome/bl4` (BSD-2-Clause) was used read-only, to
parse the game's NCS data during research; none of its code is vendored here.
