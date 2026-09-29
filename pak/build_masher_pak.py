"""Build the Jakobs Masher pak: barrel 02 of every Jakobs pistol becomes a Masher barrel.

A runtime (SDK) edit changes how a gun fires but never reaches its item card:
the card is built natively from the item's part data. So this edits the data
itself. Every common Jakobs pistol barrel carries an inline fire aspect
(`parent: inv_aspect'jak_ps_fire_projectile'`) holding spread, damage, fire rate
and `automaticburstcount: "1"`, but no `projectilespershot`. The NCS writer
cannot add a field, but a field's *name* is a 12-bit index into the payload's
key pool - so `automaticburstcount` (default 1, unused on a revolver) is renamed
in place to `projectilespershot`, and its value repointed to the Masher count.

Nothing here is hardcoded to a bit position: those move on every game patch.
Everything is re-derived from the installed game paks, via the NCS_TRACE
instrumented bl4.exe, and the result is decoded again and compared against the
original before anything is written - the only difference allowed in the
whole file is the one intended.

Usage:
    python pak/build_masher_pak.py              # build into build/
    python pak/build_masher_pak.py --install    # build and copy into Paks/
    python pak/build_masher_pak.py --uninstall  # remove it from Paks/
    python pak/build_masher_pak.py --null       # control: repack inv4 unchanged (stored)
    python pak/build_masher_pak.py --original   # control: the game's compressed file, byte for byte
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import uuid
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
SCRIPTS = PROJECT.parent / "scripts"  # the shared NCS toolkit
BUILD = PROJECT / "build"

PAKS = Path(
    os.environ.get(
        "BL4_PAKS",
        r"C:/Program Files (x86)/Steam/steamapps/common/Borderlands 4/OakGame/Content/Paks",
    )
)
BL4 = Path(os.environ.get("BL4_EXE", PROJECT.parent / "tools" / "bl4.exe"))
REPAK = Path(r"C:/repak/repak.exe")
RETOC = Path(r"C:/Users/alexa/.cargo/bin/retoc.exe")

# Sorts after every game chunk (highest _N_P wins). No other installed mod
# ships inv4, so there is nothing to collide with.
MOD_NAME = "JakobsMasher_9600_P"

# --- what the mod does -------------------------------------------------------
INV_FILE = "Nexus-Data-inv4.ncs"
WEAPON = "jak_ps"
MASHER_BARREL = "part_barrel_02"          # names its guns "... Muki"
FIRE_ASPECT = "inv_aspect'jak_ps_fire_projectile'"
# The field sacrificed for the new key. Not `automaticburstcount` ("1"): that
# is what keeps a Jakobs pistol semi-auto, and renaming it away made the
# Masher fire full auto while the trigger was held. `bautoburst` is "false",
# which is already the default, so losing it changes nothing.
RENAMED_KEY = "bautoburst"
RENAMED_VALUE = "false"
MASHER_KEY = "projectilespershot"
PROJECTILES = "6.000000"                  # BL3's Masher barrel fired 6
# The barrel's damage attribute, which the trace uses to find the barrel.
BARREL_DAMAGE_VALUE = "jak_ps_barrel_02_damage"

# EXPERIMENTAL level gate: no Masher barrel below this game stage. Parts are
# gated by a top-level `mingamestage` field (licensed accessories use it), but
# part_barrel_02 has none and fields cannot be added. Its only plain-text
# top-level field is its own name, `barrel: part_barrel_02` - which every part
# in the file carries - so that field is renamed to `mingamestage` and given
# the level. Unknown whether the game needs the name field; test before use.
# `--no-gate` builds without it.
GATE_ENABLED = "--no-gate" not in sys.argv
GATE_LEVEL = "15.000000"
GATE_RENAMED_KEY = "barrel"          # the part's own-name field
GATE_RENAMED_VALUE = MASHER_BARREL   # "part_barrel_02"
GATE_KEY = "mingamestage"

# Per-projectile damage and spread, from the barrel's data-table row. Only
# jak_ps part_barrel_02 reads Weapon_PS_Barrel_Init / JAK_Barrel_02 (rows of
# the same name in the AR/SG/SR tables belong to other tables), so scaling
# the row changes Mashers and nothing else. Scaled from whatever the current
# game data holds: the values moved between patches (damage 3.4 -> 4.2).
TABLE_FILE = "Nexus-Data-gbx_ue_data_table4.ncs"
TABLE_ENTRY = "weapon_ps_barrel_init"
TABLE_ROW = "JAK_Barrel_02"
TABLE_SCALES = {"damage_scale": 0.40, "spread_value": 3.0}  # 6 x 0.4 = 2.4x, as in BL3

# The barrel's name part: "... Muki" becomes "... Masher". The partname is a
# localized-text reference "<namespace>, <key>, <source text>"; a key the
# localization tables do not know should fall back to the source text. Only
# part_barrel_02 references this name part.
NAME_FILE = "Nexus-Data-inv_name_part4.ncs"
NAME_ENTRY = "np_weap_jak_ps_b02"
NAME_KEY = "partname"
MASHER_NAME = "Masher"
# A stable key, so every rebuild writes the same string.
MASHER_NAME_KEY = uuid.uuid5(uuid.NAMESPACE_URL, "bl4-masher/np_weap_JAK_PS_B02").hex.upper()
# -----------------------------------------------------------------------------

VALUE_RE = re.compile(r'VALUE bitpos=(\d+) bits=(\d+) raw=(\d+) idx=(\d+) val="(.*)"')


def run(cmd, **kw):
    result = subprocess.run(
        [str(c) for c in cmd], capture_output=True, text=True, encoding="utf-8", errors="replace", **kw
    )
    return result


def pak_version(pak: Path) -> int:
    match = re.search(r"_(\d+)_P\.pak$", pak.name)
    return int(match.group(1)) if match else 0


def newest_copy(filename: str) -> tuple[Path, Path]:
    """(pak, carved file) for the highest-numbered game pak holding `filename`.

    Deliberately does not use `repak list`: repak panics on 60 of the game's
    paks, including pakchunk4-Windows_20, which holds the newest inv4. A build
    from the newest pak repak *could* read (Windows_18) shadowed the real
    inv4 - the class mods of the newest class (`classmod_corpohacker`, type
    402) exist only in Windows_20 - and the game hid those items and deleted
    the equipped one and most of Lost Loot.

    The file name is always present as plain text in a pak's index, readable
    or not, so candidates are found by grepping the raw bytes, then carved out
    by content (scripts/carve_ncs.py).
    """
    sys.path.insert(0, str(SCRIPTS))
    from carve_ncs import carve  # noqa: E402

    chunk = re.search(r"(\d+)\.ncs$", filename).group(1)
    stem = filename[len("Nexus-Data-") : -len(".ncs")]  # e.g. "inv4"
    needle = filename.encode()
    candidates = sorted(PAKS.glob(f"pakchunk{chunk}-Windows_*_P.pak"), key=pak_version, reverse=True)
    for pak in candidates:
        if needle not in pak.read_bytes():
            continue
        out = BUILD / f"_carved_{pak.stem}"
        shutil.rmtree(out, ignore_errors=True)
        for _table, _size, target in carve(pak, out):
            if target.name == f"{stem}.ncs":
                return pak, target
        raise SystemExit(f"{pak.name} lists {filename} but no matching NCS could be carved from it")
    raise SystemExit(f"no game pak contains {filename}")


def extract_payload(filename: str, tag: str) -> Path:
    """Carve the newest copy out of the game paks and decompress it."""
    pak, carved = newest_copy(filename)
    print(f"  {filename}: newest copy in {pak.name}")
    raw, dec = BUILD / f"_{tag}_raw", BUILD / f"_{tag}_dec"
    for d in (raw, dec):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
    shutil.copy(carved, raw / filename)
    run([sys.executable, SCRIPTS / "ncs_decomp.py", raw, dec])
    payload = dec / (filename + ".bin")
    if not payload.exists():
        raise SystemExit(f"could not decompress {filename}")
    return payload


def trace(payload: Path, tag: str) -> list[str]:
    env = dict(os.environ, NCS_TRACE="1")
    out = subprocess.run(
        [str(BL4), "ncs", "show", str(payload)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )
    (BUILD / f"_trace_{tag}.txt").write_text(out.stderr, encoding="utf-8")
    return out.stderr.splitlines()


def decode_json(payload: Path) -> dict:
    out = run([BL4, "ncs", "show", "--json", payload])
    if out.returncode != 0:
        raise SystemExit(f"decode failed for {payload.name}: {out.stderr[:400]}")
    return json.loads(out.stdout)


# --- payload layout ----------------------------------------------------------


def string_blocks(payload: bytes):
    """(blocks by name, byte offset where the bit-packed record data starts)."""
    string_bytes = struct.unpack_from("<I", payload, 8)[0]
    pos = 16 + string_bytes
    type_code_count = payload[pos]
    type_index_count = struct.unpack_from("<H", payload, pos + 1)[0]
    pos += 3 + type_code_count + (type_code_count * type_index_count + 7) // 8
    blocks = {}
    for name in ("value_strings", "value_kinds", "key_strings"):
        declared, _flags, byte_length = struct.unpack_from("<IIQ", payload, pos)
        raw = payload[pos + 16 : pos + 16 + byte_length].split(b"\x00")[:declared]
        bits = 0 if declared <= 1 else (declared - 1).bit_length()
        blocks[name] = {"declared": declared, "bits": bits, "strings": [s.decode("utf-8", "replace") for s in raw]}
        pos += 16 + byte_length
    return blocks, pos


def read_bits(buf, bitpos: int, n: int) -> int:
    value = 0
    for i in range(n):
        b = bitpos + i
        value |= ((buf[b // 8] >> (b % 8)) & 1) << i
    return value


def write_bits(buf: bytearray, bitpos: int, value: int, n: int) -> None:
    for i in range(n):
        b = bitpos + i
        if (value >> i) & 1:
            buf[b // 8] |= 1 << (b % 8)
        else:
            buf[b // 8] &= ~(1 << (b % 8)) & 0xFF


# --- finding the cells -------------------------------------------------------


def find_masher_cells(payload: bytes, lines: list[str]) -> dict:
    """Bit positions (relative to the record data) of the key and value to change.

    The barrel is found by its damage attribute, which only it references. The
    renamed field's value sits a few values away. Its key is not traced, but
    it lies in the gap between the previous value and this one (measured on
    inv4: 9 bits into the gap, 12 bits wide, then 4 type bits). The gap is
    scanned and exactly one match is required.
    """
    blocks, data_start = string_blocks(payload)
    keys = blocks["key_strings"]
    key_bits = keys["bits"]
    renamed_index = keys["strings"].index(RENAMED_KEY)
    masher_index = keys["strings"].index(MASHER_KEY)

    values = [(i, VALUE_RE.match(line)) for i, line in enumerate(lines)]
    values = [(i, m) for i, m in values if m]
    anchors = [k for k, (_, m) in enumerate(values) if m.group(5) == BARREL_DAMAGE_VALUE]
    if len(anchors) != 1:
        raise SystemExit(f"expected 1 reference to {BARREL_DAMAGE_VALUE}, found {len(anchors)}")
    anchor = anchors[0]

    # The renamed field's value is near the damage attribute, in the same
    # behaviour. Take every value equal to RENAMED_VALUE within a small
    # window, and keep the ones whose gap holds the renamed key exactly once.
    data_bit = data_start * 8
    matches = []
    for k in range(max(anchor - 8, 1), min(anchor + 40, len(values))):
        m = values[k][1]
        if m.group(5) != RENAMED_VALUE:
            continue
        value_pos, value_bits = int(m.group(1)), int(m.group(2))
        previous = values[k - 1][1]
        gap_start = int(previous.group(1)) + int(previous.group(2))
        hits = [
            b
            for b in range(gap_start, value_pos - key_bits + 1)
            if read_bits(payload, data_bit + b, key_bits) == renamed_index
        ]
        if len(hits) == 1:
            matches.append((hits, value_pos, value_bits, gap_start))
    if len(matches) != 1:
        raise SystemExit(
            f"expected exactly 1 '{RENAMED_KEY}: {RENAMED_VALUE}' near {BARREL_DAMAGE_VALUE}, found {len(matches)}"
        )
    hits, value_pos, value_bits, gap_start = matches[0]
    return {
        "key_pos": hits[0],
        "key_bits": key_bits,
        "masher_key_index": masher_index,
        "value_pos": value_pos,
        "value_bits": value_bits,
        "gap": (gap_start, value_pos),
    }


def find_gate_cell(payload: bytes, lines: list[str]) -> dict:
    """The `barrel: part_barrel_02` own-name cell of the Masher barrel.

    `jak_ps` appears as an entry in several records, and "part_barrel_02" as a
    value in more than one place (part lists name it too, under the key
    `part`). Only the part's own-name field has the key `barrel` in its gap;
    exactly one such cell must exist across all the jak_ps entries.
    """
    blocks, data_start = string_blocks(payload)
    keys = blocks["key_strings"]
    key_bits = keys["bits"]
    renamed_index = keys["strings"].index(GATE_RENAMED_KEY)
    gate_index = keys["strings"].index(GATE_KEY)
    data_bit = data_start * 8
    hits = []
    region: list = []
    inside = False
    for line in lines + ['ENTRY key="__end__"']:
        if line.startswith("ENTRY "):
            if inside:
                for k in range(1, len(region)):
                    m = region[k]
                    if m.group(5) != GATE_RENAMED_VALUE:
                        continue
                    value_pos = int(m.group(1))
                    gap_start = int(region[k - 1].group(1)) + int(region[k - 1].group(2))
                    in_gap = [
                        b for b in range(gap_start, value_pos - key_bits + 1)
                        if read_bits(payload, data_bit + b, key_bits) == renamed_index
                    ]
                    if len(in_gap) == 1:
                        hits.append({"key_pos": in_gap[0], "value_pos": value_pos})
            inside = line.startswith(f'ENTRY key="{WEAPON}"')
            region = []
            continue
        if inside:
            m = VALUE_RE.match(line)
            if m:
                region.append(m)
    if len(hits) != 1:
        raise SystemExit(f"expected 1 '{GATE_RENAMED_KEY}: {GATE_RENAMED_VALUE}' cell, found {hits}")
    return {**hits[0], "key_bits": key_bits, "gate_key_index": gate_index}


def barrel_part(doc: dict) -> dict:
    """The masher barrel's part definition, inside a decoded inv4 document."""
    for record in doc["tables"]["inv"]["records"]:
        for entry in record["entries"]:
            if entry["key"] != WEAPON:
                continue
            for dep in entry.get("dep_entries") or []:
                if dep.get("dep_table_name") == "barrel" and dep.get("key") == MASHER_BARREL:
                    return dep["value"]
    raise SystemExit(f"{MASHER_BARREL} not found")


