#!/usr/bin/env python3
"""
resolve_markers.py — _triage.csv markup → markers on the clips in DaVinci Resolve's Media Pool.

Clips on the timelines from make_timeline.py (.otio) already carry their markers. This script
puts the same markers on the source clips in the Media Pool, so that any clip you put on a
timeline later comes with its markers too.

Resolve runs menu scripts in a sandbox (Lua only in the free version, no file access, no way to
start programs), so the markers are written INTO the menu scripts here:
  camera_triage - media pool markers   markers on every Media Pool clip that has markup
  camera_triage - remove markers       remove the markers these scripts added (yours stay)
in Workspace → Scripts (restart Resolve once when the entries are new).

make_timeline.py updates them on every run, so usually there is nothing to do by hand. The data
accumulates: each run updates the folders it read and keeps the others (a cache in
%LOCALAPPDATA%\\camera_triage), so the menu knows every trip, whichever Resolve project is open.
By hand — e.g. the whole archive at once, or after changing the markup without a new timeline:
  py tools\\resolve_markers.py "D:\\Footage"
If DaVinci Resolve is not installed (no Fusion\\Scripts folder), nothing is written.

Markers: labels with their colors, i/o clips with their duration, notes at moments with their
text, notes for the file at the start. An LRV/LRF proxy gets the markup of its MP4. Rerunning a
menu entry replaces its markers; markers placed by hand are left alone.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import triage_plan as tp                    # noqa: E402  reading _triage.csv, labels and colors
import make_timeline as mt                  # noqa: E402  the same markers as on the timeline

LUA_MODULE = Path(__file__).with_name("resolve_markers.lua")
ENTRIES = {"camera_triage - media pool markers": "mediapool",
           "camera_triage - remove markers": "remove"}
# environment overrides (tests): CAMERA_TRIAGE_RESOLVE_SCRIPTS = folder for the menu scripts or
# "none" (do not write them); CAMERA_TRIAGE_CACHE = the cache file
ENV_SCRIPTS, ENV_CACHE = "CAMERA_TRIAGE_RESOLVE_SCRIPTS", "CAMERA_TRIAGE_CACHE"


def scripts_dir():
    """Resolve's Workspace → Scripts folder for all pages, or None if Resolve is not installed."""
    env = os.environ.get(ENV_SCRIPTS)
    if env is not None:
        return None if env.strip().lower() in ("", "none") else Path(env)
    scripts = Path(os.environ.get("APPDATA", "~")).expanduser() / "Blackmagic Design/DaVinci Resolve/Support/Fusion/Scripts"
    return scripts / "Utility" if scripts.is_dir() else None


def cache_path():
    env = os.environ.get(ENV_CACHE)
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "camera_triage" / "resolve_markers.json"


# ---------------------------------------------------------------- markup

def proxy_names(fname):
    """File names under which Resolve may hold this video: itself and its LRV/LRF proxies."""
    up, stem = fname.upper(), Path(fname).stem
    names = [fname]
    if up[:2] in ("GX", "GH") and up.endswith(".MP4"):
        names.append("GL" + stem[2:] + ".LRV")                  # GoPro: GX010123.MP4 → GL010123.LRV
    else:
        names += [stem + ".LRF", stem + ".LRV"]                  # DJI and others: same name
    return names


def file_markers(d):
    """[(t, duration, color, name, note)] of a file, seconds from its start, sorted by time."""
    out = []
    for t, dur, title, note, color in mt.segment_markers(d, 0.0, float("inf"), with_file_notes=True):
        out.append([round(t, 3), round(dur, 3) if dur else 0, color, title, note or ""])
    return sorted(out, key=lambda m: m[0])


def key(path):
    """The same key as M.key in resolve_markers.lua: full path, upper case, "\\" separators."""
    return str(path).replace("/", "\\").upper()


def folder_markers(csv_path):
    """{file name: markers} of one _triage.csv, or None if it has a foreign format."""
    rows = tp.read_rows(csv_path)
    if not tp.triage_csv_ok(rows, csv_path):
        return None
    return {fname: m for fname, d in tp.collect(rows).items() if (m := file_markers(d))}


def data_from(cache):
    """Cache {folder: {file: markers}} → {key: markers} incl. proxies (proxies share the list)."""
    data = {}
    for folder, files in cache.items():
        for fname, markers in files.items():
            for name in proxy_names(fname):
                data[key(Path(folder) / name)] = markers
    return data


