#!/usr/bin/env python3
"""
make_clips.py — нарезка клипов из разметки _triage.csv (camera_triage.lua v3).

Ищет все _triage.csv в указанных папках (рекурсивно) и для каждой папки футажа
создаёт подпапку Clips/ с клипами и индексом Clips/_clips.csv.

Откуда берутся отрезки:
  * clip (i/o в mpv)  — точные границы ± --clip-pad
  * mark (точки)      — время − --pad-before … время + --pad-after
  Пересекающиеся или близкие (≤ --merge-gap) отрезки одного файла склеиваются,
  метки объединяются в имени: GX011067_00-01-24_fish+shot.mp4
  Метки из --exclude (по умолчанию bad) клипов не дают.
  Точечные метки из --point-exclude (по умолчанию нет) сами клипов не дают; внутри отрезка i/o
  они только добавляют метку клипу. Пример: --point-exclude dive.

Режем всегда из оригинального MP4 (если его нет — из прокси LRV/LRF, с предупреждением).
По умолчанию без перекодирования (-c copy): быстро и без потерь, начало сдвигается
к ближайшему ключевому кадру. --reencode — точные границы, H.264, медленнее.

Примеры:
  py make_clips.py "C:\\...\\20260918_GoPro_Raznirka_Pristrelka" --dry-run
  py make_clips.py "C:\\...\\02_SourceVideos" --min-rating 2
  py make_clips.py "D:\\Archiv" --labels fish,shot,catch --pad-before 8
"""
import argparse
import csv
import shutil
import subprocess
import sys
from pathlib import Path

CSV_NAME = "_triage.csv"
INDEX_NAME = "_clips.csv"


def parse_args():
    ap = argparse.ArgumentParser(description="Нарезка клипов из _triage.csv")
    ap.add_argument("folders", nargs="+", type=Path, help="папки футажа или родительские папки")
    ap.add_argument("--pad-before", type=float, default=5.0, help="секунд до точечной метки (5)")
    ap.add_argument("--pad-after", type=float, default=5.0, help="секунд после точечной метки (5)")
    ap.add_argument("--clip-pad", type=float, default=0.5, help="запас вокруг отрезков i/o (0.5)")
    ap.add_argument("--merge-gap", type=float, default=2.0, help="склеивать отрезки ближе N секунд (2)")
    ap.add_argument("--labels", help="только эти метки, через запятую (например fish,shot)")
    ap.add_argument("--exclude", default="bad", help="метки, из которых клипы не делать (bad)")
    ap.add_argument("--point-exclude", default="",
                    help="точечные метки, которые сами клипов не дают (например dive)")
    ap.add_argument("--min-rating", type=int, default=0,
                    help="только файлы с оценкой ≥ N; при N>0 непросмотренные файлы пропускаются")
    ap.add_argument("--no-marks", action="store_true", help="только отрезки i/o, без точечных меток")
    ap.add_argument("--reencode", action="store_true", help="точная нарезка с перекодированием в H.264")
    ap.add_argument("--out", default="Clips", help="имя подпапки для клипов (Clips)")
    ap.add_argument("--overwrite", action="store_true", help="перезаписывать существующие клипы")
    ap.add_argument("--dry-run", action="store_true", help="только показать, что будет нарезано")
    return ap.parse_args()


def label_set(s):
    return {x.strip() for x in (s or "").split(",") if x.strip()}


def hms_dash(t):
    t = int(t)
    return f"{t // 3600:02d}-{t % 3600 // 60:02d}-{t % 60:02d}"


def to_int(s):
    s = (s or "").strip()
    return int(s) if s.lstrip("-").isdigit() else None


