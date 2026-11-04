"""camera_triage.lua в настоящем mpv без окна: порядок глав GoPro, Del, u, метка, оценка + Enter,
сортировка CSV при выходе. Нужны mpv и ffmpeg (путь к mpv — переменная MPV, если он не в PATH);
без них тест пропускается."""
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from support import FFMPEG, LUA, find_mpv, make_video, row, triage_csv

MPV = find_mpv()
DRIVER = Path(__file__).with_name("mpv_driver.lua")


@unittest.skipUnless(MPV and FFMPEG, "нужны mpv и ffmpeg")
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
        # mpv называет скрипт по имени файла: [mpv_driver]
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
        # на паузе метка ставится точно в кадр, куда пришёл переход на 12 с (59.94 fps — 11.995)
        self.assertAlmostEqual(int(events[1][4]), 12000, delta=17)
        playing = [ln for ln in self.log if ln.startswith("PLAYING ")]
        self.assertEqual(playing, ["PLAYING GX010123.MP4", "PLAYING GX020123.MP4"])


if __name__ == "__main__":
    unittest.main()