# --- verification ------------------------------------------------------------


def barrel_behaviour(doc: dict) -> dict:
    """The masher barrel's fire behaviour, inside a decoded inv4 document."""
    for record in doc["tables"]["inv"]["records"]:
        for entry in record["entries"]:
            if entry["key"] != WEAPON:
                continue
            for dep in entry.get("dep_entries") or []:
                if dep.get("dep_table_name") == "barrel" and dep.get("key") == MASHER_BARREL:
                    for aspect in dep["value"].get("aspects", []):
                        if aspect.get("parent") == FIRE_ASPECT:
                            return aspect["behavior"]
    raise SystemExit(f"{MASHER_BARREL} fire behaviour not found")


def verify(original: dict, patched: dict) -> None:
    behaviour = barrel_behaviour(patched)
    if behaviour.get(MASHER_KEY) != PROJECTILES or RENAMED_KEY in behaviour:
        raise SystemExit(f"patched barrel is wrong: {json.dumps(behaviour)[:300]}")
    if GATE_ENABLED:
        part = barrel_part(patched)
        if part.get(GATE_KEY) != GATE_LEVEL or GATE_RENAMED_KEY in part:
            raise SystemExit(f"patched barrel part is wrong: {sorted(part)}")
    # Undo the intended changes on a copy; everything else must be identical.
    undone = copy.deepcopy(patched)
    b = barrel_behaviour(undone)
    b.pop(MASHER_KEY)
    b[RENAMED_KEY] = RENAMED_VALUE
    if GATE_ENABLED:
        part = barrel_part(undone)
        part.pop(GATE_KEY)
        part[GATE_RENAMED_KEY] = GATE_RENAMED_VALUE
    if undone != original:
        raise SystemExit("the patch changed something besides the masher barrel")
    gate = f", {GATE_KEY}={GATE_LEVEL}" if GATE_ENABLED else ""
    print(f"  verified: {MASHER_BARREL} now has {MASHER_KEY}={PROJECTILES}{gate}; nothing else in the file changed")


