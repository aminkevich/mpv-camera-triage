"""resolve_markers.py: Media Pool markers written into Resolve menu scripts — the cumulative data,
no Resolve installed, and the scripts themselves in Resolve's own Lua (fuscript.exe) under the
same sandbox as Resolve's menu, against a fake Resolve."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import row, run_tool, triage_csv
import resolve_markers as rm                 # noqa: E402  (support adds tools/ to sys.path)

FUSCRIPT = Path(r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fuscript.exe")
FPS = 60000 / 1001
ROWS = [row("mark", "GX010123.MP4", 10, label="fish"),
        row("mark", "GX010123.MP4", 10, label="shot"),              # the same moment
        row("clip", "GX010123.MP4", 30, 45, label="dive"),
        row("note", "GX010123.MP4", 12.5, note='Moray; «big one» "quoted" \\ back'),
        row("note", "GX010123.MP4", note="Murky water"),
        row("mark", "DJI_0001.MP4", 2, label="jump"),
        row("file_done", "C0001.MP4", rating="1", dur=20)]           # rated, no markers


class Sandbox(unittest.TestCase):
    """A temp footage folder, a temp Resolve script folder and a temp cache."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.trip = self.base / "Turkey"
        triage_csv(self.trip, ROWS)
        self.scripts = self.base / "Scripts" / "Utility"
        self.cache = self.base / "cache.json"
        self.env = {rm.ENV_SCRIPTS: str(self.scripts), rm.ENV_CACHE: str(self.cache)}
        patcher = mock.patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()


class UpdateMenuTest(Sandbox):
    def test_markers_per_file_with_proxies(self):
        rm.update_menu([self.trip / "_triage.csv"], quiet=True)
        data = rm.data_from(json.loads(self.cache.read_text(encoding="utf-8")))
        gopro = data[rm.key(self.trip.resolve() / "GX010123.MP4")]
        self.assertIs(data[rm.key(self.trip.resolve() / "GL010123.LRV")], gopro)
        self.assertIn(rm.key(self.trip.resolve() / "DJI_0001.LRF"), data)
        self.assertNotIn(rm.key(self.trip.resolve() / "C0001.MP4"), data)
        self.assertEqual([tuple(m[:4]) for m in gopro], [
            (0.0, 0, "Sand", "✎ file: Murky water"), (10.0, 0, "Blue", "fish"), (10.0, 0, "Red", "shot"),
            (12.5, 0, "Cream", '✎ Moray; «big one» "quoted" \\ back'), (30.0, 15.0, "Sky", "clip dive")])

    def test_data_accumulates_and_forgets_deleted_folders(self):
        other = self.base / "Greece"
        triage_csv(other, [row("mark", "GX020001.MP4", 4, label="catch")])
        rm.update_menu([self.trip / "_triage.csv"], quiet=True)
        _, files, markers = rm.update_menu([other / "_triage.csv"], quiet=True)
        self.assertEqual((files, markers), (3, 7))                     # Turkey kept
        (self.trip / "_triage.csv").unlink()
        _, files, markers = rm.update_menu([], quiet=True)
        self.assertEqual((files, markers), (1, 1))                     # Turkey's markup is gone
        _, files, _ = rm.update_menu([other / "_triage.csv"], clear=True, quiet=True)
        self.assertEqual(files, 1)

    def test_two_menu_scripts_old_entries_removed(self):
        self.scripts.mkdir(parents=True)
        for old in ("camera_triage - timeline markers.lua", "camera_triage - timeline markers.py",
                    "camera_triage - check lua.lua"):
            (self.scripts / old).write_text("old")
        (self.scripts / "someone else.lua").write_text("keep")
        res = run_tool("resolve_markers.py", self.base, extra_env=self.env)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("2 file(s) in 1 folder(s), 6 marker(s)", res.stdout)
        self.assertEqual(sorted(p.name for p in self.scripts.iterdir()),
                         ["camera_triage - media pool markers.lua", "camera_triage - remove markers.lua",
                          "someone else.lua"])
        for name in ("media pool markers", "remove markers"):
            text = (self.scripts / f"camera_triage - {name}.lua").read_text(encoding="utf-8")
            code = "\n".join(line.split("--")[0] for line in text.split("local M = (function()")[1].splitlines())
            for forbidden in ("io.", "dofile", "require", "os.execute", "debug."):   # missing in Resolve's sandbox
                self.assertNotIn(forbidden, code)
        remove = (self.scripts / "camera_triage - remove markers.lua").read_text(encoding="utf-8")
        self.assertNotIn("DATA[", remove)                              # remove needs no data

    def test_without_resolve_nothing_is_written(self):
        with mock.patch.dict(os.environ, {rm.ENV_SCRIPTS: "none"}):
            self.assertIsNone(rm.update_menu([self.trip / "_triage.csv"], quiet=True))
        self.assertFalse(self.scripts.exists())
        self.assertFalse(self.cache.exists())
        res = run_tool("resolve_markers.py", self.base)                # run_tool: no Resolve
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("DaVinci Resolve not found", res.stdout)


