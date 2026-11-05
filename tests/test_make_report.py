"""make_report.py: statuses, copies, TOTAL — on an archive of dummy files (no ffprobe needed)."""
import csv
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support import TOOLS, row, run_tool, triage_csv


def read(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


def video(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)


class ReportTest(unittest.TestCase):
    """An archive like a real user's: footage, a copy in a project (marked further), a markup copy in
    an export, another video with the same GoPro name, a folder without markup, a broken CSV, Clips."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = cls.root = Path(cls.tmp.name) / "Archive"
        turkey = root / "01_Footage" / "Turkey"
        video(turkey / "GX010001.MP4", 1000)
        video(turkey / "GX010002.MP4", 2000)
        video(turkey / "GX010003.MP4", 3000)                       # never opened
        video(turkey / "._GX010003.MP4", 4096)                     # macOS metadata, not a video
        video(root / "MacOnly" / "._DJI_0009.MP4", 4096)            # a folder of Mac leftovers only
        video(turkey / "Clips" / "GX010001_00-00-05_10s_fish.mp4", 10)   # not an archive video
        rows = [row("mark", "GX010001.MP4", 10, label="fish"), row("clip", "GX010001.MP4", 30, 45, label="dive"),
                row("note", "GX010001.MP4", note="murky"),
                row("file_done", "GX010001.MP4", rating="2", dur=60, camera_time="2026-09-18 09:55:29"),
                row("file_done", "GX010001.MP4", rating="3", dur=60, camera_time="2026-09-18 09:55:29"),
                row("mark", "GX010002.MP4", 5, label="catch"),
                row("file_done", "GX010002.MP4", rating="2", dur=30, camera_time="2026-09-18 10:10:00"),
                row("file_partial", "GX010002.MP4", rating="0", dur=30, camera_time="2026-09-18 10:10:00"),
                row("mark", "GX010004.MP4", 3, label="shot"),       # started, the file is not on disk
                row("mark", "DJI_0001.MP4", 1, label="jump", activity="kitesurfing"),
                row("file_partial", "DJI_0001.MP4", rating="1", dur=20, activity="kitesurfing")]
        triage_csv(turkey, rows)
        project = root / "02_Project" / "Turkey"
        project.mkdir(parents=True)
        shutil.copy(turkey / "GX010001.MP4", project / "GX010001.MP4")
        extra = row("mark", "GX010001.MP4", 50, label="catch")      # marked further in the project
        triage_csv(project, rows + [extra])
        triage_csv(root / "03_Export", rows)                         # markup only
        video(root / "Other" / "GX010001.MP4", 5000)                 # same name, different video
        bad = root / "Broken"
        bad.mkdir()
        # re-saved in Excel: another delimiter, time in another format
        (bad / "_triage.csv").write_text("logged_at,event,file\n18.09.2026 9:55,file_done,X.MP4\n",
                                         encoding="utf-8")
        cls.res = run_tool("make_report.py", root)
        cls.folders = {r["folder"]: r for r in read(root / "_report_folders.csv")}
        cls.files = {(r["folder"], r["file"]): r for r in read(root / "_report_files.csv")}
        cls.events = read(root / "_report_events.csv")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_runs_and_writes_three_tables(self):
        self.assertEqual(self.res.returncode, 0, self.res.stderr)
        self.assertIn("Report in", self.res.stdout)
        self.assertNotIn("Clips", " ".join(self.folders))

    def test_file_statuses_and_ratings(self):
        f = lambda name: self.files[("01_Footage/Turkey", name)]       # noqa: E731
        self.assertEqual((f("GX010001.MP4")["status"], f("GX010001.MP4")["rating"]), ("reviewed", "3"))
        self.assertEqual(f("GX010002.MP4")["rating_draft"], "0")       # re-rated without Enter
        self.assertEqual(f("GX010003.MP4")["status"], "not opened")
        self.assertEqual((f("GX010004.MP4")["status"], f("GX010004.MP4")["on_disk"]), ("started", "no"))
        self.assertEqual((f("DJI_0001.MP4")["status"], f("DJI_0001.MP4")["rating_draft"]),
                         ("rated, no Enter", "1"))
        self.assertEqual((f("GX010001.MP4")["fish"], f("GX010001.MP4")["dive"], f("GX010001.MP4")["clips"]),
                         ("1", "1", "1"))
        self.assertEqual(f("GX010001.MP4")["file_notes"], "murky")

    def test_copies_are_detected(self):
        project = self.files[("02_Project/Turkey", "GX010001.MP4")]
        self.assertEqual(project["copy_of"], "01_Footage/Turkey (markup differs)")
        for name in ("GX010001.MP4", "GX010002.MP4", "GX010004.MP4", "DJI_0001.MP4"):
            self.assertEqual(self.files[("03_Export", name)]["copy_of"], "01_Footage/Turkey", name)
        self.assertEqual(self.files[("01_Footage/Turkey", "GX010001.MP4")]["copies"], "2")
        self.assertEqual(self.files[("Other", "GX010001.MP4")]["copy_of"], "")   # a different video
        self.assertEqual(self.folders["03_Export"]["status"], "copy: 01_Footage/Turkey")
        self.assertEqual(self.folders["02_Project/Turkey"]["status"], "copy: 01_Footage/Turkey")
        self.assertTrue(all(e["copy_of"] for e in self.events if e["folder"] == "03_Export"))

    def test_folder_statuses(self):
        self.assertEqual(self.folders["01_Footage/Turkey"]["status"], "partial (2 of 5)")
        self.assertEqual(self.folders["Other"]["status"], "not marked up")
        self.assertEqual(self.folders["Broken"]["status"], "CSV in another format")
        self.assertIn("Broken", self.res.stdout)

    def test_mac_metadata_files_are_not_videos(self):
        self.assertNotIn("MacOnly", self.folders)
        self.assertFalse([k for k in self.files if k[1].startswith("._")])

    def test_total_counts_each_video_once(self):
        total = self.folders["TOTAL"]
        # Turkey: 5 videos (3 on disk + 2 only in the markup) + the other GX010001 in Other
        self.assertEqual(total["videos"], "6")
        self.assertEqual(total["done"], "2")
        self.assertEqual(total["rated_3"], "1")
        self.assertEqual(total["duration_ms"], str(60000 + 30000 + 20000))
        self.assertEqual(total["fish"], "1")                         # copies do not multiply marks
        self.assertEqual(total["shot_from"], "2026-09-18")
        self.assertIn("video copies", total["status"])
        self.assertIn("Copies:", self.res.stdout)

    def test_single_folder_is_its_own_archive(self):
        turkey = self.root / "01_Footage" / "Turkey"
        res = run_tool("make_report.py", turkey, "--out", Path(self.tmp.name) / "out")
        self.assertEqual(res.returncode, 0, res.stderr)
        folders = read(Path(self.tmp.name) / "out" / "_report_folders.csv")
        self.assertEqual([r["folder"] for r in folders], ["Turkey", "TOTAL"])
        self.assertEqual(folders[-1]["videos"], "5")

    def test_skip_option(self):
        res = run_tool("make_report.py", self.root, "--skip", "Clips,02_Project,03_Export",
                       "--out", Path(self.tmp.name) / "skip")
        folders = [r["folder"] for r in read(Path(self.tmp.name) / "skip" / "_report_folders.csv")]
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(folders, ["01_Footage/Turkey", "Broken", "Other", "TOTAL"])



class ProbeKeepsKnownValuesTest(unittest.TestCase):
    """--probe fills in only what is missing: the duration from the markup is not wiped, even if
    ffprobe could not read the file or is not in PATH."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "Trip"
        video(self.dir / "A.MP4", 100)                       # not a video: ffprobe cannot read it
        triage_csv(self.dir, [row("file_done", "A.MP4", rating="2", dur=30)])   # no camera_time

    def tearDown(self):
        self.tmp.cleanup()

    def duration(self):
        return read(self.dir / "_report_files.csv")[0]["duration_ms"]

    def test_unreadable_file(self):
        res = run_tool("make_report.py", self.dir, "--probe")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.duration(), "30000")

    def test_no_ffprobe_in_path(self):
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PATH=str(Path(sys.executable).parent))
        res = subprocess.run([sys.executable, str(TOOLS / "make_report.py"), str(self.dir), "--probe"],
                             capture_output=True, text=True, encoding="utf-8", env=env)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("ffprobe not found", res.stdout)
        self.assertEqual(self.duration(), "30000")


if __name__ == "__main__":
    unittest.main()