# --- the data table and the name part --------------------------------------

ENTRY_RE = re.compile(r'ENTRY key="([^"]*)"')


def find_field(payload: bytes, lines: list[str], entry: str, key: str, expected: str,
               after: str | None = None, window: int = 24) -> dict:
    """The value cell of `key` inside trace entry `entry`, asserted to hold `expected`.

    If `after` is given, only values after the first occurrence of that value
    inside the entry are considered (e.g. a row name). A candidate counts only
    if its gap holds the key's 12-bit index exactly once, so a value that is
    merely equal to `expected` elsewhere is never picked.
    """
    blocks, data_start = string_blocks(payload)
    keys = blocks["key_strings"]
    key_index = keys["strings"].index(key)
    starts = [i for i, line in enumerate(lines) if (m := ENTRY_RE.match(line)) and m.group(1) == entry]
    if len(starts) != 1:
        raise SystemExit(f"expected 1 trace entry '{entry}', found {len(starts)}")
    region = []
    for line in lines[starts[0] + 1 :]:
        if line.startswith("ENTRY "):
            break
        m = VALUE_RE.match(line)
        if m:
            region.append(m)
    if after is not None:
        first = next((k for k, m in enumerate(region) if m.group(5) == after), None)
        if first is None:
            raise SystemExit(f"'{after}' not found in entry '{entry}'")
        region = region[first : first + window + 1]
    data_bit = data_start * 8
    hits = []
    for k in range(1, len(region)):
        m = region[k]
        value_pos = int(m.group(1))
        gap_start = int(region[k - 1].group(1)) + int(region[k - 1].group(2))
        in_gap = [
            b for b in range(gap_start, value_pos - keys["bits"] + 1)
            if read_bits(payload, data_bit + b, keys["bits"]) == key_index
        ]
        if len(in_gap) == 1:
            hits.append((value_pos, m.group(5)))
    if not hits:
        raise SystemExit(f"no '{key}' cell in '{entry}'")
    if after is None and len(hits) != 1:
        raise SystemExit(f"expected 1 '{key}' cell in '{entry}', found {hits}")
    # After a row name, the row's own fields come first; the next row's
    # fields of the same name follow. The value check below guards the pick.
    value_pos, value = hits[0]
    if value != expected:
        raise SystemExit(f"'{entry}.{key}' holds {value!r}, expected {expected!r}")
    return {"bitpos": value_pos, "value": value}


