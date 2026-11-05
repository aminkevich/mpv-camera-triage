"""
resolve_markers.py — _triage.csv markup → markers ON CLIPS in DaVinci Resolve.

Timeline markers (the .edl from make_timeline.py) belong to the timeline: once clips are moved,
trimmed or removed, they no longer point at the right moments. Clip markers belong to the clip:
they move with it, stay on their frame when it is trimmed and disappear with it. The markup
times in _triage.csv are counted from the start of the video file, which is exactly how Resolve
counts clip markers.

Run inside Resolve: Workspace → Scripts → camera_triage … (install.ps1 adds the menu entries).
The free Resolve lists only Lua scripts in that menu, so the entries run resolve_markers.lua:
it collects the clips in Resolve, asks this script for their markers (--plan, with the Python
bundled with Resolve) and puts them on the clips. Output goes to Workspace → Console.
  timeline markers  — every clip of the current timeline (all video tracks)
  media pool        — every clip in the Media Pool: clips you put on a timeline later come
                      with their markers
  remove            — remove the markers this script added (yours stay)

For each clip: its file → _triage.csv in that file's folder → the markup of that file (for an
LRV/LRF proxy — the markup of its MP4). Markers: labels with the same colors as the .edl, i/o
clips with their duration, notes at moments with their text, notes for the file at the start.
Running again replaces the script's markers with the current markup (so a changed markup is
picked up); markers you placed yourself are left alone.
"""
import re
import sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import triage_plan as tp                    # noqa: E402  reading _triage.csv, labels and colors
import make_timeline as mt                  # noqa: E402  the same markers as on the timeline

TAG = "camera_triage"                       # customData of our markers: rerun and remove find them
MODES = ("timeline", "mediapool", "remove")


# ---------------------------------------------------------------- markup

def source_names(fname):
    """Names under which a file can be in _triage.csv: itself, and for a proxy — its original."""
    names, up, stem = [fname], fname.upper(), Path(fname).stem
    m = re.match(r"^GL(\d{6})\.LRV$", up)
    if m:
        names += [f"GX{m[1]}.MP4", f"GH{m[1]}.MP4"]             # GoPro: GL010123.LRV → GX010123.MP4
    elif up.endswith((".LRV", ".LRF")):
        names += [stem + ".MP4", stem + ".MOV"]                  # DJI and others: same name
    return names


def markup_for(path, cache):
    """Markup of a video file from _triage.csv in its folder, or None."""
    p = Path(path)
    folder = p.parent
    if folder not in cache:
        files, csv_path = {}, folder / tp.CSV_NAME
        if csv_path.exists():
            rows = tp.read_rows(csv_path)
            if tp.triage_csv_ok(rows, csv_path):
                files = {name.upper(): d for name, d in tp.collect(rows).items()}
            else:
                print(f"  ⚠ {csv_path}: unexpected format (re-saved in another program?), skipped")
        cache[folder] = files
    for name in source_names(p.name):
        d = cache[folder].get(name.upper())
        if d:
            return d
    return None


def exact_fps(fps):
    """Resolve reports 59.94 / 29.97 / 23.976 — the exact NTSC rates are 60000/1001 etc."""
    fps = float(fps)
    for n in (24, 30, 48, 60, 120, 240):
        if abs(fps - n * 1000 / 1001) < 0.01:
            return Fraction(n * 1000, 1001)
    return Fraction(fps).limit_denominator(1001)


def plan_markers(d, fps, frames=None):
    """[(frame, duration, color, name, note)] in source frames from the start of the file.
    Resolve keeps one marker per frame, so a marker on an occupied frame moves to the next one."""
    out, used = [], set()
    for t, dur, title, note, color in sorted(mt.segment_markers(d, 0.0, float("inf"), with_file_notes=True),
                                             key=lambda m: m[0]):
        f = max(0, round(Fraction(t).limit_denominator(1000) * fps))
        while f in used:
            f += 1
        if frames and f >= frames:
            continue
        used.add(f)
        length = max(1, round(Fraction(dur).limit_denominator(1000) * fps)) if dur else 1
        if frames:
            length = max(1, min(length, frames - f))
        out.append((f, length, color, title, note or ""))
    return out


# ---------------------------------------------------------------- Resolve

def clear_markers(item):
    """Removes the markers this script added; returns how many."""
    n = 0
    for frame, info in list((item.GetMarkers() or {}).items()):
        if (info or {}).get("customData") == TAG and item.DeleteMarkerAtFrame(frame):
            n += 1
    return n