# ---------------------------------------------------------------- Lua

def lua_str(s):
    """A Lua string literal (UTF-8 stays as is)."""
    s = str(s).replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return '"' + s + '"'


def lua_num(x):
    return repr(x) if isinstance(x, float) else str(x)


def lua_data(data):
    """DATA table: files with the same markers (an MP4 and its proxies) share one list."""
    lines, ids = ["local DATA = {}"], {}
    for k, markers in sorted(data.items()):
        ident = ids.get(id(markers))
        if ident is None:
            ident = ids[id(markers)] = f"m{len(ids) + 1}"
            items = ", ".join("{" + ", ".join([lua_num(t), lua_num(dur), lua_str(color), lua_str(name), lua_str(note)])
                              + "}" for t, dur, color, name, note in markers)
            lines.append(f"local {ident} = {{{items}}}")
        lines.append(f"DATA[{lua_str(k)}] = {ident}")
    return "\n".join(lines)


def menu_script(mode, data_lua, baked, folders):
    module = LUA_MODULE.read_text(encoding="utf-8")
    return "\n".join([
        f"-- camera_triage — {mode}. Generated by tools/resolve_markers.py on {baked}; do not edit.",
        f"-- Markup of {folders} footage folder(s); make_timeline.py and resolve_markers.py update it.",
        data_lua,
        "local M = (function()",
        module,
        "end)()",
        "local app = resolve or Resolve()",
        f'M.run(app, "{mode}", DATA, {lua_str(baked)})',
        "",
    ])


def write_scripts(target, data, folders):
    target.mkdir(parents=True, exist_ok=True)
    for old in target.glob("camera_triage - *"):            # earlier entries (timeline markers, .py)
        if old.stem not in ENTRIES or old.suffix != ".lua":
            old.unlink()
    baked = time.strftime("%Y-%m-%d %H:%M")
    written = []
    for name, mode in ENTRIES.items():
        data_lua = lua_data(data) if mode != "remove" else "local DATA = {}"
        path = target / f"{name}.lua"
        path.write_text(menu_script(mode, data_lua, baked, folders), encoding="utf-8", newline="\n")
        written.append(path)
    return written


# ---------------------------------------------------------------- update

def update_menu(csv_paths, clear=False, quiet=False):
    """Updates the cache with these _triage.csv files (drops folders whose _triage.csv is gone)
    and rewrites the menu scripts. → (scripts, files, markers) or None if Resolve is missing."""
    target = scripts_dir()
    if target is None:
        if not quiet:
            print("DaVinci Resolve not found — Media Pool marker scripts skipped")
        return None
    path = cache_path()
    cache = {}
    if path.exists() and not clear:
        try:
            cache = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            cache = {}
    for csv_path in csv_paths:
        folder = str(csv_path.parent.resolve())
        files = folder_markers(csv_path)
        if files is None:
            print(f"⚠ {csv_path}: unexpected format (re-saved in another program?), skipped")
            continue
        cache[folder] = files
    cache = {f: files for f, files in cache.items() if files and (Path(f) / tp.CSV_NAME).exists()}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
    written = write_scripts(target, data_from(cache), len(cache))
    nfiles = sum(len(files) for files in cache.values())
    nmarkers = sum(len(m) for files in cache.values() for m in files.values())
    if not quiet:
        print(f"Resolve Media Pool markers: {nfiles} file(s) in {len(cache)} folder(s), {nmarkers} marker(s) → "
              "Workspace → Scripts → camera_triage - media pool markers")
    return written, nfiles, nmarkers


def main():
    tp.setup_console()
    ap = argparse.ArgumentParser(description="Markup → Resolve menu scripts that put markers on Media Pool clips")
    ap.add_argument("folders", nargs="*", type=Path, help="footage folders (searched recursively for _triage.csv)")
    ap.add_argument("--clear", action="store_true", help="forget the folders read earlier")
    ap.add_argument("--skip", default="Clips", help="subfolders not to search, comma-separated (Clips)")
    args = ap.parse_args()
    skip = tp.label_set(args.skip)
    csvs = [c for c in tp.find_csvs(args.folders)
            if not set(c.relative_to(c.anchor).parts[:-1]) & skip]
    if args.folders and not csvs:
        print(f"No {tp.CSV_NAME} found")
    res = update_menu(csvs, clear=args.clear)
    if res:
        print("(restart Resolve once if these menu entries are new; the report is in Workspace → Console)")


if __name__ == "__main__":
    main()