def repoint(payload: Path, edits: list[dict], tag: str) -> Path:
    """Apply value repoints with the shared tool; returns the patched payload."""
    edits_file = BUILD / f"_edits_{tag}.json"
    edits_file.write_text(json.dumps(edits), encoding="utf-8")
    out_bin = BUILD / f"_{tag}_patched.bin"
    out = run([sys.executable, SCRIPTS / "ncs_multipatch.py", payload, out_bin, edits_file])
    print("  " + out.stdout.strip().replace("\n", "\n  "))
    if "verify=OK" not in out.stdout:
        raise SystemExit(f"repoint failed for {tag}:\n{out.stdout}\n{out.stderr}")
    return out_bin


def table_row(doc: dict) -> dict:
    for record in doc["tables"]["gbx_ue_data_table"]["records"]:
        for entry in record["entries"]:
            if entry["key"].lower() == TABLE_ENTRY:
                for row in entry["value"]["data"]:
                    if row["row_name"] == TABLE_ROW:
                        return row["row_value"]
    raise SystemExit(f"{TABLE_ENTRY}/{TABLE_ROW} not found")


def name_part(doc: dict) -> dict:
    for table in doc["tables"].values():
        for record in table["records"]:
            for entry in record["entries"]:
                if entry["key"].lower() == NAME_ENTRY:
                    return entry["value"]
    raise SystemExit(f"{NAME_ENTRY} not found")


