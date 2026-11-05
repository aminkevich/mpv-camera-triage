#!/usr/bin/env python3
"""
resolve_markers.py — _triage.csv markup → markers ON CLIPS in DaVinci Resolve.

Timeline markers (the .edl from make_timeline.py) belong to the timeline: once clips are moved,
trimmed or removed, they no longer point at the right moments. Clip markers belong to the clip:
they move with it, stay on their frame when it is trimmed and disappear with it. The markup
times in _triage.csv are counted from the start of the video file, exactly like clip markers.

Resolve runs menu scripts in a sandbox (Lua only in the free version, no file access, no way to
start programs), so this script does the reading here and BAKES the markers into the menu scripts:

  py tools\\resolve_markers.py "D:\\Footage\\2026-09_Turkey"

reads every _triage.csv under the given folders (recursively) and writes three scripts to
Resolve's Workspace → Scripts menu (they work right away; restart Resolve once if the menu
entries are new):
  camera_triage - timeline markers    every clip of the current timeline
  camera_triage - media pool markers  every clip in the Media Pool: clips you put on a timeline
                                      later come with their markers
  camera_triage - remove markers      remove the markers these scripts added (yours stay)

After changing the markup in mpv, run it again and click the menu entry again: the scripts'
markers are replaced, markers placed by hand are left alone. Markers: labels with the same
colors as the .edl, i/o clips with their duration, notes at moments with their text, notes for
the file at the start. An LRV/LRF proxy gets the markup of its MP4. Output of the menu scripts:
Workspace → Console.
"""
import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import triage_plan as tp                    # noqa: E402  reading _triage.csv, labels and colors
import make_timeline as mt                  # noqa: E402  the same markers as on the timeline

LUA_MODULE = Path(__file__).with_name("resolve_markers.lua")
SCRIPTS_DIR = Path(os.environ.get("APPDATA", "")) / "Blackmagic Design/DaVinci Resolve/Support/Fusion/Scripts/Utility"
ENTRIES = {"camera_triage - timeline markers": "timeline",
           "camera_triage - media pool markers": "mediapool",
           "camera_triage - remove markers": "remove"}


def parse_args():
    ap = argparse.ArgumentParser(description="Bake _triage.csv markup into Resolve menu scripts (clip markers)")
    ap.add_argument("folders", nargs="+", type=Path, help="footage folders (searched recursively for _triage.csv)")
    ap.add_argument("--scripts", type=Path, default=SCRIPTS_DIR,
                    help="where to write the menu scripts (default: Resolve's Fusion\\Scripts\\Utility)")
    ap.add_argument("--skip", default="Clips", help="subfolders not to search, comma-separated (Clips)")
    return ap.parse_args()


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
        out.append((round(t, 3), round(dur, 3) if dur else 0, color, title, note or ""))
    return sorted(out, key=lambda m: m[0])


def key(path):
    """The same key as M.key in resolve_markers.lua: full path, upper case, "\\" separators."""
    return str(path).replace("/", "\\").upper()


def collect_markers(folders, skip=("Clips",)):
    """{key: markers} for every marked file under folders (+ its proxies), and stats."""
    data, files, total, bad = {}, 0, 0, []
    for csv_path in tp.find_csvs(folders):
        if set(csv_path.relative_to(csv_path.anchor).parts[:-1]) & set(skip):
            continue
        rows = tp.read_rows(csv_path)
        if not tp.triage_csv_ok(rows, csv_path):
            bad.append(csv_path)
            continue
        folder = csv_path.parent.resolve()
        for fname, d in tp.collect(rows).items():
            markers = file_markers(d)
            if not markers:
                continue
            files += 1
            total += len(markers)
            for name in proxy_names(fname):
                data[key(folder / name)] = markers
    return data, files, total, bad


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


def menu_script(mode, data_lua, baked, sources):
    module = LUA_MODULE.read_text(encoding="utf-8")
    return "\n".join([
        f"-- camera_triage — {mode} markers. Generated by tools/resolve_markers.py on {baked}; do not edit.",
        "-- Markup from: " + "; ".join(str(s) for s in sources),
        "-- Run the generator again after changing the markup in mpv.",
        data_lua,
        "local M = (function()",
        module,
        "end)()",
        "local app = resolve or Resolve()",
        f'M.run(app, "{mode}", DATA, {lua_str(baked)})',
        "",
    ])


def write_scripts(target, data, sources):
    target.mkdir(parents=True, exist_ok=True)
    for old in target.glob("camera_triage - *"):            # earlier launchers (.py, .lua)
        if old.stem not in ENTRIES or old.suffix != ".lua":
            old.unlink()
    baked = time.strftime("%Y-%m-%d %H:%M")
    data_lua = lua_data(data)
    written = []
    for name, mode in ENTRIES.items():
        path = target / f"{name}.lua"
        path.write_text(menu_script(mode, data_lua, baked, sources), encoding="utf-8", newline="\n")
        written.append(path)
    return written


def main():
    tp.setup_console()
    args = parse_args()
    if not args.scripts.parent.exists():
        sys.exit(f"Resolve's script folder not found: {args.scripts.parent}  (is DaVinci Resolve installed? "
                 "or give --scripts)")
    data, files, total, bad = collect_markers(args.folders, tp.label_set(args.skip))
    for path in bad:
        print(f"⚠ {path}: unexpected format (re-saved in another program?), skipped")
    written = write_scripts(args.scripts, data, [p.resolve() for p in args.folders])
    print(f"Markup of {files} file(s), {total} marker(s) baked into the Resolve menu:")
    for path in written:
        print(f"  {path}")
    print("\nResolve: open the timeline → Workspace → Scripts → camera_triage - timeline markers\n"
          "(restart Resolve once if these menu entries are new; the report is in Workspace → Console)")


if __name__ == "__main__":
    main()
