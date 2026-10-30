#!/usr/bin/env python3
"""
make_clips.py — нарезка клипов из разметки _triage.csv (camera_triage.lua v3).

Ищет все _triage.csv в указанных папках (рекурсивно) и для каждой папки футажа
создаёт подпапку Clips/ с клипами и индексом Clips/_clips.csv.

Откуда берутся отрезки:
  * clip (i/o в mpv)  — точные границы ± --clip-pad; метки клипа = все точечные метки внутри
                        (+ метка из строки clip). Клип, где есть только метки из --exclude
                        (например i … b … o), не нарезается.
  * mark (точки)      — время − --pad-before … время + --pad-after
  Пересекающиеся или близкие (≤ --merge-gap) отрезки одного файла склеиваются,
  метки объединяются в имени: GX011067_00-01-24_14s_fish+shot.mp4 (начало, длительность, метки;
  при смене отступов имя меняется и клип режется заново, старый убирает --prune)
  Метки из --exclude (по умолчанию bad) клипов не дают и в имя не попадают.
  Точечные метки из --point-exclude (по умолчанию нет) сами клипов не дают; внутри отрезка i/o
  они только добавляют метку клипу. Пример: --point-exclude dive.
  --labels выбирает, какие клипы резать (в клипе есть хоть одна из меток), но имя и границы
  клипа те же, что без фильтра, — поэтому повторный запуск без фильтра не плодит дублей.

Файлы с оценкой 0 («мусор») по умолчанию пропускаются; --min-rating 0 — брать всё.

Для монтажа удобнее make_timeline.py: тот же план, но таймлайн FCPXML со ссылками на
оригиналы — отрезки можно расширять в монтажке. Общая логика плана — в triage_plan.py.

Режем всегда из оригинального MP4 (если его нет — из прокси LRV/LRF, с предупреждением;
когда MP4 появится, такие клипы перережутся автоматически).
По умолчанию без перекодирования (-c copy): байты исходника просто копируются — быстро и
без потерь. Начинать клип можно только с ключевого кадра (остальные кадры хранят лишь разницу
с соседними), поэтому начало сдвигается к предыдущему ключевому кадру, обычно на доли секунды
или около секунды; в индекс пишется фактическое начало. Для нарезки по разметке этого хватает.

--reencode — экспорт для совместимости: полное декодирование и сжатие заново в H.264 8 бит
(libx264, CRF 18) + AAC. Нужен, когда клип должен играть везде (телефон, мессенджер, старые
плееры не тянут HEVC / 10 бит) или нужен старт ровно с кадра. Очень медленно на 4K: кодирует
процессор, время — от десятков секунд до минут на клип.

Повторный запуск: существующие клипы не трогаются. Если разметка или параметры изменились,
имена новых клипов другие, а старые остаются — --prune удаляет из Clips/ клипы, которых нет
в текущем плане (только те, что записаны в _clips.csv; чужие файлы не трогаются).
План для --prune строится без --labels, --no-marks и --min-rating: эти фильтры выбирают,
что резать сейчас, но клипы с другими метками и из файлов с меньшей оценкой не удаляют.
Удаляются клипы после смены разметки, отступов или оценки 0.

Настройки (make_clips.conf рядом со скриптом, или --config ПУТЬ):
  постоянные параметры — отступы (глобально и по меткам), склейка, исключения, мин. оценка,
  папка для клипов, параметры --reencode. Приоритет: встроенные значения → конфиг → командная
  строка. --pad-before / --pad-after в командной строке задают отступ для ВСЕХ меток
  (отступы по меткам из конфига для этой стороны не действуют). Формат — см. make_clips.conf.

Примеры:
  py make_clips.py "C:\\...\\20260918_GoPro_Raznirka_Pristrelka" --dry-run
  py make_clips.py "C:\\...\\02_SourceVideos" --min-rating 2
  py make_clips.py "D:\\Archiv" --labels fish,shot,catch --pad-before 8
  py make_clips.py "C:\\...\\20260918_GoPro_Raznirka_Pristrelka" --prune --dry-run
"""
import argparse
import csv
import json
import shutil
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import triage_plan as tp                    # noqa: E402  общий план отрезков (и для make_timeline.py)

