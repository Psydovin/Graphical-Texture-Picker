#!/usr/bin/env python3
"""
otr_picker_server.py — Master archive browser for OTR/O2R packs.

Routes:
  /master-browse   — Browse 999_Master.o2r with per-path pack comparison + selection
  /pack-img        — PNG: image from a specific source archive
  /submit-browse-choices — POST: save selected choices to choices.json

Usage:  python otr_picker_server.py
        Then open http://localhost:8765

Requirements:  pip install mpyq Pillow
"""

import http.server, socketserver, json, struct, io, threading, webbrowser, sys
from socketserver import ThreadingMixIn
import mpyq, zipfile
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote, quote
from PIL import Image
from filelock import FileLock

# ── config ─────────────────────────────────────────────────────────────────────
PORT              = 8765
SCRIPT_DIR        = Path(__file__).parent
CHOICES_FILE      = SCRIPT_DIR / "choices.json"
CHOICES_LOCK_FILE = SCRIPT_DIR / "choices.json.lock"
CONFIG_FILE       = SCRIPT_DIR / "config.json"

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

def _pack_worker():
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
        Handler.master_file  = dest
        Handler.master_names = archive_names(dest)
        _log(f'Master index updated: {len(Handler.master_names):,} entries.')
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

def _do_scan():
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

        Handler.all_archives        = all_archives
        Handler.archive_names_cache = archive_names_cache
        Handler.image_paths_cache   = image_paths_cache
        Handler.image_dims_cache    = image_dims_cache
        Handler.mods_dir            = MODS_DIR

        master_path = (MASTER_DIR / '999_Master.o2r') if MASTER_DIR else None
        if master_path and master_path.exists():
            Handler.master_file  = master_path
            Handler.master_names = archive_names(master_path)
            print(f"Master: {len(Handler.master_names):,} entries")
        else:
            Handler.master_file  = None
            Handler.master_names = frozenset()
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

# ── startup checks ─────────────────────────────────────────────────────────────
def find_mods_dir():
    if MODS_DIR.exists():
        return MODS_DIR
    candidates = list(SCRIPT_DIR.glob("*.otr")) + list(SCRIPT_DIR.glob("*.o2r"))
    if candidates:
        return SCRIPT_DIR
    print(f"ERROR: Could not find mods folder at {MODS_DIR}")
    print("Edit the MODS_DIR variable at the top of this script.")
    sys.exit(1)

