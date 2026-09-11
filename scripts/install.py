"""Install the mod into the game's sdk_mods, and/or package it as a .sdkmod.

    python scripts/install.py              # copy into the game
    python scripts/install.py --package    # also write dist/JakobsMasher.sdkmod
    python scripts/install.py --uninstall  # remove it from the game

The Oak2 mod manager loads plain directories under `sdk_mods/` as readily as
`.sdkmod` zips, so a directory install is the one to use while iterating. Ship
the zip. Never leave both in place - the loader would import the mod twice.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import zipfile
from pathlib import Path

MOD_NAME = "jakobs_masher"
DIST_NAME = "JakobsMasher.sdkmod"

DEFAULT_GAME_DIR = Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\Borderlands 4"
)

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / MOD_NAME


def sdk_mods_dir(game_dir: Path) -> Path:
    path = game_dir / "sdk_mods"
    if not path.is_dir():
        sys.exit(
            f"no sdk_mods directory at {path}\n"
            "Is the Oak2 mod manager installed? See the repo README."
        )
    return path


def install(game_dir: Path) -> None:
    target = sdk_mods_dir(game_dir) / MOD_NAME

    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(
        SOURCE,
        target,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )

    stale = sdk_mods_dir(game_dir) / DIST_NAME
    if stale.exists():
        stale.unlink()
        print(f"removed {stale.name} so the mod is not loaded twice")

    print(f"installed -> {target}")
    print("Fully exit and relaunch the game; mods load at startup only.")


def uninstall(game_dir: Path) -> None:
    removed = False
    for path in (sdk_mods_dir(game_dir) / MOD_NAME, sdk_mods_dir(game_dir) / DIST_NAME):
        if path.is_dir():
            shutil.rmtree(path)
            removed = True
            print(f"removed {path}")
        elif path.exists():
            path.unlink()
            removed = True
            print(f"removed {path}")
    if not removed:
        print("nothing installed")


def package() -> Path:
    dist = REPO / "dist"
    dist.mkdir(exist_ok=True)
    out = dist / DIST_NAME

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(SOURCE):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in sorted(filenames):
                if name.endswith(".pyc"):
                    continue
                full = Path(dirpath) / name
                # Entries must sit under a single top-level mod directory.
                arc = Path(MOD_NAME) / full.relative_to(SOURCE)
                zf.write(full, arc.as_posix())

    print(f"packaged -> {out} ({out.stat().st_size} bytes)")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--game-dir",
        type=Path,
        default=DEFAULT_GAME_DIR,
        help=f"Borderlands 4 install directory (default: {DEFAULT_GAME_DIR})",
    )
    parser.add_argument(
        "--package",
        action="store_true",
        help="also write dist/" + DIST_NAME,
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="remove the mod from the game instead of installing it",
    )
    args = parser.parse_args()

    if not SOURCE.is_dir():
        sys.exit(f"mod source missing at {SOURCE}")

    if args.uninstall:
        uninstall(args.game_dir)
        return

    install(args.game_dir)
    if args.package:
        package()


if __name__ == "__main__":
    main()
