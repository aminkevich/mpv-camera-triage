"""
triage_plan.py — shared segment plan from _triage.csv for make_clips.py and make_timeline.py.

Everything that must be the same for cutting and for the editing export lives here: reading the
CSV and make_clips.conf, shared command-line options, selecting files by rating, segments around
marks and merging them. One plan — the same segments in Clips/ and on the timeline.
"""
import csv
import re
import sys
from pathlib import Path

CSV_NAME = "_triage.csv"
CONFIG_NAME = "make_clips.conf"
PROXY_EXTS = (".LRV", ".LRF")
# camera_triage.lua labels in keyboard order → Resolve marker color (make_timeline).
# The single list for Python: report columns, colors, the pad.<label> config check.
# Add a new label here and to LABELS / HELP in camera_triage.lua; tests/test_labels.py checks they match.
LABELS = {
    "highlight": "Yellow", "scenery": "Mint", "people": "Pink", "bad": "Cocoa",
    "fish": "Blue", "shot": "Red", "catch": "Green", "wildlife": "Cyan", "dive": "Sky",
    "jump": "Purple", "trick": "Fuchsia", "crash": "Rose", "ride": "Lavender",
}
KNOWN_LABELS = set(LABELS)

# built-in values; the config and the command line override them
DEFAULTS = {
    "pad_before": 5.0, "pad_after": 5.0, "clip_pad": 0.5, "merge_gap": 2.0,
    "exclude": "bad", "point_exclude": "", "min_rating": None, "out": "Clips",
    "reencode_crf": 18, "reencode_preset": "medium",
}
X264_PRESETS = {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium",
                "slow", "slower", "veryslow"}


# ---------------------------------------------------------------- settings

def parse_pad(v):
    """"6/3" → (6, 3); "7" → (7, 7)."""
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
    """Simple "key = value" format, comments # and ;. Returns (settings, label_pads)."""
    cfg, pads, errors = {}, {}, []
    for n, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.split("#", 1)[0].split(";", 1)[0].strip()
        if not line:
            continue
        if "=" not in line:
            errors.append(f"line {n}: no '=': {raw.strip()}")
            continue
        key, val = (x.strip() for x in line.split("=", 1))
        key = key.lower().replace("-", "_")
        try:
            if key.startswith("pad."):
                label = key[4:]
                if label not in KNOWN_LABELS:
                    errors.append(f"line {n}: unknown label '{label}' "
                                  f"(known: {', '.join(sorted(KNOWN_LABELS))})")
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
                errors.append(f"line {n}: unknown setting '{key}'")
        except ValueError:
            errors.append(f"line {n}: invalid value for {key}: '{val}'")
    if errors:
        sys.exit(f"Errors in {path}:\n  " + "\n  ".join(errors))
    if cfg.get("reencode_crf") is None:
        cfg.pop("reencode_crf", None)
    return cfg, pads


def add_plan_args(ap):
    """Options shared by cutting and export: folders, config, pads, merging, selection."""
    ap.add_argument("folders", nargs="+", type=Path, help="footage folders or their parent folders")
    ap.add_argument("--config", type=Path,
                    help=f"settings file (default: {CONFIG_NAME} next to the script, if present)")
    ap.add_argument("--pad-before", type=float, help="seconds before a point mark, for all labels (5)")
    ap.add_argument("--pad-after", type=float, help="seconds after a point mark, for all labels (5)")
    ap.add_argument("--clip-pad", type=float, help="margin around i/o ranges (0.5)")
    ap.add_argument("--merge-gap", type=float, help="merge segments closer than N seconds (2)")
    ap.add_argument("--labels", help="only segments with these labels, comma-separated (e.g. fish,shot)")
    ap.add_argument("--exclude", help="labels that never produce segments (bad)")
    ap.add_argument("--point-exclude",
                    help="point labels that do not produce segments on their own (e.g. dive)")
    ap.add_argument("--min-rating", type=int,
                    help="only files rated ≥ N; with N>0 unreviewed files are skipped. "
                         "Default: everything except files rated 0")
    ap.add_argument("--no-marks", action="store_true",
                    help="only i/o ranges; point marks just add labels to them")


def finish_args(ap, a):
    """Built-in values → config → command line; label sets for the filters."""
    cfg_path = a.config or Path(__file__).resolve().with_name(CONFIG_NAME)
    if a.config and not a.config.exists():
        ap.error(f"settings file not found: {a.config}")
    cfg, pads = load_config(cfg_path) if cfg_path.exists() else ({}, {})
    a.config_used = cfg_path if cfg_path.exists() else None

    cli = {k: getattr(a, k) for k in DEFAULTS if getattr(a, k, None) is not None}
    for k, v in DEFAULTS.items():
        setattr(a, k, cli.get(k, cfg.get(k, v)))
    # --pad-before/--pad-after on the command line also override per-label pads
    a.label_pads = {lab: (a.pad_before if "pad_before" in cli else b,
                          a.pad_after if "pad_after" in cli else af)
                    for lab, (b, af) in pads.items()}
    if a.merge_gap < 0 or a.pad_before < 0 or a.pad_after < 0 or a.clip_pad < 0:
        ap.error("pads and --merge-gap cannot be negative")
    a.only = label_set(a.labels)
    a.exclude_set = label_set(a.exclude)
    a.point_exclude_set = label_set(a.point_exclude)
    return a


