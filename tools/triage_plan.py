"""
triage_plan.py — общий план отрезков из _triage.csv для make_clips.py и make_timeline.py.

Здесь всё, что должно совпадать у нарезки и у экспорта в монтажку: чтение CSV и make_clips.conf,
общие ключи командной строки, отбор файлов по оценке, отрезки вокруг меток и их склейка.
Один план — одни и те же отрезки и в Clips/, и на таймлайне.
"""
import csv
import re
import sys
from pathlib import Path

CSV_NAME = "_triage.csv"
CONFIG_NAME = "make_clips.conf"
PROXY_EXTS = (".LRV", ".LRF")
# метки camera_triage.lua — для проверки опечаток в pad.<метка>
KNOWN_LABELS = {"highlight", "scenery", "people", "bad", "fish", "shot", "catch", "wildlife",
                "dive", "jump", "trick", "crash", "ride"}

# встроенные значения; конфиг и командная строка их переопределяют
DEFAULTS = {
    "pad_before": 5.0, "pad_after": 5.0, "clip_pad": 0.5, "merge_gap": 2.0,
    "exclude": "bad", "point_exclude": "", "min_rating": None, "out": "Clips",
    "reencode_crf": 18, "reencode_preset": "medium",
}
X264_PRESETS = {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium",
                "slow", "slower", "veryslow"}


# ---------------------------------------------------------------- настройки

def parse_pad(v):
    """«6/3» → (6, 3); «7» → (7, 7)."""
    parts = [x.strip() for x in v.split("/")]
    if len(parts) == 1:
        parts *= 2
    if len(parts) != 2:
        raise ValueError
    b, a = float(parts[0]), float(parts[1])
    if b < 0 or a < 0:
        raise ValueError
    return b, a


def load_config(path):
    """Простой формат «ключ = значение», комментарии # и ;. Возвращает (settings, label_pads)."""
    cfg, pads, errors = {}, {}, []
    for n, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.split("#", 1)[0].split(";", 1)[0].strip()
        if not line:
            continue
        if "=" not in line:
            errors.append(f"строка {n}: нет «=»: {raw.strip()}")
            continue
        key, val = (x.strip() for x in line.split("=", 1))
        key = key.lower().replace("-", "_")
        try:
            if key.startswith("pad."):
                label = key[4:]
                if label not in KNOWN_LABELS:
                    errors.append(f"строка {n}: неизвестная метка «{label}» "
                                  f"(есть: {', '.join(sorted(KNOWN_LABELS))})")
                    continue
                pads[label] = parse_pad(val)
            elif key in ("pad_before", "pad_after", "clip_pad", "merge_gap"):
                cfg[key] = float(val)
                if cfg[key] < 0:
                    raise ValueError
            elif key in ("min_rating", "reencode_crf"):
                cfg[key] = int(val) if val else None
            elif key == "reencode_preset":
                if val not in X264_PRESETS:
                    raise ValueError
                cfg[key] = val
            elif key in ("exclude", "point_exclude", "out"):
                cfg[key] = val
            else:
                errors.append(f"строка {n}: неизвестный параметр «{key}»")
        except ValueError:
            errors.append(f"строка {n}: неверное значение для {key}: «{val}»")
    if errors:
        sys.exit(f"Ошибки в {path}:\n  " + "\n  ".join(errors))
    if cfg.get("reencode_crf") is None:
        cfg.pop("reencode_crf", None)
    return cfg, pads


def add_plan_args(ap):
    """Ключи, общие для нарезки и экспорта: папки, конфиг, отступы, склейка, отбор."""
    ap.add_argument("folders", nargs="+", type=Path, help="папки футажа или родительские папки")
    ap.add_argument("--config", type=Path,
                    help=f"файл настроек (по умолчанию {CONFIG_NAME} рядом со скриптом, если есть)")
    ap.add_argument("--pad-before", type=float, help="секунд до точечной метки, для всех меток (5)")
    ap.add_argument("--pad-after", type=float, help="секунд после точечной метки, для всех меток (5)")
    ap.add_argument("--clip-pad", type=float, help="запас вокруг отрезков i/o (0.5)")
    ap.add_argument("--merge-gap", type=float, help="склеивать отрезки ближе N секунд (2)")
    ap.add_argument("--labels", help="только отрезки с этими метками, через запятую (например fish,shot)")
    ap.add_argument("--exclude", help="метки, из которых отрезки не делать (bad)")
    ap.add_argument("--point-exclude",
                    help="точечные метки, которые сами отрезков не дают (например dive)")
    ap.add_argument("--min-rating", type=int,
                    help="только файлы с оценкой ≥ N; при N>0 непросмотренные файлы пропускаются. "
                         "По умолчанию — всё, кроме файлов с оценкой 0")
    ap.add_argument("--no-marks", action="store_true",
                    help="только отрезки i/o; точечные метки лишь добавляют метки отрезкам")