@unittest.skipUnless(FUSCRIPT.exists(), "requires DaVinci Resolve (its Lua interpreter fuscript.exe)")
class MenuScriptTest(Sandbox):
    """The generated menu scripts in Resolve's Lua, sandboxed like Resolve's menu."""

    def run_scripts(self, *entries):
        rm.update_menu([self.trip / "_triage.csv"], quiet=True)
        env = dict(os.environ, CT_DIR=str(self.trip.resolve()),
                   CT_SCRIPTS="|".join(str(self.scripts / f"camera_triage - {e}.lua") for e in entries))
        res = subprocess.run([str(FUSCRIPT), "-l", "lua", str(Path(__file__).with_name("resolve_lua_harness.lua"))],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=60)
        self.assertNotIn("SCRIPT ERROR", res.stdout)
        return res.stdout, [ln.split("|")[1:] for ln in res.stdout.splitlines() if ln.startswith("MARKER|")]

    def test_media_pool_markers_frames_colors_durations(self):
        out, markers = self.run_scripts("media pool markers")
        self.assertEqual([tuple(m[1:7]) for m in markers], [
            ("GX010123.MP4", "0", "Sand", "✎ file: Murky water", "1", "Murky water"),
            ("GX010123.MP4", "100", "Green", "my own", "1", ""),
            ("GX010123.MP4", str(round(10 * FPS)), "Blue", "fish", "1", ""),
            ("GX010123.MP4", str(round(10 * FPS) + 1), "Red", "shot", "1", ""),      # occupied → next frame
            ("GX010123.MP4", str(round(12.5 * FPS)), "Cream", '✎ Moray; «big one» "quoted" \\ back', "1",
             'Moray; «big one» "quoted" \\ back'),
            ("GX010123.MP4", str(round(30 * FPS)), "Sky", "clip dive", str(round(15 * FPS)), "i/o 00:00:30–00:00:45"),
            ("DJI_0001.LRF", "60", "Purple", "jump", "1", ""),                       # proxy → markup of its MP4
        ])
        self.assertIn("Media Pool — 2 clip(s) marked, 6 marker(s)", out)
        self.assertIn("without markup (1): C0001.MP4", out)

    def test_rerun_replaces_and_remove_keeps_own_markers(self):
        out, markers = self.run_scripts("media pool markers", "media pool markers", "remove markers")
        runs = {}
        for m in markers:
            runs.setdefault(m[0], []).append(tuple(m[1:4]))
        self.assertEqual(len(runs["camera_triage - media pool markers"]), 2 * 7)   # same 7 after each run
        self.assertEqual(runs["camera_triage - remove markers"], [("GX010123.MP4", "100", "Green")])
        self.assertIn("removed 6 marker(s)", out)


if __name__ == "__main__":
    unittest.main()