INDEX_NAME = "_clips.csv"
PENDING_NAME = "_clips.pending.csv"   # индекс, который не удалось записать (файл был открыт)
INDEX_FIELDS = ["clip", "source", "src", "start_ms", "end_ms", "duration_ms",
                "labels", "rating", "activity", "marks", "notes"]


def parse_args():
    ap = argparse.ArgumentParser(description="Нарезка клипов из _triage.csv")
    tp.add_plan_args(ap)
    ap.add_argument("--reencode", action="store_true", help="экспорт для совместимости: перекодировать в H.264 8 бит + AAC "
                         "(играет везде, старт ровно по кадру; на 4K очень медленно)")
    ap.add_argument("--out", help="имя подпапки для клипов (Clips)")
    ap.add_argument("--overwrite", action="store_true", help="перезаписывать существующие клипы")
    ap.add_argument("--prune", action="store_true",
                    help="удалить из Clips/ клипы из _clips.csv, которых нет в текущем плане "
                         "(--labels, --no-marks, --min-rating на это не влияют)")
    ap.add_argument("--dry-run", action="store_true", help="только показать, что будет сделано")
    return tp.finish_args(ap, ap.parse_args())


def describe_settings(args):
    text = tp.describe_settings(args)
    if args.reencode:
        text += f"\n  перекодирование: libx264 CRF {args.reencode_crf}, preset {args.reencode_preset}"
    return text


def clip_name(fname, s, e, labs):
    """GX011151_00-01-20_14s_fish+shot.mp4 — начало и длительность в имени: при смене
    pad_before или pad_after имя меняется, и клип режется заново (старый убирает --prune)."""
    return f"{Path(fname).stem}_{tp.hms_dash(s)}_{round(e - s)}s_{'+'.join(sorted(labs))}.mp4"


# ---------------------------------------------------------------- индекс

def read_index(out_dir):
    """Индекс + несохранённый остаток прошлого запуска (_clips.pending.csv, он новее)."""
    rows = {}
    for name in (INDEX_NAME, PENDING_NAME):
        path = out_dir / name
        if path.exists():
            try:
                rows.update({r["clip"]: r for r in tp.read_rows(path) if r.get("clip")})
            except OSError as e:
                print(f"  ⚠ не могу прочитать {path.name}: {e}")
    return rows


def index_locked(out_dir):
    """True, если _clips.csv открыт в другой программе (Excel блокирует файл на Windows)."""
    path = out_dir / INDEX_NAME
    if not path.exists():
        return False
    try:
        with open(path, "r+b"):
            return False
    except PermissionError:
        return True
    except OSError:
        return False


def wait_index_unlocked(out_dir):
    """Перед нарезкой: если индекс открыт — попросить закрыть (в интерактивном окне)."""
    if not index_locked(out_dir):
        return
    print(f"  ⚠ {out_dir / INDEX_NAME} открыт в другой программе (Excel?).")
    if sys.stdin and sys.stdin.isatty():
        try:
            input("    Закрой его и нажми Enter (или просто Enter — продолжить без этого) ")
        except EOFError:
            pass
    if index_locked(out_dir):
        print(f"    Клипы будут нарезаны, индекс сохранится в {PENDING_NAME} "
              f"и перенесётся в {INDEX_NAME} при следующем запуске.")


def write_index(out_dir, old, entries):
    """Обновляет Clips/_clips.csv: новые записи заменяют старые с тем же именем,
    записи о клипах, которых больше нет на диске, удаляются.
    Пишет во временный файл и подменяет; если _clips.csv заблокирован — оставляет
    _clips.pending.csv, который подхватит следующий запуск."""
    merged = dict(old)
    for e in entries:
        merged[e["clip"]] = e
    rows = [r for name, r in sorted(merged.items()) if (out_dir / name).exists()]
    path, pending = out_dir / INDEX_NAME, out_dir / PENDING_NAME
    tmp = out_dir / (INDEX_NAME + ".tmp")
    try:
        if not rows:
            for p in (path, pending):
                if p.exists():
                    p.unlink()
            return
        with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=INDEX_FIELDS, delimiter=";", extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        for attempt in range(3):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(1)
        if pending.exists():
            pending.unlink()
    except OSError as e:
        try:
            if tmp.exists():
                os.replace(tmp, pending)
            print(f"  ⚠ {INDEX_NAME} не обновлён ({e.__class__.__name__}: файл открыт?). "
                  f"Записан {PENDING_NAME} — перенесётся при следующем запуске.")
        except OSError as e2:
            print(f"  ✗ индекс не сохранён: {e2}")


