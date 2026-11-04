#!/usr/bin/env python3
"""
make_clips.py — cut clips from the _triage.csv markup (camera_triage.lua v3).

Finds every _triage.csv in the given folders (recursively) and for each footage folder
creates a Clips/ subfolder with the clips and an index Clips/_clips.csv.

Where segments come from:
  * clip (i/o in mpv)  — exact bounds ± --clip-pad; the clip's labels = all point marks inside
                         (+ the label of the clip row). A clip that contains only labels from
                         --exclude (e.g. i … b … o) is not cut.
  * mark (points)      — time − --pad-before … time + --pad-after
  Overlapping or close (≤ --merge-gap) segments of one file are merged, and their labels are
  joined in the name: GX011067_00-01-24_14s_fish+shot.mp4 (start, duration, labels;
  when the pads change the name changes and the clip is cut again, --prune removes the old one)
  Labels from --exclude (bad by default) produce no clips and do not appear in names.
  Point labels from --point-exclude (none by default) produce no clips on their own; inside an
  i/o range they only add a label to the clip. Example: --point-exclude dive.
  --labels selects which clips to cut (the clip has at least one of the labels), but the clip's
  name and bounds stay the same as without the filter, so a later run without it creates no duplicates.

Files rated 0 ("junk") are skipped by default; --min-rating 0 takes everything.

For editing, make_timeline.py is more convenient: the same plan, but an FCPXML timeline that
links to the originals, so segments can be extended in the editor. The shared plan logic is in
triage_plan.py.

Clips are always cut from the original MP4 (if it is missing — from the LRV/LRF proxy, with a
warning; once the MP4 appears, such clips are re-cut automatically).
By default there is no re-encoding (-c copy): the source bytes are just copied — fast and
lossless. A clip can only start on a keyframe (other frames store only the difference to
their neighbours), so the start moves back to the previous keyframe, usually by a fraction of
a second to about a second; the index records the actual start. That is enough for cutting by markup.

--reencode — export for compatibility: full decode and new H.264 8-bit encode
(libx264, CRF 18) + AAC. Needed when a clip must play everywhere (phones, messengers, old
players cannot handle HEVC / 10-bit) or must start exactly on a frame. Very slow on 4K: the
CPU encodes, from tens of seconds to minutes per clip.

Repeated runs: existing clips are left alone. If the markup or the settings changed, the new
clips get different names and the old ones stay — --prune removes clips from Clips/ that are
not in the current plan (only those listed in _clips.csv; other files are never touched).
The plan for --prune is built without --labels, --no-marks and --min-rating: these filters
choose what to cut now, but do not delete clips with other labels or from lower-rated files.
Clips are removed after changes to the markup, the pads or a rating of 0.

Settings (make_clips.conf next to the script, or --config PATH):
  permanent options — pads (global and per label), merging, exclusions, min rating, clip
  folder, --reencode options. Priority: built-in values → config → command line.
  --pad-before / --pad-after on the command line set the pad for ALL labels (per-label pads
  from the config do not apply to that side). Format — see make_clips.conf.

Examples:
  py make_clips.py "C:\\...\\20260918_GoPro_Raznirka_Pristrelka" --dry-run
  py make_clips.py "C:\\...\\02_SourceVideos" --min-rating 2
  py make_clips.py "D:\\Archive" --labels fish,shot,catch --pad-before 8
  py make_clips.py "C:\\...\\20260918_GoPro_Raznirka_Pristrelka" --prune --dry-run
"""
import argparse
import csv
import json
import shutil
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import triage_plan as tp                    # noqa: E402  shared segment plan (also for make_timeline.py)

INDEX_NAME = "_clips.csv"
PENDING_NAME = "_clips.pending.csv"   # index that could not be written (the file was open)
INDEX_FIELDS = ["clip", "source", "src", "start_ms", "end_ms", "duration_ms",
                "labels", "rating", "activity", "marks", "notes"]


