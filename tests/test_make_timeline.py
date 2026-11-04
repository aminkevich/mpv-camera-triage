"""make_timeline.py без ffprobe: таймкоды, FCPXML (кадры, стыки, маркеры) и EDL маркеров."""
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from fractions import Fraction as F
from pathlib import Path

from support import plan_args, row, tp

import make_timeline as mt                  # noqa: E402  (support добавляет tools/ в sys.path)

NTSC60 = F(60000, 1001)


def info(fps, duration, tc=None, size=(1920, 1080)):
    """Параметры исходника, как их вернул бы probe()."""
    return {"fps": F(fps), "width": size[0], "height": size[1], "duration": duration,
            "tc0": mt.tc_frames(tc, F(fps)) if tc else 0,
            "tc_format": "DF" if tc and ";" in tc else "NDF", "audio": (2, 48000)}


def t(x):
    return F(x[:-1])


class TimecodeTest(unittest.TestCase):
    def test_non_drop_frame(self):
        self.assertEqual(mt.tc_frames("10:20:30:15", NTSC60), (10 * 3600 + 20 * 60 + 30) * 60 + 15)
        self.assertEqual(mt.tc_frames("00:00:01:00", F(25)), 25)

    def test_drop_frame(self):
        # 29.97 DF: за минуту выбрасываются кадры 00 и 01, кроме каждой десятой минуты
        self.assertEqual(mt.tc_frames("00:01:00;02", F(30000, 1001)), 1800)
        self.assertEqual(mt.tc_frames("00:10:00;00", F(30000, 1001)), 17982)
        # 59.94 DF — по 4 кадра
        self.assertEqual(mt.tc_frames("11:00:00;10", NTSC60), 39600 * 60 + 10 - 4 * (660 - 66))

    def test_bad_timecode_is_zero(self):
        self.assertEqual(mt.tc_frames("", NTSC60), 0)
        self.assertEqual(mt.tc_frames("not a tc", NTSC60), 0)

    def test_rational_time(self):
        self.assertEqual(mt.rt(F(1001, 60000)), "1001/60000s")
        self.assertEqual(mt.rt(10), "10s")

    def test_edl_timecode_counts_nominal_frames(self):
        self.assertEqual(mt.tc(216000, NTSC60), "01:00:00:00")
        self.assertEqual(mt.tc(216000 + 61, NTSC60), "01:00:01:01")


class TimelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name) / "Turkey"
        self.folder.mkdir()
        rows = [row("mark", "GX010123.MP4", 10, label="fish"), row("mark", "GX010123.MP4", 13, label="shot"),
                row("note", "GX010123.MP4", 12, note="Мурена; «большая»"),
                row("clip", "GX010123.MP4", 30, 45, label="dive"), row("mark", "GX010123.MP4", 35, label="bad"),
                row("note", "GX010123.MP4", note="Вода мутная"),
                row("file_done", "GX010123.MP4", rating="3", dur=60),
                row("mark", "DJI_0001.MP4", 38, label="jump"), row("file_done", "DJI_0001.MP4", rating="2", dur=40)]
        self.files = tp.collect(rows)
        conf = Path(self.tmp.name) / "empty.conf"
        conf.write_text("", encoding="utf-8")
        self.args = plan_args(conf)
        self.infos = {"GX010123.MP4": info(NTSC60, 60.06, tc="10:20:30:15"),
                      "DJI_0001.MP4": info(30, 40.0)}

    def tearDown(self):
        self.tmp.cleanup()

    def items(self, full=False):
        out = []
        for fname, d in tp.sorted_files(self.files):
            segs = [(0.0, self.infos[fname]["duration"], set())] if full else tp.selected_segments(d, self.args)
            out.append({"fname": fname, "d": d, "src": self.folder / fname, "info": self.infos[fname],
                        "segs": segs})
        return out

    def build(self, full=False):
        path = self.folder / ("_full.fcpxml" if full else "_selects.fcpxml")
        n, total, nm = mt.build_timeline(self.items(full), "Full" if full else "Selects",
                                         self.folder, path, full)
        return path, n, nm

    def check_fcpxml(self, path):
        """Отрезки встык с начала 01:00:00:00, на кадрах таймлайна, внутри исходника; маркеры внутри."""
        root = ET.parse(path).getroot()
        self.assertEqual(root.get("version"), mt.FCPXML_VERSION)
        fmts = {f.get("id"): t(f.get("frameDuration")) for f in root.iter("format")}
        assets = {a.get("id"): a for a in root.iter("asset")}
        seq = root.find(".//sequence")
        seqfd = fmts[seq.get("format")]
        offset = t(seq.get("tcStart"))
        self.assertEqual(offset / seqfd, 3600 * 60)
        positions = []
        for clip in root.iter("asset-clip"):
            asset = assets[clip.get("ref")]
            self.assertEqual(t(clip.get("offset")), offset)
            self.assertEqual((t(clip.get("offset")) / seqfd).denominator, 1)
            self.assertEqual((t(clip.get("duration")) / seqfd).denominator, 1)
            start, dur = t(clip.get("start")), t(clip.get("duration"))
            self.assertGreaterEqual(start, t(asset.get("start")))
            self.assertLessEqual(start + dur, t(asset.get("start")) + t(asset.get("duration")))
            for m in clip.iter("marker"):
                self.assertTrue(start <= t(m.get("start")) < start + dur)
                positions.append(round((offset + t(m.get("start")) - start) / seqfd))
            offset += dur
        self.assertEqual(t(seq.get("duration")), offset - t(seq.get("tcStart")))
        return root, sorted(positions)

    def edl_positions(self, path):
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\d{3}\s+001\s+V\s+C\s+(\d+):(\d+):(\d+):(\d+)", line)
            if m:
                h, mi, s, f = map(int, m.groups())
                out.append((h * 3600 + mi * 60 + s) * 60 + f)
        return sorted(out)

    def test_selects_timeline(self):
        path, n, nm = self.build()
        root, positions = self.check_fcpxml(path)
        names = [c.get("name") for c in root.iter("asset-clip")]
        self.assertEqual(names, ["GX010123 00:00:05 fish+shot ★3", "GX010123 00:00:29 dive ★3",
                                 "DJI_0001 00:00:33 jump ★2"])
        # исходник ищется по таймкоду: начало asset = таймкод файла
        gx = root.find(".//asset[@name='GX010123.MP4']")
        self.assertEqual(t(gx.get("start")), F(mt.tc_frames("10:20:30:15", NTSC60)) / NTSC60)
        self.assertEqual(gx.get("src"), (self.folder / "GX010123.MP4").resolve().as_uri())
        # маркеры: fish, заметка, shot, клип dive, bad, jump; заметка к файлу — только в Full
        values = [m.get("value") for m in root.iter("marker")]
        self.assertEqual(values, ["fish", "✎ Мурена; «большая»", "shot", "клип dive", "bad", "jump"])
        self.assertEqual((n, nm), (3, 6))
        self.assertEqual(positions, self.edl_positions(path.with_suffix(".edl")))

    def test_edl_names_colors_and_durations(self):
        path, _, _ = self.build()
        edl = path.with_suffix(".edl").read_text(encoding="utf-8")
        self.assertTrue(edl.startswith("TITLE: Selects Turkey\nFCM: NON-DROP FRAME\n"))
        self.assertIn("|C:ResolveColorBlue |M:fish |D:1", edl)
        self.assertIn("|C:ResolveColorCream |M:✎ Мурена; «большая» |D:1", edl)
        self.assertIn("|C:ResolveColorCocoa |M:bad |D:1", edl)
        self.assertRegex(edl, r"\|C:ResolveColorSky \|M:клип dive — i/o 00:00:30–00:00:45 \|D:89\d")
        self.assertNotIn("|M:|", edl)

    def test_mixed_frame_rates_stay_inside_source(self):
        path, _, _ = self.build()
        root, _ = self.check_fcpxml(path)
        dji = [c for c in root.iter("asset-clip") if c.get("name").startswith("DJI")][0]
        # 30 fps на таймлайне 59.94: длительность округлена вниз до кадра таймлайна
        self.assertLessEqual(t(dji.get("start")) + t(dji.get("duration")), 40)

    def test_full_timeline_has_file_notes(self):
        path, n, nm = self.build(full=True)
        root, positions = self.check_fcpxml(path)
        self.assertEqual(n, 2)
        values = [m.get("value") for m in root.iter("marker")]
        self.assertIn("✎ файл: Вода мутная", values)
        self.assertEqual(positions, self.edl_positions(path.with_suffix(".edl")))


if __name__ == "__main__":
    unittest.main()