def finish_args(ap, a):
    """Встроенные значения → конфиг → командная строка; множества меток для фильтров."""
    cfg_path = a.config or Path(__file__).resolve().with_name(CONFIG_NAME)
    if a.config and not a.config.exists():
        ap.error(f"файл настроек не найден: {a.config}")
    cfg, pads = load_config(cfg_path) if cfg_path.exists() else ({}, {})
    a.config_used = cfg_path if cfg_path.exists() else None

    cli = {k: getattr(a, k) for k in DEFAULTS if getattr(a, k, None) is not None}
    for k, v in DEFAULTS.items():
        setattr(a, k, cli.get(k, cfg.get(k, v)))
    # --pad-before/--pad-after в командной строке перекрывают и отступы по меткам
    a.label_pads = {lab: (a.pad_before if "pad_before" in cli else b,
                          a.pad_after if "pad_after" in cli else af)
                    for lab, (b, af) in pads.items()}
    if a.merge_gap < 0 or a.pad_before < 0 or a.pad_after < 0 or a.clip_pad < 0:
        ap.error("отступы и --merge-gap не могут быть отрицательными")
    a.only = label_set(a.labels)
    a.exclude_set = label_set(a.exclude)
    a.point_exclude_set = label_set(a.point_exclude)
    return a


def pads_for(args, label):
    return args.label_pads.get(label, (args.pad_before, args.pad_after))


def describe_settings(args):
    src = f"конфиг: {args.config_used}" if args.config_used else "конфиг не найден, встроенные значения"
    lines = [f"Настройки ({src})",
             f"  отступы: −{args.pad_before:g} / +{args.pad_after:g} с"]
    special = {lab: p for lab, p in args.label_pads.items() if p != (args.pad_before, args.pad_after)}
    if special:
        lines[-1] += ";  по меткам: " + ", ".join(
            f"{lab} −{b:g}/+{af:g}" for lab, (b, af) in sorted(special.items()))
    lines.append(f"  клип i/o ±{args.clip_pad:g} с, склейка ≤ {args.merge_gap:g} с, "
                 f"исключить: {args.exclude or '—'}"
                 + (f", точки без клипа: {args.point_exclude}" if args.point_exclude else "")
                 + (f", мин. оценка: {args.min_rating}" if args.min_rating is not None else ""))
    return "\n".join(lines)


def label_set(s):
    return {x.strip() for x in (s or "").split(",") if x.strip()}


# ---------------------------------------------------------------- разметка

def hms_dash(t):
    t = int(t)
    return f"{t // 3600:02d}-{t % 3600 // 60:02d}-{t % 60:02d}"


def hms(t):
    t = int(t)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def to_int(s):
    s = (s or "").strip()
    return int(s) if s.lstrip("-").isdigit() else None


