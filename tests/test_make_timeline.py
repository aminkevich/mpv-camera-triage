"""make_timeline.py without ffprobe: timecodes, the OTIO timeline with clip markers, FCPXML (frames,
joins, markers) and the marker EDL."""
import json
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from fractions import Fraction as F
from pathlib import Path

from support import plan_args, row, tp

import make_timeline as mt                  # noqa: E402  (support adds tools/ to sys.path)

NTSC60 = F(60000, 1001)


def info(fps, duration, tc=None, size=(1920, 1080)):
    """Source parameters as probe() would return them."""
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
        # 29.97 DF: frames 00 and 01 are dropped every minute except every tenth minute
        self.assertEqual(mt.tc_frames("00:01:00;02", F(30000, 1001)), 1800)
        self.assertEqual(mt.tc_frames("00:10:00;00", F(30000, 1001)), 17982)
        # 59.94 DF — 4 frames each
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
                row("note", "GX010123.MP4", 12, note="Moray; «big one»"),
                row("clip", "GX010123.MP4", 30, 45, label="dive"), row("mark", "GX010123.MP4", 35, label="bad"),
                row("note", "GX010123.MP4", note="Murky water"),
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
        """Segments back to back from 01:00:00:00, on timeline frames, inside the source; markers inside."""
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
        # the source is found by timecode: asset start = the file's timecode
        gx = root.find(".//asset[@name='GX010123.MP4']")
        self.assertEqual(t(gx.get("start")), F(mt.tc_frames("10:20:30:15", NTSC60)) / NTSC60)
        self.assertEqual(gx.get("src"), (self.folder / "GX010123.MP4").resolve().as_uri())
        # markers: fish, note, shot, clip dive, bad, jump; the note for the file — only in Full
        values = [m.get("value") for m in root.iter("marker")]
        self.assertEqual(values, ["fish", "✎ Moray; «big one»", "shot", "clip dive", "bad", "jump"])
        self.assertEqual((n, nm), (3, 6))
        self.assertEqual(positions, self.edl_positions(path.with_suffix(".edl")))

    def test_edl_names_colors_and_durations(self):
        path, _, _ = self.build()
        edl = path.with_suffix(".edl").read_text(encoding="utf-8")
        self.assertTrue(edl.startswith("TITLE: Selects Turkey\nFCM: NON-DROP FRAME\n"))
        self.assertIn("|C:ResolveColorBlue |M:fish |D:1", edl)
        self.assertIn("|C:ResolveColorCream |M:✎ Moray; «big one» |D:1", edl)
        self.assertIn("|C:ResolveColorCocoa |M:bad |D:1", edl)
        self.assertRegex(edl, r"\|C:ResolveColorSky \|M:clip dive — i/o 00:00:30–00:00:45 \|D:89\d")
        self.assertNotIn("|M:|", edl)

    def test_mixed_frame_rates_stay_inside_source(self):
        path, _, _ = self.build()
        root, _ = self.check_fcpxml(path)
        dji = [c for c in root.iter("asset-clip") if c.get("name").startswith("DJI")][0]
        # 30 fps on a 59.94 timeline: the duration is rounded down to a timeline frame
        self.assertLessEqual(t(dji.get("start")) + t(dji.get("duration")), 40)

    def test_full_timeline_has_file_notes(self):
        path, n, nm = self.build(full=True)
        root, positions = self.check_fcpxml(path)
        self.assertEqual(n, 2)
        values = [m.get("value") for m in root.iter("marker")]
        self.assertIn("✎ file: Murky water", values)
        self.assertEqual(positions, self.edl_positions(path.with_suffix(".edl")))

    # ---------------------------------------------------------------- OpenTimelineIO

    def build_otio(self, full=False):
        path = self.folder / ("_full.otio" if full else "_selects.otio")
        n, total, nm = mt.build_otio(self.items(full), "Full" if full else "Selects", self.folder, path, full)
        return json.loads(path.read_text(encoding="utf-8")), n, nm

    def check_otio(self, tl):
        """Clips inside their source, clip markers inside their clip, in the source's own time;
        the audio track in step with the video; the timeline starts at 01:00:00:00."""
        self.assertEqual(tl["OTIO_SCHEMA"], "Timeline.1")
        start = tl["global_start_time"]
        self.assertEqual(start["value"] / start["rate"], 3600 * 60 / float(NTSC60))
        tracks = tl["tracks"]["children"]
        video = tracks[0]["children"]
        for clip in video:
            sr = clip["source_range"]
            ar = clip["media_references"]["DEFAULT_MEDIA"]["available_range"]
            s0, dur = sr["start_time"]["value"], sr["duration"]["value"]
            self.assertGreaterEqual(s0, ar["start_time"]["value"])
            self.assertLessEqual(s0 + dur, ar["start_time"]["value"] + ar["duration"]["value"])
            for m in clip["markers"]:
                mr = m["marked_range"]
                self.assertEqual(mr["start_time"]["rate"], sr["start_time"]["rate"])
                self.assertTrue(s0 <= mr["start_time"]["value"] < s0 + dur, m["name"])
                self.assertLessEqual(mr["start_time"]["value"] + mr["duration"]["value"], s0 + dur)
        if len(tracks) > 1:
            self.assertEqual(tracks[1]["kind"], "Audio")
            self.assertEqual([c["source_range"]["duration"] for c in tracks[1]["children"]],
                             [c["source_range"]["duration"] for c in video])
        return video

    def test_otio_selects_with_clip_markers(self):
        tl, n, nm = self.build_otio()
        self.assertEqual(tl["name"], "Selects Turkey")
        video = self.check_otio(tl)
        self.assertEqual([c["name"] for c in video], ["GX010123 00:00:05 fish+shot ★3", "GX010123 00:00:29 dive ★3",
                                                     "DJI_0001 00:00:33 jump ★2"])
        self.assertEqual((n, nm), (3, 6))
        gx = video[0]
        tc0 = mt.tc_frames("10:20:30:15", NTSC60)
        self.assertEqual(gx["source_range"]["start_time"]["value"], tc0 + round(5 * NTSC60))
        self.assertEqual(gx["media_references"]["DEFAULT_MEDIA"]["target_url"],
                         (self.folder / "GX010123.MP4").resolve().as_uri())
        # markers in the source's own time (its timecode): Resolve reads them so
        fish = gx["markers"][0]
        self.assertEqual((fish["name"], fish["color"]), ("fish", "BLUE"))
        self.assertEqual(fish["marked_range"]["start_time"]["value"], tc0 + round(10 * NTSC60))
        self.assertEqual([m["name"] for c in video for m in c["markers"]],
                         ["fish", "✎ Moray; «big one»", "shot", "clip dive", "bad", "jump"])
        note = gx["markers"][1]
        self.assertEqual((note["color"], note["comment"]), ("WHITE", "Moray; «big one»"))
        self.assertEqual(note["metadata"]["Resolve_OTIO"]["Note"], "Moray; «big one»")   # Resolve reads this
        # each video clip and its sound share a Link Group ID: linked in Resolve
        audio = tl["tracks"]["children"][1]["children"]
        links = [(v["metadata"]["Resolve_OTIO"]["Link Group ID"], a["metadata"]["Resolve_OTIO"]["Link Group ID"])
                 for v, a in zip(video, audio)]
        self.assertEqual(links, [(1, 1), (2, 2), (3, 3)])
        self.assertEqual(len(audio[0]["metadata"]["Resolve_OTIO"]["Channels"]), 2)
        self.assertEqual(audio[0]["markers"], [])
        dive = video[1]["markers"][0]
        self.assertEqual((dive["color"], dive["marked_range"]["duration"]["value"]), ("CYAN", round(15 * NTSC60)))
        dji = video[2]
        self.assertEqual(dji["source_range"]["start_time"]["rate"], 30.0)   # each clip in its own rate

    def test_otio_full_has_file_notes(self):
        tl, n, nm = self.build_otio(full=True)
        video = self.check_otio(tl)
        self.assertEqual(n, 2)
        self.assertIn("✎ file: Murky water", [m["name"] for m in video[0]["markers"]])

    def test_otio_audio_gap_for_a_silent_source(self):
        self.infos["DJI_0001.MP4"]["audio"] = None
        tl, _, _ = self.build_otio()
        audio = tl["tracks"]["children"][1]["children"]
        self.assertEqual([c["OTIO_SCHEMA"] for c in audio], ["Clip.2", "Clip.2", "Gap.1"])
        video = self.check_otio(tl)
        self.assertEqual(video[2]["metadata"], {})                   # nothing to link with


if __name__ == "__main__":
    unittest.main()
