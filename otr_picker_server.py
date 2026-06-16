#!/usr/bin/env python3
"""
otr_picker_server.py — Graphical Texture Picker: native desktop browser/picker
for OTR/O2R texture packs. Renders entirely inside a pywebview window via
window.load_html() — there is no local network server and no port.

Usage:  python otr_picker_server.py

Requirements:  pip install mpyq Pillow filelock pywebview
"""

import json, struct, io, threading, sys, os, base64, re, html
from functools import lru_cache
import mpyq, zipfile
from pathlib import Path
from PIL import Image
from filelock import FileLock

# A --noconsole packaged build has no real stdout/stderr (they're None), and
# this codebase prints unicode status chars like ✓ — guard both cases so
# print() never crashes the app.
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w', encoding='utf-8')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w', encoding='utf-8')
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', line_buffering=True)
    except Exception:
        pass

# Windows tags every file extracted from a downloaded .zip with a hidden
# "Zone.Identifier" NTFS stream (Mark of the Web). The legacy .NET Framework
# loader pythonnet/clr_loader uses refuses to fully trust a flagged DLL,
# which breaks pywebview's winforms/edgechromium backend with a cryptic
# "Failed to resolve Python.Runtime.Loader.Initialize" error — every single
# person who downloads the packaged .exe from a GitHub release hits this.
# Strip the flag from our own bundled files before pythonnet ever loads.
if sys.platform == 'win32':
    _app_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) \
        else os.path.dirname(os.path.abspath(__file__))
    for _dirpath, _dirnames, _filenames in os.walk(_app_dir):
        for _fn in _filenames:
            try:
                os.remove(os.path.join(_dirpath, _fn) + ':Zone.Identifier')
            except OSError:
                pass

try:
    import webview
except ImportError:
    print("ERROR: pywebview is required. Install it with: pip install pywebview")
    sys.exit(1)

# Suppress a known-harmless pywebview exception: each js_api call (e.g. a
# lazy image fetch kicked off while scrolling) runs in its own throwaway
# thread, and tries to deliver its return value back to the document that
# called it. If the user has already navigated elsewhere (load_html() swaps
# the whole document) before that call resolves, the destination document
# no longer has the matching callback registered, and pywebview's delivery
# step throws a JavascriptException in that one disposable thread. It never
# breaks the app or the currently-displayed page — it's just abandoned work
# reporting back too late — but left unfiltered it floods the console.
_default_threading_excepthook = threading.excepthook

def _filtered_threading_excepthook(args):
    msg = str(args.exc_value)
    if '_returnValuesCallbacks' in msg and 'is not a function' in msg:
        return
    _default_threading_excepthook(args)

threading.excepthook = _filtered_threading_excepthook

# ── config ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR        = Path(__file__).parent
# In a packaged --onedir build, __file__ resolves inside _internal/ (the
# bundled resources folder), not next to the .exe. That's correct for
# read-only bundled assets (e.g. soh.png) but wrong for user data, which
# should live next to the .exe — discoverable, and not wiped out if a
# future update replaces _internal/ wholesale.
APP_DIR           = Path(sys.executable).parent if getattr(sys, 'frozen', False) else SCRIPT_DIR
CHOICES_FILE      = APP_DIR / "choices.json"
CHOICES_LOCK_FILE = APP_DIR / "choices.json.lock"
CONFIG_FILE       = APP_DIR / "config.json"

# ── Game definitions ───────────────────────────────────────────────────────────
# Add future games here; each entry needs a display name and logo filename.
GAME_DEFS = {
    'soh': {'name': 'Ship of Harkinian', 'image': 'soh.png'},
}

_SOH_DEFAULTS = {
    'mods_dir':       '',
    'base_game_file': '',
    'master_dir':     '',
}

# Fallback defaults per game key
_GAME_DEFAULTS = {'soh': _SOH_DEFAULTS}

def _load_path_config():
    games  = {}
    active = 'soh'
    if CONFIG_FILE.exists():
        try:
            c = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
            if 'games' in c:
                active = c.get('active_game', 'soh')
                games  = c.get('games', {})
            else:
                # Migrate flat old format into soh entry
                games['soh'] = {k: c[k] for k in ('mods_dir', 'base_game_file', 'master_dir') if k in c}
        except Exception:
            pass
    defs = _GAME_DEFAULTS.get(active, {})
    g    = games.get(active, {})
    def _p(v):
        return Path(v) if v else None
    return (
        _p(g.get('mods_dir',       defs.get('mods_dir',       ''))),
        _p(g.get('base_game_file',  defs.get('base_game_file',  ''))),
        _p(g.get('master_dir',      defs.get('master_dir',      ''))),
        active,
        games,
    )

def _save_config():
    CONFIG_FILE.write_text(
        json.dumps({'active_game': active_game, 'games': _games_config}, indent=2),
        encoding='utf-8',
    )

MODS_DIR, BASE_GAME_FILE, MASTER_DIR, active_game, _games_config = _load_path_config()

# ── Pack-master background job state ──────────────────────────────────────────
_pack_state = {'running': False, 'lines': [], 'done': False, 'error': None}
_pack_lock  = threading.Lock()

def _pack_worker(api):
    import shutil, traceback, otr_pack_master as _pm
    lines = _pack_state['lines']
    def _log(msg=''):
        lines.append(str(msg))
    try:
        if not MASTER_DIR:
            raise RuntimeError('No output destination set. Open Settings and choose an output folder.')
        target = _pm.run(log=_log, mods_dir=MODS_DIR, base_game_file=BASE_GAME_FILE)
        dest = MASTER_DIR / '999_Master.o2r'
        MASTER_DIR.mkdir(parents=True, exist_ok=True)
        _log(f'\nCopying {target.name} → {dest} ...')
        shutil.copy2(str(target), str(dest))
        _log('Done! 999_Master.o2r is in your mods folder.')
        _log('Refreshing master index...')
        api._master_file  = dest
        api._master_names = archive_names(dest)
        # _b64_png is keyed on (archive_path_str, internal_path) — the master
        # archive's path doesn't change across rebuilds even though its
        # content does, so any previously-viewed texture would otherwise stay
        # cached with stale bytes forever. Drop the whole cache; re-decoding
        # source-pack images again is cheap and only happens once per build.
        _b64_png.cache_clear()
        _log(f'Master index updated: {len(api._master_names):,} entries.')
        _pack_state['done'] = True
    except Exception as e:
        _log(f'\nERROR: {e}')
        _log(traceback.format_exc())
        _pack_state['error'] = str(e)
        _pack_state['done'] = True
    finally:
        _pack_state['running'] = False

# ── Archive rescan ─────────────────────────────────────────────────────────────
_rescan_state = {'running': False, 'done': True, 'error': None}
_rescan_lock  = threading.Lock()

def _do_scan(api):
    _rescan_state.update({'running': True, 'done': False, 'error': None})
    try:
        all_archives = []
        if MODS_DIR and MODS_DIR.exists():
            for path in sorted(MODS_DIR.iterdir()):
                if path.suffix.lower() in ('.otr', '.o2r') and path.name != '999_Master.o2r':
                    all_archives.append(path)
        if BASE_GAME_FILE and BASE_GAME_FILE.exists():
            all_archives.insert(0, BASE_GAME_FILE)

        print(f"Scanning {len(all_archives)} archives in {MODS_DIR}...")
        archive_names_cache = {}
        for arch in all_archives:
            archive_names_cache[arch] = archive_names(arch)
            print(f"  {arch.name}: {len(archive_names_cache[arch]):,} entries")

        print("Building image index...")
        image_paths_cache = {}
        image_dims_cache  = {}
        for arch in all_archives:
            img_paths = set()
            img_dims  = {}
            try:
                if arch.suffix.lower() == '.o2r':
                    with zipfile.ZipFile(str(arch)) as z:
                        for name in z.namelist():
                            with z.open(name) as f:
                                header = f.read(76)
                            if is_image_data(header):
                                img_paths.add(name)
                                d = read_image_dims(header)
                                if d:
                                    img_dims[name] = d
                else:
                    mq = mpyq.MPQArchive(str(arch))
                    for fn in (mq.files or []):
                        if not fn or fn == b'(listfile)': continue
                        data = mq.read_file(fn)
                        if data and is_image_data(data[:8]):
                            name = fn.decode('utf-8', 'replace')
                            if arch == BASE_GAME_FILE and not name.startswith('alt/'):
                                name = 'alt/' + name
                            img_paths.add(name)
                            d = read_image_dims(data)
                            if d:
                                img_dims[name] = d
            except Exception as e:
                print(f"  WARNING: image index failed for {arch.name}: {e}")
            image_paths_cache[arch] = frozenset(img_paths)
            image_dims_cache[arch]  = img_dims

        api._all_archives        = all_archives
        api._archive_names_cache = archive_names_cache
        api._image_paths_cache   = image_paths_cache
        api._image_dims_cache    = image_dims_cache
        api._mods_dir            = MODS_DIR

        master_path = (MASTER_DIR / '999_Master.o2r') if MASTER_DIR else None
        if master_path and master_path.exists():
            api._master_file  = master_path
            api._master_names = archive_names(master_path)
            print(f"Master: {len(api._master_names):,} entries")
        else:
            api._master_file  = None
            api._master_names = frozenset()
            print(f"WARNING: master not found at {master_path}")

        _rescan_state['done'] = True
    except Exception as e:
        import traceback as _tb
        print(f"Rescan error: {e}\n{_tb.format_exc()}")
        _rescan_state['error'] = str(e)
        _rescan_state['done']  = True
    finally:
        _rescan_state['running'] = False

# ── Djipi 3DS geometry/texture pack pairs ─────────────────────────────────────
# Maps geometry pack stem → paired texture pack stem.
# Geometry pack display lists reference mat_* paths that ONLY exist in the
# paired texture pack.  If the pair is missing, geometry renders borked.
DJIPI_PAIRS = {
    "Djipi's 3DE - 03 Objects Animals":           "Djipi's 3DE - 04 Objects Animals Textures (3DS)",
    "Djipi's 3DE - 05 Objects Inventory":          "Djipi's 3DE - 06 Objects Inventory Textures (3DS)",
    "Djipi's 3DE - 07 Objects Temples":            "Djipi's 3DE - 08 Objects Temples Textures(3DS)",
    "Djipi's 3DE - 09 Objects World":              "Djipi's 3DE - 10 Objects World Textures(3DS)",
    "Djipi's 3DE - 11 Objects NPC":                "Djipi's 3DE - 12 Objects NPC Textures(3DS)",
    "Djipi's 3DE - 13 Objects Ennemies":           "Djipi's 3DE - 14 Objects Ennemies Textures(3DS)",
    "Djipi's 3DE - 18 Objects ARIA 3DS(OPTIONAL)": "Djipi's 3DE - 19 Objects ARIA 3DS Textures (OPTIONAL)",
    "Djipi's 3DE - 24 Majora Chest (OPTIONAL)":    "Djipi's 3DE - 25 Majora Chest 3DS Textures (OPTIONAL)",
    "Djipi's 3DE - 26 Background 3DS":             "Djipi's 3DE - 27 Background Textures",
}