def read_rows(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


def triage_csv_ok(rows):
    """False, если _triage.csv пересохранён в другой программе (нет нужных колонок)."""
    need = {"event", "file", "time_ms", "end_ms", "label", "rating"}
    return not rows or need <= set(rows[0].keys())


def find_csvs(folders, skip_dir=None):
    """Все _triage.csv в папках (рекурсивно), кроме лежащих внутри skip_dir (Clips)."""
    csvs = []
    for root in folders:
        if not root.exists():
            print(f"⚠ Папка не найдена: {root}")
            continue
        csvs += [p for p in sorted(root.rglob(CSV_NAME))
                 if not skip_dir or skip_dir not in p.relative_to(root).parts[:-1]]
    return csvs


_GOPRO = [re.compile(r"^G[HXL](\d\d)(\d{4})\.", re.I), re.compile(r"^GP(\d\d)(\d{4})\.", re.I)]
_GOPR = re.compile(r"^GOPR(\d{4})\.", re.I)


def file_order_key(fname):
    """Порядок файлов как в camera_triage.lua: главы одной записи GoPro (GX010123, GX020123, …)
    подряд, остальные — по имени."""
    up = fname.upper()
    for rx in _GOPRO:
        m = rx.match(up)
        if m:
            return f"{m.group(2)}_{m.group(1)}_{up}"
    m = _GOPR.match(up)
    if m:
        return f"{m.group(1)}_00_{up}"
    return up


def collect(rows):
    """file -> {'clips': [(s, e, label)], 'marks': [(t, label)], 'notes': [(t, text)],
                'file_notes': [text], 'rating': int|None, 'duration': float|None, 'activity': str}"""
    files = {}
    for r in rows:
        fname = (r.get("file") or "").strip()
        if not fname:
            continue
        d = files.setdefault(fname, {"clips": [], "marks": [], "notes": [], "file_notes": [],
                                     "rating": None, "duration": None, "activity": ""})
        d["activity"] = (r.get("activity") or "").strip() or d["activity"]
        ev = r.get("event")
        if ev == "file_done":                       # последняя запись побеждает
            d["rating"] = to_int(r.get("rating"))
            dur = to_int(r.get("duration_ms"))
            d["duration"] = dur / 1000 if dur else None
            continue
        t = to_int(r.get("time_ms"))
        if t is None:
            text = (r.get("note") or "").strip()
            if ev == "note" and text:
                d["file_notes"].append(text)
            continue
        t /= 1000
        if ev == "clip":
            end = to_int(r.get("end_ms"))
            if end is not None and end / 1000 > t:
                d["clips"].append((t, end / 1000, (r.get("label") or "").strip()))
        elif ev == "mark":
            label = (r.get("label") or "").strip()
            if label:
                d["marks"].append((t, label))
        elif ev == "note":
            text = (r.get("note") or "").strip()
            if text:
                d["notes"].append((t, text))
    return files


def sorted_files(files):
    return sorted(files.items(), key=lambda kv: file_order_key(kv[0]))


def zero_rated(d, args):
    """Файл с оценкой 0 («мусор») не берётся, кроме --min-rating 0."""
    return d["rating"] == 0 and args.min_rating != 0


def below_min_rating(d, args):
    return bool(args.min_rating) and (d["rating"] is None or d["rating"] < args.min_rating)


def build_segments(d, args, no_marks=None):
    """Склеенные отрезки [(start, end, labels:set)] одного файла, без фильтра --labels.
    no_marks=None — как в командной строке (--no-marks)."""
    if no_marks is None:
        no_marks = args.no_marks
    exclude, point_exclude = args.exclude_set, args.point_exclude_set
    segs = []
    for s, e, clabel in d["clips"]:
        labs = {lab for t, lab in d["marks"] if s <= t <= e}
        if clabel:
            labs.add(clabel)
        kept = labs - exclude
        if labs and not kept:
            continue                                # i … b … o — клип целиком «плохой»
        segs.append((max(0.0, s - args.clip_pad), e + args.clip_pad, kept or {"clip"}))
    if not no_marks:
        for t, lab in d["marks"]:
            if lab in exclude or lab in point_exclude:
                continue
            if any(s <= t <= e for s, e, _ in d["clips"]):
                continue                            # внутри i/o — уже учтена в метках клипа
            before, after = pads_for(args, lab)
            segs.append((max(0.0, t - before), t + after, {lab}))
    merged = merge(segs, args.merge_gap)
    if d["duration"]:
        merged = [(s, min(e, d["duration"]), labs) for s, e, labs in merged]
    return [x for x in merged if x[1] - x[0] >= 0.5]


def selected_segments(d, args):
    """Отрезки с учётом --no-marks и --labels (то, что режем / кладём на таймлайн сейчас)."""
    segs = build_segments(d, args)
    if args.only:
        segs = [x for x in segs if x[2] & args.only]
    return segs


def merge(segs, gap):
    out = []
    for s, e, labs in sorted(segs, key=lambda x: (x[0], x[1])):
        if out and s <= out[-1][1] + gap:
            ps, pe, plabs = out[-1]
            out[-1] = (ps, max(pe, e), plabs | labs)
        else:
            out.append((s, e, set(labs)))
    return out


# ---------------------------------------------------------------- исходники

def find_source(folder, fname):
    """(путь, это_прокси) или (None, False). Оригинал MP4, если есть, иначе прокси LRV/LRF."""
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


def setup_console():
    """Вывод в файл (> log.txt) на Windows шёл бы в cp1252 — переключить на UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
