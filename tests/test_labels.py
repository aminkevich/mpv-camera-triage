"""Список меток: camera_triage.lua (клавиши, подсказка F1) и triage_plan.LABELS (отчёт, цвета,
проверка конфига) должны совпадать. Новая метка, добавленная только в одно место, роняет тест."""
import re
import tempfile
import unittest
from pathlib import Path

from support import LUA, row, run_tool, tp, triage_csv


def lua_block(src, start):
    """Текст таблицы Lua от `start` до закрывающей скобки в начале строки."""
    i = src.index(start)
    return src[i:src.index("\n}", i)]


LUA_SRC = Path(LUA).read_text(encoding="utf-8")
LUA_LABELS = re.findall(r'\{\s*"([^"]+)",\s*"([^"]+)"\s*\}', lua_block(LUA_SRC, "local LABELS = {"))
HELP = lua_block(LUA_SRC, "local HELP = table.concat({")


class LabelsTest(unittest.TestCase):
    def test_lua_labels_parsed(self):
        self.assertGreaterEqual(len(LUA_LABELS), 10)

    def test_python_list_matches_lua_in_keyboard_order(self):
        self.assertEqual(list(tp.LABELS), [label for _, label in LUA_LABELS])

    def test_keys_and_labels_unique(self):
        keys = [k for k, _ in LUA_LABELS]
        self.assertEqual(len(keys), len(set(keys)), keys)
        self.assertEqual(len(tp.LABELS), len(set(tp.LABELS)))

    def test_every_label_in_f1_help(self):
        for key, label in LUA_LABELS:
            found = re.search(rf"(^|[\s\"]){re.escape(key)} {re.escape(label)}(\s|\"|$)", HELP)
            self.assertTrue(found, f"нет «{key} {label}» в подсказке HELP в camera_triage.lua")

    def test_every_label_has_resolve_color(self):
        import make_timeline
        allowed = {"Blue", "Cyan", "Green", "Yellow", "Red", "Pink", "Purple", "Fuchsia", "Rose",
                   "Lavender", "Sky", "Mint", "Lemon", "Sand", "Cocoa", "Cream"}
        for label, color in tp.LABELS.items():
            self.assertIn(color, allowed, label)
        self.assertIs(make_timeline.LABEL_COLORS, tp.LABELS)

    def test_report_keeps_labels_missing_from_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "Trip"
            triage_csv(folder, [row("mark", "A.MP4", 5, label="fish"),
                                row("mark", "A.MP4", 9, label="octopus"),
                                row("file_done", "A.MP4", rating="2", dur=30)])
            res = run_tool("make_report.py", folder)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertIn("octopus", res.stdout)
            header = (folder / "_report_files.csv").read_text(encoding="utf-8-sig").splitlines()[0]
            cols = header.split(";")
            self.assertEqual(cols[cols.index("ride") + 1], "octopus")   # после списка, до хвоста
            line = (folder / "_report_files.csv").read_text(encoding="utf-8-sig").splitlines()[1]
            self.assertEqual(line.split(";")[cols.index("octopus")], "1")


if __name__ == "__main__":
    unittest.main()
