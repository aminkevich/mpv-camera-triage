"""End-to-end run on real tiny videos: make_clips, make_timeline, make_report.
Requires ffmpeg and ffprobe; without them the tests are skipped."""
import csv
import json
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from fractions import Fraction as F
from pathlib import Path

from support import FFMPEG, FFPROBE, make_video, row, run_tool, triage_csv


def duration(path):
    out = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "json",
                          str(path)], capture_output=True, text=True)
    return float(json.loads(out.stdout)["format"]["duration"])


@unittest.skipUnless(FFMPEG and FFPROBE, "requires ffmpeg and ffprobe")
class EndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.folder = base / "Footage" / "Turkey"
        cls.folder.mkdir(parents=True)
        cls.conf = base / "empty.conf"
        cls.conf.write_text("", encoding="utf-8")
        make_video(cls.folder / "GX010123.MP4", 20, timecode="10:20:30:15")
        make_video(cls.folder / "GX020123.MP4", 10, timecode="10:20:50:16")
        make_video(cls.folder / "DJI_0001.MP4", 10, fps="30")
        make_video(cls.folder / "GX010124.MP4", 5)                  # never opened
        triage_csv(cls.folder, [
            row("mark", "GX010123.MP4", 5, label="fish"), row("clip", "GX010123.MP4", 10, 14, label="dive"),
            row("file_done", "GX010123.MP4", rating="3", dur=20),
            row("mark", "GX020123.MP4", 2, label="catch"), row("file_done", "GX020123.MP4", rating="2", dur=10),
            row("mark", "DJI_0001.MP4", 8, label="jump", activity="kitesurfing"),
            row("file_done", "DJI_0001.MP4", rating="2", dur=10, activity="kitesurfing")])
        cls.base = base

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_make_clips_cuts_and_indexes(self):
        res = run_tool("make_clips.py", self.folder, "--config", self.conf)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        clips = self.folder / "Clips"
        names = sorted(p.name for p in clips.glob("*.mp4"))
        # fish 5 ±5 and the i/o clip 10–14 ±0.5 merge into 0–14.5 s
        self.assertEqual(names, ["DJI_0001_00-00-03_7s_jump.mp4", "GX010123_00-00-00_14s_dive+fish.mp4",
                                 "GX020123_00-00-00_7s_catch.mp4"])
        with open(clips / "_clips.csv", encoding="utf-8-sig") as f:
            index = {r["clip"]: r for r in csv.DictReader(f, delimiter=";")}
        for name in names:
            entry = index[name]
            # -c copy: starts on a keyframe no later than planned, file duration ≈ index
            self.assertAlmostEqual(duration(clips / name), int(entry["duration_ms"]) / 1000, delta=0.15)
        self.assertIn("already existed 0", res.stdout)
        again = run_tool("make_clips.py", self.folder, "--config", self.conf)
        self.assertIn("cut 0, already existed 3", again.stdout)

    def test_make_clips_into_project_folder(self):
        out = self.base / "Project" / "Clips"
        res = run_tool("make_clips.py", self.folder, "--config", self.conf, "--out", out, "--labels", "catch")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual([p.name for p in out.glob("*.mp4")], ["GX020123_00-00-00_7s_catch.mp4"])
        self.assertTrue((out / "_clips.csv").exists())

    def test_make_timeline_otio_by_default(self):
        res = run_tool("make_timeline.py", self.folder, "--config", self.conf, "--full")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("DaVinci Resolve not found", res.stdout)       # run_tool: no Resolve menu
        tl = json.loads((self.folder / "_selects.otio").read_text(encoding="utf-8"))
        clips = {c["media_references"]["DEFAULT_MEDIA"]["name"]: c for c in tl["tracks"]["children"][0]["children"]}
        gx = clips["GX010123.MP4"]["media_references"]["DEFAULT_MEDIA"]["available_range"]["start_time"]
        self.assertEqual(gx["value"], (10 * 3600 + 20 * 60 + 30) * 60 + 15)   # the source timecode
        self.assertEqual(sum(len(c["markers"]) for c in clips.values()), 4)
        self.assertEqual([t["kind"] for t in tl["tracks"]["children"]], ["Video", "Audio"])
        self.assertTrue((self.folder / "_full.otio").exists())

    def test_make_timeline_fcpxml_uses_source_timecode(self):
        res = run_tool("make_timeline.py", self.folder, "--config", self.conf, "--full", "--fcpxml")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        root = ET.parse(self.folder / "_selects.fcpxml").getroot()
        assets = {a.get("name"): a for a in root.iter("asset")}
        fps = F(60000, 1001)
        self.assertEqual(F(assets["GX010123.MP4"].get("start")[:-1]),
                         F((10 * 3600 + 20 * 60 + 30) * 60 + 15) / fps)
        self.assertEqual(assets["DJI_0001.MP4"].get("start"), "0s")
        frame_durations = {f.get("frameDuration") for f in root.iter("format")}
        self.assertEqual(frame_durations, {"1001/60000s", "1/30s"})
        for name in ("_selects.edl", "_full.fcpxml", "_full.edl"):
            self.assertTrue((self.folder / name).exists(), name)
        self.assertEqual(len(list(root.iter("marker"))), 4)

    def test_make_report_with_probe(self):
        res = run_tool("make_report.py", self.base / "Footage", "--probe")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        with open(self.base / "Footage" / "_report_folders.csv", encoding="utf-8-sig") as f:
            total = list(csv.DictReader(f, delimiter=";"))[-1]
        self.assertEqual((total["videos"], total["done"], total["not_opened"]), ("4", "3", "1"))
        self.assertEqual(total["unknown_duration"], "0")              # --probe filled in the unopened one
        self.assertAlmostEqual(int(total["duration_ms"]) / 1000, 45, delta=0.5)


if __name__ == "__main__":
    unittest.main()
