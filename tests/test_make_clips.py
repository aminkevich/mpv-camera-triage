"""make_clips.py without ffmpeg: clip names, output folder, --prune (no sources — nothing is cut)."""
import tempfile
import unittest
from pathlib import Path

from support import empty_config, quiet, row, tool_args, triage_csv

import make_clips as mc                     # noqa: E402  (support adds tools/ to sys.path)


class ClipNameTest(unittest.TestCase):
    def test_name_has_start_duration_and_sorted_labels(self):
        self.assertEqual(mc.clip_name("GX011151.MP4", 80.4, 94.6, {"shot", "fish"}),
                         "GX011151_00-01-20_14s_fish+shot.mp4")
        self.assertEqual(mc.clip_name("DJI_0001.MOV", 3725, 3730, {"jump"}),
                         "DJI_0001_01-02-05_5s_jump.mp4")


class OutDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conf = empty_config(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_relative_out_is_next_to_footage(self):
        csv = triage_csv(self.root / "Turkey", [])
        args = tool_args(mc, self.root, "--config", self.conf)
        self.assertEqual(mc.clips_dir(csv, args, many=False), self.root / "Turkey" / "Clips")

    def test_absolute_out_single_and_many(self):
        a = triage_csv(self.root / "Footage" / "Turkey", [])
        b = triage_csv(self.root / "Footage" / "Kite" / "day1", [])
        out = self.root / "Project" / "Clips"
        args = tool_args(mc, self.root / "Footage", "--config", self.conf, "--out", out)
        self.assertEqual(mc.clips_dir(a, args, many=False), out)
        self.assertEqual(mc.clips_dir(a, args, many=True), out / "Turkey")
        self.assertEqual(mc.clips_dir(b, args, many=True), out / "Kite" / "day1")
        # the marked footage folder itself is given — a subfolder by its name
        args = tool_args(mc, self.root / "Footage" / "Turkey", "--config", self.conf, "--out", out)
        self.assertEqual(mc.clips_dir(a, args, many=True), out / "Turkey")


class PruneTest(unittest.TestCase):
    """Regression: --prune together with filters deleted clips excluded by the filter."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name) / "Turkey"
        self.conf = empty_config(self.tmp.name)
        self.out = self.folder / "Clips"
        self.write_markup(shot=True)
        # clips of the previous run and their index; there is no source MP4 — nothing to cut
        self.fish = "GX010001_00-00-05_10s_fish.mp4"
        self.shot = "GX010001_00-00-35_10s_shot.mp4"
        self.low = "GX010002_00-00-15_10s_catch.mp4"
        self.out.mkdir(parents=True)
        entries = []
        for name in (self.fish, self.shot, self.low):
            (self.out / name).write_bytes(b"clip")
            entries.append({"clip": name, "source": name[:8] + ".MP4", "src": "mp4"})
        mc.write_index(self.out, {}, entries)

    def tearDown(self):
        self.tmp.cleanup()

    def write_markup(self, shot):
        rows = [row("mark", "GX010001.MP4", 10, label="fish")]
        if shot:
            rows.append(row("mark", "GX010001.MP4", 40, label="shot"))
        rows += [row("file_done", "GX010001.MP4", rating="3", dur=60),
                 row("mark", "GX010002.MP4", 20, label="catch"),
                 row("file_done", "GX010002.MP4", rating="1", dur=60)]
        triage_csv(self.folder, rows)

    def run_clips(self, *cli):
        args = tool_args(mc, self.folder, "--config", self.conf, *cli)
        with quiet() as out:
            st = mc.process_folder(self.folder / "_triage.csv", args, self.out)
        return st, out.getvalue()

    def existing(self):
        return {p.name for p in self.out.glob("*.mp4")}

    def test_filters_do_not_prune_other_clips(self):
        for cli in (["--labels", "fish"], ["--min-rating", "2"], ["--no-marks"]):
            with self.subTest(cli=cli):
                st, _ = self.run_clips("--prune", *cli)
                self.assertEqual(st["pruned"], 0)
                self.assertEqual(self.existing(), {self.fish, self.shot, self.low})

    def test_prune_removes_clips_after_markup_change(self):
        self.write_markup(shot=False)
        st, out = self.run_clips("--prune")
        self.assertEqual(st["pruned"], 1)
        self.assertEqual(self.existing(), {self.fish, self.low})
        self.assertIn(self.shot, out)

    def test_prune_dry_run_deletes_nothing(self):
        self.write_markup(shot=False)
        st, _ = self.run_clips("--prune", "--dry-run")
        self.assertEqual(st["pruned"], 1)
        self.assertEqual(self.existing(), {self.fish, self.shot, self.low})

    def test_missing_source_is_reported_and_clips_kept(self):
        st, out = self.run_clips()
        self.assertEqual(st["nosrc"], 3)
        self.assertIn("are not in the folder", out)
        self.assertEqual(self.existing(), {self.fish, self.shot, self.low})
        self.assertEqual(len(mc.read_index(self.out)), 3)   # the index of the previous run is kept


if __name__ == "__main__":
    unittest.main()