def enforce_geometry_rules(choices, all_archives, archive_names_cache, image_paths_cache):
    """
    Enforce consistency between image pack selections and Djipi 3DS geometry.

    Mode is determined by constrained paths — the union of images in the paired
    tex pack AND in the geo pack (self-referential) for this object.  This covers
    cases like Djipi 18/19 (object_ane) where the tex pack only has chickenlady_00_1
    but the geo pack's 3DS display lists also reference gCuccoLadyEye* with a
    different UV layout than OoT Reloaded's version.
    Geo packs with zero tex-pack image coverage for an object (e.g. Djipi 07/08
    for gameplay_dangeon_keep) produce an empty constrained set — those textures
    remain freely selectable since the geo is self-contained.

      Djipi mode    — at least one explicit mat_* choice uses the paired texture
                      pack (e.g. Djipi 08 for an object in Djipi 07's scope).
                      Geometry stays active. Standard-path choices are untouched.

      Non-Djipi mode — explicit mat_* choices exist but none use the paired tex
                      pack. Djipi geometry is auto-excluded so the base-game
                      model takes over and can read the user's image choices.

    Objects with no explicit mat_* choices are left alone; first-sorted defaults
    apply and no geometry decision is forced.

    Case 1: paired tex pack has zero image coverage for this object → geometry
    auto-excluded unconditionally (the mat_* paths wouldn't exist in the master).

    Returns (excl_added, cleared):
      excl_added — geometry paths newly set to __exclude__
      cleared    — always 0 (no image choices are deleted)
    """
    from collections import defaultdict

    stem_to_arch = {a.stem: a for a in all_archives}
    excl_added = 0
    cleared    = 0

    # Image paths in at least one non-Djipi archive.  A path that also appears
    # in the tex pack but IS covered here is freely swappable (e.g. gEyeSwitch*
    # in both Djipi 08 and OoT Reloaded → not constrained).
    all_djipi_stems = set(DJIPI_PAIRS.keys()) | set(DJIPI_PAIRS.values())
    non_djipi_imgs  = set()
    for arch, imgs in image_paths_cache.items():
        if arch.stem not in all_djipi_stems:
            non_djipi_imgs.update(imgs)

    for geom_stem, tex_stem in DJIPI_PAIRS.items():
        geom_arch = stem_to_arch.get(geom_stem)
        tex_arch  = stem_to_arch.get(tex_stem)
        if not geom_arch or not tex_arch:
            continue

        geom_all  = archive_names_cache.get(geom_arch, frozenset())
        geom_imgs = image_paths_cache.get(geom_arch, frozenset())
        tex_imgs  = image_paths_cache.get(tex_arch,  frozenset())

        # Group non-image paths (display lists, vertex buffers, skeletons)
        # by item prefix. Objects and overlays use 3-level paths
        # (alt/category/item_name/); scenes have a subcategory layer and use
        # 4-level paths (alt/scenes/subcategory/scene_name/).
        by_obj = defaultdict(list)
        for p in geom_all:
            if p in geom_imgs:
                continue
            parts = p.split('/')
            if parts[0] != 'alt':
                continue
            depth = 4 if (len(parts) > 2 and parts[1] == 'scenes') else 3
            if len(parts) > depth:
                obj_key = '/'.join(parts[:depth]) + '/'
                by_obj[obj_key].append(p)

        for obj_key, non_img_paths in by_obj.items():
            # Case 1: paired tex pack has zero image coverage for this item.
            # The geometry display lists would reference mat_* paths that don't
            # exist in the master → always auto-exclude regardless of user choices.
            if not any(p.startswith(obj_key) for p in tex_imgs):
                for p in non_img_paths:
                    if p not in choices:
                        choices[p] = '__exclude__'
                        excl_added += 1
                continue

            # Geometry-constrained paths: two components:
            # 1. tex_exclusive: images in the tex pack that NO non-Djipi archive
            #    provides (e.g. doorkagi_model__0, chickenlady_00_1).  These must
            #    come from the Djipi tex pack.
            # 2. geo_only: images in the geo pack that the tex pack did NOT upgrade
            #    (i.e. in geom_imgs but NOT in tex_imgs).  The geo pack's 3DS display
            #    lists self-reference these at Djipi-specific UV coordinates, so using
            #    a non-Djipi version (e.g. OoT Reloaded) causes rendering artifacts.
            #    Example: Djipi 18/19 — tex pack only has chickenlady_00_1, but the
            #    3DS Cucco Lady model also reads gCuccoLadyEye* at ARIA-specific UVs.
            # Paths that the tex pack covers AND non-Djipi archives also provide
            # (e.g. gEyeSwitch* in both Djipi 08 and OoT Reloaded) are left free —
            # the tex pack's version uses the same UV space, so any compatible pack
            # works and the user's preference (e.g. OoT Reloaded) should be respected.
            obj_tex = {p for p in tex_imgs  if p.startswith(obj_key)}
            obj_geo = {p for p in geom_imgs if p.startswith(obj_key)}
            tex_exclusive = obj_tex - non_djipi_imgs   # Djipi-only tex paths
            geo_only      = obj_geo - obj_tex          # geo self-ref'd, tex didn't upgrade
            geo_constrained = tex_exclusive | geo_only
            if not geo_constrained:
                continue

            mat_choices = {
                k: v for k, v in choices.items()
                if k in geo_constrained and v != '__exclude__'
            }
            if not mat_choices:
                continue  # No explicit choices on constrained paths — defaults are safe

            if tex_stem not in mat_choices.values():
                # Non-Djipi mode: constrained paths are set to non-Djipi packs →
                # Djipi 3DS geometry can't find its textures → exclude it.
                for p in non_img_paths:
                    if p not in choices:
                        choices[p] = '__exclude__'
                        excl_added += 1
            # Djipi mode (tex_stem in mat_choices.values()): geometry active; nothing to change.

    return excl_added, cleared


def compute_geo_builds(obj_key, all_archives, archive_names_cache, image_paths_cache, choices):
    """
    Return (builds, all_geo_paths, off_active) for obj_key.

    builds      — list of dicts, one per Djipi geo pack that has geometry for this object:
                    geo_stem      : pack stem string
                    tex_stem      : paired tex-pack stem
                    geo_paths     : frozenset of non-image paths in this geo pack for obj_key
                    constrained   : {image_path → preferred_stem}
                                    tex_exclusive paths → tex_stem
                                    geo_only paths      → geo_stem (self-referential)
                    active        : True when all geo_paths in choices point to geo_stem
    all_geo_paths — union of all geo_paths across all builds
    off_active    — True when all geo_paths are __exclude__ in choices
    """
    stem_to_arch    = {a.stem: a for a in all_archives}
    all_djipi_stems = set(DJIPI_PAIRS.keys()) | set(DJIPI_PAIRS.values())
    non_djipi_imgs  = set()
    for arch, imgs in image_paths_cache.items():
        if arch.stem not in all_djipi_stems:
            non_djipi_imgs.update(imgs)

    builds        = []
    all_geo_paths = set()

    for geo_stem, tex_stem in DJIPI_PAIRS.items():
        geo_arch = stem_to_arch.get(geo_stem)
        tex_arch = stem_to_arch.get(tex_stem)
        if not geo_arch or not tex_arch:
            continue
        geo_all  = archive_names_cache.get(geo_arch, frozenset())
        geo_imgs = image_paths_cache.get(geo_arch, frozenset())
        tex_imgs = image_paths_cache.get(tex_arch, frozenset())

        geo_paths = frozenset(
            p for p in geo_all
            if p.startswith(obj_key) and p not in geo_imgs
        )
        if not geo_paths:
            continue

        all_geo_paths.update(geo_paths)

        obj_tex      = frozenset(p for p in tex_imgs  if p.startswith(obj_key))
        obj_geo_imgs = frozenset(p for p in geo_imgs  if p.startswith(obj_key))
        geo_only = obj_geo_imgs - obj_tex

        # All tex-pack images are constrained: 3DS geometry needs its paired tex-pack
        # textures regardless of whether they also exist in OoT Reloaded.
        # (tex_exclusive = tex-only paths; tex_shared = also in OoT Reloaded — both need pairing)
        constrained = {}
        for p in obj_tex:  constrained[p] = tex_stem
        for p in geo_only: constrained[p] = geo_stem

        builds.append({
            'geo_stem':   geo_stem,
            'tex_stem':   tex_stem,
            'geo_paths':  geo_paths,
            'constrained': constrained,
        })

    all_geo_paths = frozenset(all_geo_paths)

    # Active detection: ALL geo_paths that have a choices entry must point to geo_stem
    for b in builds:
        in_choices = [p for p in b['geo_paths'] if p in choices]
        b['active'] = bool(in_choices) and all(
            choices[p] == b['geo_stem'] for p in in_choices
        )

    off_active = bool(all_geo_paths) and all(
        choices.get(p) == '__exclude__' for p in all_geo_paths
    )

    return builds, all_geo_paths, off_active


def apply_geo_build(obj_key, geo_stem, all_archives, archive_names_cache,
                    image_paths_cache, choices):
    """
    Write geometry-build choices into `choices` in-place.

    geo_stem == '__off__'  → exclude all geo paths for every build on this object
    otherwise              → activate that build's geo paths; exclude unique geo paths
                             from other builds; set constrained texture paths.

    Returns the number of keys changed.
    """
    builds, all_geo_paths, _ = compute_geo_builds(
        obj_key, all_archives, archive_names_cache, image_paths_cache, choices
    )
    changed = 0

    selected = next((b for b in builds if b['geo_stem'] == geo_stem), None)

    if geo_stem == '__off__':
        all_djipi_stems = set(DJIPI_PAIRS.keys()) | set(DJIPI_PAIRS.values())
        geo_path_set = set(all_geo_paths)

        # Build set of image paths available in non-Djipi packs (e.g. OoT Reloaded)
        non_djipi_imgs = set()
        for arch, imgs in image_paths_cache.items():
            if arch.stem not in all_djipi_stems:
                non_djipi_imgs.update(imgs)

        # 1. Exclude all 3DS geometry (non-image) paths
        for p in all_geo_paths:
            if choices.get(p) != '__exclude__':
                choices[p] = '__exclude__'
                changed += 1
        # 2. Clean up image-path choices for this object:
        #    - Djipi choice on path also in OoT Reloaded → delete; first-sorted picks OoT Reloaded
        #    - Djipi choice on Djipi-only path (e.g. chickenlady_00_1) → __exclude__
        #    - Stale __exclude__ on path OoT Reloaded has → delete (stale from old bug)
        #    - __exclude__ on Djipi-only path (e.g. ARIA mat_*) → leave alone
        for k in list(choices.keys()):
            if k.startswith(obj_key) and k not in geo_path_set:
                v = choices[k]
                if v in all_djipi_stems:
                    if k in non_djipi_imgs:
                        del choices[k]          # also in OoT Reloaded → let it win
                    else:
                        choices[k] = '__exclude__'  # Djipi-only → exclude for base game
                    changed += 1
                elif v == '__exclude__' and k in non_djipi_imgs:
                    del choices[k]              # stale exclude; OoT Reloaded should win
                    changed += 1
    elif selected:
        # Activate selected build
        for p in selected['geo_paths']:
            if choices.get(p) != geo_stem:
                choices[p] = geo_stem
                changed += 1
        # Exclude paths unique to OTHER builds
        for b in builds:
            if b['geo_stem'] == geo_stem:
                continue
            for p in b['geo_paths'] - selected['geo_paths']:
                if choices.get(p) != '__exclude__':
                    choices[p] = '__exclude__'
                    changed += 1
        # Set constrained texture paths
        for p, stem in selected['constrained'].items():
            if choices.get(p) != stem:
                choices[p] = stem
                changed += 1

    return changed