def parse_args():
    ap = argparse.ArgumentParser(description="Cut clips from _triage.csv")
    tp.add_plan_args(ap)
    ap.add_argument("--reencode", action="store_true", help="export for compatibility: re-encode to H.264 8-bit + AAC "
                         "(plays everywhere, starts exactly on a frame; very slow on 4K)")
    ap.add_argument("--out", help="clip subfolder next to the videos (Clips) or a full path, "
                                  "e.g. D:\\Project\\Clips — then _triage.csv need not be copied")
    ap.add_argument("--overwrite", action="store_true", help="overwrite existing clips")
    ap.add_argument("--prune", action="store_true",
                    help="delete clips listed in _clips.csv that are not in the current plan "
                         "(--labels, --no-marks, --min-rating do not affect this)")
    ap.add_argument("--dry-run", action="store_true", help="only show what would be done")
    return tp.finish_args(ap, ap.parse_args())


def describe_settings(args):
    text = tp.describe_settings(args)
    if args.reencode:
        text += f"\n  re-encoding: libx264 CRF {args.reencode_crf}, preset {args.reencode_preset}"
    return text


def clip_name(fname, s, e, labs):
    """GX011151_00-01-20_14s_fish+shot.mp4 — start and duration in the name: when pad_before
    or pad_after changes, the name changes and the clip is cut again (--prune removes the old one)."""
    return f"{Path(fname).stem}_{tp.hms_dash(s)}_{round(e - s)}s_{'+'.join(sorted(labs))}.mp4"


# ---------------------------------------------------------------- index

def read_index(out_dir):
    """Index + the unsaved rest of the previous run (_clips.pending.csv, it is newer)."""
    rows = {}
    for name in (INDEX_NAME, PENDING_NAME):
        path = out_dir / name
        if path.exists():
            try:
                rows.update({r["clip"]: r for r in tp.read_rows(path) if r.get("clip")})
            except OSError as e:
                print(f"  ⚠ cannot read {path.name}: {e}")
    return rows


def index_locked(out_dir):
    """True if _clips.csv is open in another program (Excel locks the file on Windows)."""
    path = out_dir / INDEX_NAME
    if not path.exists():
        return False
    try:
        with open(path, "r+b"):
            return False
    except PermissionError:
        return True
    except OSError:
        return False


def wait_index_unlocked(out_dir):
    """Before cutting: if the index is open, ask to close it (in an interactive window)."""
    if not index_locked(out_dir):
        return
    print(f"  ⚠ {out_dir / INDEX_NAME} is open in another program (Excel?).")
    if sys.stdin and sys.stdin.isatty():
        try:
            input("    Close it and press Enter (or just Enter to continue without it) ")
        except EOFError:
            pass
    if index_locked(out_dir):
        print(f"    Clips will be cut, the index will be saved to {PENDING_NAME} "
              f"and moved to {INDEX_NAME} on the next run.")


def write_index(out_dir, old, entries):
    """Updates Clips/_clips.csv: new entries replace old ones with the same name,
    entries for clips no longer on disk are dropped.
    Writes to a temporary file and swaps it in; if _clips.csv is locked, leaves
    _clips.pending.csv for the next run to pick up."""
    merged = dict(old)
    for e in entries:
        merged[e["clip"]] = e
    rows = [r for name, r in sorted(merged.items()) if (out_dir / name).exists()]
    path, pending = out_dir / INDEX_NAME, out_dir / PENDING_NAME
    tmp = out_dir / (INDEX_NAME + ".tmp")
    try:
        if not rows:
            for p in (path, pending):
                if p.exists():
                    p.unlink()
            return
        with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=INDEX_FIELDS, delimiter=";", extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        for attempt in range(3):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(1)
        if pending.exists():
            pending.unlink()
    except OSError as e:
        try:
            if tmp.exists():
                os.replace(tmp, pending)
            print(f"  ⚠ {INDEX_NAME} not updated ({e.__class__.__name__}: is the file open?). "
                  f"Wrote {PENDING_NAME} — it will be moved on the next run.")
        except OSError as e2:
            print(f"  ✗ index not saved: {e2}")


# ---------------------------------------------------------------- ffmpeg

