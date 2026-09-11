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
> [jakobs_masher] enabled - 6 projectiles at 0.40x damage, 1 in 4 revolvers
> [jakobs_masher] hooks bound: ServerStartUsing, ServerEquipInterruptible, ...
> ```

## Settings

All four are live-adjustable from the mods menu.

| Setting | Default | Effect |
|---|---|---|
| Projectiles Per Shot | 6 | what BL3's Masher barrel fired |
| Damage Per Projectile | 0.40 | 6 × 0.40 = **2.4× card damage** on a full hit |
| Spread Multiplier | 3.0 | wide enough to read as a Masher, tight enough to aim |
| Masher Frequency (in 4) | 1 | roughly a quarter of Jakobs revolvers |

### Which revolvers become Mashers

The decision is derived from the weapon's **part indices** — the same numbers
its serial encodes — so a given revolver is a Masher in every session, or in
none. It is a property of the gun, not a per-shot roll, exactly as a barrel
part would be. Changing **Masher Frequency** reshuffles which guns qualify.

## If nothing happens

Press the **Scan Weapons Now** keybind (bind it in the mod menu), or run
`masher scan`. That sweeps every loaded weapon directly and does not depend on
any hook firing — so if the automatic triggers turn out not to run in solo
play, this still applies the variant.

Then `masher status` shows which triggers are bound and which have actually
fired:

```
hook activity:
  ServerStartUsing                   bound=True  fired=14
  ServerEquipInterruptible           bound=True  fired=0
  ...
```

A trigger with `bound=True fired=0` after a firefight is one BL4 resolves
natively instead of through the script VM. That table is the thing to report.

## Console commands

The console key on this install is **F10**.

```
masher dump      # print the live fire behaviours, their properties and owners
masher status    # what the mod found, plus the hook activity table
masher scan      # apply to every loaded weapon now, ignoring hooks
masher restore   # undo every change without disabling the mod
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

26 checks against a fake engine (`tests/fake_engine.py`) that models the parts
of the SDK the mod touches — unreal objects with properties and an `Outer`
chain, class default objects, `find_all`, weak pointers, and the `mods_base`
decorators. Covers Jakobs-pistol detection, roll stability and distribution,
damage not compounding across repeated shots, buffs surviving a rescale, clean
restore, caching, and graceful degradation when properties are missing.

These cover the mod's logic. They cannot cover the live object graph — see
**Caveats**.

## Caveats

- **Host only.** The change is applied where the shot is resolved, so in co-op
  it affects the host's own guns.
- **The item card still reads "Jakobs Pistol".** Weapon names come from the
  `inv_name_part` / naming-strategy system keyed on attribute thresholds;
  renaming would mean driving that subsystem separately.
- **Two things need confirming in game on a first run.** The mod logs which
  weapon property it used to identify gun types (`identifying weapons via …`);
  if that line never appears, `masher dump` prints everything it can see. And
  if `Damage` turns out to be recomputed faster than expected, you would see
  six full-damage projectiles rather than six fractional ones.
- Disabling the mod restores every gun it touched.

## Licence

The mod is original work. `monokrome/bl4` (BSD-2-Clause) was used read-only, to
parse the game's NCS data during research; none of its code is vendored here.