def build_table(root: Path) -> None:
    payload = extract_payload(TABLE_FILE, "dt4")
    original = decode_json(payload)
    row = table_row(original)
    lines = trace(payload, "dt4")
    edits, expected = [], {}
    for key, scale in TABLE_SCALES.items():
        old = row[key]
        new = f"{float(old) * scale:.6f}"
        cell = find_field(payload.read_bytes(), lines, TABLE_ENTRY, key, old, after=TABLE_ROW)
        edits.append({"bitpos": cell["bitpos"], "target": new})
        expected[key] = new
        print(f"  {TABLE_ROW}.{key}: {old} -> {new} (x{scale})")
    patched = repoint(payload, edits, "dt4")
    want = copy.deepcopy(original)
    table_row(want).update(expected)
    if decode_json(patched) != want:
        raise SystemExit("the data table changed beyond the intended row cells")
    print(f"  verified: only {TABLE_ROW}'s {', '.join(TABLE_SCALES)} changed")
    store(patched, root / "Engine" / "Content" / "_NCS" / TABLE_FILE)


def build_name(root: Path) -> None:
    payload = extract_payload(NAME_FILE, "np4")
    original = decode_json(payload)
    old = name_part(original)[NAME_KEY]
    namespace = old.split(",", 1)[0]
    new = f"{namespace}, {MASHER_NAME_KEY}, {MASHER_NAME}"
    cell = find_field(payload.read_bytes(), trace(payload, "np4"), NAME_ENTRY, NAME_KEY, old)
    print(f"  {NAME_ENTRY}.{NAME_KEY}: {old!r} -> {new!r}")
    patched = repoint(payload, [{"bitpos": cell["bitpos"], "target": new}], "np4")
    want = copy.deepcopy(original)
    name_part(want)[NAME_KEY] = new
    if decode_json(patched) != want:
        raise SystemExit("the name parts changed beyond the Masher name")
    print(f"  verified: only {NAME_ENTRY}'s name changed")
    store(patched, root / "Engine" / "Content" / "_NCS" / NAME_FILE)


