#!/usr/bin/env python3
"""
otr_pack_master.py — Build a master .o2r archive from the best of all your OTR/O2R packs.

Usage:
    1. Use the browser (otr_picker_server.py) to select which pack wins for each conflict.
    2. Run:  python otr_pack_master.py
    3. Find your master archive at:  master_output/999_Master.o2r
       Drop it into your Ship of Harkinian mods folder.

Priority rules (highest first):
    1. choices.json  — explicit per-path override (set via the browser)
    2. Highest resolution — the archive with the most pixels wins unresolved conflicts
    3. First sorted  — tie-break when resolutions are equal or unknown

Requirements:
    pip install mpyq
"""

import mpyq
import zipfile
import json
import struct
import sys
from pathlib import Path
from collections import defaultdict


def read_image_dims(data):
    """Return (w, h) from SoH OTEX or plain PNG header bytes.  None if unrecognised."""
    if not data or len(data) < 24:
        return None
    if data[:4] == b'\x89PNG':
        return (struct.unpack_from('>I', data, 16)[0],
                struct.unpack_from('>I', data, 20)[0])
    if len(data) >= 0x4C and data[4:8] in (b'XETO', b'OTEX'):
        w = struct.unpack_from('<I', data, 0x44)[0]
        h = struct.unpack_from('<I', data, 0x48)[0]
        if w and h:
            return (w, h)
    return None

# ── Config ────────────────────────────────────────────────────────────────────
SCRIPT_DIR   = Path(__file__).parent
CHOICES_FILE = SCRIPT_DIR / "choices.json"
OUTPUT_DIR   = SCRIPT_DIR / "master_output"
OUTPUT_FILE  = OUTPUT_DIR / "999_Master.o2r"

def _load_cfg():
    cfg_path = SCRIPT_DIR / "config.json"
    if cfg_path.exists():
        try:
            c = json.loads(cfg_path.read_text(encoding='utf-8'))
            mods_dir       = c.get('mods_dir', '')
            base_game_file = c.get('base_game_file', '')
            return (
                Path(mods_dir)       if mods_dir       else None,
                Path(base_game_file) if base_game_file else None,
            )
        except Exception:
            pass
    return None, None

MODS_DIR, BASE_GAME_FILE = _load_cfg()
# ──────────────────────────────────────────────────────────────────────────────


def find_mods_dir():
    if MODS_DIR and MODS_DIR.exists():
        return MODS_DIR
    candidates = list(SCRIPT_DIR.glob("*.otr")) + list(SCRIPT_DIR.glob("*.o2r"))
    if candidates:
        return SCRIPT_DIR
    print(f"ERROR: Could not find mods folder.  Expected: {MODS_DIR}")
    print("Set it via the picker UI's Settings panel, or in config.json.")
    sys.exit(1)


def iter_archive(path):
    """Yield (internal_path_str, raw_bytes) for every file in an OTR or O2R."""
    if path.suffix.lower() == ".o2r":
        with zipfile.ZipFile(str(path)) as z:
            for name in z.namelist():
                yield name, z.read(name)
    else:
        try:
            arch = mpyq.MPQArchive(str(path))
            for f in (arch.files or []):
                if f and f != b"(listfile)":
                    name = f.decode("utf-8", errors="replace")
                    data = arch.read_file(f)
                    if data:
                        yield name, data
        except Exception as e:
            print(f"  WARNING: could not read {path.name}: {e}")


def pick_winner(path, sources, choices, path_dims):
    """
    Return the archive that wins for `path`.

    sources is sorted ascending (sources[0] = alphabetically first).

    1. choices.json explicit override
    2. Highest resolution (pixel count) wins
    3. First sorted as tie-break
    """
    if path in choices:
        chosen = choices[path]
        match = next((s for s in sources if s.name == chosen or s.stem == chosen), None)
        if match:
            return match
        print(f"  WARNING: chosen pack '{chosen}' not found for {path!r}, falling through.")

    # Resolution fallback: prefer the highest-pixel-count source.
    # sources[0] is already the alphabetical first-sorted, so it wins on ties.
    best_px   = 0
    best_arch = sources[0]
    for s in sources:
        d  = path_dims.get((path, s))
        px = d[0] * d[1] if d else 0
        if px > best_px:
            best_px   = px
            best_arch = s
    return best_arch


