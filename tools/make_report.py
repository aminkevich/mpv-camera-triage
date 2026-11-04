#!/usr/bin/env python3
"""
make_report.py — archive summary report from every _triage.csv (camera_triage.lua v3).

Walks the given folders recursively, reads every _triage.csv and looks for videos (MP4/MOV) on
disk, including folders without markup yet. A single folder with one _triage.csv is an archive
too, just with one row.

Three tables, from general to detailed (CSV: ";", UTF-8 with BOM — open in Excel/OnlyOffice):
  _report_folders.csv  one row per folder: what was shot (activity, dates), how many videos, how
                       many reviewed, ratings 0–3, duration, marks by label; TOTAL at the end.
  _report_files.csv    one row per video: status (reviewed / rated, no Enter / started / not opened),
                       rating, duration, shooting time, marks by label, notes for the file,
                       whether the file is on disk.
  _report_events.csv   every row of every _triage.csv — the same columns as _triage.csv,
                       plus folder, file rating and status. For filters like "every catch in
                       files rated 3".

--xlsx — the same as one workbook _report.xlsx (sheets "Folders", "Files", "Events", header filters);
requires openpyxl: py -m pip install openpyxl
--probe — missing duration and shooting time (unopened videos have none in the markup)
via ffprobe; without it such videos are not included in the duration totals.

The report is a snapshot: _triage.csv files are not changed, the report can be rebuilt any time.
It is fine to re-save the report in Excel (unlike _triage.csv).

Examples:
  py make_report.py "D:\\Footage"
  py make_report.py "D:\\Footage" --probe --xlsx
  py make_report.py "D:\\Footage\\2026-09_Turkey" --out "D:\\Reports"
"""
import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import triage_plan as tp                    # noqa: E402  shared _triage.csv reading

VIDEO_EXTS = {".mp4", ".mov"}
# label order as on the keyboard in camera_triage.lua (shared list in triage_plan);
# labels missing from it (old, renamed) get columns at the end — see main
LABELS = list(tp.LABELS)
TRIAGE_COLS = ["logged_at", "event", "activity", "file", "src", "time_ms", "end_ms", "time_hms",
               "label", "rating", "note", "speed_pct", "duration_ms", "duration_hms", "camera_time"]

DONE, DRAFT, STARTED, NEW = "reviewed", "rated, no Enter", "started", "not opened"
COMPLETE, NOT_MARKED, BAD_CSV, COPY = "complete", "not marked up", "CSV in another format", "copy: "
ON_DISK_NONE = "no"
MARKUP_DIFFERS = " (markup differs)"


def folder_cols(labels=LABELS):
    return (["folder", "status", "activity", "shot_from", "shot_to", "videos", "copies",
             "done", "draft", "started", "not_opened", "rated_0", "rated_1", "rated_2", "rated_3",
             "duration_hms", "duration_ms", "reviewed_hms", "reviewed_ms", "unknown_duration",
             "marks", "clips", "notes"] + list(labels) + ["last_triage"])


def file_cols(labels=LABELS):
    return (["folder", "file", "status", "rating", "rating_draft", "activity", "camera_time",
             "duration_ms", "duration_hms", "marks", "clips", "clips_hms", "notes"] + list(labels)
            + ["file_notes", "viewed_as", "on_disk", "copy_of", "copies", "last_triage"])


EVENT_COLS = ["folder"] + TRIAGE_COLS + ["file_rating", "file_status", "copy_of"]

OUT_NAMES = {"folders": "_report_folders.csv", "files": "_report_files.csv",
             "events": "_report_events.csv"}
XLSX_NAME = "_report.xlsx"


def parse_args():
    ap = argparse.ArgumentParser(description="Archive summary report from every _triage.csv")
    ap.add_argument("folders", nargs="+", type=Path, help="archive folder (or several)")
    ap.add_argument("--out", type=Path, help="where to put the report (default: the first folder)")
    ap.add_argument("--probe", action="store_true",
                    help="missing duration and shooting time via ffprobe (unopened videos)")
    ap.add_argument("--xlsx", action="store_true", help=f"also {XLSX_NAME} (requires openpyxl)")
    ap.add_argument("--skip", default="Clips",
                    help="subfolders not to walk, comma-separated (Clips)")
    return ap.parse_args()


