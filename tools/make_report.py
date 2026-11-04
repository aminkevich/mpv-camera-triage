#!/usr/bin/env python3
"""
make_report.py — сводный отчёт по архиву футажа из всех _triage.csv (camera_triage.lua v3).

Обходит указанные папки рекурсивно, читает каждый _triage.csv и ищет видео (MP4/MOV) на диске,
в том числе в папках, где разметки ещё нет. Одна папка с одним _triage.csv — тоже архив,
просто из одной строки.

Три таблицы, от общего к частному (CSV: «;», UTF-8 с BOM — открываются в Excel/OnlyOffice):
  _report_folders.csv  папка — строка: что снято (активность, даты), сколько видео, сколько
                       просмотрено, оценки 0–3, длительность, метки по видам; в конце — ИТОГО.
  _report_files.csv    видео — строка: статус (просмотрен / оценка без Enter / начат / не открыт),
                       оценка, длительность, время съёмки, метки по видам, заметки к файлу,
                       есть ли файл на диске.
  _report_events.csv   все строки всех _triage.csv подряд — те же колонки, что в _triage.csv,
                       плюс папка, оценка и статус файла. Для фильтров вида «все catch в файлах
                       с оценкой 3».

--xlsx — то же одной книгой _report.xlsx (листы «Папки», «Файлы», «События», фильтры в шапке);
нужен openpyxl: py -m pip install openpyxl
--probe — недостающие длительность и время съёмки (у неоткрытых видео их нет в разметке)
через ffprobe; без него такие видео в суммы длительности не входят.

Отчёт — снимок: _triage.csv не меняются, отчёт можно перестраивать сколько угодно.
Пересохранять его в Excel можно (в отличие от _triage.csv).

Примеры:
  py make_report.py "D:\\Footage"
  py make_report.py "D:\\Footage" --probe --xlsx
  py make_report.py "D:\\Footage\\2026-09_Turkey" --out "D:\\Reports"
"""
import argparse
import csv
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import triage_plan as tp                    # noqa: E402  общее чтение _triage.csv

VIDEO_EXTS = {".mp4", ".mov"}
# порядок меток — как на клавиатуре в camera_triage.lua (общий список в triage_plan);
# метки, которых в нём нет (старые, переименованные), получают колонки в конце — см. main
LABELS = list(tp.LABELS)
TRIAGE_COLS = ["logged_at", "event", "activity", "file", "src", "time_ms", "end_ms", "time_hms",
               "label", "rating", "note", "speed_pct", "duration_ms", "duration_hms", "camera_time"]

DONE, DRAFT, STARTED, NEW = "просмотрен", "оценка без Enter", "начат", "не открыт"


def folder_cols(labels=LABELS):
    return (["folder", "status", "activity", "shot_from", "shot_to", "videos", "copies",
             "done", "draft", "started", "not_opened", "rated_0", "rated_1", "rated_2", "rated_3",
             "duration_hms", "duration_ms", "reviewed_hms", "reviewed_ms", "unknown_duration",
             "marks", "clips", "notes"] + list(labels) + ["last_triage"])


def file_cols(labels=LABELS):
    return (["folder", "file", "status", "rating", "rating_draft", "activity", "camera_time",
             "duration_ms", "duration_hms", "marks", "clips", "clips_hms", "notes"] + list(labels)
            + ["file_notes", "viewed_as", "on_disk", "copy_of", "copies", "last_triage"])


EVENT_COLS = ["folder"] + TRIAGE_COLS + ["file_rating", "file_status", "copy_of"]

OUT_NAMES = {"folders": "_report_folders.csv", "files": "_report_files.csv",
             "events": "_report_events.csv"}
XLSX_NAME = "_report.xlsx"


def parse_args():
    ap = argparse.ArgumentParser(description="Сводный отчёт по архиву из всех _triage.csv")
    ap.add_argument("folders", nargs="+", type=Path, help="папка архива (или несколько)")
    ap.add_argument("--out", type=Path, help="куда положить отчёт (по умолчанию — первая папка)")
    ap.add_argument("--probe", action="store_true",
                    help="недостающие длительность и время съёмки через ffprobe (неоткрытые видео)")
    ap.add_argument("--xlsx", action="store_true", help=f"ещё {XLSX_NAME} (нужен openpyxl)")
    ap.add_argument("--skip", default="Clips",
                    help="подпапки, которые не обходить, через запятую (Clips)")
    return ap.parse_args()


