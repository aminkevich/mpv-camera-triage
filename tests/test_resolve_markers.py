"""resolve_markers.py against a fake Resolve (objects with the same methods as the scripting API):
marker frames, colors and durations, reruns, proxies, timeline and Media Pool, remove."""
import tempfile
import unittest
from pathlib import Path

from support import row, triage_csv
import resolve_markers as rm                 # noqa: E402  (support adds tools/ to sys.path)


class FakeClip:
    """MediaPoolItem / TimelineItem: markers by source frame, one per frame (as in Resolve)."""

    def __init__(self, path, fps=59.94, frames=60 * 60, name=None):
        self.props = {"File Path": str(path), "FPS": fps, "Frames": str(frames)}
        self.name = name or Path(path).name
        self.markers = {}

    def GetName(self):
        return self.name

    def GetClipProperty(self, key):
        return self.props.get(key)

    def GetMediaPoolItem(self):
        return self

    def GetMarkers(self):
        return dict(self.markers)

    def AddMarker(self, frame, color, name, note, duration, custom=None):
        if frame in self.markers:
            return False
        self.markers[frame] = {"color": color, "name": name, "note": note, "duration": duration,
                               "customData": custom or ""}
        return True

    def DeleteMarkerAtFrame(self, frame):
        return self.markers.pop(frame, None) is not None


class FakeFolder:
    def __init__(self, clips, subs=()):
        self.clips, self.subs = clips, list(subs)

    def GetClipList(self):
        return self.clips

    def GetSubFolderList(self):
        return self.subs


class FakeTimeline:
    def __init__(self, tracks):
        self.tracks = tracks

    def GetName(self):
        return "Selects Turkey"

    def GetTrackCount(self, kind):
        return len(self.tracks) if kind == "video" else 0

    def GetItemListInTrack(self, kind, i):
        return self.tracks[i - 1]


class FakeResolve:
    def __init__(self, root, timeline):
        project = type("P", (), {})()
        pool = type("MP", (), {"GetRootFolder": lambda s: root})()
        project.GetMediaPool = lambda: pool
        project.GetCurrentTimeline = lambda: timeline
        manager = type("PM", (), {"GetCurrentProject": lambda s: project})()
        self.GetProjectManager = lambda: manager


class ResolveMarkersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "Turkey"
        triage_csv(self.dir, [
            row("mark", "GX010123.MP4", 10, label="fish"),
            row("mark", "GX010123.MP4", 10, label="shot"),            # the same moment
            row("clip", "GX010123.MP4", 30, 45, label="dive"),
            row("note", "GX010123.MP4", 12.5, note="Moray; «big one»"),
            row("note", "GX010123.MP4", note="Murky water"),
            row("mark", "DJI_0001.MP4", 2, label="jump")])
        self.clip = FakeClip(self.dir / "GX010123.MP4")
        self.proxy = FakeClip(self.dir / "DJI_0001.LRF", fps=30)
        self.other = FakeClip(Path(self.tmp.name) / "Elsewhere" / "C0001.MP4")
        self.out = []
        rm.print = lambda *a, **k: self.out.append(" ".join(map(str, a)))

    def tearDown(self):
        del rm.print
        self.tmp.cleanup()

    def markers(self, clip):
        return {f: (m["color"], m["name"], m["duration"]) for f, m in clip.markers.items()}

    def test_frames_colors_and_durations_from_the_start_of_the_file(self):
        self.assertEqual(rm.mark(self.clip, self.clip, {}), (5, 5))
        fps = 60000 / 1001
        self.assertEqual(self.markers(self.clip), {
            0: ("Sand", "✎ file: Murky water", 1),                   # note for the file — at the start
            round(10 * fps): ("Blue", "fish", 1),
            round(10 * fps) + 1: ("Red", "shot", 1),                 # same moment → next frame
            round(12.5 * fps): ("Cream", "✎ Moray; «big one»", 1),
            round(30 * fps): ("Sky", "clip dive", round(15 * fps)),
        })
        self.assertEqual(self.clip.markers[round(12.5 * fps)]["note"], "Moray; «big one»")

    def test_rerun_replaces_own_markers_and_keeps_the_users(self):
        self.clip.AddMarker(100, "Green", "my own", "", 1)
        rm.mark(self.clip, self.clip, {})
        rm.mark(self.clip, self.clip, {})
        self.assertEqual(len(self.clip.markers), 6)
        self.assertEqual(self.clip.markers[100]["name"], "my own")

    def test_proxy_gets_the_markup_of_its_original(self):
        self.assertEqual(rm.mark(self.proxy, self.proxy, {}), (1, 1))
        self.assertEqual(self.markers(self.proxy), {60: ("Purple", "jump", 1)})
        self.assertEqual(rm.source_names("GL010123.LRV"), ["GL010123.LRV", "GX010123.MP4", "GH010123.MP4"])

    def test_markers_past_the_end_of_the_file_are_dropped(self):
        short = FakeClip(self.dir / "GX010123.MP4", frames=round(31 * 60000 / 1001))
        rm.mark(short, short, {})
        dive = [m for m in short.markers.values() if m["name"] == "clip dive"][0]
        self.assertEqual(dive["duration"], round(31 * 60000 / 1001) - round(30 * 60000 / 1001))

    def test_timeline_media_pool_and_remove(self):
        timeline = FakeTimeline([[self.clip, self.other], [self.proxy]])
        resolve = FakeResolve(FakeFolder([], [FakeFolder([self.clip, self.proxy])]), timeline)
        rm.run(resolve, "timeline")
        self.assertIn('timeline "Selects Turkey" — 2 clip(s) marked, 6 marker(s)', self.out[-2])
        self.assertIn("without markup (1): C0001.MP4", self.out[-1])
        self.clip.markers.clear()
        rm.run(resolve, "mediapool")
        self.assertIn("Media Pool — 2 clip(s) marked, 6 marker(s)", self.out[-1])
        self.clip.AddMarker(9999, "Green", "my own", "", 1)
        rm.run(resolve, "remove")
        self.assertEqual(list(self.clip.markers), [9999])
        self.assertEqual(self.proxy.markers, {})

    def test_exact_ntsc_rates(self):
        self.assertEqual(rm.exact_fps(59.94), rm.Fraction(60000, 1001))
        self.assertEqual(rm.exact_fps("29.97"), rm.Fraction(30000, 1001))
        self.assertEqual(rm.exact_fps(25.0), 25)


if __name__ == "__main__":
    unittest.main()