# ---------------------------------------------------------------- collecting

def hms_ms(ms):
    return tp.hms((ms or 0) / 1000) if ms else ""


def probe(path):
    """(duration_ms, camera_time) of an unopened video via ffprobe; (None, "") on error."""
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration:format_tags=creation_time",
           "-of", "json", str(path)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=60)
        fmt = json.loads(res.stdout or "{}").get("format") or {}
        dur = float(fmt.get("duration") or 0)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None, ""
    ct = ((fmt.get("tags") or {}).get("creation_time") or "")[:19].replace("T", " ")
    return (round(dur * 1000) if dur else None), ct


def scan(roots, skip):
    """{folder: path to _triage.csv or None} — every folder with markup or with videos."""
    found = {}
    for root in roots:
        if not root.exists():
            print(f"⚠ Folder not found: {root}")
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in skip)
            here = Path(dirpath).resolve()
            if tp.CSV_NAME in filenames:
                found[here] = here / tp.CSV_NAME
            elif any(Path(f).suffix.lower() in VIDEO_EXTS for f in filenames):
                found.setdefault(here, None)
    return found


def new_file(name):
    return {"file": name, "activity": "", "marks": 0, "clips": 0, "clips_ms": 0, "notes": 0,
            "labels": Counter(), "file_notes": [], "rating": None, "done_at": -1,
            "draft": None, "draft_at": -1, "duration_ms": None, "camera_time": "",
            "src": set(), "first": "", "last": "", "rows": False}


def read_folder(folder, csv_path, args):
    """→ (files: {NAME: dict}, events: [row], csv_state) for one folder."""
    files, events, state = {}, [], "none"
    rows = tp.read_rows(csv_path) if csv_path else []
    if csv_path:
        state = "ok" if tp.triage_csv_ok(rows, csv_path) else "bad"
    if state == "bad":
        rows = []
    for i, r in enumerate(rows):
        name = (r.get("file") or "").strip()
        if not name:
            continue
        f = files.setdefault(name.upper(), new_file(name))
        f["rows"] = True
        events.append(r)
        f["activity"] = (r.get("activity") or "").strip() or f["activity"]
        at = r.get("logged_at") or ""
        f["last"] = max(f["last"], at)
        f["first"] = min(f["first"], at) if f["first"] else at
        if r.get("src"):
            f["src"].add(r["src"].strip())
        ev = r.get("event")
        if ev in ("file_done", "file_partial"):
            rating = tp.to_int(r.get("rating"))
            if ev == "file_done":
                f["rating"], f["done_at"] = rating, i
            else:
                f["draft"], f["draft_at"] = rating, i
            f["duration_ms"] = tp.to_int(r.get("duration_ms")) or f["duration_ms"]
            f["camera_time"] = (r.get("camera_time") or "").strip() or f["camera_time"]
        elif ev == "mark":
            f["marks"] += 1
            f["labels"][(r.get("label") or "").strip()] += 1
        elif ev == "clip":
            f["clips"] += 1
            s, e = tp.to_int(r.get("time_ms")), tp.to_int(r.get("end_ms"))
            if s is not None and e is not None and e > s:
                f["clips_ms"] += e - s
            if (r.get("label") or "").strip():
                f["labels"][r["label"].strip()] += 1
        elif ev == "note":
            f["notes"] += 1
            if tp.to_int(r.get("time_ms")) is None and (r.get("note") or "").strip():
                f["file_notes"].append(r["note"].strip())

    # videos on disk that are not in the markup (proxies are not separate videos)
    for p in sorted(folder.iterdir()):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS and p.name.upper() not in files:
            files[p.name.upper()] = new_file(p.name)
    for f in files.values():
        src, is_proxy = tp.find_source(folder, f["file"])
        f["on_disk"] = ON_DISK_NONE if src is None else ("proxy " + src.suffix.lstrip(".").upper()
                                                         if is_proxy else "original")
        if src is not None and not is_proxy:
            f["size"] = src.stat().st_size          # a sign of a copy of the same video (mark_copies)
        if f["rating"] is not None or f["done_at"] >= 0:
            f["status"] = DONE
        elif f["draft_at"] >= 0:
            f["status"] = DRAFT
        elif f["rows"]:
            f["status"] = STARTED
        else:
            f["status"] = NEW
        if f["draft_at"] < f["done_at"]:
            f["draft"] = None                       # the draft is older than the final rating
        if args.probe and src is not None and (f["duration_ms"] is None or not f["camera_time"]):
            dur, ct = probe(src)                    # only what is missing: keep values from the markup
            f["duration_ms"] = f["duration_ms"] or dur
            f["camera_time"] = f["camera_time"] or ct
    return files, events, state