# --- packaging -------------------------------------------------------------


def store(payload: Path, dest: Path) -> None:
    """Write the payload as an uncompressed .ncs - the only form the game accepts from a mod."""
    data = payload.read_bytes()
    header = bytearray(16)
    header[0] = 1
    header[1:4] = b"NCS"
    struct.pack_into("<III", header, 4, 0, len(data), len(data))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(bytes(header) + data)


def build() -> None:
    BUILD.mkdir(exist_ok=True)
    print("Building the Jakobs Masher pak against the installed game\n")

    if ORIGINAL_BUILD:
        # Control build: the game's own file, still Oodle-compressed exactly as
        # shipped, in our pak. Separates "our stored container" from "any inv4
        # supplied by a mod pak".
        pak, carved = newest_copy(INV_FILE)
        print(f"  ORIGINAL BUILD: packing {pak.name}'s compressed {INV_FILE} byte for byte")
        package_file(carved)
        return
    payload = extract_payload(INV_FILE, "inv4")
    if NULL_BUILD:
        # Control build: the game's own payload, repacked unchanged. If this
        # alone makes the game drop items, replacing inv4 is unsafe as such.
        print("  NULL BUILD: packing the unmodified payload as a control")
        package(payload)
        return
    cells = find_masher_cells(payload.read_bytes(), trace(payload, "inv4"))
    print(f"  {RENAMED_KEY} key at bit {cells['key_pos']} (gap {cells['gap']}), value at bit {cells['value_pos']}")

    # 1. Repoint the value with the shared tool (it handles pool appends and
    #    verifies its own write).
    value_edits = [{"bitpos": cells["value_pos"], "target": PROJECTILES}]
    gate = None
    if GATE_ENABLED:
        gate = find_gate_cell(payload.read_bytes(), trace(payload, "inv4"))
        print(f"  {GATE_RENAMED_KEY} key at bit {gate['key_pos']}, value at bit {gate['value_pos']}")
        value_edits.append({"bitpos": gate["value_pos"], "target": GATE_LEVEL})
    edits = BUILD / "_edits_inv4.json"
    edits.write_text(json.dumps(value_edits), encoding="utf-8")
    stage = BUILD / "_inv4_value.bin"
    out = run([sys.executable, SCRIPTS / "ncs_multipatch.py", payload, stage, edits])
    print("  " + out.stdout.strip().replace("\n", "\n  "))
    if "verify=OK" not in out.stdout:
        raise SystemExit(f"value repoint failed:\n{out.stdout}\n{out.stderr}")

    # 2. Rename the key, against the (possibly shifted) data start of the output.
    buf = bytearray(stage.read_bytes())
    _, data_start = string_blocks(bytes(buf))
    bit = data_start * 8 + cells["key_pos"]
    before = read_bits(buf, bit, cells["key_bits"])
    write_bits(buf, bit, cells["masher_key_index"], cells["key_bits"])
    if gate is not None:
        gbit = data_start * 8 + gate["key_pos"]
        gbefore = read_bits(buf, gbit, gate["key_bits"])
        write_bits(buf, gbit, gate["gate_key_index"], gate["key_bits"])
        print(f"  key index {gbefore} -> {gate['gate_key_index']} ({GATE_RENAMED_KEY} -> {GATE_KEY})")
    patched = BUILD / "_inv4_patched.bin"
    patched.write_bytes(bytes(buf))
    print(f"  key index {before} -> {cells['masher_key_index']} ({RENAMED_KEY} -> {MASHER_KEY})")

    # 3. Decode both and require the change to be exactly the one intended.
    verify(decode_json(payload), decode_json(patched))
    root = BUILD / "_modroot"
    shutil.rmtree(root, ignore_errors=True)
    store(patched, ncs_destination(root))

    if "--projectiles-only" not in sys.argv:
        print()
        build_table(root)
        print()
        build_name(root)
    pack_root(root)