def keyframe_before(src, t):
    """Time of the keyframe ≤ t (where the clip actually starts with -c copy) or None."""
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
           "-read_intervals", f"{max(0.0, t - 15):.3f}%{t + 0.05:.3f}",
           "-show_entries", "frame=pts_time,best_effort_timestamp_time:format=start_time",
           "-of", "json", str(src)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=60)
        data = json.loads(res.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    t0 = float((data.get("format") or {}).get("start_time") or 0)   # ffmpeg -ss counts from start_time
    best = None
    for fr in data.get("frames", []):
        v = fr.get("pts_time") or fr.get("best_effort_timestamp_time")
        try:
            k = float(v) - t0
        except (TypeError, ValueError):
            continue
        if k <= t + 0.0005 and (best is None or k > best):
            best = k
    return max(0.0, best) if best is not None else None


def ffmpeg_cmd(src, dst, start, dur, reencode, crf=18, preset="medium"):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{dur:.3f}",
           "-map", "0:v:0", "-map", "0:a:0?"]
    if reencode:
        cmd += ["-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
    return cmd + ["-movflags", "+faststart", str(dst)]


def cut(src, dst, start, dur, args):
    """Cuts into a temporary file and renames it only on success:
    a broken clip (error, Ctrl+C) never stays on disk under the "finished" name."""
    tmp = dst.with_name(dst.stem + ".part" + dst.suffix)
    try:
        res = subprocess.run(ffmpeg_cmd(src, tmp, start, dur, args.reencode,
                                        args.reencode_crf, args.reencode_preset),
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        if res.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(dst)
            return None
        return res.stderr.strip()[-300:] or f"ffmpeg exit code {res.returncode}"
    finally:
        if tmp.exists():
            tmp.unlink()


# ---------------------------------------------------------------- folder

def fmt_points(items, s, e):
    return " | ".join(f"{txt}@{int(t * 1000)}" for t, txt in sorted(items) if s <= t <= e)


def clips_dir(csv_path, args, many):
    """Where to put a folder's clips. --out is a subfolder name (Clips, next to the videos) or a
    full path (D:\\Project\\Clips): then the videos and _triage.csv stay where they are. A full
    path with several footage folders gets subfolders by their path from the given folder, so
    clips from files with the same names do not mix."""
    out = Path(args.out).expanduser()
    if not out.is_absolute():
        return csv_path.parent / out
    if not many:
        return out
    folder = csv_path.parent.resolve()
    for root in args.folders:
        try:
            rel = folder.relative_to(root.resolve())
        except ValueError:
            continue
        return out / (rel if rel.parts else folder.name)
    return out / folder.name


def process_folder(csv_path, args, out_dir):
    folder = csv_path.parent
    rows = tp.read_rows(csv_path)
    st = {"made": 0, "skipped": 0, "failed": 0, "todo": 0, "pruned": 0, "nosrc": 0}
    print(f"\n== {folder}")
    if not tp.triage_csv_ok(rows, csv_path):
        print(f"  ⚠ {tp.CSV_NAME} has an unexpected format (re-saved in another program?), skipped")
        st["failed"] += 1
        return st
    files = tp.collect(rows)
    old = read_index(out_dir)
    if not args.dry_run:
        wait_index_unlocked(out_dir)
    index, keep, missing, planned = [], set(), [], False

    try:
        for fname, d in tp.sorted_files(files):
            rating = d["rating"]
            if tp.zero_rated(d, args):
                if args.min_rating is None and (d["marks"] or d["clips"]):
                    print(f"  · {fname}: rated 0, skipped (--min-rating 0 takes it)")
                continue
            # --prune: --labels, --no-marks and --min-rating only choose what to cut now;
            # clips of the full plan are not deleted (otherwise --labels fish --prune would wipe the rest)
            keep.update(clip_name(fname, s, e, labs)
                        for s, e, labs in tp.build_segments(d, args, no_marks=False))
            if tp.below_min_rating(d, args):
                continue
            segs = tp.selected_segments(d, args)
            if not segs:
                continue
            names = [clip_name(fname, s, e, labs) for s, e, labs in segs]
            keep.update(names)                      # even without a source — do not delete with --prune
            planned = True
            src, is_proxy = tp.find_source(folder, fname)
            if src is None:
                missing.append(fname)               # summarized in one line at the end of the folder
                st["nosrc"] += len(segs)
                continue
            src_kind = src.suffix.lower().lstrip(".") if is_proxy else "mp4"
            if is_proxy:
                print(f"  ⚠ {fname}: no MP4, cutting from {src.name} (low resolution)")

            for (s, e, labs), name in zip(segs, names):
                dst = out_dir / name
                prev = old.get(name)
                upgrade = (dst.exists() and not is_proxy and prev is not None
                           and (prev.get("src") or "mp4") != "mp4")
                tag = f"  {name}  ({e - s:.0f} s)"
                entry = {"clip": name, "source": fname, "src": src_kind,
                         "start_ms": int(s * 1000), "end_ms": int(e * 1000),
                         "duration_ms": int((e - s) * 1000), "labels": "+".join(sorted(labs)),
                         "rating": "" if rating is None else rating, "activity": d["activity"],
                         "marks": fmt_points(d["marks"], s, e), "notes": fmt_points(d["notes"], s, e)}
                if dst.exists() and not args.overwrite and not upgrade:
                    print(tag + "  — exists")
                    st["skipped"] += 1
                    if prev:                        # keep the actual start from the previous run
                        for k in ("src", "start_ms", "duration_ms"):
                            if prev.get(k):
                                entry[k] = prev[k]
                    elif not args.dry_run and not args.reencode:   # the index was lost — restore it
                        kf = keyframe_before(src, s)
                        if kf is not None and kf < s:
                            entry["start_ms"] = int(kf * 1000)
                            entry["duration_ms"] = int((e - kf) * 1000)
                    index.append(entry)
                    continue
                if upgrade:
                    tag += "  (was cut from a proxy, re-cutting from MP4)"
                st["todo"] += 1
                if args.dry_run:
                    print(tag)
                    continue
                out_dir.mkdir(parents=True, exist_ok=True)
                if not args.reencode:               # with -c copy the start moves to a keyframe
                    kf = keyframe_before(src, s)
                    if kf is not None and kf < s:
                        entry["start_ms"] = int(kf * 1000)
                        entry["duration_ms"] = int((e - kf) * 1000)
                err = cut(src, dst, s, e - s, args)
                if err is None:
                    print(tag + "  ✓")
                    st["made"] += 1
                    index.append(entry)
                else:
                    print(tag + "  ✗ " + err)
                    st["failed"] += 1

        if args.prune:
            for name in sorted(set(old) - keep):
                p = out_dir / name
                if p.exists():
                    print(f"  ✂ {name}" + ("  (will be deleted)" if args.dry_run else "  deleted"))
                    if not args.dry_run:
                        p.unlink()
                    st["pruned"] += 1
    finally:                                        # the index is written on Ctrl+C too
        if not args.dry_run and out_dir.exists():
            write_index(out_dir, old, index)
    if missing:
        shown = ", ".join(Path(m).stem for m in missing[:3])
        more = f" and {len(missing) - 3} more" if len(missing) > 3 else ""
        print(f"  ⚠ {len(missing)} file(s) from the markup are not in the folder "
              f"(neither MP4 nor proxy), skipped: {shown}{more}")
    if not planned:
        print("  (nothing to cut)")
    return st


def main():
    tp.setup_console()
    args = parse_args()
    print(describe_settings(args))
    if not args.dry_run and shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg not found in PATH. Install it: winget install Gyan.FFmpeg  (and open a new terminal window)")
    if not args.dry_run and not args.reencode and shutil.which("ffprobe") is None:
        print("⚠ ffprobe not found: the index will get the planned clip start, not the actual one")
    csvs = tp.find_csvs(args.folders, skip_dir=Path(args.out).name)
    if not csvs:
        sys.exit(f"No {tp.CSV_NAME} found")
    if Path(args.out).expanduser().is_absolute():
        print(f"Clips go to {Path(args.out).expanduser()}" + ("  (in subfolders per footage folder)" if len(csvs) > 1 else ""))

    total = {"made": 0, "skipped": 0, "failed": 0, "todo": 0, "pruned": 0, "nosrc": 0}
    try:
        for c in csvs:
            for k, v in process_folder(c, args, clips_dir(c, args, len(csvs) > 1)).items():
                total[k] += v
    except KeyboardInterrupt:
        print("\nInterrupted. Finished clips and the index are saved, the unfinished file is deleted.")
        sys.exit(130)
    ns = f", no source {total['nosrc']} (see ⚠ above)" if total["nosrc"] else ""
    if args.dry_run:
        pr = f", delete old {total['pruned']}" if args.prune else ""
        print(f"\nDry run: to cut {total['todo']}, already exist {total['skipped']}{ns}{pr}.")
    else:
        pr = f", old deleted {total['pruned']}" if args.prune else ""
        print(f"\nDone: cut {total['made']}, already existed {total['skipped']}, "
              f"errors {total['failed']}{ns}{pr}.")


if __name__ == "__main__":
    main()