# ---------------------------------------------------------------- tables

def file_row(folder_name, f, labels=LABELS):
    row = {"folder": folder_name, "file": f["file"], "status": f["status"],
           "rating": f["rating"], "rating_draft": f["draft"], "activity": f["activity"],
           "camera_time": f["camera_time"], "duration_ms": f["duration_ms"],
           "duration_hms": hms_ms(f["duration_ms"]), "marks": f["marks"], "clips": f["clips"],
           "clips_hms": hms_ms(f["clips_ms"]), "notes": f["notes"],
           "file_notes": " | ".join(f["file_notes"]), "viewed_as": ",".join(sorted(f["src"])),
           "on_disk": f["on_disk"], "copy_of": f.get("copy_of", ""), "copies": f.get("copies", 0),
           "last_triage": f["last"]}
    for lab in labels:
        row[lab] = f["labels"].get(lab, 0)
    return row


def folder_row(folder_name, files, state, labels=LABELS):
    fs = list(files.values())
    st = Counter(f["status"] for f in fs)
    dates = sorted(f["camera_time"][:10] for f in fs if f["camera_time"])
    known = [f["duration_ms"] for f in fs if f["duration_ms"]]
    reviewed = [f["duration_ms"] for f in fs if f["duration_ms"] and f["status"] == DONE]
    acts = Counter(f["activity"] for f in fs if f["activity"])
    dup = [f for f in fs if f.get("copy_of")]
    if state == "bad":
        status = BAD_CSV
    elif dup and len(dup) == len(fs):
        src = Counter(f["copy_of"].split(" (")[0] for f in dup)
        status = COPY + ", ".join(s for s, _ in src.most_common())
    elif state == "none":
        status = NOT_MARKED
    elif st[DONE] == len(fs):
        status = COMPLETE
    else:
        status = f"partial ({st[DONE]} of {len(fs)})"
    row = {"folder": folder_name, "status": status,
           "activity": ", ".join(a for a, _ in acts.most_common()),
           "shot_from": dates[0] if dates else "", "shot_to": dates[-1] if dates else "",
           "videos": len(fs), "copies": len(dup), "done": st[DONE], "draft": st[DRAFT], "started": st[STARTED],
           "not_opened": st[NEW],
           "duration_ms": sum(known), "duration_hms": hms_ms(sum(known)),
           "reviewed_ms": sum(reviewed), "reviewed_hms": hms_ms(sum(reviewed)),
           "unknown_duration": len(fs) - len(known),
           "marks": sum(f["marks"] for f in fs), "clips": sum(f["clips"] for f in fs),
           "notes": sum(f["notes"] for f in fs),
           "last_triage": max((f["last"] for f in fs), default="")}
    for r in range(4):
        row[f"rated_{r}"] = sum(1 for f in fs if f["status"] == DONE and f["rating"] == r)
    for lab in labels:
        row[lab] = sum(f["labels"].get(lab, 0) for f in fs)
    return row