def mark(item, mp_item, cache):
    """Markers from the markup on item (a MediaPoolItem or a TimelineItem; both count marker
    frames from the start of the source file). → (added, planned) or None if there is no markup."""
    path = mp_item.GetClipProperty("File Path") or ""
    fps = mp_item.GetClipProperty("FPS")
    d = markup_for(path, cache) if path else None
    if not d or not fps:
        return None
    try:
        frames = int(mp_item.GetClipProperty("Frames") or 0) or None
    except ValueError:
        frames = None
    clear_markers(item)
    plan = plan_markers(d, exact_fps(fps), frames)
    added = sum(1 for f, length, color, name, note in plan
                if item.AddMarker(f, color, name, note, length, TAG))
    return added, len(plan)


def timeline_items(timeline):
    items = []
    for i in range(1, (timeline.GetTrackCount("video") or 0) + 1):
        items += timeline.GetItemListInTrack("video", i) or []
    return items


def media_pool_clips(folder):
    clips = list(folder.GetClipList() or [])
    for sub in folder.GetSubFolderList() or []:
        clips += media_pool_clips(sub)
    return clips


def run(resolve, mode):
    """Entry point of the Workspace → Scripts menu entries (see install.ps1)."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    project = resolve.GetProjectManager().GetCurrentProject()
    if not project:
        print("camera_triage: no project is open")
        return
    timeline = project.GetCurrentTimeline()
    pool = media_pool_clips(project.GetMediaPool().GetRootFolder())
    if mode == "remove":
        targets = pool + (timeline_items(timeline) if timeline else [])
        removed = sum(clear_markers(item) for item in targets)
        print(f"camera_triage: removed {removed} marker(s) added by this script")
        return
    if mode == "timeline":
        if not timeline:
            print("camera_triage: no timeline is open")
            return
        pairs = [(item, item.GetMediaPoolItem()) for item in timeline_items(timeline)]
        where = f"timeline \"{timeline.GetName()}\""
    else:
        pairs = [(clip, clip) for clip in pool]
        where = "Media Pool"
    cache, done, markers, failed, skipped = {}, 0, 0, 0, []
    for item, mp_item in pairs:
        if mp_item is None:                         # titles, generators, compound clips
            continue
        res = mark(item, mp_item, cache)
        if res is None:
            skipped.append(item.GetName())
            continue
        added, planned = res
        done += 1
        markers += added
        failed += planned - added
    print(f"camera_triage: {where} — {done} clip(s) marked, {markers} marker(s)"
          + (f", {failed} not accepted by Resolve" if failed else ""))
    if skipped:
        names = sorted(set(skipped))
        print(f"  without markup ({len(names)}): " + ", ".join(names[:10])
              + (f" and {len(names) - 10} more" if len(names) > 10 else ""))


# ---------------------------------------------------------------- for resolve_markers.lua

def clean(text):
    return re.sub(r"[\t\r\n]+", " ", text or "").strip()


def plan_file(request, answer):
    """The free Resolve runs only Lua scripts from its menu, so resolve_markers.lua asks this
    script for the markers. request: lines "id<TAB>file path<TAB>fps<TAB>frames" (UTF-8).
    answer: "id<TAB>MARKUP" for a clip with markup (then its markers: "id<TAB>frame<TAB>duration
    <TAB>color<TAB>name<TAB>note"), or "id<TAB>NONE" for a clip without markup."""
    cache, out = {}, []
    for line in Path(request).read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        item_id, path, fps, frames = (line.split("\t") + ["", "", "", ""])[:4]
        d = markup_for(path, cache) if path else None
        try:
            fps = exact_fps(fps) if d else None
        except ValueError:
            fps = None
        if not d or not fps:
            out.append(f"{item_id}\tNONE")
            continue
        try:
            frames = int(float(frames)) or None
        except ValueError:
            frames = None
        out.append(f"{item_id}\tMARKUP")
        for f, length, color, name, note in plan_markers(d, fps, frames):
            out.append("\t".join([item_id, str(f), str(length), color, clean(name), clean(note)]))
    Path(answer).write_text("\n".join(out) + "\n", encoding="utf-8")


if __name__ == "__main__":
    tp.setup_console()
    if len(sys.argv) == 4 and sys.argv[1] == "--plan":
        plan_file(sys.argv[2], sys.argv[3])         # called by resolve_markers.lua
    else:
        # External run (Resolve Studio with external scripting enabled): py resolve_markers.py timeline
        import DaVinciResolveScript as dvr_script
        run(dvr_script.scriptapp("Resolve"), sys.argv[1] if len(sys.argv) > 1 else "timeline")