# ── image decoding ─────────────────────────────────────────────────────────────
def decode_rgba16(raw, w, h):
    px = bytearray()
    for i in range(0, min(w*h*2, len(raw)), 2):
        word = struct.unpack_from('>H', raw, i)[0]
        px += bytes([((word>>11)&31)*255//31, ((word>>6)&31)*255//31,
                     ((word>>1)&31)*255//31, (word&1)*255])
    return bytes(px)

def soh_to_png_bytes(data):
    if not data:
        return None
    # Raw PNG passthrough (Djipi 3DS exports store textures as plain PNG inside MPQ)
    if data[:4] == b'\x89PNG':
        return data
    # OTEX / SOH texture format (used by OoT Reloaded and Djipi packs)
    # OTR v1 layout (Djipi): 0x40=texType, 0x44=w, 0x48=h, 0x4C=0, 0x50/0x54=scale floats,
    #                         0x58=dataSize, 0x5C=pixels
    # OTR v2 layout (alternate): 0x44=w, 0x48=h, 0x4C=dataSize (non-zero, non-1), 0x50=pixels
    if len(data) >= 0x60 and data[4:8] in (b'XETO', b'OTEX'):
        w    = struct.unpack_from('<I', data, 0x44)[0]
        h    = struct.unpack_from('<I', data, 0x48)[0]
        flag = struct.unpack_from('<I', data, 0x4C)[0]
        if flag in (0, 1):
            # OTR v1: DataSize at 0x58, pixels at 0x5C
            raw_size = struct.unpack_from('<I', data, 0x58)[0]
            pixels   = data[0x5C:0x5C+raw_size]
        else:
            # OTR v2: flag IS the dataSize, pixels at 0x50
            raw_size = flag
            pixels   = data[0x50:0x50+raw_size]
        if w and h and pixels:
            bpp = raw_size / (w * h) if w * h else 0
            tex_type = struct.unpack_from('<I', data, 0x40)[0] if len(data) > 0x44 else 0xFF
            try:
                if bpp == 4:
                    img = Image.frombytes('RGBA', (w, h), pixels)
                elif bpp == 2:
                    img = Image.frombytes('RGBA', (w, h), decode_rgba16(pixels, w, h))
                elif bpp == 1:
                    px = bytearray()
                    # tex_type 5 or 13 → IA8 (4-bit intensity + 4-bit alpha)
                    if tex_type in (5, 13):
                        for b in pixels:
                            i = ((b >> 4) & 0xF) * 17
                            a = (b & 0xF) * 17
                            px += bytes([i, i, i, a])
                    else:   # I8 / CI8 → grayscale, fully opaque
                        for b in pixels: px += bytes([b, b, b, 255])
                    img = Image.frombytes('RGBA', (w, h), bytes(px))
                elif bpp == 0.5:
                    # 4-bit texture (I4, IA4, CI4) — 2 pixels per byte
                    px = bytearray()
                    if tex_type in (6, 16):   # IA4: 3-bit intensity + 1-bit alpha
                        for byte in pixels:
                            for nib in ((byte >> 4) & 0xF, byte & 0xF):
                                i = (nib >> 1) * 255 // 7
                                a = (nib & 1) * 255
                                px += bytes([i, i, i, a])
                    else:                     # I4 or CI4: treat as 4-bit grayscale
                        for byte in pixels:
                            hi = (byte >> 4) & 0xF
                            lo = byte & 0xF
                            px += bytes([hi*17, hi*17, hi*17, 255,
                                         lo*17, lo*17, lo*17, 255])
                    img = Image.frombytes('RGBA', (w, h), bytes(px))
                else:
                    img = None
                if img:
                    buf = io.BytesIO()
                    img.save(buf, format='PNG')
                    return buf.getvalue()
            except:
                pass
    # Fallback: try Pillow for any other image format (DDS, BMP, TGA, etc.)
    try:
        img = Image.open(io.BytesIO(data)).convert('RGBA')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return buf.getvalue()
    except:
        pass
    return None

def read_image_dims(data):
    """Return (w, h) from SoH OTEX or plain PNG header bytes.  Returns None if unrecognised."""
    if not data or len(data) < 24:
        return None
    if data[:4] == b'\x89PNG':   # plain PNG (Djipi 3DS exports)
        return (struct.unpack_from('>I', data, 16)[0],
                struct.unpack_from('>I', data, 20)[0])
    if len(data) >= 0x4C and data[4:8] in (b'XETO', b'OTEX'):  # SoH OTEX
        w = struct.unpack_from('<I', data, 0x44)[0]
        h = struct.unpack_from('<I', data, 0x48)[0]
        if w and h:
            return (w, h)
    return None

# ── archive utilities ──────────────────────────────────────────────────────────
def archive_names(path):
    """Return frozenset of all internal path names in an archive (no data read)."""
    if path.suffix.lower() == '.o2r':
        try:
            with zipfile.ZipFile(str(path)) as z:
                return frozenset(z.namelist())
        except Exception as e:
            print(f"  WARNING {path.name}: {e}")
            return frozenset()
    else:
        try:
            arch = mpyq.MPQArchive(str(path))
            return frozenset(
                f.decode("utf-8", "replace")
                for f in (arch.files or [])
                if f and f != b"(listfile)"
            )
        except Exception as e:
            print(f"  WARNING {path.name}: {e}")
            return frozenset()

def is_image_data(data):
    """Quick header check — no full decode needed."""
    if not data or len(data) < 8:
        return False
    if data[:4] == b'\x89PNG':
        return True
    if data[4:8] in (b'XETO', b'OTEX'):
        return True
    return False

def read_from_archive(path, internal_name):
    """Read one file from an archive by internal path. Returns bytes or None."""
    if path.suffix.lower() == '.o2r':
        try:
            with zipfile.ZipFile(str(path)) as z:
                if internal_name in z.namelist():
                    return z.read(internal_name)
        except Exception as e:
            print(f"  read_from_archive zip {path.name!r}: {e}")
    else:
        try:
            arch = mpyq.MPQArchive(str(path))
            # Try direct lookup (encoded string)
            data = arch.read_file(internal_name.encode())
            if data:
                return data
            # Fallback: iterate and match using raw bytes keys (handles any encoding edge cases)
            for f in (arch.files or []):
                if f and f != b"(listfile)":
                    if f.decode("utf-8", "replace") == internal_name:
                        data = arch.read_file(f)
                        if data:
                            return data
        except Exception as e:
            print(f"  read_from_archive mpq {path.name!r} / {internal_name!r}: {e}")
    return None

def _res_winner(path, sorted_archives, image_paths_cache, image_dims_cache):
    """Return stem of the best archive for path: highest pixel-count first, then first-sorted."""
    candidates = []
    for a in sorted_archives:
        if path in image_paths_cache.get(a, frozenset()):
            d = image_dims_cache.get(a, {}).get(path)
            candidates.append((a, d[0] * d[1] if d else 0))
    if not candidates:
        return '__none__'
    best_px = max(px for _, px in candidates)
    if best_px > 0:
        return min((a for a, px in candidates if px == best_px), key=lambda a: a.stem).stem
    return candidates[0][0].stem  # all dims unknown → first-sorted

# ── image decode cache ─────────────────────────────────────────────────────────
@lru_cache(maxsize=4000)
def _b64_png(archive_path_str, internal_path):
    """Decode one texture to a base64 PNG string. Cached since repeat scroll
    past the same thumbnail is common and archive reads aren't free."""
    raw = read_from_archive(Path(archive_path_str), internal_path)
    png = soh_to_png_bytes(raw) if raw else None
    return base64.b64encode(png).decode('ascii') if png else None

@lru_cache(maxsize=None)
def _icon_data_uri(filename):
    """Base64-inline a small static icon (e.g. game logos) once at first use."""
    if not filename:
        return ''
    p = SCRIPT_DIR / filename
    if not p.exists() or p.suffix.lower() != '.png':
        return ''
    return 'data:image/png;base64,' + base64.b64encode(p.read_bytes()).decode('ascii')

# ── pywebview bridge ───────────────────────────────────────────────────────────
class Api:
    def __init__(self):
        self._window             = None
        self._mods_dir             = None
        self._master_file          = None         # Path to 999_Master.o2r (or None if not found)
        self._master_names         = frozenset()  # All internal paths in the master archive
        self._all_archives         = []           # All archives in MODS_DIR, sorted
        self._archive_names_cache  = {}            # archive_path → frozenset of internal names
        self._image_paths_cache    = {}            # archive_path → frozenset of paths with image data
        self._image_dims_cache     = {}            # archive_path → {internal_path: (w, h)}

    # ── images (called lazily by the IntersectionObserver bootstrap in JS) ─────
    def get_master_image(self, path):
        if not self._master_file or not path:
            return {'ok': False}
        data = _b64_png(str(self._master_file), path)
        return {'ok': True, 'data': data} if data else {'ok': False}

    def get_pack_image(self, archive_name, path):
        arch = next((p for p in self._all_archives if p.name == archive_name), None)
        if not arch or not path:
            return {'ok': False}
        # Base game stores paths without 'alt/' — strip it before reading
        read_path = path[4:] if (arch == BASE_GAME_FILE and path.startswith('alt/')) else path
        data = _b64_png(str(arch), read_path)
        return {'ok': True, 'data': data} if data else {'ok': False}

    # ── navigation: render full HTML and swap the document directly ────────────
    def go_master_browse(self, obj='', ptype='objects', q='', page=1):
        try:
            html_out = self.render_master_browse(obj, ptype, q, page)
        except Exception as e:
            import traceback
            html_out = f'<pre>render_master_browse error: {e}\n{traceback.format_exc()}</pre>'
        # Deferred: js_bridge_call's worker thread still needs to deliver this
        # call's return value back to the *current* (pre-navigation) document
        # via evaluate_js() right after this method returns. Swapping the
        # document synchronously here means that delivery lands on the new,
        # unrelated document and throws (window.pywebview._returnValuesCallbacks
        # lookup fails) — defer the swap so that delivery finishes first. The
        # resulting exception is harmless either way (filtered out by our
        # threading.excepthook below) but deferring avoids it being raised at all.
        #
        # Before the very first load_html() ever happens, also make sure the
        # GUI message loop has actually finished initializing (window.events
        # .loaded fires once the initial _LOADING_HTML has rendered) — without
        # this, a fast path (e.g. zero archives configured, so _do_scan
        # returns near-instantly) can call load_html() before the native
        # window is ready to receive a cross-thread call, silently dropping
        # it and leaving the loading screen stuck forever. Slower paths never
        # hit this race only by accident (the scan itself takes long enough
        # for the window to become ready), so wait explicitly instead of
        # relying on that.
        self._window.events.loaded.wait(timeout=10)
        threading.Timer(0.05, self._window.load_html, args=(html_out,)).start()
        return {'ok': True}

    def render_master_browse(self, obj='', ptype='objects', q='', page=1):
            # Browse 999_Master.o2r with side-by-side comparison against source packs.
            selected = obj or ''
            ptype    = ptype or 'objects'
            q        = (q or '').strip().lower()

            PER_PAGE = 50
            try:
                page = max(1, int(page))
            except (TypeError, ValueError):
                page = 1

            # All non-empty categories present in the master
            all_types = sorted(set(
                p.split('/')[1] for p in self._master_names
                if p.startswith('alt/') and p.count('/') >= 2
            ))

            prefix  = f'alt/{ptype}/'
            # Scenes have an extra subcategory level: alt/scenes/subcategory/scene_name/
            # Show "subcategory/scene_name" in the sidebar so each scene is selectable.
            # All other categories use the simpler alt/category/item_name/ structure.
            if ptype == 'scenes':
                objects = sorted(set(
                    '/'.join(p.split('/')[2:4]) for p in self._master_names
                    if p.startswith(prefix) and p.count('/') >= 4
                ))
            else:
                objects = sorted(set(
                    p.split('/')[2] for p in self._master_names
                    if p.startswith(prefix) and p.count('/') >= 3
                ))

            type_nav = ''.join(
                f'<button class="type-nav-btn" data-type="{html.escape(t)}" '
                f'style="background:none;border:none;cursor:pointer;font:inherit;'
                f'color:{"#e94560" if t == ptype else "#7ec8e3"};font-size:12px;'
                f'text-decoration:none;padding:2px 8px;border-radius:4px;'
                f'{"background:#1a0010;" if t == ptype else ""}">{html.escape(t)}</button>'
                for t in all_types
            )
            sidebar = '\n'.join(
                f'<div class="obj-link" data-obj="{html.escape(o)}" style="padding:3px 6px;cursor:pointer;'
                f'{"font-weight:bold;color:#e94560;" if o == selected else ""}"'
                f'>{html.escape(o)}</div>'
                for o in objects
            )

            def pack_label(arch):
                n = arch.stem
                if n == 'oot':           return '🕹 Base Game'
                if 'OoT_Reloaded' in n: return '🌟 OoT Reloaded'
                if '08_Art' in n:        return '⚔ Dark Link'
                if n.startswith('06_'):  return '🎒 06 Items'
                if n.startswith('07_'):  return '🎒 07 Items'
                m = re.search(r'3DE\s*-\s*(\d+)\s+(.+)', n)
                if m:
                    num  = m.group(1)
                    name = m.group(2).replace('Objects ', '').replace(' (OPTIONAL)', '').replace('3DS', '').replace('Textures', 'Tex').strip()
                    return f'Djipi {num}: {name[:22]}'
                return n[:28]

            # Default values used in JS template even when no object is selected
            geo_build_html  = ''

            if selected:
                choices_now_geo = json.loads(CHOICES_FILE.read_text()) if CHOICES_FILE.exists() else {}
                _stem_to_arch = {a.stem: a for a in self._all_archives}
                _obj_key = f'alt/{ptype}/{selected}/'

                # ── Geometry build selector ────────────────────────────────────
                _geo_builds, _all_geo_paths, _off_active = compute_geo_builds(
                    _obj_key, self._all_archives,
                    self._archive_names_cache, self._image_paths_cache,
                    choices_now_geo,
                )
                geo_build_html = ''
                if _geo_builds:
                    def _gb_label(stem):
                        m = re.search(r'3DE\s*-\s*(\d+)\s+(.+)', stem)
                        if m:
                            num  = m.group(1)
                            name = (m.group(2)
                                    .replace('Objects ', '').replace(' (OPTIONAL)', '')
                                    .replace('3DS', '').replace('Textures', 'Tex').strip())
                            return f'Djipi {num}: {name[:20]}'
                        return stem[:24]

                    def _gb_btn(label, stem, active, extra_style=''):
                        if active:
                            style = ('background:#0a2a0a;border:2px solid #4caf50;'
                                     'color:#4caf50;font-weight:700;')
                        else:
                            style = ('background:#111;border:1px solid #333;'
                                     'color:#888;')
                        style += ('padding:4px 12px;border-radius:4px;cursor:pointer;'
                                  'font-size:11px;font-family:monospace;' + extra_style)
                        applied = '1' if active else '0'
                        return (f'<button class="geo-btn" data-geo-stem="{html.escape(stem)}" '
                                f'data-applied="{applied}" style="{style}">'
                                f'{label}{"  ✓" if active else ""}</button>')

                    btns = [_gb_btn('Base Game', '__off__', _off_active)]
                    for _b in _geo_builds:
                        btns.append(_gb_btn(_gb_label(_b['geo_stem']),
                                            _b['geo_stem'], _b['active']))

                    geo_build_html = (
                        '<div style="margin-bottom:8px;padding:8px 14px;'
                        'background:#0d0d1a;border:1px solid #2a2a4a;border-radius:6px;'
                        'display:flex;align-items:center;gap:8px;flex-wrap:wrap">'
                        '<span style="color:#555;font-size:11px;font-weight:600;'
                        'text-transform:uppercase;letter-spacing:.05em;'
                        'white-space:nowrap">Geometry Build</span>'
                        + ''.join(btns) +
                        '</div>'
                    )

                # Union of master paths + all source pack paths for this object
                master_obj_paths = frozenset(p for p in self._master_names if f'/{selected}/' in p)
                all_obj_paths = set(master_obj_paths)
                for arch in self._all_archives:
                    for n in self._archive_names_cache.get(arch, frozenset()):
                        if f'/{selected}/' in n:
                            all_obj_paths.add(n)
                obj_paths = sorted(all_obj_paths)

                # Packs that have ANY path containing /{selected}/
                relevant_packs = [
                    arch for arch in self._all_archives
                    if any(f'/{selected}/' in n
                           for n in self._archive_names_cache.get(arch, frozenset()))
                ]

                # Column headers
                th_master  = '<th style="background:#1a3a1a;color:#4caf50;min-width:130px">✓ MASTER</th>'
                th_exclude = '<th style="background:#1a0a0a;color:#c0392b;min-width:48px">✕</th>'
                th_packs  = ''.join(
                    f'<th class="pack-col-hdr" data-arch="{a.stem}" style="min-width:130px;font-size:11px">'
                    f'{pack_label(a)}<span class="col-hint">▣ click to select all</span></th>'
                    for a in relevant_packs
                )

                # Filter to image-only paths first, then paginate
                master_img_set = self._image_paths_cache.get(self._master_file, frozenset())
                filtered_paths = []
                for p in obj_paths:
                    if q and q not in p.lower():
                        continue
                    in_m = p in master_obj_paths
                    if in_m and p in master_img_set:
                        filtered_paths.append(p)
                        continue
                    if any(p in self._image_paths_cache.get(arch, frozenset())
                           for arch in relevant_packs
                           if p in self._archive_names_cache.get(arch, frozenset())):
                        filtered_paths.append(p)

                total_rows  = len(filtered_paths)
                total_pages = max(1, (total_rows + PER_PAGE - 1) // PER_PAGE)
                page        = min(page, total_pages)
                page_paths  = filtered_paths[(page - 1) * PER_PAGE : page * PER_PAGE]

                # Committed winner per path — used for winner-graying in JS
                _sorted_rel = sorted(relevant_packs, key=lambda a: a.stem)
                committed_winners = {}
                for _pw in page_paths:
                    if _pw in choices_now_geo:
                        committed_winners[_pw] = choices_now_geo[_pw]
                    else:
                        committed_winners[_pw] = _res_winner(
                            _pw, _sorted_rel,
                            self._image_paths_cache, self._image_dims_cache
                        )

                rows = []
                for p in page_paths:
                    fname = p.split('/')[-1]
                    in_master = p in master_obj_paths
                    if in_master:
                        master_td = (
                            f'<td style="background:#0a1a0a;border:2px solid #4caf50;vertical-align:top;padding:4px">'
                            f'<img data-kind="master" data-path="{html.escape(p)}" '
                            f'style="max-width:180px;max-height:180px;image-rendering:pixelated;display:block;background:#0d0d1a"></td>'
                        )
                    else:
                        master_td = '<td style="background:#1a1a1a;border:1px solid #333;color:#444;text-align:center;vertical-align:middle;font-size:10px;padding:4px">not in<br>master</td>'
                    pack_tds = []
                    img_count = 1 if in_master else 0
                    for arch in relevant_packs:
                        if p in self._archive_names_cache.get(arch, frozenset()):
                            img_count += 1
                            pack_tds.append(
                                f'<td class="pick-cell" data-path="{p}" data-arch="{arch.stem}" style="vertical-align:top;padding:4px">'
                                f'<img data-kind="pack" data-archfile="{html.escape(arch.name)}" data-path="{html.escape(p)}" '
                                f'style="max-width:180px;max-height:180px;image-rendering:pixelated;display:block;background:#0d0d1a"></td>'
                            )
                        else:
                            pack_tds.append('<td style="color:#333;text-align:center;vertical-align:middle;font-size:20px">—</td>')

                    exclude_td = (
                        f'<td class="excl-cell" data-path="{p}" data-arch="__exclude__" '
                        f'style="vertical-align:middle;text-align:center;padding:4px;cursor:pointer;min-width:48px;'
                        f'color:#c0392b;font-size:18px;border:1px solid #3a1a1a;background:#1a0a0a" '
                        f'title="Exclude this asset from master">✕</td>'
                    )
                    rows.append(
                        f'<tr data-img-count="{img_count}" data-img-failed="0" data-path="{p}" data-committed-winner="{committed_winners[p]}">'
                        f'<td style="font-size:10px;font-family:monospace;max-width:180px;word-break:break-all;color:#666;vertical-align:top;padding:4px">{fname}</td>'
                        f'{master_td}{"".join(pack_tds)}{exclude_td}'
                        f'</tr>'
                    )

                def _page_btn_style():
                    return ('background:none;border:1px solid #2a2a4a;cursor:pointer;font:inherit;'
                            'color:#7ec8e3;text-decoration:none;padding:3px 10px;border-radius:4px')

                prev_btn = (f'<button class="page-btn" data-page="{page-1}" style="{_page_btn_style()}">&#8592; Prev</button>'
                            if page > 1 else
                            '<span style="color:#333;padding:3px 10px;border:1px solid #222;border-radius:4px">&#8592; Prev</span>')
                next_btn = (f'<button class="page-btn" data-page="{page+1}" style="{_page_btn_style()}">Next &#8594;</button>'
                            if page < total_pages else
                            '<span style="color:#333;padding:3px 10px;border:1px solid #222;border-radius:4px">Next &#8594;</span>')
                pag_row = (f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap">'
                           f'{prev_btn}'
                           f'<span style="color:#888;font-size:12px">Page {page} of {total_pages} &bull; '
                           f'{total_rows} entries &bull; showing {len(rows)}</span>'
                           f'{next_btn}'
                           + ''.join(
                               f'<button class="page-btn" data-page="{pg}" style="background:none;border:none;cursor:pointer;font:inherit;'
                               f'color:{"#e94560" if pg==page else "#7ec8e3"};'
                               f'font-size:11px;text-decoration:none;padding:2px 7px;border-radius:3px;'
                               f'{"background:#1a0010;" if pg==page else ""}">{pg}</button>'
                               for pg in range(max(1, page-4), min(total_pages, page+4)+1)
                           )
                           + '</div>')

                grid = (
                    f'<h3 style="color:#eee;margin-bottom:8px">{selected}</h3>'
                    f'<p style="color:#666;font-size:12px;margin-bottom:8px">Green column = what\'s in the master (loads in-game). Other columns = competing sources.</p>'
                    + (geo_build_html + '\n' if geo_build_html else '')
                    + pag_row +
                    f'<table style="border-collapse:collapse;font-size:12px">'
                    f'<tr><th>Name</th>{th_master}{th_packs}{th_exclude}</tr>'
                    + '\n'.join(rows)
                    + '</table>'
                    + '<div style="margin-top:12px">' + pag_row + '</div>'
                )
            elif q:
                # Global search: find all image paths matching q across every archive
                from collections import defaultdict as _dd
                hits    = _dd(list)   # obj -> [path, ...]
                obj_cat = {}          # obj -> category (objects / textures / scenes / …)
                seen = set()
                all_img_arches = list(self._all_archives) + ([self._master_file] if self._master_file else [])
                for arch in all_img_arches:
                    for p in self._image_paths_cache.get(arch, frozenset()):
                        if q in p.lower() and p not in seen:
                            seen.add(p)
                            parts = p.split('/')
                            obj = parts[2] if len(parts) >= 3 else '—'
                            hits[obj].append(p)
                            if obj not in obj_cat:
                                obj_cat[obj] = parts[1] if len(parts) >= 2 else 'objects'
                total_hits = sum(len(v) for v in hits.values())
                choices_now = json.loads(CHOICES_FILE.read_text()) if CHOICES_FILE.exists() else {}
                rows_html = []
                for obj in sorted(hits.keys()):
                    cat = obj_cat.get(obj, 'objects')
                    rows_html.append(
                        f'<tr><td colspan="5" style="background:#16213e;color:#7ec8e3;font-weight:bold;'
                        f'padding:5px 8px;font-size:12px">'
                        f'<span class="search-obj-link" data-cat="{html.escape(cat)}" data-obj="{html.escape(obj)}" '
                        f'style="color:#e94560;text-decoration:underline;cursor:pointer">{html.escape(obj)}</span>'
                        f'<span style="color:#444;font-size:10px;margin-left:8px">{html.escape(cat)}</span>'
                        f'</td></tr>'
                    )
                    for p in sorted(hits[obj]):
                        fname = p.split('/')[-1]
                        in_m  = p in self._master_names
                        choice = choices_now.get(p, '')
                        m_cell = ('<span style="color:#4caf50">✓ master</span>' if in_m
                                  else '<span style="color:#444">—</span>')
                        c_cell = (f'<span style="color:#e9a020">{choice}</span>' if choice
                                  else '<span style="color:#444">first-sorted</span>')
                        # Thumbnail: prefer master, else first source archive with this image
                        if in_m and self._master_file:
                            thumb_html = (
                                f'<img data-kind="master" data-path="{html.escape(p)}" '
                                f'style="max-width:80px;max-height:80px;image-rendering:pixelated;'
                                f'vertical-align:middle;display:block;margin:auto;background:#0d0d1a">'
                            )
                        else:
                            src_arch = next(
                                (a for a in self._all_archives
                                 if p in self._image_paths_cache.get(a, frozenset())),
                                None
                            )
                            thumb_html = (
                                f'<img data-kind="pack" data-archfile="{html.escape(src_arch.name)}" data-path="{html.escape(p)}" '
                                f'style="max-width:80px;max-height:80px;image-rendering:pixelated;'
                                f'vertical-align:middle;display:block;margin:auto;background:#0d0d1a">'
                                if src_arch else ''
                            )
                        rows_html.append(
                            f'<tr>'
                            f'<td style="padding:4px 8px;font-family:monospace;font-size:11px;color:#ddd">{fname}</td>'
                            f'<td style="padding:4px;text-align:center;min-width:90px">{thumb_html}</td>'
                            f'<td style="padding:4px 8px;font-size:10px;color:#888;max-width:300px;word-break:break-all">{p}</td>'
                            f'<td style="padding:4px 8px;font-size:11px;text-align:center">{m_cell}</td>'
                            f'<td style="padding:4px 8px;font-size:11px">{c_cell}</td>'
                            f'</tr>'
                        )
                grid = (
                    f'<p style="color:#e9a020;margin-bottom:12px">{total_hits} result(s) for <b>{q}</b> '
                    f'across {len(hits)} object(s)</p>'
                    f'<table style="border-collapse:collapse;font-size:12px;width:100%">'
                    f'<tr style="position:sticky;top:0;z-index:10">'
                    f'<th style="background:#1a1a2e;color:#7ec8e3;padding:5px 8px;border:1px solid #2a2a4a">Name</th>'
                    f'<th style="background:#1a1a2e;color:#7ec8e3;padding:5px 8px;border:1px solid #2a2a4a">Preview</th>'
                    f'<th style="background:#1a1a2e;color:#7ec8e3;padding:5px 8px;border:1px solid #2a2a4a">Path</th>'
                    f'<th style="background:#1a1a2e;color:#7ec8e3;padding:5px 8px;border:1px solid #2a2a4a">Master</th>'
                    f'<th style="background:#1a1a2e;color:#7ec8e3;padding:5px 8px;border:1px solid #2a2a4a">Choice</th>'
                    f'</tr>'
                    + '\n'.join(rows_html)
                    + '</table>'
                )
            else:
                grid = '<p style="color:#888;padding:20px">← Select an object to compare textures.</p>'

            master_size = f'{self._master_file.stat().st_size / 1024 / 1024:.0f} MB' if self._master_file and self._master_file.exists() else '?'
            needs_setup = MODS_DIR is None
            if needs_setup:
                setup_banner = (
                    '<div style="margin-bottom:12px;padding:12px 16px;background:#1a1400;'
                    'border:1px solid #e9a020;border-radius:6px;color:#e9a020;font-size:13px">'
                    '<b>Welcome!</b> No mods folder is set up yet. '
                    '<button onclick="openSettingsModal()" style="margin-left:6px;padding:4px 12px;'
                    'background:#e9a020;color:#000;border:none;border-radius:4px;cursor:pointer;'
                    'font-size:12px;font-weight:700">Open Settings</button></div>'
                )
            elif not self._master_file:
                setup_banner = (
                    '<div style="margin-bottom:12px;padding:10px 16px;background:#0d0d1a;'
                    'border:1px solid #2a2a4a;border-radius:6px;color:#888;font-size:12px">'
                    'No master archive built yet — make your selections, then use the '
                    '<b style="color:#4caf50">Pack Master</b> button to build one.</div>'
                )
            else:
                setup_banner = ''
            _ag = GAME_DEFS.get(active_game, {})
            _ag_img     = _ag.get('image', '')
            _ag_img_uri = _icon_data_uri(_ag_img)
            _ag_name    = _ag.get('name', 'Settings')
            out_html = f'''<!doctype html>
<html><head><meta charset="UTF-8"><title>Master Browser</title>
<style>
* {{ box-sizing:border-box; margin:0; padding:0; }}
html, body {{ height:100%; margin:0; overflow:hidden; }}
body {{ font-family:system-ui,sans-serif; background:#111; color:#ddd;
        display:flex; flex-direction:column; }}
h1 {{ color:#e94560; margin:0 0 2px; }}
.topbar {{ background:#1a1a2e; padding:10px 16px; border-bottom:1px solid #2a2a4a;
           display:flex; justify-content:space-between; align-items:baseline;
           flex-shrink:0; }}
.topbar a {{ color:#7ec8e3; font-size:12px; text-decoration:none; margin-left:14px; }}
.topbar a:hover {{ text-decoration:underline; }}
.meta {{ color:#888; font-size:12px; }}
.layout {{ display:flex; flex:1; overflow:hidden; }}
.sidebar {{ width:230px; flex-shrink:0; overflow-y:auto; border-right:1px solid #2a2a4a;
            padding:2px; font-family:monospace; font-size:12px; background:#0d0d1a; }}
.sidebar div {{ padding:3px 7px; cursor:pointer; }}
.sidebar div:hover {{ background:#1a1a3a; color:#fff; }}
.toggle {{ padding:5px 8px; background:#16213e; border-bottom:1px solid #2a2a4a;
           font-size:11px; color:#7ec8e3; cursor:pointer; text-align:center; }}
.toggle:hover {{ background:#1e2e50; }}
.content {{ flex:1; overflow:auto; padding:16px; }}
table {{ border-collapse:collapse; }}
th {{ background:#1a1a2e; color:#7ec8e3; padding:5px 8px; border:1px solid #2a2a4a; font-size:11px; position:sticky; top:0; z-index:10; }}
td {{ border:1px solid #2a2a4a; }}
.pick-cell {{ cursor:pointer; }}
.pick-cell:hover {{ outline:2px solid #e9a02066; }}
.pick-cell.sel {{ outline:3px solid #e9a020 !important; background:#2a1a00 !important; }}
.excl-cell {{ cursor:pointer; }}
.excl-cell:hover {{ background:#2a0a0a !important; color:#e74c3c !important; }}
.excl-cell.sel {{ outline:3px solid #c0392b !important; background:#2a0000 !important; color:#e74c3c !important; }}
.pick-cell.group-hover {{ outline:2px solid #e9a02044; background:#1a1200; }}
.group-badge {{ display:inline-block; font-size:9px; font-weight:700; letter-spacing:.04em;
                background:#2a1a00; color:#e9a020; border:1px solid #e9a02066;
                border-radius:3px; padding:1px 4px; margin-bottom:3px; }}
.pack-col-hdr {{ cursor:pointer; user-select:none; }}
.pack-col-hdr:hover {{ background:#2a2000 !important; color:#e9a020 !important; }}
.pack-col-hdr .col-hint {{ display:block; font-size:9px; color:#555; font-weight:400; margin-top:2px; }}
.pick-cell.dim,.excl-cell.dim {{ opacity:0.2; }}
.sub-bar {{ position:fixed; bottom:0; left:0; right:0; background:#1a1400; border-top:2px solid #e9a020;
            padding:10px 20px; display:flex; align-items:center; gap:14px; z-index:100; }}
.sub-btn {{ padding:8px 20px; border:none; border-radius:6px; cursor:pointer; font-size:13px; font-weight:700; }}
.sub-save {{ background:#e9a020; color:#000; }}
.sub-save:hover {{ background:#c88010; }}
.sub-clear {{ background:#333; color:#aaa; }}
.sub-clear:hover {{ background:#444; }}
.sub-info {{ color:#e9a020; font-size:13px; }}
</style>
</head><body>
<div class="topbar">
  <div>
    <h1>Master Archive Browser</h1>
    <div class="meta">{len(self._master_names):,} entries &bull; {master_size}</div>
  </div>
  <div style="display:flex;gap:6px;align-items:center;flex:1;max-width:400px;margin:0 20px">
    <input id="searchInput" type="text" value="{html.escape(q)}" placeholder="search all paths…"
           onkeydown="if(event.key==='Enter')doSearch()"
           style="flex:1;background:#0d0d1a;color:#ddd;border:1px solid #2a2a4a;border-radius:4px;
                  padding:5px 10px;font-size:12px;font-family:monospace;outline:none">
    <button onclick="doSearch()" style="background:#2a2a4a;color:#7ec8e3;border:none;border-radius:4px;
            padding:5px 12px;cursor:pointer;font-size:12px">Search</button>
    {'<button onclick="clearSearch()" style="background:none;border:none;cursor:pointer;color:#888;font-size:11px;text-decoration:none;white-space:nowrap">✕ clear</button>' if q else ''}
  </div>
  <div style="display:flex;align-items:center;gap:8px">
    {type_nav}
    <button onclick="openPackModal()"
            style="padding:5px 14px;background:#0a2a0a;border:1px solid #4caf50;color:#4caf50;
                   border-radius:4px;cursor:pointer;font-size:12px;font-weight:600;white-space:nowrap">
      Pack Master
    </button>
    <button onclick="openSettingsModal()"
            style="padding:5px 10px;background:#0d0d1a;border:1px solid #2a2a4a;color:#7ec8e3;
                   border-radius:4px;cursor:pointer;font-size:12px;font-weight:600;white-space:nowrap;
                   display:flex;align-items:center;gap:6px">
      {'<img src="' + _ag_img_uri + '" style="width:20px;height:20px;object-fit:contain">' if _ag_img_uri else '⚙'}
      {_ag_name}
    </button>
  </div>
</div>
<div class="layout">
  <div class="sidebar">
    <div style="padding:4px 6px;background:#0d0d1a;border-bottom:1px solid #2a2a4a;position:sticky;top:0;z-index:5">
      <input id="sideSearch" type="text" placeholder="filter objects…"
             style="width:100%;background:#111;color:#ddd;border:1px solid #2a2a4a;border-radius:4px;
                    padding:4px 6px;font-size:11px;font-family:monospace;outline:none"
             oninput="filterSidebar(this.value)">
    </div>
    <div class="toggle">{len(objects)} {ptype}</div>
    {sidebar}
  </div>
  <div class="content">
    {setup_banner}
    {grid}
  </div>
</div>
<div class="sub-bar" id="subBar" style="display:none">
  <span class="sub-info">&#9670; <b id="selCount">0</b> texture(s) selected</span>
  <div id="normalBarBtns" style="display:flex;gap:8px;align-items:center">
    <button class="sub-btn sub-save" onclick="submitSels()">Save to choices.json</button>
    <button class="sub-btn sub-clear" onclick="clearSels()">Clear all</button>
    <span style="color:#666;font-size:11px">Rebuild master after saving: python otr_pack_master.py</span>
  </div>
  <div id="previewBarBtns" style="display:none;gap:8px;align-items:center">
    <span style="color:#e9a020;font-size:11px;font-weight:600">⬡ Geometry preview: <span id="geoBuildLabel"></span></span>
    <button class="sub-btn sub-save" onclick="applyGeoBuild()">Apply Build</button>
    <button class="sub-btn sub-clear" onclick="cancelGeoBuild()">Cancel Preview</button>
  </div>
</div>
<script>
// Hide rows where every image fails to load (non-image assets).
// data-img-count is set server-side to the exact number of <img> tags in the row.
function checkRowImages(img) {{
  var row = img.closest('tr');
  if (!row) return;
  var failed = (+row.getAttribute('data-img-failed') || 0) + 1;
  row.setAttribute('data-img-failed', failed);
  if (failed >= (+row.getAttribute('data-img-count') || 1))
    row.style.display = 'none';
}}

// Sidebar filter
function filterSidebar(val) {{
  var term = val.toLowerCase();
  document.querySelectorAll('.sidebar > div:not(.toggle):not([style*="sticky"])').forEach(function(d) {{
    d.style.display = (!term || d.textContent.toLowerCase().includes(term)) ? '' : 'none';
  }});
}}

// Navigation: every "page change" re-renders server-side and swaps the
// whole document via window.load_html() — there is no HTTP server/URL bar.
let SELECTED = {json.dumps(selected)};
let PTYPE    = {json.dumps(ptype)};
let SEARCH_Q = {json.dumps(q)};
let PAGE     = {page};

function goObj(obj) {{
  pywebview.api.go_master_browse(obj, PTYPE, '', 1);
}}
function goType(t) {{
  pywebview.api.go_master_browse('', t, '', 1);
}}
function goPage(n) {{
  pywebview.api.go_master_browse(SELECTED, PTYPE, SEARCH_Q, n);
}}
function doSearch() {{
  var val = document.getElementById('searchInput').value;
  pywebview.api.go_master_browse('', 'objects', val, 1);
}}
function clearSearch() {{
  pywebview.api.go_master_browse('', 'objects', '', 1);
}}
function goSearchObj(cat, obj) {{
  pywebview.api.go_master_browse(obj, cat, SEARCH_Q, 1);
}}
function reloadCurrentView() {{
  pywebview.api.go_master_browse(SELECTED, PTYPE, SEARCH_Q, PAGE);
}}

document.querySelectorAll('.obj-link').forEach(function(d) {{
  d.addEventListener('click', function() {{ goObj(this.dataset.obj); }});
}});
document.querySelectorAll('.type-nav-btn').forEach(function(b) {{
  b.addEventListener('click', function() {{ goType(this.dataset.type); }});
}});
document.querySelectorAll('.page-btn').forEach(function(b) {{
  b.addEventListener('click', function() {{ goPage(parseInt(this.dataset.page, 10)); }});
}});
document.querySelectorAll('.search-obj-link').forEach(function(s) {{
  s.addEventListener('click', function() {{ goSearchObj(this.dataset.cat, this.dataset.obj); }});
}});
document.querySelectorAll('.geo-btn').forEach(function(b) {{
  b.addEventListener('click', function() {{ setGeoBuild(this.dataset.geoStem); }});
}});

// ── Lazy image loading via the pywebview bridge (no HTTP, no native lazy-load) ──
(function() {{
  let inFlight = 0;
  const MAX_INFLIGHT = 5;
  const queue = [];
  const timers = new WeakMap();

  function drain() {{
    if (inFlight >= MAX_INFLIGHT || queue.length === 0) return;
    const img = queue.shift();
    if (!img.isConnected) {{ drain(); return; }}
    inFlight++;
    let p;
    const kind = img.dataset.kind;
    if (kind === 'master') p = pywebview.api.get_master_image(img.dataset.path);
    else if (kind === 'pack') p = pywebview.api.get_pack_image(img.dataset.archfile, img.dataset.path);
    else {{ inFlight--; drain(); return; }}
    p.then(function(result) {{
      if (img.isConnected) {{
        if (result && result.ok) {{
          img.onerror = function() {{ this.style.display = 'none'; checkRowImages(this); }};
          img.src = 'data:image/png;base64,' + result.data;
        }} else {{
          img.style.display = 'none';
          checkRowImages(img);
        }}
      }}
    }}).catch(function() {{}}).finally(function() {{
      inFlight--;
      drain();
    }});
  }}

  function schedule(img) {{
    if (timers.has(img)) return;
    timers.set(img, setTimeout(function() {{
      timers.delete(img);
      queue.push(img);
      drain();
    }}, 120));
  }}
  function cancel(img) {{
    const t = timers.get(img);
    if (t) {{ clearTimeout(t); timers.delete(img); }}
  }}

  const observer = new IntersectionObserver(function(entries) {{
    entries.forEach(function(entry) {{
      if (entry.isIntersecting) schedule(entry.target);
      else cancel(entry.target);
    }});
  }}, {{rootMargin: '200px'}});

  // window.load_html() delivers this document via a fresh navigation; the
  // pywebview.api bridge may not be re-attached yet at the instant this
  // script runs (it's not gated on a user click like the nav functions are).
  function startObserving() {{
    document.querySelectorAll('img[data-kind]').forEach(function(img) {{ observer.observe(img); }});
  }}
  if (window.pywebview && window.pywebview.api) startObserving();
  else window.addEventListener('pywebviewready', startObserving);
}})();

let sels = {{}};
let pendingGeoBuild = null;  // {{stem, totalChanges}} while previewing a geo build

const OBJ_KEY = {json.dumps(_obj_key if selected else '')};
const NEEDS_SETUP = {json.dumps(needs_setup)};
if (NEEDS_SETUP) openSettingsModal();

// Winner graying: grey out every cell that won't be in the master
// given current committed choices + pending selections.
function updateGraying() {{
  document.querySelectorAll('tr[data-committed-winner]').forEach(function(row) {{
    const path   = row.dataset.path;
    const winner = (sels[path] !== undefined) ? sels[path] : row.dataset.committedWinner;
    row.querySelectorAll('.pick-cell').forEach(function(cell) {{
      cell.classList.toggle('dim', winner === '__exclude__' || cell.dataset.arch !== winner);
    }});
    row.querySelectorAll('.excl-cell').forEach(function(cell) {{
      cell.classList.toggle('dim', winner !== '__exclude__');
    }});
  }});
}}

// Pack groups: clicking any member selects all others in the group (same row).
const PACK_GROUPS = [
  ['Items Models','Items Textures'],          // Items
  ['Adult Link Model','Adult Link Textures'], // Adult Link
  ['Young Link Model','Young Link Textures'], // Young Link
];

function pairedCells(td) {{
  const arch = td.dataset.arch, row = td.closest('tr');
  if (!row) return [];
  const group = PACK_GROUPS.find(function(g) {{ return g.some(function(s) {{ return arch.includes(s); }}); }});
  if (!group) return [];
  const out = [];
  row.querySelectorAll('.pick-cell').forEach(function(el) {{
    if (el !== td && group.some(function(s) {{ return el.dataset.arch.includes(s); }}))
      out.push(el);
  }});
  return out;
}}

// Select a cell. exemptArchs = set of arch stems that should NOT be cleared
// (used so group members don't wipe each other out).
function selectCell(td, exemptArchs) {{
  document.querySelectorAll('.pick-cell').forEach(function(el) {{
    if (el.dataset.path === td.dataset.path &&
        !(exemptArchs && exemptArchs.has(el.dataset.arch))) {{
      el.classList.remove('sel');
      delete sels[el.dataset.path];
    }}
  }});
  sels[td.dataset.path] = td.dataset.arch;
  td.classList.add('sel');
}}

function selectGroup(cells) {{
  const exempt = new Set(cells.map(function(td) {{ return td.dataset.arch; }}));
  cells.forEach(function(td) {{ selectCell(td, exempt); }});
}}

function deselectCell(td) {{
  delete sels[td.dataset.path];
  td.classList.remove('sel');
}}

function refreshBar() {{
  const n = Object.keys(sels).length;
  document.getElementById('selCount').textContent = n;
  if (pendingGeoBuild) {{
    document.getElementById('subBar').style.display = 'flex';
    document.getElementById('normalBarBtns').style.display = 'none';
    document.getElementById('previewBarBtns').style.display = 'flex';
  }} else {{
    document.getElementById('subBar').style.display = n ? 'flex' : 'none';
    document.getElementById('normalBarBtns').style.display = n ? 'flex' : 'none';
    document.getElementById('previewBarBtns').style.display = 'none';
  }}
  updateGraying();
}}

// Stamp group badges onto cells that have partners in the same row
document.querySelectorAll('tr').forEach(function(row) {{
  const cells = Array.from(row.querySelectorAll('.pick-cell'));
  cells.forEach(function(td) {{
    const partners = pairedCells(td);
    if (partners.length > 0) {{
      const badge = document.createElement('div');
      badge.className = 'group-badge';
      badge.textContent = '⬡ LINKED';
      badge.title = 'Clicking this also selects ' + partners.length + ' linked pack(s)';
      td.insertBefore(badge, td.firstChild);
    }}
  }});
}});

document.querySelectorAll('.pick-cell').forEach(function(td) {{
  td.addEventListener('mouseenter', function() {{
    pairedCells(this).forEach(function(el) {{ el.classList.add('group-hover'); }});
  }});
  td.addEventListener('mouseleave', function() {{
    pairedCells(this).forEach(function(el) {{ el.classList.remove('group-hover'); }});
  }});
  td.addEventListener('click', function() {{
    // Manual click exits preview mode so user takes over
    if (pendingGeoBuild) {{
      pendingGeoBuild = null;
      document.getElementById('normalBarBtns').style.display = 'flex';
      document.getElementById('previewBarBtns').style.display = 'none';
    }}
    const deselecting = sels[this.dataset.path] === this.dataset.arch;
    if (deselecting) {{
      deselectCell(this);
      pairedCells(this).forEach(deselectCell);
    }} else {{
      const group = [this].concat(pairedCells(this));
      selectGroup(group);
    }}
    refreshBar();
  }});
}});

// Exclude cell click
document.querySelectorAll('.excl-cell').forEach(function(td) {{
  td.addEventListener('click', function() {{
    if (pendingGeoBuild) {{
      pendingGeoBuild = null;
      document.getElementById('normalBarBtns').style.display = 'flex';
      document.getElementById('previewBarBtns').style.display = 'none';
    }}
    var path = this.dataset.path;
    var wasExcluded = sels[path] === '__exclude__';
    // Clear any pack selections for this path
    document.querySelectorAll('.pick-cell').forEach(function(el) {{
      if (el.dataset.path === path) el.classList.remove('sel');
    }});
    delete sels[path];
    if (!wasExcluded) {{
      this.classList.add('sel');
      sels[path] = '__exclude__';
    }} else {{
      this.classList.remove('sel');
    }}
    refreshBar();
  }});
}});

// Column header click — select entire column + any linked columns
document.querySelectorAll('.pack-col-hdr').forEach(function(th) {{
  th.addEventListener('click', function() {{
    const arch = this.dataset.arch;
    // Collect all pick-cells in this column
    const colCells = Array.from(document.querySelectorAll('.pick-cell')).filter(function(td) {{
      return td.dataset.arch === arch;
    }});
    // Expand to include linked cells from paired groups (per row)
    const allCells = new Set(colCells);
    colCells.forEach(function(td) {{
      pairedCells(td).forEach(function(p) {{ allCells.add(p); }});
    }});
    selectGroup(Array.from(allCells));
    refreshBar();
  }});
}});

function clearSels() {{
  sels = {{}};
  pendingGeoBuild = null;
  document.querySelectorAll('.pick-cell.sel,.excl-cell.sel').forEach(function(el) {{ el.classList.remove('sel'); }});
  document.getElementById('subBar').style.display = 'none';
  document.getElementById('normalBarBtns').style.display = 'flex';
  document.getElementById('previewBarBtns').style.display = 'none';
  updateGraying();
}}

// Apply winner graying on initial page load
updateGraying();

async function submitSels() {{
  const j = await pywebview.api.submit_browse_choices(sels);
  if (j.ok) {{
    alert('Saved ' + j.count + ' choice(s) to choices.json.\\nRebuild the master: python otr_pack_master.py');
    clearSels();
  }} else {{
    alert('Error: ' + j.error);
  }}
}}

// ── Geometry build preview + apply ───────────────────────────────────────────
async function setGeoBuild(geoStem) {{
  if (!OBJ_KEY) return;
  let j;
  try {{
    j = await pywebview.api.preview_geo_build(OBJ_KEY, geoStem);
  }} catch(e) {{ alert('Preview error: ' + e); return; }}
  if (!j.ok) {{ alert('Preview error: ' + (j.error || 'unknown')); return; }}

  // Clear existing selections
  sels = {{}};
  document.querySelectorAll('.pick-cell.sel,.excl-cell.sel').forEach(function(el) {{
    el.classList.remove('sel');
  }});

  // Highlight effective winner for every image path under this object.
  // j.effective = {{path: stem_or___exclude__}} accounting for first-sorted fallback.
  for (const [path, newVal] of Object.entries(j.effective)) {{
    if (newVal === '__exclude__') {{
      document.querySelectorAll('.excl-cell').forEach(function(excl) {{
        if (excl.dataset.path === path) {{ excl.classList.add('sel'); sels[path] = '__exclude__'; }}
      }});
    }} else {{
      document.querySelectorAll('.pick-cell').forEach(function(cell) {{
        if (cell.dataset.path === path) {{
          if (cell.dataset.arch === newVal) {{ cell.classList.add('sel'); sels[path] = newVal; }}
          else {{ cell.classList.remove('sel'); }}
        }}
      }});
    }}
  }}

  const shortLabel = geoStem === '__off__'
    ? 'Base Game'
    : geoStem.replace(/^.*?3DE\\s*-\\s*\\d+\\s+/, '').replace(/\\s*\\(OPTIONAL\\)\\s*/i, '').trim();
  document.getElementById('geoBuildLabel').textContent = shortLabel;
  pendingGeoBuild = {{stem: geoStem, totalChanges: Object.keys(j.changes).length, effective: j.effective}};
  highlightGeoBtnSelected(geoStem);
  refreshBar();
}}

function highlightGeoBtnSelected(selectedStem) {{
  document.querySelectorAll('.geo-btn').forEach(function(btn) {{
    const isSel = btn.dataset.geoStem === selectedStem;
    if (isSel) {{
      btn.style.background = '#0a2a0a';
      btn.style.border     = '2px solid #4caf50';
      btn.style.color      = '#4caf50';
      btn.style.fontWeight = '700';
    }} else {{
      btn.style.background = '#111';
      btn.style.border     = '1px solid #333';
      btn.style.color      = '#888';
      btn.style.fontWeight = '';
    }}
  }});
}}

async function applyGeoBuild() {{
  if (!pendingGeoBuild) return;
  try {{
    const j = await pywebview.api.set_geo_build(OBJ_KEY, pendingGeoBuild.stem, PTYPE);
    if (j.ok) {{
      // Update committed-winner on every visible row so graying recalculates correctly
      const eff = pendingGeoBuild.effective || {{}};
      document.querySelectorAll('tr[data-committed-winner]').forEach(function(row) {{
        if (row.dataset.path in eff) row.dataset.committedWinner = eff[row.dataset.path];
      }});
      // Move ✓ to the newly applied geo button
      const appliedStem = pendingGeoBuild.stem;
      document.querySelectorAll('.geo-btn').forEach(function(btn) {{
        const isApplied = btn.dataset.geoStem === appliedStem;
        btn.dataset.applied = isApplied ? '1' : '0';
        btn.textContent = btn.textContent.replace(/\\s*✓$/, '') + (isApplied ? '  ✓' : '');
      }});
      clearSels();
    }} else {{ alert('Error: ' + (j.error || 'unknown')); }}
  }} catch(e) {{ alert('Apply error: ' + e); }}
}}

function cancelGeoBuild() {{ clearSels(); }}

// ── Settings modal ────────────────────────────────────────────────────────────
let _settingsGames      = {{}};
let _settingsActiveGame = '';

async function openSettingsModal() {{
  const cfg = await pywebview.api.get_settings();
  _settingsGames      = cfg.games      || {{}};
  _settingsActiveGame = cfg.active_game || '';
  _buildGameSelector();
  _fillSettingsPaths(_settingsActiveGame);
  document.getElementById('settingsStatus').textContent = '';
  document.getElementById('settingsSaveBtn').disabled   = false;
  document.getElementById('settingsSaveBtn').style.opacity = '1';
  document.getElementById('settingsModal').style.display = 'flex';
}}
function _buildGameSelector() {{
  const container = document.getElementById('gameSelector');
  container.innerHTML = '';
  Object.entries(_settingsGames).forEach(function([gk, gv]) {{
    const isActive = (gk === _settingsActiveGame);
    const btn = document.createElement('button');
    btn.onclick = function() {{ selectGame(gk); }};
    btn.style.cssText = [
      'display:flex;flex-direction:column;align-items:center;gap:6px',
      'padding:10px 18px',
      'background:' + (isActive ? '#0a1a2a' : '#060610'),
      'border:2px solid ' + (isActive ? '#7ec8e3' : '#2a2a4a'),
      'border-radius:6px;cursor:pointer',
    ].join(';');
    if (gv.image_data_uri) {{
      const img = document.createElement('img');
      img.src = gv.image_data_uri;
      img.style.cssText = 'width:52px;height:52px;object-fit:contain';
      btn.appendChild(img);
    }}
    const label = document.createElement('span');
    label.textContent = gv.name || gk;
    label.style.cssText = 'color:' + (isActive ? '#7ec8e3' : '#888') + ';font-size:11px;font-weight:600';
    btn.appendChild(label);
    container.appendChild(btn);
  }});
}}
function _fillSettingsPaths(gameKey) {{
  const g = _settingsGames[gameKey] || {{}};
  document.getElementById('cfgModsDir').value   = g.mods_dir       || '';
  document.getElementById('cfgBaseGame').value  = g.base_game_file || '';
  document.getElementById('cfgMasterDir').value = g.master_dir     || '';
}}
async function selectGame(gameKey) {{
  if (gameKey === _settingsActiveGame) return;
  const status = document.getElementById('settingsStatus');
  const btn    = document.getElementById('settingsSaveBtn');
  btn.disabled = true; btn.style.opacity = '0.45';
  status.style.color = '#888'; status.textContent = 'Switching game...';
  try {{
    const j = await pywebview.api.switch_game(gameKey);
    if (!j.ok) {{
      status.textContent = 'Error: ' + j.error;
      status.style.color = '#e94560';
      btn.disabled = false; btn.style.opacity = '1';
      return;
    }}
    _settingsActiveGame = gameKey;
    if (_settingsGames[gameKey]) {{
      _settingsGames[gameKey].mods_dir       = j.mods_dir;
      _settingsGames[gameKey].base_game_file = j.base_game_file;
      _settingsGames[gameKey].master_dir     = j.master_dir;
    }}
    _buildGameSelector();
    _fillSettingsPaths(gameKey);
  }} catch(e) {{
    status.textContent = 'Network error: ' + e;
    status.style.color = '#e94560';
    btn.disabled = false; btn.style.opacity = '1';
    return;
  }}
  status.textContent = 'Rescanning archives...';
  _pollRescan();
}}
function closeSettingsModal() {{
  document.getElementById('settingsModal').style.display = 'none';
}}
async function browseModsDir() {{
  const path = await pywebview.api.browse_folder();
  if (path) document.getElementById('cfgModsDir').value = path;
}}
async function browseBaseGame() {{
  const path = await pywebview.api.browse_o2r_file();
  if (path) document.getElementById('cfgBaseGame').value = path;
}}
async function browseMasterDir() {{
  const path = await pywebview.api.browse_folder();
  if (path) document.getElementById('cfgMasterDir').value = path;
}}
async function saveSettings() {{
  const btn    = document.getElementById('settingsSaveBtn');
  const status = document.getElementById('settingsStatus');
  btn.disabled = true; btn.style.opacity = '0.45';
  status.style.color = '#888'; status.textContent = 'Saving...';
  try {{
    const j = await pywebview.api.save_settings(
      document.getElementById('cfgModsDir').value.trim(),
      document.getElementById('cfgBaseGame').value.trim(),
      document.getElementById('cfgMasterDir').value.trim(),
    );
    if (!j.ok) {{
      status.textContent = 'Error: ' + j.error;
      status.style.color = '#e94560';
      btn.disabled = false; btn.style.opacity = '1';
      return;
    }}
  }} catch(e) {{
    status.textContent = 'Network error: ' + e;
    status.style.color = '#e94560';
    btn.disabled = false; btn.style.opacity = '1';
    return;
  }}
  status.textContent = 'Rescanning archives...';
  _pollRescan();
}}
function _pollRescan() {{
  setTimeout(async function() {{
    try {{
      const j = await pywebview.api.get_rescan_status();
      if (j.done) {{
        if (j.error) {{
          document.getElementById('settingsStatus').textContent = 'Error: ' + j.error;
          document.getElementById('settingsStatus').style.color = '#e94560';
          document.getElementById('settingsSaveBtn').disabled = false;
          document.getElementById('settingsSaveBtn').style.opacity = '1';
        }} else {{
          reloadCurrentView();
        }}
      }} else {{ _pollRescan(); }}
    }} catch(e) {{ _pollRescan(); }}
  }}, 500);
}}

// ── Pack Master modal ─────────────────────────────────────────────────────────
let _packPollTimer = null;
let _packOffset    = 0;

function openPackModal() {{
  document.getElementById('packModal').style.display = 'flex';
}}
function closePackModal() {{
  if (_packPollTimer) {{ clearTimeout(_packPollTimer); _packPollTimer = null; }}
  document.getElementById('packModal').style.display = 'none';
}}
async function startPack() {{
  const btn    = document.getElementById('packRunBtn');
  const status = document.getElementById('packStatus');
  btn.disabled = true;
  btn.style.opacity = '0.45';
  status.style.color = '#888';
  status.textContent = 'Starting...';
  document.getElementById('packLog').textContent = '';
  _packOffset = 0;
  try {{
    const j = await pywebview.api.start_pack_master();
    if (!j.ok) {{
      status.textContent = 'Error: ' + j.error;
      status.style.color = '#e94560';
      btn.disabled = false; btn.style.opacity = '1';
      return;
    }}
  }} catch(e) {{
    status.textContent = 'Network error: ' + e;
    status.style.color = '#e94560';
    btn.disabled = false; btn.style.opacity = '1';
    return;
  }}
  status.textContent = 'Running...';
  _pollPack();
}}
function _pollPack() {{
  _packPollTimer = setTimeout(async function() {{
    try {{
      const j = await pywebview.api.get_pack_master_log(_packOffset);
      if (j.lines && j.lines.length) {{
        const log = document.getElementById('packLog');
        log.textContent += j.lines.join('\\n') + '\\n';
        log.scrollTop = log.scrollHeight;
        _packOffset += j.lines.length;
      }}
      if (j.done) {{
        const btn    = document.getElementById('packRunBtn');
        const status = document.getElementById('packStatus');
        btn.disabled = false; btn.style.opacity = '1';
        if (j.error) {{
          status.textContent = 'Failed: ' + j.error;
          status.style.color = '#e94560';
        }} else {{
          status.textContent = 'Done!';
          status.style.color = '#4caf50';
          reloadCurrentView();
        }}
      }} else {{
        _pollPack();
      }}
    }} catch(e) {{ _pollPack(); }}
  }}, 400);
}}
</script>

<div id="settingsModal" style="display:none;position:fixed;inset:0;background:#000a;z-index:200;
     align-items:center;justify-content:center">
  <div style="background:#0d0d1a;border:1px solid #2a2a4a;border-radius:8px;
              width:640px;max-width:90vw;display:flex;flex-direction:column">
    <div style="padding:12px 16px;border-bottom:1px solid #2a2a4a;
                display:flex;justify-content:space-between;align-items:center">
      <span style="color:#7ec8e3;font-weight:700;font-size:14px">⚙ Settings</span>
      <button onclick="closeSettingsModal()"
              style="background:none;border:none;color:#666;cursor:pointer;font-size:18px;line-height:1">✕</button>
    </div>
    <div id="gameSelector" style="display:flex;gap:10px;padding:14px 16px;border-bottom:1px solid #2a2a4a;flex-wrap:wrap">
      <!-- populated by openSettingsModal() -->
    </div>
    <div style="padding:18px 16px;display:flex;flex-direction:column;gap:14px">
      <label style="display:flex;flex-direction:column;gap:5px">
        <span style="color:#888;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.05em">Mods Directory <span style="font-weight:400;text-transform:none">(Folder where the mods you wish to merge live.)</span></span>
        <div style="display:flex;gap:6px">
          <input id="cfgModsDir" type="text"
                 style="background:#060610;color:#ddd;border:1px solid #2a2a4a;border-radius:4px;
                        padding:6px 10px;font-size:12px;font-family:monospace;outline:none;width:100%"
                 placeholder="Folder containing .otr / .o2r packs">
          <button onclick="browseModsDir()" style="padding:6px 14px;background:#111;border:1px solid #333;
                  color:#7ec8e3;border-radius:4px;cursor:pointer;font-size:12px;white-space:nowrap">Browse…</button>
        </div>
      </label>
      <label style="display:flex;flex-direction:column;gap:5px">
        <span style="color:#888;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.05em">Base Game File (oot.o2r)</span>
        <div style="display:flex;gap:6px">
          <input id="cfgBaseGame" type="text"
                 style="background:#060610;color:#ddd;border:1px solid #2a2a4a;border-radius:4px;
                        padding:6px 10px;font-size:12px;font-family:monospace;outline:none;width:100%"
                 placeholder="Path to oot.o2r  (leave blank to skip)">
          <button onclick="browseBaseGame()" style="padding:6px 14px;background:#111;border:1px solid #333;
                  color:#7ec8e3;border-radius:4px;cursor:pointer;font-size:12px;white-space:nowrap">Browse…</button>
        </div>
      </label>
      <label style="display:flex;flex-direction:column;gap:5px">
        <span style="color:#888;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.05em">Output Destination <span style="font-weight:400;text-transform:none">(Ship of Harkinian mods folder)</span></span>
        <div style="display:flex;gap:6px">
          <input id="cfgMasterDir" type="text"
                 style="background:#060610;color:#ddd;border:1px solid #2a2a4a;border-radius:4px;
                        padding:6px 10px;font-size:12px;font-family:monospace;outline:none;width:100%"
                 placeholder="Folder to write 999_Master.o2r into">
          <button onclick="browseMasterDir()" style="padding:6px 14px;background:#111;border:1px solid #333;
                  color:#7ec8e3;border-radius:4px;cursor:pointer;font-size:12px;white-space:nowrap">Browse…</button>
        </div>
      </label>
    </div>
    <div style="padding:10px 16px;border-top:1px solid #2a2a4a;display:flex;gap:12px;align-items:center">
      <button id="settingsSaveBtn" onclick="saveSettings()"
              style="padding:7px 20px;background:#0a1a2a;border:1px solid #7ec8e3;
                     color:#7ec8e3;border-radius:5px;cursor:pointer;font-size:13px;font-weight:700">Save &amp; Rescan</button>
      <button onclick="closeSettingsModal()"
              style="padding:7px 16px;background:#111;border:1px solid #333;
                     color:#888;border-radius:5px;cursor:pointer;font-size:13px">Cancel</button>
      <span id="settingsStatus" style="color:#888;font-size:12px"></span>
    </div>
  </div>
</div>

<div id="packModal" style="display:none;position:fixed;inset:0;background:#000a;z-index:200;
     align-items:center;justify-content:center">
  <div style="background:#0d0d1a;border:1px solid #2a2a4a;border-radius:8px;
              width:720px;max-width:90vw;display:flex;flex-direction:column;max-height:80vh">
    <div style="padding:12px 16px;border-bottom:1px solid #2a2a4a;
                display:flex;justify-content:space-between;align-items:center;flex-shrink:0">
      <span style="color:#4caf50;font-weight:700;font-size:14px">Pack Master</span>
      <button onclick="closePackModal()"
              style="background:none;border:none;color:#666;cursor:pointer;font-size:18px;line-height:1">✕</button>
    </div>
    <pre id="packLog"
         style="flex:1;overflow-y:auto;margin:0;padding:12px 16px;
                font-family:monospace;font-size:12px;color:#ccc;
                background:#060610;min-height:260px;white-space:pre-wrap;word-break:break-all"></pre>
    <div style="padding:10px 16px;border-top:1px solid #2a2a4a;
                display:flex;gap:12px;align-items:center;flex-shrink:0">
      <button id="packRunBtn" onclick="startPack()"
              style="padding:7px 20px;background:#0a2a0a;border:1px solid #4caf50;
                     color:#4caf50;border-radius:5px;cursor:pointer;
                     font-size:13px;font-weight:700">Build</button>
      <span id="packStatus" style="color:#888;font-size:12px"></span>
    </div>
  </div>
</div>
</body></html>'''
            return out_html

    def get_pack_master_log(self, offset=0):
        offset = int(offset)
        return {
            'lines': _pack_state['lines'][offset:],
            'done':  _pack_state['done'],
            'error': _pack_state['error'],
        }

    def get_rescan_status(self):
        return {
            'running': _rescan_state['running'],
            'done':    _rescan_state['done'],
            'error':   _rescan_state['error'],
        }

    def browse_folder(self):
        result = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        return result[0] if result else ''

    def browse_o2r_file(self):
        result = self._window.create_file_dialog(
            webview.FileDialog.OPEN,
            file_types=('OoT base game archive (*.o2r)', 'All files (*.*)'),
        )
        return result[0] if result else ''

    def get_settings(self):
        games_out = {}
        for gk, gdef in GAME_DEFS.items():
            defs = _GAME_DEFAULTS.get(gk, {})
            gcfg = _games_config.get(gk, {})
            if gk == active_game:
                mods_dir_str, base_game_str, master_dir_str = (
                    str(MODS_DIR)       if MODS_DIR       else '',
                    str(BASE_GAME_FILE) if BASE_GAME_FILE else '',
                    str(MASTER_DIR)     if MASTER_DIR     else '',
                )
            else:
                mods_dir_str   = gcfg.get('mods_dir',   defs.get('mods_dir', ''))
                base_game_str  = gcfg.get('base_game_file', defs.get('base_game_file', ''))
                master_dir_str = gcfg.get('master_dir', defs.get('master_dir', ''))
            games_out[gk] = {
                'name':            gdef['name'],
                'image':           gdef['image'],
                'image_data_uri':  _icon_data_uri(gdef['image']),
                'mods_dir':        mods_dir_str,
                'base_game_file':  base_game_str,
                'master_dir':      master_dir_str,
            }
        return {'active_game': active_game, 'games': games_out}
    def preview_geo_build(self, obj_key='', geo_stem=''):
            try:
                with FileLock(CHOICES_LOCK_FILE, timeout=15):
                    original = json.loads(CHOICES_FILE.read_text()) if CHOICES_FILE.exists() else {}
                preview = dict(original)
                apply_geo_build(obj_key, geo_stem, self._all_archives,
                                self._archive_names_cache, self._image_paths_cache, preview)
                changes = {}
                for k in set(original) | set(preview):
                    if original.get(k) != preview.get(k):
                        changes[k] = preview.get(k)  # None = deleted
                # Effective winner for every image path under obj_key
                sorted_arches = sorted(self._all_archives, key=lambda a: a.stem)
                all_img_paths = set()
                for arch, imgs in self._image_paths_cache.items():
                    all_img_paths.update(p for p in imgs if p.startswith(obj_key))
                effective = {}
                for p in all_img_paths:
                    if p in preview:
                        effective[p] = preview[p]
                    else:
                        effective[p] = _res_winner(
                            p, sorted_arches,
                            self._image_paths_cache, self._image_dims_cache
                        )
                return {'ok': True, 'changes': changes, 'effective': effective}
            except Exception as e:
                return {'ok': False, 'error': str(e)}

    def set_geo_build(self, obj_key='', geo_stem='', ptype=''):
            try:
                with FileLock(CHOICES_LOCK_FILE, timeout=15):
                    existing = json.loads(CHOICES_FILE.read_text()) if CHOICES_FILE.exists() else {}
                    changed = apply_geo_build(
                        obj_key, geo_stem,
                        self._all_archives,
                        self._archive_names_cache,
                        self._image_paths_cache,
                        existing,
                    )
                    merged_text = json.dumps(existing, indent=2)
                    validated   = json.loads(merged_text)
                    if len(validated) != len(existing):
                        raise ValueError(f'JSON round-trip mismatch: {len(existing)} → {len(validated)}')
                    if CHOICES_FILE.exists():
                        import shutil
                        shutil.copy2(CHOICES_FILE, CHOICES_FILE.with_suffix('.json.bak'))
                    tmp = CHOICES_FILE.with_suffix('.json.tmp')
                    tmp.write_text(merged_text, encoding='utf-8')
                    tmp.replace(CHOICES_FILE)
                print(f'\n✓ Geo build: {obj_key!r} → {geo_stem!r} ({changed} paths changed)')
                return {'ok': True, 'changed': changed}
            except Exception as e:
                return {'ok': False, 'error': str(e)}

    def submit_browse_choices(self, new_choices=None):
            try:
                new_choices = new_choices or {}
                with FileLock(CHOICES_LOCK_FILE, timeout=15):
                    # Merge into existing choices.json (new selections overwrite conflicting keys)
                    existing = json.loads(CHOICES_FILE.read_text()) if CHOICES_FILE.exists() else {}
                    before_count = len(existing)
                    existing.update(new_choices)
                    # Enforce Djipi geometry rules: auto-exclude geometry when
                    # user picks non-Djipi images, and purge dead image choices
                    # so each object is consistently all-Djipi or all-non-Djipi.
                    geo_added, geo_cleared = enforce_geometry_rules(
                        existing,
                        self._all_archives,
                        self._archive_names_cache,
                        self._image_paths_cache,
                    )
                    merged_text = json.dumps(existing, indent=2)
                    # Validate before touching the real file.
                    # Enforcement may delete keys (cleared dead image choices),
                    # so the only invariant is that the JSON round-trip is lossless.
                    validated = json.loads(merged_text)
                    if len(validated) != len(existing):
                        raise ValueError(
                            f"JSON round-trip mismatch: "
                            f"{len(existing)} → {len(validated)}"
                        )
                    # Back up current file before overwriting
                    if CHOICES_FILE.exists():
                        import shutil
                        shutil.copy2(CHOICES_FILE, CHOICES_FILE.with_suffix('.json.bak'))
                    # Write atomically: temp file → rename
                    tmp = CHOICES_FILE.with_suffix('.json.tmp')
                    tmp.write_text(merged_text, encoding='utf-8')
                    tmp.replace(CHOICES_FILE)
                if geo_added or geo_cleared:
                    print(f"  Geometry rules: +{geo_added} auto-excludes, {geo_cleared} dead choices cleared")
                print(f"\n✓ Browse-select: merged {len(new_choices)} choice(s) → {len(existing)} total in {CHOICES_FILE.name}")
                return {
                    "ok": True,
                    "count": len(new_choices),
                    "geo_excluded": geo_added,
                    "geo_cleared": geo_cleared,
                }
            except Exception as e:
                return {"ok": False, "error": str(e)}

    def save_settings(self, mods_dir='', base_game_file='', master_dir=''):
            global MODS_DIR, BASE_GAME_FILE, MASTER_DIR, active_game
            new_mods   = (mods_dir or '').strip()
            new_base   = (base_game_file or '').strip()
            new_master = (master_dir or '').strip()
            if not new_mods or not new_master:
                return {'ok': False, 'error': 'Mods dir and output destination are required'}
            _games_config[active_game] = {'mods_dir': new_mods, 'base_game_file': new_base, 'master_dir': new_master}
            _save_config()
            MODS_DIR       = Path(new_mods)
            BASE_GAME_FILE = Path(new_base) if new_base else BASE_GAME_FILE
            MASTER_DIR     = Path(new_master)
            with _rescan_lock:
                if not _rescan_state['running']:
                    threading.Thread(target=_do_scan, args=(self,), daemon=True).start()
            return {'ok': True}

    def switch_game(self, game=''):
            global MODS_DIR, BASE_GAME_FILE, MASTER_DIR, active_game
            if game not in GAME_DEFS:
                return {'ok': False, 'error': f'Unknown game: {game}'}
            active_game = game
            defs = _GAME_DEFAULTS.get(game, {})
            gcfg = _games_config.get(game, {})
            new_mods   = gcfg.get('mods_dir',       defs.get('mods_dir',       ''))
            new_base   = gcfg.get('base_game_file', defs.get('base_game_file', ''))
            new_master = gcfg.get('master_dir',     defs.get('master_dir',     ''))
            MODS_DIR       = Path(new_mods)   if new_mods   else None
            BASE_GAME_FILE = Path(new_base)   if new_base   else None
            MASTER_DIR     = Path(new_master) if new_master else None
            _save_config()
            with _rescan_lock:
                if not _rescan_state['running']:
                    threading.Thread(target=_do_scan, args=(self,), daemon=True).start()
            return {
                'ok': True,
                'mods_dir':       str(MODS_DIR)       if MODS_DIR       else '',
                'base_game_file': str(BASE_GAME_FILE) if BASE_GAME_FILE else '',
                'master_dir':     str(MASTER_DIR)     if MASTER_DIR     else '',
            }

    def start_pack_master(self):
            with _pack_lock:
                if _pack_state['running']:
                    return {'ok': False, 'error': 'Already running'}
                _pack_state.update({'running': True, 'lines': [], 'done': False, 'error': None})
            threading.Thread(target=_pack_worker, args=(self,), daemon=True).start()
            return {'ok': True}

_LOADING_HTML = """<!DOCTYPE html>
<html><head><style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d0d1a; color: #7ec8e3; font-family: sans-serif;
         display: flex; align-items: center; justify-content: center; height: 100vh; }
  h2   { color: #e94560; font-size: 22px; margin-bottom: 10px; letter-spacing: 1px; }
  p    { color: #888; font-size: 14px; }
  .dots span { animation: blink 1.4s infinite; opacity: .2; }
  .dots span:nth-child(2) { animation-delay: .35s; }
  .dots span:nth-child(3) { animation-delay: .70s; }
  @keyframes blink { 40% { opacity: 1; } }
</style></head>
<body><div style="text-align:center">
  <h2>Graphical Texture Picker</h2>
  <p>Loading archives<span class="dots"><span>.</span><span>.</span><span>.</span></span></p>
</div></body></html>"""


def _startup(api):
    print(f"Graphical Texture Picker  —  mods: {MODS_DIR}")
    _do_scan(api)
    api.go_master_browse()


def main():
    api = Api()
    window = webview.create_window(
        'Graphical Texture Picker', html=_LOADING_HTML,
        width=1440, height=900, min_size=(800, 600),
        js_api=api,
    )
    api._window = window
    # debug=False (the default) keeps WebView2 devtools off — never flip this
    # in a packaged build, it opens a real TCP devtools port.
    webview.start(_startup, api, gui='edgechromium')


if __name__ == '__main__':
    main()