# ---------------------------------------------------------------- сбор

def hms_ms(ms):
    return tp.hms((ms or 0) / 1000) if ms else ""


def probe(path):
    """(duration_ms, camera_time) неоткрытого видео через ffprobe; (None, "") при ошибке."""
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration:format_tags=creation_time",
           "-of", "json", str(path)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=60)
        fmt = json.loads(res.stdout or "{}").get("format") or {}
        dur = float(fmt.get("duration") or 0)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None, ""
    ct = ((fmt.get("tags") or {}).get("creation_time") or "")[:19].replace("T", " ")
    return (round(dur * 1000) if dur else None), ct


def scan(roots, skip):
    """{папка: путь к _triage.csv или None} — все папки с разметкой или с видео."""
    found = {}
    for root in roots:
        if not root.exists():
            print(f"⚠ Папка не найдена: {root}")
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in skip)
            here = Path(dirpath).resolve()
            if tp.CSV_NAME in filenames:
                found[here] = here / tp.CSV_NAME
            elif any(Path(f).suffix.lower() in VIDEO_EXTS for f in filenames):
                found.setdefault(here, None)
    return found


def new_file(name):
    return {"file": name, "activity": "", "marks": 0, "clips": 0, "clips_ms": 0, "notes": 0,
            "labels": Counter(), "file_notes": [], "rating": None, "done_at": -1,
            "draft": None, "draft_at": -1, "duration_ms": None, "camera_time": "",
            "src": set(), "first": "", "last": "", "rows": False}


def read_folder(folder, csv_path, args):
    """→ (files: {ИМЯ: dict}, events: [row], csv_state) для одной папки."""
    files, events, state = {}, [], "нет"
    rows = tp.read_rows(csv_path) if csv_path else []
    if csv_path:
        state = "ok" if tp.triage_csv_ok(rows, csv_path) else "bad"
    if state == "bad":
        rows = []
    for i, r in enumerate(rows):
        name = (r.get("file") or "").strip()
        if not name:
            continue
        f = files.setdefault(name.upper(), new_file(name))
        f["rows"] = True
        events.append(r)
        f["activity"] = (r.get("activity") or "").strip() or f["activity"]
        at = r.get("logged_at") or ""
        f["last"] = max(f["last"], at)
        f["first"] = min(f["first"], at) if f["first"] else at
        if r.get("src"):
            f["src"].add(r["src"].strip())
        ev = r.get("event")
        if ev in ("file_done", "file_partial"):
            rating = tp.to_int(r.get("rating"))
            if ev == "file_done":
                f["rating"], f["done_at"] = rating, i
            else:
                f["draft"], f["draft_at"] = rating, i
            f["duration_ms"] = tp.to_int(r.get("duration_ms")) or f["duration_ms"]
            f["camera_time"] = (r.get("camera_time") or "").strip() or f["camera_time"]
        elif ev == "mark":
            f["marks"] += 1
            f["labels"][(r.get("label") or "").strip()] += 1
        elif ev == "clip":
            f["clips"] += 1
            s, e = tp.to_int(r.get("time_ms")), tp.to_int(r.get("end_ms"))
            if s is not None and e is not None and e > s:
                f["clips_ms"] += e - s
            if (r.get("label") or "").strip():
                f["labels"][r["label"].strip()] += 1
        elif ev == "note":
            f["notes"] += 1
            if tp.to_int(r.get("time_ms")) is None and (r.get("note") or "").strip():
                f["file_notes"].append(r["note"].strip())

    # видео на диске, которых нет в разметке (прокси — не отдельные видео)
    for p in sorted(folder.iterdir()):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS and p.name.upper() not in files:
            files[p.name.upper()] = new_file(p.name)
    for f in files.values():
        src, is_proxy = tp.find_source(folder, f["file"])
        f["on_disk"] = "нет" if src is None else ("прокси " + src.suffix.lstrip(".").upper()
                                                  if is_proxy else "оригинал")
        if src is not None and not is_proxy:
            f["size"] = src.stat().st_size          # признак копии того же видео (mark_copies)
        if f["rating"] is not None or f["done_at"] >= 0:
            f["status"] = DONE
        elif f["draft_at"] >= 0:
            f["status"] = DRAFT
        elif f["rows"]:
            f["status"] = STARTED
        else:
            f["status"] = NEW
        if f["draft_at"] < f["done_at"]:
            f["draft"] = None                       # черновик старше окончательной оценки
        if args.probe and src is not None and (f["duration_ms"] is None or not f["camera_time"]):
            f["duration_ms"], ct = probe(src)
            f["camera_time"] = f["camera_time"] or ct
    return files, events, state


