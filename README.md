# Borderlands 4 — Masher Pistols

Brings BL3's **Masher** revolvers to Borderlands 4.

A Masher is a Jakobs revolver whose barrel fires a burst of projectiles instead
of a single slug, each one landing for a fraction of the card damage. BL3 had
them; BL4 does not. This adds them back.

No new art and no new legendary — it reuses the Jakobs pistols already in the
game, so a Masher looks exactly like the revolver it is.

## Install

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

## Settings

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

The record is the mod's own file,
`sdk_mods/settings/jakobs_masher_guns.json`. **Nothing is written to your
save.** Delete that file, or run `masher forget`, to judge every gun again at
the current chance.

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

## How it applies itself

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

## Console commands

The console key on this install is **F10**.

```
masher dump      # print the live fire behaviours, their properties and owners
masher status    # what the mod found: hooks, ownership link, resolved properties
masher scan      # apply to every loaded weapon now, ignoring hooks
masher mine      # list the guns you are carrying, and which are Mashers
masher probe     # full dump of those guns: every struct field and its value
masher restore   # undo every change without disabling the mod
masher forget    # forget every remembered verdict; judge your guns again
```

## Why this is a runtime mod and not a new weapon part

Worth stating plainly, because the obvious approach does not work.

Weapon parts live in `Nexus-Data-inv4.ncs`, not in Unreal assets. The `jak_ps`
record lists its barrels — `part_barrel_01`, `part_barrel_02`, and the `_a`–`_d`
accessory variants — and **none of them set `projectilespershot`**. The records
that do are shotgun-shaped: `bor_sg`'s barrel is a flat `4.000000`, and others
read a `Weapon_SG_Underbarrel_Init` data-table row that has no pistol
equivalent. That absence is the entire reason BL4 has no Masher.

Adding one as real data is blocked twice over:

1. It needs **new NCS records**, which needs record-level NCS writing — the same
   re-encoder wall documented in the parent repo's `CLAUDE.md`. Existing values
   can be repointed; new keys cannot be added.
2. Part *behaviour* is **native C++** compiled into the game binary. The NCS is
   only the registry that names parts and wires them together, so even a
   successfully injected record would have nothing behind it.

So the variant is applied to the live `WeaponBehavior_FireProjectile` instead.
See [CLAUDE.md](CLAUDE.md) for how that object was located.

## Tests

```bash
python tests/test_masher.py
```

180 checks against a fake engine (`tests/fake_engine.py`) that models the parts
of the SDK the mod touches — unreal objects with properties, structs, arrays
and an `Outer` chain, class default objects, `find_all`, weak pointers, and the
`mods_base` decorators.

Covers Jakobs-pistol detection through nested objects, arrays and reference
cycles; explicit tags outranking the directory heuristic; ownership filtering;
knobs resolving to whichever property the engine actually exposes, including
values hidden behind attribute structs; roll stability and distribution at any chance; verdicts judged once and remembered across chance changes and restarts; `masher forget`; an unreadable record never being overwritten; the input heartbeat falling back to a key list; damage not compounding across repeated shots; buffs
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
