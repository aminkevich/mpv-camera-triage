#!/usr/bin/env python3
"""
make_timeline.py — разметка _triage.csv → таймлайн для монтажа (FCPXML, DaVinci Resolve и др.).

Вместо физической нарезки: тот же план отрезков, что у make_clips.py (те же отступы, склейка,
исключения, --labels, --min-rating, make_clips.conf), но на выходе — файл таймлайна, который
ссылается на ОРИГИНАЛЬНЫЕ видео. В монтажке любой отрезок растягивается до краёв исходника,
отступы задают только начальную ширину. Ничего не копируется и не перекодируется.

Для каждой папки футажа рядом с _triage.csv:
  _selects.fcpxml  таймлайн «Selects <папка>»: отрезки подряд (порядок записи, главы GoPro
                   подряд), на каждом маркеры — метки (название = метка), клипы i/o (маркер
                   с длительностью), заметки к моменту (текст в примечании маркера).
  _full.fcpxml     (--full) таймлайн «Full <папка>»: каждый размеченный файл целиком,
                   вся разметка маркерами, включая bad и заметки к файлу. Обзор всего материала.

Рядом с каждым .fcpxml — .edl с тем же именем: маркеры таймлайна с цветом по метке
(fish синий, shot красный, catch зелёный, …). Resolve не берёт маркеры из FCPXML, поэтому
маркеры импортируются вторым шагом.

Resolve:
  1. File → Import → Timeline… → выбрать .fcpxml. Видео попадут в Media Pool целиком.
  2. Правый клик по этому таймлайну в Media Pool → Timelines → Import →
     Timeline Markers from EDL… → выбрать .edl с тем же именем.
Если папку футажа потом переместить — Relink Media в Resolve.

Нужен ffprobe (частота кадров, длительность, таймкод исходника): winget install Gyan.FFmpeg
Частота таймлайна — самая частая среди исходников, начало — 01:00:00:00.

Примеры:
  py make_timeline.py "D:\\Footage\\2026-09_Turkey"
  py make_timeline.py "D:\\Footage\\2026-09_Turkey" --min-rating 2 --pad-before 8 --full
  py make_timeline.py "D:\\Footage" --labels fish,shot,catch --dry-run
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import triage_plan as tp                    # noqa: E402  общий план отрезков (как у make_clips.py)

SELECTS_NAME = "_selects.fcpxml"
FULL_NAME = "_full.fcpxml"
FCPXML_VERSION = "1.8"      # понимают Resolve 16+ и Final Cut Pro 10.4+


def parse_args():
    ap = argparse.ArgumentParser(description="Разметка _triage.csv → таймлайн FCPXML для монтажа")
    tp.add_plan_args(ap)
    ap.add_argument("--full", action="store_true",
                    help=f"ещё {FULL_NAME}: все размеченные файлы целиком со всей разметкой")
    ap.add_argument("--dry-run", action="store_true", help="только показать, что будет на таймлайне")
    return tp.finish_args(ap, ap.parse_args())


# ---------------------------------------------------------------- время

def rt(x):
    """Время FCPXML: рациональное число секунд — «1001/60000s», «10s»."""
    x = Fraction(x)
    return f"{x.numerator}s" if x.denominator == 1 else f"{x.numerator}/{x.denominator}s"


def tc_frames(tc, fps):
    """Таймкод «чч:мм:сс:кк» (или «;» — drop frame) → номер кадра от нуля."""
    m = re.match(r"^(\d+):(\d+):(\d+)([:;.,])(\d+)$", (tc or "").strip())
    if not m:
        return 0
    h, mi, s, sep, f = int(m[1]), int(m[2]), int(m[3]), m[4], int(m[5])
    nominal = round(fps)
    frames = (h * 3600 + mi * 60 + s) * nominal + f
    if sep in ";.," and nominal in (30, 60):     # drop frame: 29.97 / 59.94
        drop = nominal // 15
        minutes = h * 60 + mi
        frames -= drop * (minutes - minutes // 10)
    return frames


def probe(src):
    """Параметры исходника через ffprobe: fps, размер кадра, длительность, таймкод, звук."""
    cmd = ["ffprobe", "-v", "error",
           "-show_entries", "stream=codec_type,r_frame_rate,avg_frame_rate,width,height,channels,"
           "sample_rate:stream_tags=timecode:format=duration:format_tags=timecode",
           "-of", "json", str(src)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=60)
        data = json.loads(res.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        return None
    fps = None
    for key in ("r_frame_rate", "avg_frame_rate"):
        try:
            fps = Fraction(video.get(key) or "0")
        except (ValueError, ZeroDivisionError):
            fps = None
        if fps:
            break
    if not fps:
        return None
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format") or {}
    tc = ((fmt.get("tags") or {}).get("timecode")
          or next(((s.get("tags") or {}).get("timecode") for s in streams
                   if (s.get("tags") or {}).get("timecode")), None))
    try:
        duration = float(fmt.get("duration") or 0)
    except ValueError:
        duration = 0
    return {"fps": fps, "width": int(video.get("width") or 0), "height": int(video.get("height") or 0),
            "duration": duration, "tc0": tc_frames(tc, fps) if tc else 0,
            "tc_format": "DF" if tc and re.search(r"[;.,]\d+$", tc) else "NDF",
            "audio": (int(audio.get("channels") or 2), int(audio.get("sample_rate") or 48000))
            if audio else None}


# ---------------------------------------------------------------- FCPXML

class Doc:
    """Ресурсы (форматы и исходники) одного файла FCPXML."""

    def __init__(self):
        self.root = ET.Element("fcpxml", version=FCPXML_VERSION)
        self.resources = ET.SubElement(self.root, "resources")
        self.formats = {}       # (fps, w, h) → id
        self.assets = {}        # путь → id
        self.n = 0

    def _id(self):
        self.n += 1
        return f"r{self.n}"

    def format_id(self, fps, w, h):
        key = (fps, w, h)
        if key not in self.formats:
            self.formats[key] = self._id()
            ET.SubElement(self.resources, "format", id=self.formats[key],
                          frameDuration=rt(1 / fps), width=str(w), height=str(h))
        return self.formats[key]

    def asset_id(self, src, info):
        key = str(src)
        if key not in self.assets:
            fps = info["fps"]
            aid = self.assets[key] = self._id()
            attrs = {"id": aid, "name": src.name, "src": src.resolve().as_uri(),
                     "start": rt(Fraction(info["tc0"]) / fps),
                     "duration": rt(Fraction(round(info["duration"] * fps)) / fps),
                     "hasVideo": "1", "format": self.format_id(fps, info["width"], info["height"])}
            if info["audio"]:
                ch, rate = info["audio"]
                attrs.update(hasAudio="1", audioSources="1", audioChannels=str(ch), audioRate=str(rate))
            ET.SubElement(self.resources, "asset", attrs)
        return self.assets[key]

    def sequence(self, event_name, project_name, fps, w, h):
        library = self.root.find("library")
        if library is None:
            library = ET.SubElement(self.root, "library")
        event = ET.SubElement(library, "event", name=event_name)
        project = ET.SubElement(event, "project", name=project_name)
        tc_start = Fraction(3600 * round(fps)) / fps            # 01:00:00:00, как в Resolve
        seq = ET.SubElement(project, "sequence", format=self.format_id(fps, w, h),
                            tcStart=rt(tc_start), tcFormat="NDF", audioLayout="stereo", audioRate="48k")
        return seq, ET.SubElement(seq, "spine"), tc_start

    def write(self, path):
        ET.indent(self.root, space="  ")
        body = ET.tostring(self.root, encoding="unicode")
        path.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n\n' + body + "\n",
                        encoding="utf-8")


def add_clip(spine, doc, item, s, e, offset, seq_fps, name, markers):
    """Отрезок s…e (секунды от начала файла) исходника → asset-clip на таймлайне с offset.
    markers: [(t, длительность|None, название, примечание)]. Возвращает длительность на таймлайне."""
    info, src = item["info"], item["src"]
    fps, tc0 = info["fps"], info["tc0"]
    last = round(info["duration"] * fps) if info["duration"] else None
    sf = max(0, round(s * fps))
    ef = round(e * fps) if last is None else min(round(e * fps), last)
    if ef <= sf:
        return Fraction(0), []
    start = Fraction(tc0 + sf) / fps
    # целые кадры таймлайна, вниз — не выйти за конец исходника при другой частоте кадров
    length = Fraction(max(1, int(Fraction(ef - sf) / fps * seq_fps))) / seq_fps
    clip = ET.SubElement(spine, "asset-clip", ref=doc.asset_id(src, info), name=name,
                         offset=rt(offset), start=rt(start), duration=rt(length),
                         format=doc.format_id(fps, info["width"], info["height"]),
                         tcFormat=info["tc_format"])
    one = Fraction(1) / fps
    timeline = []           # для EDL: (кадр таймлайна, длительность в кадрах, цвет, название)
    for t, dur, title, note, color in sorted(markers, key=lambda m: m[0]):
        mf = min(max(round(t * fps), sf), ef - 1)
        mstart = Fraction(tc0 + mf) / fps
        mdur = one
        if dur:
            mdur = max(one, min(Fraction(round(dur * fps)) / fps, start + length - mstart))
        attrs = {"start": rt(mstart), "duration": rt(mdur), "value": title}
        if note:
            attrs["note"] = note
        ET.SubElement(clip, "marker", attrs)
        at = offset + (mstart - start)
        if not note:
            label = title
        elif title.endswith(short(note)):            # «✎ начало заметки…» → «✎ вся заметка»
            label = title[:len(title) - len(short(note))] + note
        else:
            label = f"{title} — {note}"
        timeline.append((round(at * seq_fps), max(1, round(mdur * seq_fps)), color, label))
    return length, timeline


def short(text, n=40):
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


# Цвета маркеров Resolve по меткам (в EDL; FCPXML цвет не передаёт)
LABEL_COLORS = {
    "fish": "Blue", "shot": "Red", "catch": "Green", "wildlife": "Cyan", "dive": "Sky",
    "scenery": "Mint", "people": "Pink", "highlight": "Yellow", "bad": "Cocoa",
    "jump": "Purple", "trick": "Fuchsia", "crash": "Rose", "ride": "Lavender",
}
NOTE_COLOR, FILE_NOTE_COLOR, OTHER_COLOR = "Cream", "Sand", "Lemon"


def segment_markers(d, s, e, with_file_notes=False):
    """Разметка внутри s…e → маркеры (t, длительность, название, примечание, цвет):
    все метки, включая bad; клипы i/o; заметки."""
    out = []
    for t, lab in d["marks"]:
        if s <= t <= e:
            out.append((t, None, lab, None, LABEL_COLORS.get(lab, OTHER_COLOR)))
    for cs, ce, lab in d["clips"]:
        if cs < e and ce > s:
            a, b = max(cs, s), min(ce, e)
            out.append((a, b - a, f"клип {lab}" if lab else "клип",
                        f"i/o {tp.hms(cs)}–{tp.hms(ce)}", LABEL_COLORS.get(lab, OTHER_COLOR)))
    for t, text in d["notes"]:
        if s <= t <= e:
            out.append((t, None, "✎ " + short(text), text, NOTE_COLOR))
    if with_file_notes:
        for text in d["file_notes"]:
            out.append((s, None, "✎ файл: " + short(text), text, FILE_NOTE_COLOR))
    return out


def tc(frame, fps):
    """Кадр таймлайна → таймкод non-drop «чч:мм:сс:кк» (счёт по номинальной частоте, как в Resolve)."""
    nominal = round(fps)
    f = frame % nominal
    s = frame // nominal
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}:{f:02d}"


def write_marker_edl(path, title, fps, markers):
    """EDL маркеров таймлайна в формате Resolve (Timelines → Export → Timeline Markers to EDL):
    импорт — правый клик по таймлайну в Media Pool → Timelines → Import → Timeline Markers from EDL."""
    lines = [f"TITLE: {title}", "FCM: NON-DROP FRAME", ""]
    for n, (frame, dur, color, name) in enumerate(sorted(markers), 1):
        a, b = tc(frame, fps), tc(frame + 1, fps)
        name = re.sub(r"[|\r\n]+", " ", name).strip()
        lines += [f"{n:03d}  001      V     C        {a} {b} {a} {b}  ",
                  f" |C:ResolveColor{color} |M:{name} |D:{dur}", ""]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def stars(rating):
    return "" if rating is None else f" ★{rating}"


def build_timeline(items, title, folder, out_path, full):
    """items: [{fname, d, src, info, segs}] → FCPXML с одним таймлайном
    и рядом EDL его маркеров (то же имя, .edl)."""
    used = [it for it in items if it["info"]]
    if not used:
        return 0, 0, 0
    fps_count = Counter(it["info"]["fps"] for it in used)
    seq_fps = fps_count.most_common(1)[0][0]
    w, h = Counter((it["info"]["width"], it["info"]["height"]) for it in used
                   if it["info"]["fps"] == seq_fps).most_common(1)[0][0]
    doc = Doc()
    seq, spine, tc_start = doc.sequence(folder.name, f"{title} {folder.name}", seq_fps, w, h)
    offset, n, markers = tc_start, 0, []
    for it in used:
        d, stem = it["d"], Path(it["fname"]).stem
        for s, e, labs in it["segs"]:
            if full:
                name = f"{stem}{stars(d['rating'])}"
            else:
                name = f"{stem} {tp.hms(s)} {'+'.join(sorted(labs))}{stars(d['rating'])}"
            length, clip_markers = add_clip(spine, doc, it, s, e, offset, seq_fps, name,
                                            segment_markers(d, s, e, with_file_notes=full))
            if length:
                offset += length
                markers += clip_markers
                n += 1
    seq.set("duration", rt(offset - tc_start))
    doc.write(out_path)
    write_marker_edl(out_path.with_suffix(".edl"), f"{title} {folder.name}", seq_fps, markers)
    return n, float(offset - tc_start), len(markers)


# ---------------------------------------------------------------- папка

def process_folder(csv_path, args):
    folder = csv_path.parent.resolve()          # имя папки — в названии таймлайна
    print(f"\n== {folder}")
    rows = tp.read_rows(csv_path)
    if not tp.triage_csv_ok(rows):
        print(f"  ⚠ {tp.CSV_NAME} в неожиданном формате (пересохранён в другой программе?), пропуск")
        return False
    selects, full, missing, probes = [], [], [], {}

    def info_for(src):
        if src not in probes:
            probes[src] = probe(src)
            if probes[src] is None:
                print(f"  ⚠ {src.name}: ffprobe не смог прочитать файл, пропуск")
        return probes[src]

    for fname, d in tp.sorted_files(tp.collect(rows)):
        if tp.zero_rated(d, args) or tp.below_min_rating(d, args):
            continue
        segs = tp.selected_segments(d, args)
        has_markup = d["marks"] or d["clips"] or d["notes"] or d["file_notes"]
        if not segs and not (args.full and has_markup):
            continue
        src, is_proxy = tp.find_source(folder, fname)
        if src is None:
            missing.append(fname)
            continue
        if is_proxy:
            print(f"  ⚠ {fname}: MP4 нет, на таймлайн идёт {src.name} (низкое разрешение)")
        for s, e, labs in segs:
            print(f"  {Path(fname).stem}  {tp.hms(s)}–{tp.hms(e)}  {'+'.join(sorted(labs))}"
                  f"{stars(d['rating'])}")
        if args.dry_run:
            continue
        info = info_for(src)
        if segs:
            selects.append({"fname": fname, "d": d, "src": src, "info": info, "segs": segs})
        if args.full:
            whole = info["duration"] if info else (d["duration"] or 0)
            full.append({"fname": fname, "d": d, "src": src, "info": info, "segs": [(0.0, whole, set())]})
    if missing:
        shown = ", ".join(Path(m).stem for m in missing[:3])
        more = f" и ещё {len(missing) - 3}" if len(missing) > 3 else ""
        print(f"  ⚠ {len(missing)} файл(ов) из разметки нет в папке (ни MP4, ни прокси), пропуск: "
              f"{shown}{more}")
    if args.dry_run:
        return True
    for items, title, name, is_full in ((selects, "Selects", SELECTS_NAME, False),
                                        (full, "Full", FULL_NAME, True)):
        if not items:
            continue
        path = folder / name
        try:
            n, total, nm = build_timeline(items, title, folder, path, is_full)
        except OSError as e:
            print(f"  ✗ {name} не записан: {e}")
            continue
        if n:
            print(f"  → {path.name}: {n} отрезк(ов), {tp.hms(total)};  "
                  f"{path.with_suffix('.edl').name}: {nm} маркер(ов)")
    if not selects and not full:
        print("  (нечего класть на таймлайн)")
    return True


def main():
    tp.setup_console()
    args = parse_args()
    print(tp.describe_settings(args))
    if not args.dry_run and shutil.which("ffprobe") is None:
        sys.exit("ffprobe не найден в PATH. Установи: winget install Gyan.FFmpeg  (и открой новое окно терминала)")
    csvs = tp.find_csvs(args.folders, skip_dir=args.out)
    if not csvs:
        sys.exit(f"Не найдено ни одного {tp.CSV_NAME}")
    for c in csvs:
        process_folder(c, args)
    if not args.dry_run:
        print("\nResolve:\n"
              "  1. File → Import → Timeline… → выбрать .fcpxml\n"
              "  2. маркеры: правый клик по этому таймлайну в Media Pool → Timelines → Import →\n"
              "     Timeline Markers from EDL… → выбрать .edl с тем же именем")


if __name__ == "__main__":
    main()