# ---------------------------------------------------------------- таблицы

def file_row(folder_name, f, labels=LABELS):
    row = {"folder": folder_name, "file": f["file"], "status": f["status"],
           "rating": f["rating"], "rating_draft": f["draft"], "activity": f["activity"],
           "camera_time": f["camera_time"], "duration_ms": f["duration_ms"],
           "duration_hms": hms_ms(f["duration_ms"]), "marks": f["marks"], "clips": f["clips"],
           "clips_hms": hms_ms(f["clips_ms"]), "notes": f["notes"],
           "file_notes": " | ".join(f["file_notes"]), "viewed_as": ",".join(sorted(f["src"])),
           "on_disk": f["on_disk"], "copy_of": f.get("copy_of", ""), "copies": f.get("copies", 0),
           "last_triage": f["last"]}
    for lab in labels:
        row[lab] = f["labels"].get(lab, 0)
    return row


def folder_row(folder_name, files, state, labels=LABELS):
    fs = list(files.values())
    st = Counter(f["status"] for f in fs)
    dates = sorted(f["camera_time"][:10] for f in fs if f["camera_time"])
    known = [f["duration_ms"] for f in fs if f["duration_ms"]]
    reviewed = [f["duration_ms"] for f in fs if f["duration_ms"] and f["status"] == DONE]
    acts = Counter(f["activity"] for f in fs if f["activity"])
    dup = [f for f in fs if f.get("copy_of")]
    if state == "bad":
        status = "CSV в другом формате"
    elif dup and len(dup) == len(fs):
        src = Counter(f["copy_of"].split(" (")[0] for f in dup)
        status = "копия: " + ", ".join(s for s, _ in src.most_common())
    elif state == "нет":
        status = "не размечена"
    elif st[DONE] == len(fs):
        status = "размечена"
    else:
        status = f"частично ({st[DONE]} из {len(fs)})"
    row = {"folder": folder_name, "status": status,
           "activity": ", ".join(a for a, _ in acts.most_common()),
           "shot_from": dates[0] if dates else "", "shot_to": dates[-1] if dates else "",
           "videos": len(fs), "copies": len(dup), "done": st[DONE], "draft": st[DRAFT], "started": st[STARTED],
           "not_opened": st[NEW],
           "duration_ms": sum(known), "duration_hms": hms_ms(sum(known)),
           "reviewed_ms": sum(reviewed), "reviewed_hms": hms_ms(sum(reviewed)),
           "unknown_duration": len(fs) - len(known),
           "marks": sum(f["marks"] for f in fs), "clips": sum(f["clips"] for f in fs),
           "notes": sum(f["notes"] for f in fs),
           "last_triage": max((f["last"] for f in fs), default="")}
    for r in range(4):
        row[f"rated_{r}"] = sum(1 for f in fs if f["status"] == DONE and f["rating"] == r)
    for lab in labels:
        row[lab] = sum(f["labels"].get(lab, 0) for f in fs)
    return row