# ── threaded server ────────────────────────────────────────────────────────────
class ThreadedTCPServer(ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True

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

# ── HTTP handler ───────────────────────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    mods_dir          = None
    master_file         = None         # Path to 999_Master.o2r (or None if not found)
    master_names        = frozenset()  # All internal paths in the master archive
    all_archives        = []           # All archives in MODS_DIR, sorted
    archive_names_cache = {}           # archive_path → frozenset of internal names
    image_paths_cache   = {}           # archive_path → frozenset of paths with image data
    image_dims_cache    = {}           # archive_path → {internal_path: (w, h)}

    def log_message(self, *a): pass  # suppress access log

    def do_GET(self):
        route = self.path.split('?')[0]

        if route == '/':
            # Redirect root to master browser
            self.send_response(302)
            self.send_header('Location', '/master-browse')
            self.end_headers()
        elif route == '/master-img':
            # Serve a texture directly from 999_Master.o2r.
            # ?path=alt/objects/...
            qs    = parse_qs(urlparse(self.path).query)
            ipath = unquote(qs.get('path', [''])[0])
            raw   = read_from_archive(self.master_file, ipath) if self.master_file and ipath else None
            self._img(soh_to_png_bytes(raw) if raw else None)

        elif route == '/pack-img':
            # Serve a texture from a specific archive by exact filename.
            # ?archive=001_OoT_Reloaded_v11.0.0_HD.o2r&path=alt/objects/...
            qs       = parse_qs(urlparse(self.path).query)
            ipath    = unquote(qs.get('path',    [''])[0])
            arc_name = unquote(qs.get('archive', [''])[0])
            arch     = next((p for p in self.all_archives if p.name == arc_name), None)
            # Base game stores paths without 'alt/' — strip it before reading
            read_path = ipath[4:] if (arch == BASE_GAME_FILE and ipath.startswith('alt/')) else ipath
            raw      = read_from_archive(arch, read_path) if arch and ipath else None
            self._img(soh_to_png_bytes(raw) if raw else None)

        elif route == '/master-browse':
            # Browse 999_Master.o2r with side-by-side comparison against source packs.
            # ?obj=object_cow  — select an object to compare
            # ?type=objects|scenes  — path category (default: objects)
            import re as _re2
            if not self.master_file:
                self._html('<h2>999_Master.o2r not found</h2><p>Build it first: <code>python otr_pack_master.py</code></p>')
                return

            qs       = parse_qs(urlparse(self.path).query)
            selected = unquote(qs.get('obj',  [''])[0])
            ptype    = unquote(qs.get('type', ['objects'])[0])
            q        = unquote(qs.get('q',    [''])[0]).strip().lower()
            PER_PAGE = 50
            try:
                page = max(1, int(qs.get('page', ['1'])[0]))
            except ValueError:
                page = 1

            # All non-empty categories present in the master
            all_types = sorted(set(
                p.split('/')[1] for p in self.master_names
                if p.startswith('alt/') and p.count('/') >= 2
            ))

            prefix  = f'alt/{ptype}/'
            # Scenes have an extra subcategory level: alt/scenes/subcategory/scene_name/
            # Show "subcategory/scene_name" in the sidebar so each scene is selectable.
            # All other categories use the simpler alt/category/item_name/ structure.
            if ptype == 'scenes':
                objects = sorted(set(
                    '/'.join(p.split('/')[2:4]) for p in self.master_names
                    if p.startswith(prefix) and p.count('/') >= 4
                ))
            else:
                objects = sorted(set(
                    p.split('/')[2] for p in self.master_names
                    if p.startswith(prefix) and p.count('/') >= 3
                ))

            type_nav = ''.join(
                f'<a href="/master-browse?type={t}" '
                f'style="color:{"#e94560" if t == ptype else "#7ec8e3"};font-size:12px;'
                f'text-decoration:none;padding:2px 8px;border-radius:4px;'
                f'{"background:#1a0010;" if t == ptype else ""}">{t}</a>'
                for t in all_types
            )
            sidebar = '\n'.join(
                f'<div style="padding:3px 6px;cursor:pointer;'
                f'{"font-weight:bold;color:#e94560;" if o == selected else ""}"'
                f' onclick="goObj(\'{quote(o)}\')">{o}</div>'
                for o in objects
            )

            def pack_label(arch):
                n = arch.stem
                if n == 'oot':           return '🕹 Base Game'
                if 'OoT_Reloaded' in n: return '🌟 OoT Reloaded'
                if '08_Art' in n:        return '⚔ Dark Link'
                if n.startswith('06_'):  return '🎒 06 Items'
                if n.startswith('07_'):  return '🎒 07 Items'
                m = _re2.search(r'3DE\s*-\s*(\d+)\s+(.+)', n)
                if m:
                    num  = m.group(1)
                    name = m.group(2).replace('Objects ', '').replace(' (OPTIONAL)', '').replace('3DS', '').replace('Textures', 'Tex').strip()
                    return f'Djipi {num}: {name[:22]}'
                return n[:28]

            # Default values used in JS template even when no object is selected
            geo_build_html  = ''

            if selected:
                choices_now_geo = json.loads(CHOICES_FILE.read_text()) if CHOICES_FILE.exists() else {}
                _stem_to_arch = {a.stem: a for a in self.all_archives}
                _obj_key = f'alt/{ptype}/{selected}/'

                # ── Geometry build selector ────────────────────────────────────
                _geo_builds, _all_geo_paths, _off_active = compute_geo_builds(
                    _obj_key, self.all_archives,
                    self.archive_names_cache, self.image_paths_cache,
                    choices_now_geo,
                )
                geo_build_html = ''
                if _geo_builds:
                    import re as _re_gb
                    def _gb_label(stem):
                        m = _re_gb.search(r'3DE\s*-\s*(\d+)\s+(.+)', stem)
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
                        stem_js  = stem.replace("'", "\\'")   # for JS string in onclick
                        stem_attr = stem.replace('"', '&quot;') # for HTML attribute
                        applied = '1' if active else '0'
                        return (f'<button class="geo-btn" data-geo-stem="{stem_attr}" '
                                f'data-applied="{applied}" style="{style}" '
                                f'onclick="setGeoBuild(\'{stem_js}\')">'
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
                master_obj_paths = frozenset(p for p in self.master_names if f'/{selected}/' in p)
                all_obj_paths = set(master_obj_paths)
                for arch in self.all_archives:
                    for n in self.archive_names_cache.get(arch, frozenset()):
                        if f'/{selected}/' in n:
                            all_obj_paths.add(n)
                obj_paths = sorted(all_obj_paths)

                # Packs that have ANY path containing /{selected}/
                relevant_packs = [
                    arch for arch in self.all_archives
                    if any(f'/{selected}/' in n
                           for n in self.archive_names_cache.get(arch, frozenset()))
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
                master_img_set = self.image_paths_cache.get(self.master_file, frozenset())
                filtered_paths = []
                for p in obj_paths:
                    if q and q not in p.lower():
                        continue
                    in_m = p in master_obj_paths
                    if in_m and p in master_img_set:
                        filtered_paths.append(p)
                        continue
                    if any(p in self.image_paths_cache.get(arch, frozenset())
                           for arch in relevant_packs
                           if p in self.archive_names_cache.get(arch, frozenset())):
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
                            self.image_paths_cache, self.image_dims_cache
                        )

                rows = []
                for p in page_paths:
                    fname = p.split('/')[-1]
                    in_master = p in master_obj_paths
                    if in_master:
                        m_url = f'/master-img?path={quote(p)}'
                        master_td = (
                            f'<td style="background:#0a1a0a;border:2px solid #4caf50;vertical-align:top;padding:4px">'
                            f'<img src="{m_url}" loading="lazy" style="max-width:180px;max-height:180px;image-rendering:pixelated;display:block" '
                            f'onerror="this.style.display=\'none\';checkRowImages(this)"></td>'
                        )
                    else:
                        master_td = '<td style="background:#1a1a1a;border:1px solid #333;color:#444;text-align:center;vertical-align:middle;font-size:10px;padding:4px">not in<br>master</td>'
                    pack_tds = []
                    img_count = 1 if in_master else 0
                    for arch in relevant_packs:
                        if p in self.archive_names_cache.get(arch, frozenset()):
                            img_count += 1
                            url = f'/pack-img?archive={quote(arch.name)}&path={quote(p)}'
                            pack_tds.append(
                                f'<td class="pick-cell" data-path="{p}" data-arch="{arch.stem}" style="vertical-align:top;padding:4px">'
                                f'<img src="{url}" loading="lazy" style="max-width:180px;max-height:180px;image-rendering:pixelated;display:block" '
                                f'onerror="this.style.display=\'none\';checkRowImages(this)"></td>'
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

                def page_url(pg):
                    base = f'/master-browse?type={ptype}&obj={quote(selected)}&page={pg}'
                    return base + (f'&q={quote(q)}' if q else '')

                prev_btn = (f'<a href="{page_url(page-1)}" style="color:#7ec8e3;text-decoration:none;padding:3px 10px;'
                            f'border:1px solid #2a2a4a;border-radius:4px">&#8592; Prev</a>'
                            if page > 1 else
                            '<span style="color:#333;padding:3px 10px;border:1px solid #222;border-radius:4px">&#8592; Prev</span>')
                next_btn = (f'<a href="{page_url(page+1)}" style="color:#7ec8e3;text-decoration:none;padding:3px 10px;'
                            f'border:1px solid #2a2a4a;border-radius:4px">Next &#8594;</a>'
                            if page < total_pages else
                            '<span style="color:#333;padding:3px 10px;border:1px solid #222;border-radius:4px">Next &#8594;</span>')
                pag_row = (f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap">'
                           f'{prev_btn}'
                           f'<span style="color:#888;font-size:12px">Page {page} of {total_pages} &bull; '
                           f'{total_rows} entries &bull; showing {len(rows)}</span>'
                           f'{next_btn}'
                           + ''.join(
                               f'<a href="{page_url(pg)}" style="color:{"#e94560" if pg==page else "#7ec8e3"};'
                               f'font-size:11px;text-decoration:none;padding:2px 7px;border-radius:3px;'
                               f'{"background:#1a0010;" if pg==page else ""}">{pg}</a>'
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
                all_img_arches = list(self.all_archives) + ([self.master_file] if self.master_file else [])
                for arch in all_img_arches:
                    for p in self.image_paths_cache.get(arch, frozenset()):
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
                        f'<a href="/master-browse?type={cat}&obj={quote(obj)}&q={quote(q)}" '
                        f'style="color:#e94560;text-decoration:none">{obj}</a>'
                        f'<span style="color:#444;font-size:10px;margin-left:8px">{cat}</span>'
                        f'</td></tr>'
                    )
                    for p in sorted(hits[obj]):
                        fname = p.split('/')[-1]
                        in_m  = p in self.master_names
                        choice = choices_now.get(p, '')
                        m_cell = ('<span style="color:#4caf50">✓ master</span>' if in_m
                                  else '<span style="color:#444">—</span>')
                        c_cell = (f'<span style="color:#e9a020">{choice}</span>' if choice
                                  else '<span style="color:#444">first-sorted</span>')
                        # Thumbnail: prefer master, else first source archive with this image
                        if in_m and self.master_file:
                            thumb_url = f'/master-img?path={quote(p)}'
                        else:
                            src_arch = next(
                                (a for a in self.all_archives
                                 if p in self.image_paths_cache.get(a, frozenset())),
                                None
                            )
                            thumb_url = (f'/pack-img?archive={quote(src_arch.name)}&path={quote(p)}'
                                         if src_arch else '')
                        thumb_html = (
                            f'<img src="{thumb_url}" loading="lazy" '
                            f'style="max-width:80px;max-height:80px;image-rendering:pixelated;'
                            f'vertical-align:middle;display:block;margin:auto">'
                            if thumb_url else ''
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

            master_size = f'{self.master_file.stat().st_size / 1024 / 1024:.0f} MB' if self.master_file and self.master_file.exists() else '?'
            _ag = GAME_DEFS.get(active_game, {})
            _ag_img  = _ag.get('image', '')
            _ag_name = _ag.get('name', 'Settings')
            html = f'''<!doctype html>
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
    <div class="meta">{len(self.master_names):,} entries &bull; {master_size}</div>
  </div>
  <form method="get" action="/master-browse" style="display:flex;gap:6px;align-items:center;flex:1;max-width:400px;margin:0 20px">
    <input name="q" type="text" value="{q}" placeholder="search all paths…"
           style="flex:1;background:#0d0d1a;color:#ddd;border:1px solid #2a2a4a;border-radius:4px;
                  padding:5px 10px;font-size:12px;font-family:monospace;outline:none">
    <button type="submit" style="background:#2a2a4a;color:#7ec8e3;border:none;border-radius:4px;
            padding:5px 12px;cursor:pointer;font-size:12px">Search</button>
    {'<a href="/master-browse" style="color:#888;font-size:11px;text-decoration:none;white-space:nowrap">✕ clear</a>' if q else ''}
  </form>
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
      {'<img src="/game-img/' + _ag_img + '" style="width:20px;height:20px;object-fit:contain">' if _ag_img else '⚙'}
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

// Sidebar scroll persistence
function goObj(obj) {{
  var sb = document.querySelector('.sidebar');
  if (sb) sessionStorage.setItem('sbScroll', sb.scrollTop);
  location = '/master-browse?type={ptype}&obj=' + encodeURIComponent(obj) + '&page=1';
}}
document.addEventListener('DOMContentLoaded', function() {{
  var sb = document.querySelector('.sidebar');
  var saved = sessionStorage.getItem('sbScroll');
  if (sb && saved) sb.scrollTop = parseInt(saved, 10);
}});

let sels = {{}};
let pendingGeoBuild = null;  // {{stem, totalChanges}} while previewing a geo build

const OBJ_KEY = {json.dumps(_obj_key if selected else '')};
const PTYPE   = {json.dumps(ptype)};

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
  const r = await fetch('/submit-browse-choices', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(sels)
  }});
  const j = await r.json();
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
    const r = await fetch('/preview-geo-build', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{obj_key: OBJ_KEY, geo_stem: geoStem}})
    }});
    j = await r.json();
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
    const r = await fetch('/set-geo-build', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{obj_key: OBJ_KEY, geo_stem: pendingGeoBuild.stem, ptype: PTYPE}})
    }});
    const j = await r.json();
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
  const r   = await fetch('/settings');
  const cfg = await r.json();
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
    if (gv.image) {{
      const img = document.createElement('img');
      img.src = '/game-img/' + gv.image;
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
    const r = await fetch('/switch-game', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{game: gameKey}})
    }});
    const j = await r.json();
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
async function saveSettings() {{
  const btn    = document.getElementById('settingsSaveBtn');
  const status = document.getElementById('settingsStatus');
  btn.disabled = true; btn.style.opacity = '0.45';
  status.style.color = '#888'; status.textContent = 'Saving...';
  try {{
    const r = await fetch('/settings', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{
        mods_dir:       document.getElementById('cfgModsDir').value.trim(),
        base_game_file: document.getElementById('cfgBaseGame').value.trim(),
        master_dir:     document.getElementById('cfgMasterDir').value.trim(),
      }})
    }});
    const j = await r.json();
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
      const j = await (await fetch('/rescan-status')).json();
      if (j.done) {{
        if (j.error) {{
          document.getElementById('settingsStatus').textContent = 'Error: ' + j.error;
          document.getElementById('settingsStatus').style.color = '#e94560';
          document.getElementById('settingsSaveBtn').disabled = false;
          document.getElementById('settingsSaveBtn').style.opacity = '1';
        }} else {{
          location.reload();
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
    const r = await fetch('/pack-master', {{method: 'POST'}});
    const j = await r.json();
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
      const r = await fetch('/pack-master-log?offset=' + _packOffset);
      const j = await r.json();
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
          location.reload();
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
        <input id="cfgModsDir" type="text"
               style="background:#060610;color:#ddd;border:1px solid #2a2a4a;border-radius:4px;
                      padding:6px 10px;font-size:12px;font-family:monospace;outline:none;width:100%"
               placeholder="Folder containing .otr / .o2r packs">
      </label>
      <label style="display:flex;flex-direction:column;gap:5px">
        <span style="color:#888;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.05em">Base Game File (oot.o2r)</span>
        <input id="cfgBaseGame" type="text"
               style="background:#060610;color:#ddd;border:1px solid #2a2a4a;border-radius:4px;
                      padding:6px 10px;font-size:12px;font-family:monospace;outline:none;width:100%"
               placeholder="Path to oot.o2r  (leave blank to skip)">
      </label>
      <label style="display:flex;flex-direction:column;gap:5px">
        <span style="color:#888;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.05em">Output Destination <span style="font-weight:400;text-transform:none">(Ship of Harkinian mods folder)</span></span>
        <input id="cfgMasterDir" type="text"
               style="background:#060610;color:#ddd;border:1px solid #2a2a4a;border-radius:4px;
                      padding:6px 10px;font-size:12px;font-family:monospace;outline:none;width:100%"
               placeholder="Folder to write 999_Master.o2r into">
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
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(html.encode())

        elif route == '/pack-browse':
            # List all unique object names in any pack.
            # Matches by: "3DE - XX" prefix, bare number prefix, or any name substring.
            # Visit http://localhost:8765/pack-browse?pack=06_Items or ?pack=01
            qs       = parse_qs(urlparse(self.path).query)
            query    = unquote(qs.get('pack', [''])[0])
            padded   = query.zfill(2)
            archive  = next((p for p in sorted(self.mods_dir.iterdir())
                             if p.suffix.lower() in ('.otr', '.o2r') and (
                                 f'3DE - {padded}' in p.name or
                                 p.name.startswith(query) or
                                 query in p.name
                             )), None)
            if not archive:
                self._html(f"<h2>Pack {pack_num} not found</h2>")
                return
            names = archive_names(archive)
            # Extract unique object folder names (alt/objects/OBJECT_NAME/...)
            objects = sorted(set(
                p.split('/')[2] for p in names
                if p.startswith('alt/') and p.count('/') >= 3
            ))
            rows = ''.join(f'<tr><td><code>{o}</code></td></tr>' for o in objects)
            html = (f'<h2>{archive.name}</h2>'
                    f'<p>{len(names)} total files, {len(objects)} unique objects</p>'
                    f'<table border=1 cellpadding=6><tr><th>Object name</th></tr>{rows}</table>')
            self._html(html)
        elif route == '/bg-img':
            # Serve a texture directly from an archive by pack keyword + internal path.
            # ?pack=26  → Djipi's 3DE - 26 Background 3DS
            # ?pack=oot → 001_OoT_Reloaded
            # ?path=alt/...
            qs      = parse_qs(urlparse(self.path).query)
            ipath   = unquote(qs.get('path', [''])[0])
            pk      = unquote(qs.get('pack', [''])[0])
            if pk == 'oot':
                arch = next((p for p in self.mods_dir.iterdir() if p.name.startswith('001_')), None)
            else:
                arch = next((p for p in sorted(self.mods_dir.iterdir())
                             if p.suffix.lower() in ('.otr', '.o2r') and pk in p.name), None)
            raw = read_from_archive(arch, ipath) if arch and ipath else None
            self._img(soh_to_png_bytes(raw) if raw else None)
        elif route == '/bg-browser':
            # Background scene browser for pack 26/27.
            # Lists scenes extracted from filenames; click a scene to view thumbnails.
            # Visit http://localhost:8765/bg-browser
            import re as _re
            qs       = parse_qs(urlparse(self.path).query)
            selected = unquote(qs.get('scene', [''])[0])
            pack26   = next((p for p in self.mods_dir.iterdir() if '3DE - 26' in p.name), None)
            ootpack  = next((p for p in self.mods_dir.iterdir() if p.name.startswith('001_')), None)
            if not pack26:
                self._html('<h2>Pack 26 not found</h2>')
                return
            all_names = sorted(archive_names(pack26))
            # Extract scene prefix from filename: everything before Tex_ / _room / _scene
            def scene_of(path):
                fname = path.split('/')[-1]
                m = _re.match(r'^([A-Za-z][A-Za-z0-9_]*?)(?:Tex_|_room\d|_scene)', fname)
                return m.group(1) if m else None
            scenes = sorted(set(s for p in all_names for s in [scene_of(p)] if s))
            # Already-excluded scenes
            excluded_scenes = ['hairal_niwa', 'spot00', 'spot15']
            # Sidebar scene list
            sidebar_items = []
            for s in scenes:
                is_excl  = any(e in s for e in excluded_scenes)
                style    = 'color:#c00;' if is_excl else ''
                selected_style = 'font-weight:bold;background:#ffe;' if s == selected else ''
                sidebar_items.append(
                    f'<div style="padding:3px 6px;cursor:pointer;{style}{selected_style}"'
                    f' onclick="location=\'/bg-browser?scene={s}\'">{s}'
                    + (' ✗' if is_excl else '') + '</div>'
                )
            sidebar = '\n'.join(sidebar_items)
            # Thumbnail grid for selected scene
            if selected:
                scene_paths = [p for p in all_names if (scene_of(p) or '') == selected]
                oot_names   = archive_names(ootpack) if ootpack else frozenset()
                thumbs = []
                for p in sorted(scene_paths):
                    fname = p.split('/')[-1]
                    djipi_url = f'/bg-img?pack=26&path={p}'
                    # find matching OoT Reloaded path
                    oot_match = next((n for n in oot_names if fname in n), None)
                    oot_url   = f'/bg-img?pack=oot&path={oot_match}' if oot_match else ''
                    oot_cell  = f'<img src="{oot_url}" style="max-width:200px;max-height:200px;image-rendering:pixelated">' if oot_url else '<em style="color:#999">no OoT match</em>'
                    thumbs.append(
                        f'<tr><td style="font-size:11px;max-width:220px;word-break:break-all">{fname}</td>'
                        f'<td><img src="{djipi_url}" style="max-width:200px;max-height:200px;image-rendering:pixelated"></td>'
                        f'<td>{oot_cell}</td></tr>'
                    )
                grid = (f'<h3>{selected} ({len(scene_paths)} textures)</h3>'
                        f'<table border=1 cellpadding=4><tr><th>File</th><th>Djipi</th><th>OoT Reloaded</th></tr>'
                        + '\n'.join(thumbs) + '</table>')
            else:
                grid = '<p style="color:#666">← Click a scene to preview its textures.</p>'
            html = f'''<!doctype html><html><head><title>BG Browser</title></head><body>
<h2>Background Scene Browser (Pack 26/27)</h2>
<p>Red = already excluded (using OoT Reloaded). Click a scene to preview.</p>
<div style="display:flex;gap:20px;align-items:flex-start">
  <div style="min-width:200px;max-height:90vh;overflow-y:auto;border:1px solid #ccc;padding:4px;font-family:monospace;font-size:13px">
    {sidebar}
  </div>
  <div style="flex:1;overflow-x:auto">{grid}</div>
</div>
</body></html>'''
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(html.encode())
        elif route == '/debug-tex':
            # Inspect raw texture header from any archive.
            # ?archive=oot.o2r&path=textures/vr_ALVR_static/gMarketPotionShopBgTex
            qs       = parse_qs(urlparse(self.path).query)
            ipath    = unquote(qs.get('path',    [''])[0])
            arc_name = unquote(qs.get('archive', [''])[0])
            arch     = next((p for p in self.all_archives if p.name == arc_name), None)
            raw      = read_from_archive(arch, ipath) if arch and ipath else None
            if not raw:
                self._html(f'<pre>NOT FOUND: archive={arc_name!r} path={ipath!r}</pre>')
                return
            import struct as _s
            lines = [f'archive: {arc_name}', f'path: {ipath}', f'size: {len(raw)} bytes',
                     f'magic: {raw[:8].hex()}', '']
            if len(raw) >= 0x60 and raw[4:8] in (b'XETO', b'OTEX'):
                flag = _s.unpack_from('<I', raw, 0x4C)[0]
                w    = _s.unpack_from('<I', raw, 0x44)[0]
                h    = _s.unpack_from('<I', raw, 0x48)[0]
                tt   = _s.unpack_from('<I', raw, 0x40)[0]
                if flag in (0, 1):
                    rs = _s.unpack_from('<I', raw, 0x58)[0]
                    pix_off = 0x5C
                else:
                    rs = flag
                    pix_off = 0x50
                bpp = rs / (w * h) if w * h else 0
                pix_end = pix_off + rs
                extra = len(raw) - pix_end
                lines += [f'texType: {tt}', f'w: {w}', f'h: {h}', f'flag: {flag:#x}',
                          f'raw_size: {rs}', f'bpp: {bpp:.3f}', f'pix_off: {pix_off:#x}',
                          f'pix_end: {pix_end}', f'extra_after_pixels: {extra} bytes',
                          f'has_ci8_tlut (>=512): {extra >= 512}',
                          f'has_ci4_tlut (>=32):  {extra >= 32}', '']
            lines += [f'first 96 bytes:']
            for off in range(0, min(96, len(raw)), 16):
                lines.append(f'  {off:02x}: {raw[off:off+16].hex(" ")}')
            self._html('<pre>' + '\n'.join(lines) + '</pre>')
        elif route == '/debug-paths':
            # Scan every archive and return all internal paths, grouped by archive.
            # Visit http://localhost:8765/debug-paths?q=mamenoki to filter.
            qs    = parse_qs(urlparse(self.path).query)
            query = unquote(qs.get('q', [''])[0]).lower()
            result = {}
            for p in sorted(self.mods_dir.iterdir()):
                if p.suffix.lower() not in ('.otr', '.o2r'):
                    continue
                names = sorted(archive_names(p))
                if query:
                    names = [n for n in names if query in n.lower()]
                result[p.name] = names
            self._json(json.dumps(result, indent=2))
        elif route == '/pack-master-log':
            qs     = parse_qs(urlparse(self.path).query)
            offset = int(qs.get('offset', ['0'])[0])
            self._json(json.dumps({
                'lines': _pack_state['lines'][offset:],
                'done':  _pack_state['done'],
                'error': _pack_state['error'],
            }))
        elif route == '/rescan-status':
            self._json(json.dumps({
                'running': _rescan_state['running'],
                'done':    _rescan_state['done'],
                'error':   _rescan_state['error'],
            }))
        elif route == '/settings':
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
                    'mods_dir':        mods_dir_str,
                    'base_game_file':  base_game_str,
                    'master_dir':      master_dir_str,
                }
            self._json(json.dumps({'active_game': active_game, 'games': games_out}))
        elif route.startswith('/game-img/'):
            img_name = Path(route.split('/')[-1]).name  # prevent path traversal
            img_path = SCRIPT_DIR / img_name
            if img_path.suffix.lower() == '.png' and img_path.exists():
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.end_headers()
                self.wfile.write(img_path.read_bytes())
            else:
                self.send_response(404); self.end_headers()
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path == '/preview-geo-build':
            length = int(self.headers.get('Content-Length', 0))
            body   = json.loads(self.rfile.read(length))
            obj_key  = body.get('obj_key', '')
            geo_stem = body.get('geo_stem', '')
            try:
                with FileLock(CHOICES_LOCK_FILE, timeout=15):
                    original = json.loads(CHOICES_FILE.read_text()) if CHOICES_FILE.exists() else {}
                preview = dict(original)
                apply_geo_build(obj_key, geo_stem, self.all_archives,
                                self.archive_names_cache, self.image_paths_cache, preview)
                changes = {}
                for k in set(original) | set(preview):
                    if original.get(k) != preview.get(k):
                        changes[k] = preview.get(k)  # None = deleted
                # Effective winner for every image path under obj_key
                sorted_arches = sorted(self.all_archives, key=lambda a: a.stem)
                all_img_paths = set()
                for arch, imgs in self.image_paths_cache.items():
                    all_img_paths.update(p for p in imgs if p.startswith(obj_key))
                effective = {}
                for p in all_img_paths:
                    if p in preview:
                        effective[p] = preview[p]
                    else:
                        effective[p] = _res_winner(
                            p, sorted_arches,
                            self.image_paths_cache, self.image_dims_cache
                        )
                self._json(json.dumps({'ok': True, 'changes': changes, 'effective': effective}))
            except Exception as e:
                self._json(json.dumps({'ok': False, 'error': str(e)}))
            return

        elif self.path == '/set-geo-build':
            length = int(self.headers.get('Content-Length', 0))
            body   = json.loads(self.rfile.read(length))
            obj_key  = body.get('obj_key', '')
            geo_stem = body.get('geo_stem', '')
            try:
                with FileLock(CHOICES_LOCK_FILE, timeout=15):
                    existing = json.loads(CHOICES_FILE.read_text()) if CHOICES_FILE.exists() else {}
                    changed = apply_geo_build(
                        obj_key, geo_stem,
                        self.all_archives,
                        self.archive_names_cache,
                        self.image_paths_cache,
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
                self._json(json.dumps({'ok': True, 'changed': changed}))
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'ok': False, 'error': str(e)}).encode())

        elif self.path == '/submit-browse-choices':
            length = int(self.headers.get('Content-Length', 0))
            body   = self.rfile.read(length)
            try:
                new_choices = json.loads(body)
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
                        self.all_archives,
                        self.archive_names_cache,
                        self.image_paths_cache,
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
                self._json(json.dumps({
                    "ok": True,
                    "count": len(new_choices),
                    "geo_excluded": geo_added,
                    "geo_cleared": geo_cleared,
                }))
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode())
        elif self.path == '/settings':
            global MODS_DIR, BASE_GAME_FILE, MASTER_DIR, active_game
            length = int(self.headers.get('Content-Length', 0))
            body   = json.loads(self.rfile.read(length))
            new_mods   = body.get('mods_dir',       '').strip()
            new_base   = body.get('base_game_file',  '').strip()
            new_master = body.get('master_dir',      '').strip()
            if not new_mods or not new_master:
                self._json(json.dumps({'ok': False, 'error': 'Mods dir and output destination are required'}))
                return
            _games_config[active_game] = {'mods_dir': new_mods, 'base_game_file': new_base, 'master_dir': new_master}
            _save_config()
            MODS_DIR       = Path(new_mods)
            BASE_GAME_FILE = Path(new_base) if new_base else BASE_GAME_FILE
            MASTER_DIR     = Path(new_master)
            with _rescan_lock:
                if not _rescan_state['running']:
                    threading.Thread(target=_do_scan, daemon=True).start()
            self._json(json.dumps({'ok': True}))
        elif self.path == '/switch-game':
            length = int(self.headers.get('Content-Length', 0))
            body   = json.loads(self.rfile.read(length))
            game   = body.get('game', '')
            if game not in GAME_DEFS:
                self._json(json.dumps({'ok': False, 'error': f'Unknown game: {game}'}))
                return
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
                    threading.Thread(target=_do_scan, daemon=True).start()
            self._json(json.dumps({
                'ok': True,
                'mods_dir':       str(MODS_DIR)       if MODS_DIR       else '',
                'base_game_file': str(BASE_GAME_FILE) if BASE_GAME_FILE else '',
                'master_dir':     str(MASTER_DIR)     if MASTER_DIR     else '',
            }))
        elif self.path == '/pack-master':
            with _pack_lock:
                if _pack_state['running']:
                    self._json(json.dumps({'ok': False, 'error': 'Already running'}))
                    return
                _pack_state.update({'running': True, 'lines': [], 'done': False, 'error': None})
            threading.Thread(target=_pack_worker, daemon=True).start()
            self._json(json.dumps({'ok': True}))
        else:
            self.send_response(404); self.end_headers()

    def _html(self, content):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(content.encode())

    def _json(self, payload):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(payload.encode())

    def _img(self, data):
        if data:
            self.send_response(200)
            self.send_header('Content-Type', 'image/png')
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

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


def _run_server(window=None):
    print(f"Graphical Texture Picker  —  mods: {MODS_DIR}  port: {PORT}")
    _do_scan()

    server = ThreadedTCPServer(('', PORT), Handler)
    server.allow_reuse_address = True
    print(f"\nListening on http://localhost:{PORT}")
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    if window is not None:
        # pywebview path: navigate the native window to the app; the webview
        # event loop in the main thread keeps the process alive until the
        # window is closed, at which point all daemon threads die cleanly.
        window.load_url(f'http://localhost:{PORT}/master-browse')
    else:
        webbrowser.open(f'http://localhost:{PORT}')
        print("  Press Ctrl+C to stop.")
        try:
            t.join()
        except KeyboardInterrupt:
            print("\nShutting down.")
            server.shutdown()


def main():
    try:
        import webview
        window = webview.create_window(
            'Graphical Texture Picker', html=_LOADING_HTML,
            width=1440, height=900, min_size=(800, 600),
        )
        webview.start(_run_server, window, gui='edgechromium')
    except ImportError:
        _run_server()


if __name__ == '__main__':
    main()
