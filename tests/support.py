"""Shared test helpers: tool paths, building _triage.csv, running the scripts, test videos."""
import argparse
import contextlib
import csv
import io
import itertools
import os
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
LUA = ROOT / "mpv" / "scripts" / "camera_triage.lua"
sys.path.insert(0, str(TOOLS))

import triage_plan as tp                    # noqa: E402

HEADER = ["logged_at", "event", "activity", "file", "src", "time_ms", "end_ms", "time_hms",
          "label", "rating", "note", "speed_pct", "duration_ms", "duration_hms", "camera_time"]
_clock = itertools.count()

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def row(event, file, t=None, end=None, label="", rating="", note="", dur=None, camera_time="",
        at=None, src="mp4", activity="spearfishing"):
    """A _triage.csv row; t, end, dur are seconds. logged_at grows with every row by default."""
    if at is None:
        n = next(_clock)
        at = f"2026-10-01 10:{n // 60 % 60:02d}:{n % 60:02d}"
    ms = lambda s: "" if s is None else str(round(s * 1000))          # noqa: E731
    return {"logged_at": at, "event": event, "activity": activity, "file": file,
            "src": src if event in ("mark", "clip", "note") else "", "time_ms": ms(t),
            "end_ms": ms(end), "time_hms": tp.hms(t) if t is not None else "", "label": label,
            "rating": rating, "note": note, "speed_pct": "100" if t is not None else "",
            "duration_ms": ms(dur), "duration_hms": tp.hms(dur) if dur else "",
            "camera_time": camera_time}


def triage_csv(folder, rows):
    """Writes <folder>/_triage.csv in the camera_triage.lua format (UTF-8 with BOM, ";")."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / tp.CSV_NAME
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADER, delimiter=";", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    return path


def empty_config(folder):
    """An empty config: tests must not depend on tools/make_clips.conf."""
    path = Path(folder) / "empty.conf"
    path.write_text("", encoding="utf-8")
    return path


def plan_args(config, *cli, folder="."):
    ap = argparse.ArgumentParser()
    tp.add_plan_args(ap)
    return tp.finish_args(ap, ap.parse_args([str(folder), "--config", str(config), *cli]))


def tool_args(module, *argv):
    """The script's parse_args() with the given command line."""
    with mock.patch.object(sys, "argv", ["test", *map(str, argv)]):
        return module.parse_args()


@contextlib.contextmanager
def quiet():
    with contextlib.redirect_stdout(io.StringIO()) as out:
        yield out


def run_tool(script, *args, timeout=300, extra_env=None):
    """Runs a script from tools/ as a separate process (as from the command line)."""
    # never touch the real Resolve menu (make_timeline.py updates it); tests that need it set their own
    env = dict(os.environ, PYTHONIOENCODING="utf-8", CAMERA_TRIAGE_RESOLVE_SCRIPTS="none")
    env.update(extra_env or {})
    return subprocess.run([sys.executable, str(TOOLS / script), *map(str, args)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env=env, timeout=timeout)


def make_video(path, seconds, fps="60000/1001", timecode=None, size="160x90"):
    """A tiny test video with audio (requires ffmpeg); a keyframe every second."""
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc2=d={seconds}:s={size}:r={fps}",
           "-f", "lavfi", "-i", f"sine=d={seconds}:sample_rate=48000", "-ac", "2",
           "-c:v", "libx264", "-preset", "ultrafast", "-g", str(round(Fraction(fps))),
           "-c:a", "aac", "-shortest"]
    if timecode:
        cmd += ["-timecode", timecode]
    subprocess.run(cmd + [str(path)], check=True, capture_output=True)
    return Path(path)


def find_mpv():
    """mpv for testing the Lua script: the MPV variable, PATH or the standard Windows install."""
    cands = [os.environ.get("MPV"), shutil.which("mpv.com"), shutil.which("mpv"),
             r"C:\Program Files\MPV Player\mpv.com", r"C:\Program Files\mpv\mpv.com"]
    return next((c for c in cands if c and Path(c).exists()), None)