# ---------------------------------------------------------------- ffmpeg

def keyframe_before(src, t):
    """Время ключевого кадра ≤ t (с которого реально начнётся клип при -c copy) или None."""
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
           "-read_intervals", f"{max(0.0, t - 15):.3f}%{t + 0.05:.3f}",
           "-show_entries", "frame=pts_time,best_effort_timestamp_time:format=start_time",
           "-of", "json", str(src)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=60)
        data = json.loads(res.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    t0 = float((data.get("format") or {}).get("start_time") or 0)   # -ss в ffmpeg считается от start_time
    best = None
    for fr in data.get("frames", []):
        v = fr.get("pts_time") or fr.get("best_effort_timestamp_time")
        try:
            k = float(v) - t0
        except (TypeError, ValueError):
            continue
        if k <= t + 0.0005 and (best is None or k > best):
            best = k
    return max(0.0, best) if best is not None else None


def ffmpeg_cmd(src, dst, start, dur, reencode, crf=18, preset="medium"):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{dur:.3f}",
           "-map", "0:v:0", "-map", "0:a:0?"]
    if reencode:
        cmd += ["-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
    return cmd + ["-movflags", "+faststart", str(dst)]


def cut(src, dst, start, dur, args):
    """Режет во временный файл и переименовывает только при успехе:
    оборванный клип (ошибка, Ctrl+C) не остаётся на диске под «готовым» именем."""
    tmp = dst.with_name(dst.stem + ".part" + dst.suffix)
    try:
        res = subprocess.run(ffmpeg_cmd(src, tmp, start, dur, args.reencode,
                                        args.reencode_crf, args.reencode_preset),
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        if res.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(dst)
            return None
        return res.stderr.strip()[-300:] or f"ffmpeg код {res.returncode}"
    finally:
        if tmp.exists():
            tmp.unlink()


# ---------------------------------------------------------------- папка

def fmt_points(items, s, e):
    return " | ".join(f"{txt}@{int(t * 1000)}" for t, txt in sorted(items) if s <= t <= e)


def process_folder(csv_path, args):
    folder = csv_path.parent
    out_dir = folder / args.out
    rows = tp.read_rows(csv_path)
    st = {"made": 0, "skipped": 0, "failed": 0, "todo": 0, "pruned": 0, "nosrc": 0}
    print(f"\n== {folder}")
    if not tp.triage_csv_ok(rows):
        print(f"  ⚠ {tp.CSV_NAME} в неожиданном формате (пересохранён в другой программе?), пропуск")
        st["failed"] += 1
        return st
    files = tp.collect(rows)
    old = read_index(out_dir)
    if not args.dry_run:
        wait_index_unlocked(out_dir)
    index, keep, missing, planned = [], set(), [], False

    try:
        for fname, d in tp.sorted_files(files):
            rating = d["rating"]
            if tp.zero_rated(d, args):
                if args.min_rating is None and (d["marks"] or d["clips"]):
                    print(f"  · {fname}: оценка 0, пропуск (--min-rating 0 — брать)")
                continue
            # --prune: --labels, --no-marks и --min-rating — только выбор, что резать сейчас;
            # клипы полного плана не удаляются (иначе --labels fish --prune стёр бы всё остальное)
            keep.update(clip_name(fname, s, e, labs)
                        for s, e, labs in tp.build_segments(d, args, no_marks=False))
            if tp.below_min_rating(d, args):
                continue
            segs = tp.selected_segments(d, args)
            if not segs:
                continue
            names = [clip_name(fname, s, e, labs) for s, e, labs in segs]
            keep.update(names)                      # даже если исходника нет — не удалять при --prune
            planned = True
            src, is_proxy = tp.find_source(folder, fname)
            if src is None:
                missing.append(fname)               # сводка одной строкой в конце папки
                st["nosrc"] += len(segs)
                continue
            src_kind = src.suffix.lower().lstrip(".") if is_proxy else "mp4"
            if is_proxy:
                print(f"  ⚠ {fname}: MP4 нет, режу из {src.name} (низкое разрешение)")

            for (s, e, labs), name in zip(segs, names):
                dst = out_dir / name
                prev = old.get(name)
                upgrade = (dst.exists() and not is_proxy and prev is not None
                           and (prev.get("src") or "mp4") != "mp4")
                tag = f"  {name}  ({e - s:.0f} с)"
                entry = {"clip": name, "source": fname, "src": src_kind,
                         "start_ms": int(s * 1000), "end_ms": int(e * 1000),
                         "duration_ms": int((e - s) * 1000), "labels": "+".join(sorted(labs)),
                         "rating": "" if rating is None else rating, "activity": d["activity"],
                         "marks": fmt_points(d["marks"], s, e), "notes": fmt_points(d["notes"], s, e)}
                if dst.exists() and not args.overwrite and not upgrade:
                    print(tag + "  — уже есть")
                    st["skipped"] += 1
                    if prev:                        # сохранить фактическое начало из прошлого запуска
                        for k in ("src", "start_ms", "duration_ms"):
                            if prev.get(k):
                                entry[k] = prev[k]
                    elif not args.dry_run and not args.reencode:   # индекс был потерян — восстановить
                        kf = keyframe_before(src, s)
                        if kf is not None and kf < s:
                            entry["start_ms"] = int(kf * 1000)
                            entry["duration_ms"] = int((e - kf) * 1000)
                    index.append(entry)
                    continue
                if upgrade:
                    tag += "  (был из прокси, перерезаю из MP4)"
                st["todo"] += 1
                if args.dry_run:
                    print(tag)
                    continue
                out_dir.mkdir(exist_ok=True)
                if not args.reencode:               # при -c copy старт уходит на ключевой кадр
                    kf = keyframe_before(src, s)
                    if kf is not None and kf < s:
                        entry["start_ms"] = int(kf * 1000)
                        entry["duration_ms"] = int((e - kf) * 1000)
                err = cut(src, dst, s, e - s, args)
                if err is None:
                    print(tag + "  ✓")
                    st["made"] += 1
                    index.append(entry)
                else:
                    print(tag + "  ✗ " + err)
                    st["failed"] += 1

        if args.prune:
            for name in sorted(set(old) - keep):
                p = out_dir / name
                if p.exists():
                    print(f"  ✂ {name}" + ("  (будет удалён)" if args.dry_run else "  удалён"))
                    if not args.dry_run:
                        p.unlink()
                    st["pruned"] += 1
    finally:                                        # индекс пишется и при Ctrl+C
        if not args.dry_run and out_dir.exists():
            write_index(out_dir, old, index)
    if missing:
        shown = ", ".join(Path(m).stem for m in missing[:3])
        more = f" и ещё {len(missing) - 3}" if len(missing) > 3 else ""
        print(f"  ⚠ {len(missing)} файл(ов) из разметки нет в папке "
              f"(ни MP4, ни прокси), пропуск: {shown}{more}")
    if not planned:
        print("  (нечего нарезать)")
    return st


def main():
    tp.setup_console()
    args = parse_args()
    print(describe_settings(args))
    if not args.dry_run and shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg не найден в PATH. Установи: winget install Gyan.FFmpeg  (и открой новое окно терминала)")
    if not args.dry_run and not args.reencode and shutil.which("ffprobe") is None:
        print("⚠ ffprobe не найден: в индекс пойдёт запланированное начало клипа, а не фактическое")
    csvs = tp.find_csvs(args.folders, skip_dir=args.out)
    if not csvs:
        sys.exit(f"Не найдено ни одного {tp.CSV_NAME}")

    total = {"made": 0, "skipped": 0, "failed": 0, "todo": 0, "pruned": 0, "nosrc": 0}
    try:
        for c in csvs:
            for k, v in process_folder(c, args).items():
                total[k] += v
    except KeyboardInterrupt:
        print("\nПрервано. Готовые клипы и индекс сохранены, недорезанный файл удалён.")
        sys.exit(130)
    ns = f", без исходника {total['nosrc']} (см. ⚠ выше)" if total["nosrc"] else ""
    if args.dry_run:
        pr = f", удалить старых {total['pruned']}" if args.prune else ""
        print(f"\nDry run: нарезать {total['todo']}, уже есть {total['skipped']}{ns}{pr}.")
    else:
        pr = f", удалено старых {total['pruned']}" if args.prune else ""
        print(f"\nГотово: нарезано {total['made']}, уже было {total['skipped']}, "
              f"ошибок {total['failed']}{ns}{pr}.")


if __name__ == "__main__":
    main()
