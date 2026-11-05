"""camera_triage.lua in a real windowless mpv: GoPro chapter order, Del, u, a mark, rating + Enter,
CSV sorting on exit. Requires mpv and ffmpeg (the mpv path via the MPV variable if it is not in PATH);
without them the test is skipped."""
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from support import FFMPEG, LUA, find_mpv, make_video, row, triage_csv

MPV = find_mpv()
DRIVER = Path(__file__).with_name("mpv_driver.lua")


@unittest.skipUnless(MPV and FFMPEG, "requires mpv and ffmpeg")
class MpvLuaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        folder = base / "Turkey"
        folder.mkdir()
        for name in ("GX010123", "GX010124", "GX020123", "DJI_0001"):
            make_video(folder / f"{name}.MP4", 15)
        cls.csv = triage_csv(folder, [row("mark", "GX010123.MP4", 5, label="fish",
                                          at="2026-10-01 10:00:00")])
        (base / "watch_later").mkdir()
        log = base / "mpv.log"
        cmd = [MPV, "--no-config", "--pause", "--vo=null", "--ao=null", "--really-quiet",
               "--playlist-start=1", f"--log-file={log}", f"--watch-later-dir={base / 'watch_later'}",
               f"--script={LUA}", f"--script={DRIVER}", f"--script-opts=driver-csv={cls.csv}", str(folder)]
        cls.proc = subprocess.run(cmd, capture_output=True, timeout=90)
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        # mpv names the script after its file: [mpv_driver]
        cls.log = [m.group(1) for m in (re.search(r"\[mpv_driver\] (.*)$", ln) for ln in lines) if m]
        cls.errors = [ln for ln in lines if "[camera_triage]" in ln and re.search(r"\]\[(e|f)\]", ln)]
        cls.final = [ln for ln in cls.csv.read_text(encoding="utf-8-sig").splitlines()[1:] if ln]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def csv_at(self, tag):
        return [ln[len(f"CSV {tag} "):] for ln in self.log
                if ln.startswith(f"CSV {tag} ") and not ln.endswith(" END")]

    def test_script_loads_without_errors(self):
        self.assertEqual(self.proc.returncode, 0)
        self.assertEqual(self.errors, [])

    def test_gopro_chapters_follow_each_other(self):
        self.assertIn("PLAYLIST DJI_0001.MP4,GX010123.MP4,GX020123.MP4,GX010124.MP4", self.log)

    def test_del_removes_old_mark_and_u_restores_it(self):
        self.assertFalse(any(";fish;" in ln for ln in self.csv_at("after_del")))
        self.assertTrue(any(";5000;" in ln and ";fish;" in ln for ln in self.csv_at("after_undo")))

    def test_mark_rating_enter_and_sorted_csv(self):
        events = [ln.split(";")[1:10] for ln in self.final]
        # [event, activity, file, src, time_ms, end_ms, time_hms, label, rating]
        self.assertEqual([(e[0], e[2], e[7], e[8]) for e in events],
                         [("mark", "GX010123.MP4", "fish", ""), ("mark", "GX010123.MP4", "shot", ""),
                          ("file_done", "GX010123.MP4", "", "2")])
        self.assertEqual(events[0][4], "5000")
        # while paused the mark lands exactly on the frame the seek to 12 s reached (59.94 fps — 11.995)
        self.assertAlmostEqual(int(events[1][4]), 12000, delta=17)
        playing = [ln for ln in self.log if ln.startswith("PLAYING ")]
        self.assertEqual(playing, ["PLAYING GX010123.MP4", "PLAYING GX020123.MP4"])


def run_mpv(base, folder, driver, csv):
    """mpv with camera_triage.lua and a driver script → (process, lines logged by the driver)."""
    (base / "watch_later").mkdir(exist_ok=True)
    log = base / f"{driver.stem}.log"
    cmd = [MPV, "--no-config", "--pause", "--vo=null", "--ao=null", "--really-quiet",
           f"--log-file={log}", f"--watch-later-dir={base / 'watch_later'}",
           f"--script={LUA}", f"--script={driver}", f"--script-opts=driver-csv={csv}", str(folder)]
    proc = subprocess.run(cmd, capture_output=True, timeout=90)
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    tag = re.escape(f"[{driver.stem}]")
    return proc, [m.group(1) for m in (re.search(tag + r" (.*)$", ln) for ln in lines) if m]


@unittest.skipUnless(MPV and FFMPEG, "requires mpv and ffmpeg")
class ClipUndoAndSubfoldersTest(unittest.TestCase):
    """u inside an open clip undoes the latest mark, not the clip start; subfolders are in the
    playlist right away, after the folder's own files, and GoPro chapters are reordered only
    within their own folder."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        trip = base / "Trip"
        for rel in ("DJI_0001.MP4", "GX010001.MP4", "GX010003.MP4", "GX020001.MP4",
                    "Day2/GX010002.MP4", "Day2/DJI_0005.MP4", "Mid/GX010007.MP4"):
            (trip / rel).parent.mkdir(parents=True, exist_ok=True)
            make_video(trip / rel, 6)
        cls.proc, cls.log = run_mpv(base, trip, Path(__file__).with_name("mpv_driver_clip_folders.lua"),
                                    trip / "_triage.csv")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def csv_at(self, tag):
        rows = [ln[len(f"CSV {tag} "):].split(";") for ln in self.log
                if ln.startswith(f"CSV {tag} ") and not ln.endswith(" END")]
        return [(r[1], r[7], r[8]) for r in rows]          # event, time_hms, label

    def test_u_undoes_the_latest_mark_inside_an_open_clip(self):
        self.assertEqual(self.proc.returncode, 0)
        self.assertEqual(self.csv_at("after_undo"), [("mark", "00:00:02", "fish")])
        # the clip stayed open and takes its label from the mark that is left
        self.assertEqual(self.csv_at("after_close"),
                         [("mark", "00:00:02", "fish"), ("clip", "00:00:01", "fish")])

    def test_subfolders_follow_the_folder_and_keep_their_place(self):
        order = ["DJI_0001.MP4", "GX010001.MP4", "GX020001.MP4", "GX010003.MP4",
                 "Day2/DJI_0005.MP4", "Day2/GX010002.MP4", "Mid/GX010007.MP4"]
        self.assertIn("PLAYLIST " + ",".join(order), self.log)
        playing = [ln[len("PLAYING "):] for ln in self.log if ln.startswith("PLAYING ")]
        self.assertEqual(playing, order)                    # > walks every file once, in order


if __name__ == "__main__":
    unittest.main()