def copy_keys(f):
    """Signs of the same video in different folders. The name is not enough: after formatting the
    card a GoPro writes GX010001 again. File size — for originals on disk; time of the first
    markup row for the file — for copies of _triage.csv (copies share it, different videos do not);
    shooting time and duration — if the markup was done again; if the camera writes no time —
    the exact duration in ms (full GoPro chapters can have the same length, so only without a time)."""
    keys, name = [], f["file"].upper()
    if f.get("size"):
        keys.append(("size", name, f["size"]))
    if f["first"]:
        keys.append(("log", name, f["first"]))
    if f["camera_time"] and f["duration_ms"]:
        keys.append(("time", name, f["camera_time"], f["duration_ms"] // 1000))
    elif f["duration_ms"] and f["rows"]:
        keys.append(("dur", name, f["duration_ms"]))
    return keys


def mark_copies(entries):
    """entries: [(folder, file)] in folder order. One video in several folders → the primary copy
    is where the video is on disk and the folder has the most videos (the original footage folder,
    not a project with some of the files); on a tie — the first by path. The others get
    copy_of = the primary's folder (+ "markup differs"). Returns the number of copies."""
    per_folder = Counter(name for name, _ in entries)
    parent = list(range(len(entries)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    seen = {}
    for i, (_, f) in enumerate(entries):
        for k in copy_keys(f):
            if k in seen:
                parent[find(i)] = find(seen[k])
            else:
                seen[k] = i
    groups = {}
    for i in range(len(entries)):
        groups.setdefault(find(i), []).append(i)

    def sig(f):
        return (f["marks"], f["clips"], f["notes"], f["rating"], f["last"])

    n = 0
    for idx in groups.values():
        if len(idx) < 2:
            continue
        prim = max(idx, key=lambda i: (entries[i][1]["on_disk"] != ON_DISK_NONE,
                                       per_folder[entries[i][0]], -i))
        pname, pf = entries[prim]
        pf["copies"] = len(idx) - 1
        for i in idx:
            if i != prim:
                f = entries[i][1]
                f["copy_of"] = pname + (MARKUP_DIFFERS if sig(f) != sig(pf) else "")
                n += 1
    return n


def event_row(folder_name, r, files):
    f = files.get((r.get("file") or "").strip().upper())
    row = {"folder": folder_name, **{c: r.get(c, "") for c in TRIAGE_COLS}}
    row["file_rating"] = "" if not f or f["rating"] is None else f["rating"]
    row["file_status"] = f["status"] if f else ""
    row["copy_of"] = f.get("copy_of", "") if f else ""
    return row


# ---------------------------------------------------------------- output

def write_csv(path, cols, rows):
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, delimiter=";", extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if v is None else v) for k, v in r.items()})
    try:
        os.replace(tmp, path)
    except OSError:
        tmp.unlink()
        raise


def typed(v):
    """For xlsx: numbers from the CSV as numbers, so they can be summed and sorted."""
    if isinstance(v, str) and v.lstrip("-").isdigit() and len(v) < 16:
        return int(v)
    return "" if v is None else v


def write_xlsx(path, tables):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
        from openpyxl.utils import get_column_letter
    except ImportError:
        print(f"⚠ {XLSX_NAME} not created: requires openpyxl (py -m pip install openpyxl)")
        return False
    wb = Workbook()
    wb.remove(wb.active)
    for title, cols, rows in tables:
        ws = wb.create_sheet(title)
        ws.append(cols)
        for r in rows:
            ws.append([typed(r.get(c)) for c in cols])
        for cell in ws[1]:
            cell.font = Font(bold=True)
        ws.freeze_panes = "B2"
        ws.auto_filter.ref = ws.dimensions
        for i, c in enumerate(cols, 1):
            width = max([len(str(c))] + [len(str(r.get(c) or "")) for r in rows[:500]])
            ws.column_dimensions[get_column_letter(i)].width = min(60, max(6, width + 2))
    tmp = path.with_name(path.name + ".tmp")
    wb.save(tmp)
    try:
        os.replace(tmp, path)
    except OSError:
        tmp.unlink()
        raise
    return True


def main():
    tp.setup_console()
    args = parse_args()
    if args.probe and shutil.which("ffprobe") is None:
        print("⚠ ffprobe not found in PATH: --probe is off, duration and shooting time come from the markup only."
              " Install it: winget install Gyan.FFmpeg  (and open a new terminal window)")
        args.probe = False
    skip = tp.label_set(args.skip)
    found = scan(args.folders, skip)
    if not found:
        sys.exit(f"Found neither {tp.CSV_NAME} nor videos")
    roots = [r.resolve() for r in args.folders if r.exists()]
    out = (args.out or roots[0]).resolve()
    out.mkdir(parents=True, exist_ok=True)

    def display(folder):
        for root in roots:
            try:
                rel = folder.relative_to(root)
            except ValueError:
                continue
            name = rel.as_posix()
            return root.name if name == "." else (f"{root.name}/{name}" if len(roots) > 1 else name)
        return str(folder)

    read, bad = [], []
    for folder in sorted(found, key=lambda p: display(p).lower()):
        name = display(folder)
        files, events, state = read_folder(folder, found[folder], args)
        if state == "bad":
            bad.append(name)
        if files or state == "bad":
            read.append((name, files, events, state))
    copies = mark_copies([(name, f) for name, files, _, _ in read for f in files.values()])
    unknown = sorted({lab for _, files, _, _ in read for f in files.values() for lab in f["labels"]
                      if lab and lab not in tp.LABELS})
    labels = LABELS + unknown

    folders, files_t, events_t, unique = [], [], [], {}
    for name, files, events, state in read:
        folders.append(folder_row(name, files, state, labels))
        for _, f in sorted(files.items(), key=lambda kv: tp.file_order_key(kv[0])):
            files_t.append(file_row(name, f, labels))
            if not f.get("copy_of"):
                unique[(name, f["file"].upper())] = f
        events_t += [event_row(name, r, files) for r in events]
    # TOTAL counts unique videos: folder copies (project, export, backup) do not multiply the archive
    total = folder_row("TOTAL", unique, "ok", labels)
    total["status"] = f"folders {len(folders)}" + (f", video copies {copies}" if copies else "")
    total["copies"] = copies
    folders.append(total)

    written = []
    folder_c, file_c = folder_cols(labels), file_cols(labels)
    for key, cols, rows in (("folders", folder_c, folders), ("files", file_c, files_t),
                            ("events", EVENT_COLS, events_t)):
        path = out / OUT_NAMES[key]
        try:
            write_csv(path, cols, rows)
            written.append(path.name)
        except PermissionError:
            print(f"✗ {path} is open in another program (Excel?) — close it and run again")
    if args.xlsx:
        try:
            if write_xlsx(out / XLSX_NAME, [("Folders", folder_c, folders),
                                            ("Files", file_c, files_t),
                                            ("Events", EVENT_COLS, events_t)]):
                written.append(XLSX_NAME)
        except PermissionError:
            print(f"✗ {out / XLSX_NAME} is open in another program (Excel?) — close it and run again")

    t = folders[-1]
    top = sorted(((lab, t[lab]) for lab in labels if t[lab]), key=lambda x: -x[1])
    print(f"Folders: {len(folders) - 1}, videos: {t['videos']}, reviewed: {t['done']}"
          f" (rated without Enter {t['draft']}, started {t['started']}, not opened {t['not_opened']})")
    if copies:
        cf = [r["folder"] for r in folders[:-1] if r["copies"]]
        diff = sum(1 for r in files_t if r["copy_of"].endswith(MARKUP_DIFFERS))
        print(f"Copies: {copies} videos also lie in other folders ({', '.join(cf[:5])}"
              + (f" and {len(cf) - 5} more" if len(cf) > 5 else "") + ") — counted once in the total"
              + (f"; {diff} have markup that differs from the primary copy" if diff else ""))
    if unknown:
        print(f"Labels not in the camera_triage.lua list: {', '.join(unknown)} — columns at the end of the tables")
    print(f"Duration: {t['duration_hms'] or '—'}"
          + (f" (unknown for {t['unknown_duration']} videos"
             + (": no file on disk or ffprobe could not read it)" if args.probe else " — --probe will read it)")
             if t["unknown_duration"] else "")
          + f", reviewed: {t['reviewed_hms'] or '—'}")
    print(f"Ratings: 0 — {t['rated_0']}, 1 — {t['rated_1']}, 2 — {t['rated_2']}, 3 — {t['rated_3']}")
    if top:
        print("Labels: " + ", ".join(f"{lab} {n}" for lab, n in top))
    undone = [r["folder"] for r in folders[:-1]
              if r["status"] != COMPLETE and not r["status"].startswith(COPY)]
    if undone:
        print(f"Not fully marked up: {len(undone)} — " + ", ".join(undone[:5])
              + (f" and {len(undone) - 5} more" if len(undone) > 5 else ""))
    if bad:
        print(f"⚠ {tp.CSV_NAME} in another format (not read): " + ", ".join(bad))
    if written:
        print(f"\nReport in {out}: " + ", ".join(written))


if __name__ == "__main__":
    main()
