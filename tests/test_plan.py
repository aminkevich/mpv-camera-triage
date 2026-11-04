"""triage_plan.py — общий план отрезков (его используют make_clips и make_timeline)."""
import tempfile
import unittest
from pathlib import Path

from support import plan_args, row, tp, triage_csv


def plan(rows, *cli, config):
    files = tp.collect(rows)
    args = plan_args(config, *cli)
    return {f: [(round(s, 3), round(e, 3), sorted(labs)) for s, e, labs in tp.selected_segments(d, args)]
            for f, d in files.items()}


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.conf = self.dir / "empty.conf"
        self.conf.write_text("", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def config(self, text):
        path = self.dir / "custom.conf"
        path.write_text(text, encoding="utf-8")
        return path

    # ---------------------------------------------------------------- отрезки

    def test_point_marks_default_pads_and_merge(self):
        rows = [row("mark", "A.MP4", 10, label="fish"), row("mark", "A.MP4", 21, label="shot"),
                row("mark", "A.MP4", 40, label="catch")]
        # 10±5 и 21±5: зазор 1 с ≤ merge_gap 2 → склеены; 40±5 — отдельно
        self.assertEqual(plan(rows, config=self.conf)["A.MP4"],
                         [(5, 26, ["fish", "shot"]), (35, 45, ["catch"])])

    def test_merge_gap_zero_keeps_segments_apart(self):
        rows = [row("mark", "A.MP4", 10, label="fish"), row("mark", "A.MP4", 21, label="shot")]
        self.assertEqual(len(plan(rows, "--merge-gap", "0", config=self.conf)["A.MP4"]), 2)

    def test_label_pads_from_config_and_cli_override(self):
        conf = self.config("pad_before = 7\npad_after = 6\npad.shot = 6/3\npad.scenery = 4\n")
        rows = [row("mark", "A.MP4", 20, label="shot"), row("mark", "A.MP4", 100, label="scenery"),
                row("mark", "A.MP4", 200, label="fish")]
        self.assertEqual(plan(rows, config=conf)["A.MP4"],
                         [(14, 23, ["shot"]), (96, 104, ["scenery"]), (193, 206, ["fish"])])
        # --pad-before в командной строке перекрывает и отступы по меткам; pad_after — из конфига
        self.assertEqual(plan(rows, "--pad-before", "1", config=conf)["A.MP4"],
                         [(19, 23, ["shot"]), (99, 104, ["scenery"]), (199, 206, ["fish"])])

    def test_clip_io_gets_inner_marks_and_clip_pad(self):
        rows = [row("clip", "A.MP4", 30, 45, label="dive"), row("mark", "A.MP4", 35, label="fish")]
        self.assertEqual(plan(rows, config=self.conf)["A.MP4"], [(29.5, 45.5, ["dive", "fish"])])

    def test_clip_without_label_is_named_clip(self):
        rows = [row("clip", "A.MP4", 30, 45)]
        self.assertEqual(plan(rows, config=self.conf)["A.MP4"], [(29.5, 45.5, ["clip"])])

    def test_bad_only_clip_is_dropped_and_bad_never_in_labels(self):
        rows = [row("clip", "A.MP4", 30, 45, label="bad"), row("mark", "A.MP4", 35, label="bad"),
                row("mark", "A.MP4", 100, label="bad"), row("clip", "A.MP4", 200, 210),
                row("mark", "A.MP4", 205, label="bad"), row("mark", "A.MP4", 206, label="fish")]
        self.assertEqual(plan(rows, config=self.conf)["A.MP4"], [(199.5, 210.5, ["fish"])])

    def test_point_exclude_marks_only_label_clips(self):
        rows = [row("mark", "A.MP4", 10, label="dive"), row("clip", "A.MP4", 30, 45),
                row("mark", "A.MP4", 40, label="dive")]
        self.assertEqual(plan(rows, "--point-exclude", "dive", config=self.conf)["A.MP4"],
                         [(29.5, 45.5, ["dive"])])

    def test_no_marks_keeps_only_io_clips(self):
        rows = [row("mark", "A.MP4", 10, label="fish"), row("clip", "A.MP4", 30, 45, label="dive")]
        self.assertEqual(plan(rows, "--no-marks", config=self.conf)["A.MP4"], [(29.5, 45.5, ["dive"])])

    def test_labels_filter_selects_without_changing_bounds(self):
        rows = [row("mark", "A.MP4", 10, label="fish"), row("mark", "A.MP4", 12, label="shot"),
                row("mark", "A.MP4", 40, label="catch")]
        full = plan(rows, config=self.conf)["A.MP4"]
        only = plan(rows, "--labels", "shot", config=self.conf)["A.MP4"]
        self.assertEqual(only, [full[0]])                   # тот же отрезок, не урезан до shot

    def test_clamped_to_file_start_and_duration_short_dropped(self):
        rows = [row("mark", "A.MP4", 2, label="fish"), row("mark", "A.MP4", 59.8, label="shot"),
                row("file_done", "A.MP4", rating="2", dur=60)]
        self.assertEqual(plan(rows, config=self.conf)["A.MP4"],
                         [(0, 7, ["fish"]), (54.8, 60, ["shot"])])
        rows = [row("clip", "A.MP4", 59.9, 59.95, label="dive"), row("file_done", "A.MP4", rating="2", dur=60)]
        self.assertEqual(plan(rows, "--clip-pad", "0", config=self.conf)["A.MP4"], [])

    # ---------------------------------------------------------------- разметка

    def test_collect_last_file_done_wins_and_file_notes(self):
        rows = [row("file_done", "A.MP4", rating="1", dur=30), row("note", "A.MP4", note="мутно"),
                row("note", "A.MP4", 12, note="мурена"), row("file_done", "A.MP4", rating="3", dur=30),
                row("file_partial", "A.MP4", rating="0", dur=30)]
        d = tp.collect(rows)["A.MP4"]
        self.assertEqual(d["rating"], 3)                    # file_partial — черновик, не оценка
        self.assertEqual(d["file_notes"], ["мутно"])
        self.assertEqual(d["notes"], [(12, "мурена")])
        self.assertEqual(d["duration"], 30)

    def test_rating_filters(self):
        args0 = plan_args(self.conf)
        args2 = plan_args(self.conf, "--min-rating", "2")
        argsall = plan_args(self.conf, "--min-rating", "0")
        zero, one, none = {"rating": 0}, {"rating": 1}, {"rating": None}
        self.assertTrue(tp.zero_rated(zero, args0))
        self.assertFalse(tp.zero_rated(zero, argsall))
        self.assertFalse(tp.below_min_rating(none, args0))   # без --min-rating непросмотренные берутся
        self.assertTrue(tp.below_min_rating(none, args2))
        self.assertTrue(tp.below_min_rating(one, args2))

    def test_gopro_chapter_order(self):
        names = ["GX020123.MP4", "DJI_0001.MP4", "GX010124.MP4", "GX010123.MP4", "GOPR0050.MP4",
                 "GP010050.MP4"]
        self.assertEqual(sorted(names, key=tp.file_order_key),
                         ["GOPR0050.MP4", "GP010050.MP4", "GX010123.MP4", "GX020123.MP4",
                          "GX010124.MP4", "DJI_0001.MP4"])

    def test_find_source_prefers_original_then_proxy(self):
        (self.dir / "GL010001.LRV").write_bytes(b"x")
        (self.dir / "DJI_0002.LRF").write_bytes(b"x")
        self.assertEqual(tp.find_source(self.dir, "GX010001.MP4"), (self.dir / "GL010001.LRV", True))
        self.assertEqual(tp.find_source(self.dir, "DJI_0002.MP4"), (self.dir / "DJI_0002.LRF", True))
        self.assertEqual(tp.find_source(self.dir, "NONE.MP4"), (None, False))
        (self.dir / "GX010001.MP4").write_bytes(b"x")
        self.assertEqual(tp.find_source(self.dir, "GX010001.MP4"), (self.dir / "GX010001.MP4", False))

    def test_foreign_csv_format_detected_even_without_rows(self):
        good = triage_csv(self.dir / "good", [])
        self.assertTrue(tp.triage_csv_ok(tp.read_rows(good), good))
        bad = self.dir / "bad.csv"
        bad.write_text("logged_at,event,file\n", encoding="utf-8")     # только шапка, «,»
        self.assertFalse(tp.triage_csv_ok(tp.read_rows(bad), bad))
        bad.write_text("logged_at,event,file\n1,file_done,X.MP4\n", encoding="utf-8")
        self.assertFalse(tp.triage_csv_ok(tp.read_rows(bad), bad))
        empty = self.dir / "empty.csv"
        empty.write_text("", encoding="utf-8")
        self.assertTrue(tp.triage_csv_ok(tp.read_rows(empty), empty))

    # ---------------------------------------------------------------- конфиг

    def test_config_errors_are_reported_with_line_numbers(self):
        conf = self.config("pad_before = 7\npad.fsh = 3\nmerge_gap = -1\nbogus = 1\nno equals here\n")
        with self.assertRaises(SystemExit) as cm:
            tp.load_config(conf)
        msg = str(cm.exception)
        for part in ("строка 2", "«fsh»", "строка 3", "строка 4", "«bogus»", "строка 5"):
            self.assertIn(part, msg)

    def test_config_comments_and_pad_forms(self):
        cfg, pads = tp.load_config(self.config("# комментарий\n pad.jump = 10 ; как в примере\n"
                                               "pad.shot = 6 / 3\nout = Clips # хвост\n"))
        self.assertEqual(pads, {"jump": (10, 10), "shot": (6, 3)})
        self.assertEqual(cfg, {"out": "Clips"})


if __name__ == "__main__":
    unittest.main()