def read_rows(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


def collect(rows, args, only, exclude, point_exclude=frozenset()):
    """file -> {'segs': [(start, end, labels:set)], 'rating': int|None, 'duration': float|None}

    Отрезки i/o берутся как есть (± clip-pad). Точечная метка, попавшая внутрь отрезка i/o,
    не раздвигает его, а только добавляет свою метку в имя клипа.
    """
    files = {}
    for r in rows:
        fname = (r.get("file") or "").strip()
        if not fname:
            continue
        d = files.setdefault(fname, {"clips": [], "marks": [], "rating": None, "duration": None})
        ev = r.get("event")
        if ev == "file_done":                       # последняя запись побеждает
            d["rating"] = to_int(r.get("rating"))
            dur = to_int(r.get("duration_ms"))
            d["duration"] = dur / 1000 if dur else None
            continue
        if ev not in ("mark", "clip"):
            continue
        label = (r.get("label") or "").strip() or "clip"
        if label in exclude or (only and label not in only):
            continue
        t = to_int(r.get("time_ms"))
        if t is None:
            continue
        t /= 1000
        if ev == "clip":
            end = to_int(r.get("end_ms"))
            if end is not None and end / 1000 > t:
                d["clips"].append([t, end / 1000, {label}])
        elif not args.no_marks:
            d["marks"].append((t, label))

    for d in files.values():
        segs = [(max(0.0, s - args.clip_pad), e + args.clip_pad, labs) for s, e, labs in d["clips"]]
        for t, label in d["marks"]:
            inside = [c for c in d["clips"] if c[0] <= t <= c[1]]
            if inside:
                for c in inside:
                    c[2].add(label)
            elif label not in point_exclude:
                segs.append((max(0.0, t - args.pad_before), t + args.pad_after, {label}))
        d["segs"] = segs
    return files


def merge(segs, gap):
    out = []
    for s, e, labs in sorted(segs, key=lambda x: (x[0], x[1])):
        if out and s <= out[-1][1] + gap:
            ps, pe, plabs = out[-1]
            out[-1] = (ps, max(pe, e), plabs | labs)
        else:
            out.append((s, e, set(labs)))
    return out


def write_index(out_dir, entries):
    """Обновляет Clips/_clips.csv: новые записи заменяют старые с тем же именем,
    записи о клипах, которых больше нет на диске, удаляются."""
    path = out_dir / INDEX_NAME
    merged = {}
    if path.exists():
        for r in read_rows(path):
            if r.get("clip"):
                merged[r["clip"]] = r
    for e in entries:
        merged[e["clip"]] = e
    rows = [r for name, r in sorted(merged.items()) if (out_dir / name).exists()]
    if not rows:
        return
    fields = ["clip", "source", "start_ms", "end_ms", "labels", "rating"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter=";", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def find_source(folder, fname):
    p = folder / fname
    if p.exists():
        return p, False
    stem, up = Path(fname).stem, fname.upper()
    if up[:2] in ("GX", "GH") and up.endswith(".MP4"):
        cands = [folder / ("GL" + stem[2:] + ".LRV")]           # GoPro
    else:
        cands = [folder / (stem + ".LRF"), folder / (stem + ".LRV")]   # DJI и прочие
    for proxy in cands:
        if proxy.exists():
            return proxy, True
    return None, False


def ffmpeg_cmd(src, dst, start, dur, reencode):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{dur:.3f}",
           "-map", "0:v:0", "-map", "0:a:0?"]
    if reencode:
        cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
    return cmd + ["-movflags", "+faststart", str(dst)]


def process_folder(csv_path, args, only, exclude):
    folder = csv_path.parent
    out_dir = folder / args.out
    rows = read_rows(csv_path)
    index, made, skipped, failed = [], 0, 0, 0
    print(f"\n== {folder}")
    need = {"event", "file", "time_ms", "end_ms", "label", "rating"}
    if rows and not need <= set(rows[0].keys()):
        print(f"  ⚠ {CSV_NAME} в неожиданном формате (пересохранён в другой программе?), пропуск")
        return 0, 0, 1, 0
    files = collect(rows, args, only, exclude, label_set(args.point_exclude))

    for fname, d in sorted(files.items()):
        rating = d["rating"]
        if args.min_rating > 0 and (rating is None or rating < args.min_rating):
            continue
        segs = merge(d["segs"], args.merge_gap)
        if not segs:
            continue
        src, is_lrv = find_source(folder, fname)
        if src is None:
            print(f"  ⚠ {fname}: исходник не найден, пропуск")
            failed += len(segs)
            continue
        if is_lrv:
            print(f"  ⚠ {fname}: MP4 нет, режу из {src.name} (низкое разрешение)")

        for s, e, labs in segs:
            if d["duration"]:
                e = min(e, d["duration"])
            if e - s < 0.5:
                continue
            name = f"{Path(fname).stem}_{hms_dash(s)}_{'+'.join(sorted(labs))}.mp4"
            dst = out_dir / name
            index.append({"clip": name, "source": fname, "start_ms": int(s * 1000),
                          "end_ms": int(e * 1000), "labels": "+".join(sorted(labs)),
                          "rating": "" if rating is None else rating})
            tag = f"  {name}  ({e - s:.0f} с)"
            if dst.exists() and not args.overwrite:
                print(tag + "  — уже есть")
                skipped += 1
                continue
            if args.dry_run:
                print(tag)
                continue
            out_dir.mkdir(exist_ok=True)
            res = subprocess.run(ffmpeg_cmd(src, dst, s, e - s, args.reencode),
                                 capture_output=True, text=True, encoding="utf-8", errors="replace")
            if res.returncode == 0:
                print(tag + "  ✓")
                made += 1
            else:
                print(tag + "  ✗ " + res.stderr.strip()[-300:])
                failed += 1

    if index and not args.dry_run and out_dir.exists():
        write_index(out_dir, index)
    if not index:
        print("  (нечего нарезать)")
    return made, skipped, failed, len(index)


def main():
    args = parse_args()
    if not args.dry_run and shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg не найден в PATH. Установи: winget install Gyan.FFmpeg  (и открой новое окно терминала)")
    only, exclude = label_set(args.labels), label_set(args.exclude)

    csvs = []
    for root in args.folders:
        if not root.exists():
            print(f"⚠ Папка не найдена: {root}")
            continue
        csvs += [p for p in sorted(root.rglob(CSV_NAME)) if args.out not in p.parts]
    if not csvs:
        sys.exit(f"Не найдено ни одного {CSV_NAME}")

    total = [0, 0, 0, 0]
    for c in csvs:
        for i, v in enumerate(process_folder(c, args, only, exclude)):
            total[i] += v
    made, skipped, failed, planned = total
    if args.dry_run:
        print(f"\nDry run: {planned} клипов было бы нарезано (уже есть: {skipped}).")
    else:
        print(f"\nГотово: нарезано {made}, уже было {skipped}, ошибок {failed}.")


if __name__ == "__main__":
    main()
