# Graphical Texture Picker

A mod browser and packer toolset for **Ship of Harkinian** (the Ocarina of Time PC port). It lets you compare textures across multiple `.otr`/`.o2r` mod packs side-by-side, choose which pack wins per-texture, and build a merged master archive that SoH loads.

Runs as a native desktop app (via [pywebview](https://pywebview.flowrl.com/)) — no browser, no local server, no port, nothing to install beyond the app itself and its Python dependencies.

## Features

- Side-by-side texture comparison grid, per object/scene, across every pack you have installed
- Pick a winning pack per texture, or exclude a texture from the build entirely
- Automatic resolution-based fallback (highest pixel count wins) for anything you haven't chosen explicitly
- Built-in support for Djipi's 3DS geometry+texture pack pairs, including automatic conflict resolution between 3DS geometry and competing texture packs
- One-click build of a merged `999_Master.o2r`, copied straight into your SoH mods folder

## Getting started

**Run the prebuilt app:** grab the latest release zip, extract it anywhere, and run `Graphical Texture Picker.exe`. On first launch, open Settings and point it at your mods folder, base game file (`oot.o2r`), and SoH mods output folder.

**Run from source:**
```
pip install mpyq Pillow filelock pywebview
python otr_picker_server.py
```

**Build the master archive** (after making your selections in the app):
```
python otr_pack_master.py
```
Or use the in-app "Pack Master" button.

## How resolution works

For every texture path, in priority order:
1. Your explicit choice (if you picked one in the app)
2. Highest pixel count among the packs that have it
3. Alphabetically first pack name, as a final tie-break

## Building the standalone .exe

```
build_exe.bat
```
Produces a self-contained `dist/Graphical Texture Picker/` folder (PyInstaller, `--onedir`) — zip it up and it's good to distribute.