def copy_keys(f):
    """Признаки одного и того же видео в разных папках. Имени мало: GoPro после форматирования
    карты снова пишет GX010001. Размер файла — для лежащих на диске оригиналов; время первой
    записи разметки о файле — для копий _triage.csv (у копий оно одно, у разных видео — нет);
    время съёмки и длительность — если разметку вели заново; если камера не пишет время —
    точная длительность в мс (полные главы GoPro бывают одной длины, поэтому только без времени)."""
    keys, name = [], f["file"].upper()
    if f.get("size"):
        keys.append(("size", name, f["size"]))
    if f["first"]:
        keys.append(("log", name, f["first"]))
    if f["camera_time"] and f["duration_ms"]:
        keys.append(("time", name, f["camera_time"], f["duration_ms"] // 1000))
    elif f["duration_ms"] and f["rows"]:
        keys.append(("dur", name, f["duration_ms"]))
    return keys


def mark_copies(entries):
    """entries: [(папка, файл)] в порядке папок. Одно видео в нескольких папках → основная копия
    там, где видео лежит на диске и видео в папке больше всего (исходная папка футажа, а не
    проект с частью файлов), при равенстве — первая по пути. У остальных copy_of = папка
    основной (+ «разметка отличается»). Возвращает число копий."""
    per_folder = Counter(name for name, _ in entries)
    parent = list(range(len(entries)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    seen = {}
    for i, (_, f) in enumerate(entries):
        for k in copy_keys(f):
            if k in seen:
                parent[find(i)] = find(seen[k])
            else:
                seen[k] = i
    groups = {}
    for i in range(len(entries)):
        groups.setdefault(find(i), []).append(i)

    def sig(f):
        return (f["marks"], f["clips"], f["notes"], f["rating"], f["last"])

    n = 0
    for idx in groups.values():
        if len(idx) < 2:
            continue
        prim = max(idx, key=lambda i: (entries[i][1]["on_disk"] != "нет",
                                       per_folder[entries[i][0]], -i))
        pname, pf = entries[prim]
        pf["copies"] = len(idx) - 1
        for i in idx:
            if i != prim:
                f = entries[i][1]
                f["copy_of"] = pname + (" (разметка отличается)" if sig(f) != sig(pf) else "")
                n += 1
    return n


def event_row(folder_name, r, files):
    f = files.get((r.get("file") or "").strip().upper())
    row = {"folder": folder_name, **{c: r.get(c, "") for c in TRIAGE_COLS}}
    row["file_rating"] = "" if not f or f["rating"] is None else f["rating"]
    row["file_status"] = f["status"] if f else ""
    row["copy_of"] = f.get("copy_of", "") if f else ""
    return row


# ---------------------------------------------------------------- вывод

def write_csv(path, cols, rows):
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, delimiter=";", extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if v is None else v) for k, v in r.items()})
    try:
        os.replace(tmp, path)
    except OSError:
        tmp.unlink()
        raise


def typed(v):
    """Для xlsx: числа из CSV — числами, чтобы суммировались и сортировались."""
    if isinstance(v, str) and v.lstrip("-").isdigit() and len(v) < 16:
        return int(v)
    return "" if v is None else v


def write_xlsx(path, tables):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
        from openpyxl.utils import get_column_letter
    except ImportError:
        print(f"⚠ {XLSX_NAME} не создан: нужен openpyxl (py -m pip install openpyxl)")
        return False
    wb = Workbook()
    wb.remove(wb.active)
    for title, cols, rows in tables:
        ws = wb.create_sheet(title)
        ws.append(cols)
        for r in rows:
            ws.append([typed(r.get(c)) for c in cols])
        for cell in ws[1]:
            cell.font = Font(bold=True)
        ws.freeze_panes = "B2"
        ws.auto_filter.ref = ws.dimensions
        for i, c in enumerate(cols, 1):
            width = max([len(str(c))] + [len(str(r.get(c) or "")) for r in rows[:500]])
            ws.column_dimensions[get_column_letter(i)].width = min(60, max(6, width + 2))
    tmp = path.with_name(path.name + ".tmp")
    wb.save(tmp)
    try:
        os.replace(tmp, path)
    except OSError:
        tmp.unlink()
        raise
    return True


def main():
    tp.setup_console()
    args = parse_args()
    skip = tp.label_set(args.skip)
    found = scan(args.folders, skip)
    if not found:
        sys.exit(f"Не найдено ни {tp.CSV_NAME}, ни видео")
    roots = [r.resolve() for r in args.folders if r.exists()]
    out = (args.out or roots[0]).resolve()
    out.mkdir(parents=True, exist_ok=True)

    def display(folder):
        for root in roots:
            try:
                rel = folder.relative_to(root)
            except ValueError:
                continue
            name = rel.as_posix()
            return root.name if name == "." else (f"{root.name}/{name}" if len(roots) > 1 else name)
        return str(folder)

    read, bad = [], []
    for folder in sorted(found, key=lambda p: display(p).lower()):
        name = display(folder)
        files, events, state = read_folder(folder, found[folder], args)
        if state == "bad":
            bad.append(name)
        if files or state == "bad":
            read.append((name, files, events, state))
    copies = mark_copies([(name, f) for name, files, _, _ in read for f in files.values()])
    unknown = sorted({lab for _, files, _, _ in read for f in files.values() for lab in f["labels"]
                      if lab and lab not in tp.LABELS})
    labels = LABELS + unknown

    folders, files_t, events_t, unique = [], [], [], {}
    for name, files, events, state in read:
        folders.append(folder_row(name, files, state, labels))
        for _, f in sorted(files.items(), key=lambda kv: tp.file_order_key(kv[0])):
            files_t.append(file_row(name, f, labels))
            if not f.get("copy_of"):
                unique[(name, f["file"].upper())] = f
        events_t += [event_row(name, r, files) for r in events]
    # ИТОГО — по уникальным видео: копии папок (проект, экспорт, бэкап) не умножают архив
    total = folder_row("ИТОГО", unique, "ok", labels)
    total["status"] = f"папок {len(folders)}" + (f", копий видео {copies}" if copies else "")
    total["copies"] = copies
    folders.append(total)

    written = []
    folder_c, file_c = folder_cols(labels), file_cols(labels)
    for key, cols, rows in (("folders", folder_c, folders), ("files", file_c, files_t),
                            ("events", EVENT_COLS, events_t)):
        path = out / OUT_NAMES[key]
        try:
            write_csv(path, cols, rows)
            written.append(path.name)
        except PermissionError:
            print(f"✗ {path} открыт в другой программе (Excel?) — закрой и запусти ещё раз")
    if args.xlsx:
        try:
            if write_xlsx(out / XLSX_NAME, [("Папки", folder_c, folders),
                                            ("Файлы", file_c, files_t),
                                            ("События", EVENT_COLS, events_t)]):
                written.append(XLSX_NAME)
        except PermissionError:
            print(f"✗ {out / XLSX_NAME} открыт в другой программе (Excel?) — закрой и запусти ещё раз")

    t = folders[-1]
    top = sorted(((lab, t[lab]) for lab in labels if t[lab]), key=lambda x: -x[1])
    print(f"Папок: {len(folders) - 1}, видео: {t['videos']}, просмотрено: {t['done']}"
          f" (оценка без Enter {t['draft']}, начато {t['started']}, не открыто {t['not_opened']})")
    if copies:
        cf = [r["folder"] for r in folders[:-1] if r["copies"]]
        diff = sum(1 for r in files_t if "отличается" in r["copy_of"])
        print(f"Копии: {copies} видео ещё раз лежат в других папках ({', '.join(cf[:5])}"
              + (f" и ещё {len(cf) - 5}" if len(cf) > 5 else "") + ") — в итог вошли один раз"
              + (f"; у {diff} разметка отличается от основной копии" if diff else ""))
    if unknown:
        print(f"Метки не из списка camera_triage.lua: {', '.join(unknown)} — колонки в конце таблиц")
    print(f"Длительность: {t['duration_hms'] or '—'}"
          + (f" (у {t['unknown_duration']} видео неизвестна"
             + (": нет файла на диске)" if args.probe else " — --probe прочитает)")
             if t["unknown_duration"] else "")
          + f", просмотрено: {t['reviewed_hms'] or '—'}")
    print(f"Оценки: 0 — {t['rated_0']}, 1 — {t['rated_1']}, 2 — {t['rated_2']}, 3 — {t['rated_3']}")
    if top:
        print("Метки: " + ", ".join(f"{lab} {n}" for lab, n in top))
    undone = [r["folder"] for r in folders[:-1]
              if r["status"] != "размечена" and not r["status"].startswith("копия")]
    if undone:
        print(f"Не размечены полностью: {len(undone)} — " + ", ".join(undone[:5])
              + (f" и ещё {len(undone) - 5}" if len(undone) > 5 else ""))
    if bad:
        print(f"⚠ {tp.CSV_NAME} в другом формате (не прочитан): " + ", ".join(bad))
    if written:
        print(f"\nОтчёт в {out}: " + ", ".join(written))


if __name__ == "__main__":
    main()
