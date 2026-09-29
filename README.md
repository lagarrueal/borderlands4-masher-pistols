# Borderlands 4 — Masher Pistols

Brings BL3's **Masher** revolvers to Borderlands 4.

A Masher is a Jakobs revolver whose barrel fires a burst of projectiles instead
of a single slug, each one landing for a fraction of the card damage. BL3 had
them; BL4 does not. This adds them back, the way BL3 did it: as a barrel.

No new art and no new legendary. Every Jakobs pistol built with **barrel 02**,
the one whose guns used to be called "… Muki", is now a Masher:

| | Before | Masher |
|---|---|---|
| Projectiles per shot | 1 | **6**, still semi-auto |
| Damage per projectile | card damage | **0.4×**, so 2.4× per trigger pull, as in BL3 |
| Spread | normal | **3×** |
| Name | "… Muki" | **"… Masher"** |

The game decides at drop, from the barrel it rolls, and the gun's serial keeps
it: the same revolver is a Masher in every save, on every character, and on
its card, which reads `damage x 6`.

## Install

```bash
python pak/build_masher_pak.py --install     # build against the installed game, then install
python pak/build_masher_pak.py --uninstall   # remove it
```

The build reads the game's own data from your install, applies the changes,
and writes `JakobsMasher_9600_P.pak/.ucas/.utoc` into `OakGame/Content/Paks/`.
It needs the NCS toolkit from the parent repo (`../scripts`, `../tools/bl4.exe`),
[repak](https://github.com/trumank/repak) and
[retoc](https://github.com/trumank/retoc).

> **Rebuild after every game patch, before playing.** A mod pak replaces whole
> data files, so a build made against an older game version silently undoes
> whatever the patch changed in them. A build from an out-of-date file made
> the game **delete items** from a save during development (see
> [CLAUDE.md](CLAUDE.md)). After a patch: uninstall, then `--install` again.

## What it changes

Three game data files, each rebuilt from the newest copy the game has
installed:

| File | Change |
|---|---|
| `Nexus-Data-inv4.ncs` | `jak_ps` `part_barrel_02`'s fire behaviour: field `bautoburst` (`false`, already the default) renamed to `projectilespershot` and set to `6` |
| `Nexus-Data-gbx_ue_data_table4.ncs` | `Weapon_PS_Barrel_Init / JAK_Barrel_02`: `damage_scale` ×0.4, `spread_value` ×3 |
| `Nexus-Data-inv_name_part4.ncs` | `np_weap_JAK_PS_B02`'s name: "Muki" → "Masher" |

Nothing else reads that data-table row or that name part, and every other
entry in the three files is left exactly as shipped. The build enforces this:
it decodes each patched file and refuses to write anything unless it equals the
original apart from those cells.

Flags: `--projectiles-only` builds only the inv4 change; `--null` and
`--original` build the unmodified-file control paks used during development.

## How a pak adds a field the data never had

No common Jakobs pistol barrel sets `projectilespershot`, which is why BL4 has
no Masher. The NCS writer can repoint existing values but cannot add fields.
However, every common barrel already carries an inline fire behaviour (spread,
damage, fire rate, `automaticburstcount`, `bautoburst`), and a field's *name*
is just an index into the file's key pool. Rewriting that one index renames
`bautoburst` to `projectilespershot`, and its value is then repointed to `6`.
The game reads it like any shotgun barrel's pellet count, and the item card,
which is built from exactly this data, shows it.

`automaticburstcount` would have worked too, but its `1` is what keeps a
Jakobs pistol semi-auto; renamed away, the gun fires full auto.

## Caveats

- **Co-op:** every player probably needs the pak, since each client reads its
  own data.
- **All barrel-02 Jakobs pistols** are Mashers, including ones you already own.
  About half of common Jakobs pistols roll barrel 02.
- Legendary Jakobs pistols with their own barrels (King's Gambit, Quickdraw…)
  are unaffected.

## History

This started as an Oak2 SDK (Python) mod that edited the live weapon at
runtime. It worked, but the item card never followed, because the card is
built natively from the item's data. It was replaced by this pak and removed.
It is in git history up to commit `f34ccdd` if needed; [CLAUDE.md](CLAUDE.md)
keeps its research.

## Licence

The mod is original work. `monokrome/bl4` (BSD-2-Clause) was used read-only, to
parse the game's NCS data; none of its code is vendored here.