def pads_for(args, label):
    return args.label_pads.get(label, (args.pad_before, args.pad_after))


def describe_settings(args):
    src = f"config: {args.config_used}" if args.config_used else "no config found, built-in values"
    lines = [f"Settings ({src})",
             f"  pads: −{args.pad_before:g} / +{args.pad_after:g} s"]
    special = {lab: p for lab, p in args.label_pads.items() if p != (args.pad_before, args.pad_after)}
    if special:
        lines[-1] += ";  per label: " + ", ".join(
            f"{lab} −{b:g}/+{af:g}" for lab, (b, af) in sorted(special.items()))
    lines.append(f"  i/o clip ±{args.clip_pad:g} s, merge ≤ {args.merge_gap:g} s, "
                 f"exclude: {args.exclude or '—'}"
                 + (f", points that make no segment: {args.point_exclude}" if args.point_exclude else "")
                 + (f", min rating: {args.min_rating}" if args.min_rating is not None else ""))
    return "\n".join(lines)


def label_set(s):
    return {x.strip() for x in (s or "").split(",") if x.strip()}


# ---------------------------------------------------------------- markup

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


def triage_csv_ok(rows, path=None):
    """False if _triage.csv was re-saved in another program (the required columns are missing).
    Without data rows the file header (path) is checked."""
    need = {"event", "file", "time_ms", "end_ms", "label", "rating"}
    if rows:
        return need <= set(rows[0].keys())
    if path is None:
        return True
    with open(path, encoding="utf-8-sig", newline="") as f:
        header = next(csv.reader(f, delimiter=";"), None)
    return header is None or need <= set(header)


def find_csvs(folders, skip_dir=None):
    """All _triage.csv in the folders (recursively), except those inside skip_dir (Clips)."""
    csvs = []
    for root in folders:
        if not root.exists():
            print(f"⚠ Folder not found: {root}")
            continue
        csvs += [p for p in sorted(root.rglob(CSV_NAME))
                 if not skip_dir or skip_dir not in p.relative_to(root).parts[:-1]]
    return csvs


_GOPRO = [re.compile(r"^G[HXL](\d\d)(\d{4})\.", re.I), re.compile(r"^GP(\d\d)(\d{4})\.", re.I)]
_GOPR = re.compile(r"^GOPR(\d{4})\.", re.I)


def file_order_key(fname):
    """File order as in camera_triage.lua: chapters of one GoPro recording (GX010123, GX020123, …)
    follow each other, other files by name."""
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
        if ev == "file_done":                       # the last row wins
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
    """A file rated 0 ("junk") is not taken, except with --min-rating 0."""
    return d["rating"] == 0 and args.min_rating != 0


def below_min_rating(d, args):
    return bool(args.min_rating) and (d["rating"] is None or d["rating"] < args.min_rating)


def build_segments(d, args, no_marks=None):
    """Merged segments [(start, end, labels:set)] of one file, without the --labels filter.
    no_marks=None — as on the command line (--no-marks)."""
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
            continue                                # i … b … o — the whole clip is "bad"
        segs.append((max(0.0, s - args.clip_pad), e + args.clip_pad, kept or {"clip"}))
    if not no_marks:
        for t, lab in d["marks"]:
            if lab in exclude or lab in point_exclude:
                continue
            if any(s <= t <= e for s, e, _ in d["clips"]):
                continue                            # inside i/o — already counted in the clip's labels
            before, after = pads_for(args, lab)
            segs.append((max(0.0, t - before), t + after, {lab}))
    merged = merge(segs, args.merge_gap)
    if d["duration"]:
        merged = [(s, min(e, d["duration"]), labs) for s, e, labs in merged]
    return [x for x in merged if x[1] - x[0] >= 0.5]


def selected_segments(d, args):
    """Segments with --no-marks and --labels applied (what is cut / put on the timeline now)."""
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


# ---------------------------------------------------------------- sources

def find_source(folder, fname):
    """(path, is_proxy) or (None, False). The original MP4 if present, otherwise an LRV/LRF proxy."""
    p = folder / fname
    if p.exists():
        return p, False
    stem, up = Path(fname).stem, fname.upper()
    if up[:2] in ("GX", "GH") and up.endswith(".MP4"):
        cands = [folder / ("GL" + stem[2:] + ".LRV")]           # GoPro
    else:
        cands = [folder / (stem + ".LRF"), folder / (stem + ".LRV")]   # DJI and others
    for proxy in cands:
        if proxy.exists():
            return proxy, True
    return None, False


def setup_console():
    """Output redirected to a file (> log.txt) on Windows would be cp1252 — switch to UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