# Where the file goes in the pak.
#
# Default: replace Engine/Content/_NCS/Nexus-Data-inv4.ncs, built from the
# real newest copy (see newest_copy). `--patch-route` instead puts the file
# on Gearbox's hotfix path (OakGame/_PATCH/PAK/_NCS/), where the online patch
# ships partial NCS files. Tested 2026-09-29: from a mod pak it has no effect
# at all (no dmg x 6), so the game applies those only from its own patch pak.
PATCH_ROUTE = "--patch-route" in sys.argv
PATCH_FILE = "Nexus-Data-inv.ncs"  # hotfix files carry no chunk number


def ncs_destination(root: Path) -> Path:
    if PATCH_ROUTE:
        return root / "OakGame" / "_PATCH" / "PAK" / "_NCS" / PATCH_FILE
    return root / "Engine" / "Content" / "_NCS" / INV_FILE


def package(patched: Path) -> None:
    root = BUILD / "_modroot"
    shutil.rmtree(root, ignore_errors=True)
    store(patched, ncs_destination(root))
    pack_root(root)


def package_file(ncs_file: Path) -> None:
    """Pack an already-complete .ncs file (header included) unchanged."""
    root = BUILD / "_modroot"
    shutil.rmtree(root, ignore_errors=True)
    dest = ncs_destination(root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(ncs_file, dest)
    pack_root(root)


def pack_root(root: Path) -> None:
    pak = BUILD / f"{MOD_NAME}.pak"
    for ext in ("pak", "ucas", "utoc"):
        (BUILD / f"{MOD_NAME}.{ext}").unlink(missing_ok=True)
    r = run([REPAK, "pack", "--version", "V11", "--mount-point", "../../../", root, pak])
    if not pak.exists():
        raise SystemExit(f"repak failed: {r.stdout}{r.stderr}")
    r = run([RETOC, "to-zen", "--version", "UE5_4", root, BUILD / f"{MOD_NAME}.utoc"])
    if not (BUILD / f"{MOD_NAME}.utoc").exists():
        raise SystemExit(f"retoc failed: {r.stdout}{r.stderr}")
    listing = run([REPAK, "list", pak]).stdout.strip()
    print(f"\nbuilt {MOD_NAME}.pak/.ucas/.utoc  ({listing})")


def install() -> None:
    for ext in ("pak", "ucas", "utoc"):
        shutil.copy(BUILD / f"{MOD_NAME}.{ext}", PAKS / f"{MOD_NAME}.{ext}")
    print(f"installed to {PAKS}")


def uninstall() -> None:
    removed = 0
    for ext in ("pak", "ucas", "utoc"):
        target = PAKS / f"{MOD_NAME}.{ext}"
        if target.exists():
            target.unlink()
            removed += 1
    print(f"removed {removed} file(s) from {PAKS}")


NULL_BUILD = "--null" in sys.argv
ORIGINAL_BUILD = "--original" in sys.argv

if __name__ == "__main__":
    if "--uninstall" in sys.argv:
        uninstall()
    else:
        build()
        if "--install" in sys.argv:
            install()
        else:
            print("(pass --install to copy into the game's Paks folder)")
