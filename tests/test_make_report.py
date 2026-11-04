"""make_report.py: статусы, копии, ИТОГО — на архиве из фиктивных файлов (ffprobe не нужен)."""
import csv
import shutil
import tempfile
import unittest
from pathlib import Path

from support import row, run_tool, triage_csv


def read(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


def video(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)


class ReportTest(unittest.TestCase):
    """Архив как у пользователя: футаж, копия в проект (дометили), копия разметки в экспорт,
    другое видео с тем же именем GoPro, папка без разметки, испорченный CSV, Clips."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = cls.root = Path(cls.tmp.name) / "Archive"
        turkey = root / "01_Footage" / "Turkey"
        video(turkey / "GX010001.MP4", 1000)
        video(turkey / "GX010002.MP4", 2000)
        video(turkey / "GX010003.MP4", 3000)                       # не открывался
        video(turkey / "Clips" / "GX010001_00-00-05_10s_fish.mp4", 10)   # не видео архива
        rows = [row("mark", "GX010001.MP4", 10, label="fish"), row("clip", "GX010001.MP4", 30, 45, label="dive"),
                row("note", "GX010001.MP4", note="мутно"),
                row("file_done", "GX010001.MP4", rating="2", dur=60, camera_time="2026-09-18 09:55:29"),
                row("file_done", "GX010001.MP4", rating="3", dur=60, camera_time="2026-09-18 09:55:29"),
                row("mark", "GX010002.MP4", 5, label="catch"),
                row("file_done", "GX010002.MP4", rating="2", dur=30, camera_time="2026-09-18 10:10:00"),
                row("file_partial", "GX010002.MP4", rating="0", dur=30, camera_time="2026-09-18 10:10:00"),
                row("mark", "GX010004.MP4", 3, label="shot"),       # начат, файла на диске нет
                row("mark", "DJI_0001.MP4", 1, label="jump", activity="kitesurfing"),
                row("file_partial", "DJI_0001.MP4", rating="1", dur=20, activity="kitesurfing")]
        triage_csv(turkey, rows)
        project = root / "02_Project" / "Turkey"
        project.mkdir(parents=True)
        shutil.copy(turkey / "GX010001.MP4", project / "GX010001.MP4")
        extra = row("mark", "GX010001.MP4", 50, label="catch")      # дометил в проекте
        triage_csv(project, rows + [extra])
        triage_csv(root / "03_Export", rows)                         # только разметка
        video(root / "Other" / "GX010001.MP4", 5000)                 # то же имя, другое видео
        bad = root / "Broken"
        bad.mkdir()
        # пересохранён в Excel: другой разделитель, время в другом формате
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
        self.assertIn("Отчёт в", self.res.stdout)
        self.assertNotIn("Clips", " ".join(self.folders))

    def test_file_statuses_and_ratings(self):
        f = lambda name: self.files[("01_Footage/Turkey", name)]       # noqa: E731
        self.assertEqual((f("GX010001.MP4")["status"], f("GX010001.MP4")["rating"]), ("просмотрен", "3"))
        self.assertEqual(f("GX010002.MP4")["rating_draft"], "0")       # переоценка без Enter
        self.assertEqual(f("GX010003.MP4")["status"], "не открыт")
        self.assertEqual((f("GX010004.MP4")["status"], f("GX010004.MP4")["on_disk"]), ("начат", "нет"))
        self.assertEqual((f("DJI_0001.MP4")["status"], f("DJI_0001.MP4")["rating_draft"]),
                         ("оценка без Enter", "1"))
        self.assertEqual((f("GX010001.MP4")["fish"], f("GX010001.MP4")["dive"], f("GX010001.MP4")["clips"]),
                         ("1", "1", "1"))
        self.assertEqual(f("GX010001.MP4")["file_notes"], "мутно")

    def test_copies_are_detected(self):
        project = self.files[("02_Project/Turkey", "GX010001.MP4")]
        self.assertEqual(project["copy_of"], "01_Footage/Turkey (разметка отличается)")
        for name in ("GX010001.MP4", "GX010002.MP4", "GX010004.MP4", "DJI_0001.MP4"):
            self.assertEqual(self.files[("03_Export", name)]["copy_of"], "01_Footage/Turkey", name)
        self.assertEqual(self.files[("01_Footage/Turkey", "GX010001.MP4")]["copies"], "2")
        self.assertEqual(self.files[("Other", "GX010001.MP4")]["copy_of"], "")   # другое видео
        self.assertEqual(self.folders["03_Export"]["status"], "копия: 01_Footage/Turkey")
        self.assertEqual(self.folders["02_Project/Turkey"]["status"], "копия: 01_Footage/Turkey")
        self.assertTrue(all(e["copy_of"] for e in self.events if e["folder"] == "03_Export"))

    def test_folder_statuses(self):
        self.assertEqual(self.folders["01_Footage/Turkey"]["status"], "частично (2 из 5)")
        self.assertEqual(self.folders["Other"]["status"], "не размечена")
        self.assertEqual(self.folders["Broken"]["status"], "CSV в другом формате")
        self.assertIn("Broken", self.res.stdout)

    def test_total_counts_each_video_once(self):
        total = self.folders["ИТОГО"]
        # Turkey: 5 видео (3 на диске + 2 только в разметке) + другое GX010001 в Other
        self.assertEqual(total["videos"], "6")
        self.assertEqual(total["done"], "2")
        self.assertEqual(total["rated_3"], "1")
        self.assertEqual(total["duration_ms"], str(60000 + 30000 + 20000))
        self.assertEqual(total["fish"], "1")                         # копии не умножают метки
        self.assertEqual(total["shot_from"], "2026-09-18")
        self.assertIn("копий видео", total["status"])
        self.assertIn("Копии:", self.res.stdout)

    def test_single_folder_is_its_own_archive(self):
        turkey = self.root / "01_Footage" / "Turkey"
        res = run_tool("make_report.py", turkey, "--out", Path(self.tmp.name) / "out")
        self.assertEqual(res.returncode, 0, res.stderr)
        folders = read(Path(self.tmp.name) / "out" / "_report_folders.csv")
        self.assertEqual([r["folder"] for r in folders], ["Turkey", "ИТОГО"])
        self.assertEqual(folders[-1]["videos"], "5")

    def test_skip_option(self):
        res = run_tool("make_report.py", self.root, "--skip", "Clips,02_Project,03_Export",
                       "--out", Path(self.tmp.name) / "skip")
        folders = [r["folder"] for r in read(Path(self.tmp.name) / "skip" / "_report_folders.csv")]
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(folders, ["01_Footage/Turkey", "Broken", "Other", "ИТОГО"])


if __name__ == "__main__":
    unittest.main()