def run(log=print, mods_dir=None, base_game_file=None):
    if mods_dir       is None: mods_dir       = MODS_DIR
    if base_game_file is None: base_game_file = BASE_GAME_FILE
    if not mods_dir:
        raise RuntimeError(
            "No mods folder configured. Set it via the picker UI's Settings panel, "
            "or in config.json."
        )
    mods_dir       = Path(mods_dir)
    base_game_file = Path(base_game_file) if base_game_file else None

    archives = sorted(p for p in mods_dir.iterdir()
                      if p.suffix.lower() in (".otr", ".o2r"))
    # Include base game so 'oot' choices resolve correctly
    if base_game_file and base_game_file.exists() and base_game_file not in archives:
        archives = [base_game_file] + archives

    if not archives:
        log(f"No .otr/.o2r files found in {mods_dir}")
        sys.exit(1)

    log(f"Found {len(archives)} archives in {mods_dir}:")
    for a in archives:
        log(f"  {a.name}")

    # Load choices
    choices = {}
    if CHOICES_FILE.exists():
        choices = json.loads(CHOICES_FILE.read_text())
        log(f"\nLoaded {len(choices)} choices from {CHOICES_FILE.name}")
    else:
        log(f"\nNo {CHOICES_FILE.name} found — first-sorted wins all conflicts.")

    # ── Pass 1: scan all archives ─────────────────────────────────────────────
    log("\nScanning archives...")
    path_sources = defaultdict(list)   # path -> [arch, arch, …] in sorted order
    path_dims    = {}                   # (path, arch) -> (w, h)
    for arch_path in archives:
        log(f"  {arch_path.name}")
        for name, data in iter_archive(arch_path):
            path_sources[name].append(arch_path)
            d = read_image_dims(data)
            if d:
                path_dims[(name, arch_path)] = d

    conflicts = sum(1 for v in path_sources.values() if len(v) > 1)
    log(f"\nTotal unique paths : {len(path_sources)}")
    log(f"Conflicting paths  : {conflicts}")

    # ── Pass 2: resolve winners ───────────────────────────────────────────────
    winner_map   = {}
    choices_used = 0
    excluded     = 0

    for path, sources in path_sources.items():
        if choices.get(path) == '__exclude__':
            excluded += 1
            continue
        if len(sources) == 1:
            winner_map[path] = sources[0]
        else:
            winner = pick_winner(path, sources, choices, path_dims)
            winner_map[path] = winner
            if path in choices:
                choices_used += 1

    log(f"choices.json used  : {choices_used} paths")
    log(f"Excluded paths     : {excluded}")
    log(f"Highest-res wins   : {conflicts - choices_used} paths")

    # ── Pass 3: write the master archive ─────────────────────────────────────
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    target = OUTPUT_FILE
    if OUTPUT_FILE.exists():
        try:
            OUTPUT_FILE.unlink()
        except OSError:
            target = OUTPUT_DIR / "999_Master_new.o2r"
            log(f"  NOTE: {OUTPUT_FILE.name} is locked — writing to {target.name} instead.")
            if target.exists():
                try:
                    target.unlink()
                except OSError:
                    pass

    log(f"\nBuilding {target.name}  (this will take a few minutes)...")

    by_archive = defaultdict(list)
    for path, arch_path in winner_map.items():
        by_archive[arch_path].append(path)

    written = 0
    with zipfile.ZipFile(str(target), "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zout:
        for arch_path, paths in sorted(by_archive.items()):
            wanted = set(paths)
            file_data = {name: data for name, data in iter_archive(arch_path)
                         if name in wanted}
            for path in paths:
                data = file_data.get(path)
                if data:
                    zout.writestr(path, data)
                    written += 1

    size_mb = target.stat().st_size / 1024 / 1024
    log(f"Done!  Wrote {written} files → {target}  ({size_mb:.1f} MB)")
    return target


def main():
    target = run()
    print("Copy 999_Master.o2r into your Ship of Harkinian mods folder.")


if __name__ == "__main__":
    main()
