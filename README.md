# mpv camera triage

Manual triage of action-camera footage (GoPro and others) in the **mpv** player: quickly go through lots of videos, mark the interesting moments, and turn that into an editing timeline or clips.

While watching, you place marks with single keys (`fish`, `shot`, `scenery`, …), clip ranges (`i` / `o`), notes and a 0–3 rating per file. Everything is written immediately to `_triage.csv` **in the video folder**, so the markup moves to the archive together with the footage and serves as a detailed description of what is in it.

The label set is designed for spearfishing and kitesurfing, but it works for other action video too.

## Why

- **Picking moments for editing.** Marked moments are exported as a DaVinci Resolve timeline that links to the originals (`tools/make_timeline.py`) — in the editor every segment can be extended as far as needed. They can also be cut into files for sharing and viewing (`tools/make_clips.py`).
- **Describing the archive.** A CSV per folder: what was shot, what worked, where to look.
- **Reference markup.** Marks, clips and ratings are structured data. They can serve as ground truth for automatic tools: check which moments an algorithm found and which it missed, or train it. Notes are for people.

## Layout

```
mpv/                              ← copy of the mpv settings folder (%APPDATA%\mpv)
  mpv.conf                        basic player settings
  scripts/camera_triage.lua       the markup script
  script-opts/camera_triage.conf  script settings
tools/
  make_timeline.py                FCPXML timeline for editing (DaVinci Resolve) from _triage.csv
  make_clips.py                   cuts clips from _triage.csv with ffmpeg
  make_report.py                  archive summary report from every _triage.csv
  resolve_markers.py              markers on clips in DaVinci Resolve (Workspace → Scripts)
  resolve_markers.lua             its part that runs inside Resolve
  triage_plan.py                  shared segment plan for both scripts
  make_clips.conf                 settings for both: pads (global and per label) etc.
docs/
  triage-format.md                CSV format, labels, behavior
tests/                            automated tests (py -m unittest discover tests)
install.ps1                       installs into %APPDATA%\mpv (Windows)
```

## Installation

**Windows**

1. mpv 0.38+ (shinchiro build): `winget install --id shinchiro.mpv -e`
2. In PowerShell from the repository folder:
   ```powershell
   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass   # allow scripts in this window only
   .\install.ps1          # copy the files to %APPDATA%\mpv
   .\install.ps1 -Link    # or: symbolic links to the repository
   ```
   - **Copy** — run `install.ps1` again after every `git pull`.
   - **`-Link`** — `git pull` updates mpv right away. Requires Windows "Developer Mode" (Settings → System → For developers) or PowerShell as administrator; without it the script changes nothing and tells you what to do. The repository folder must not be moved afterwards.

   Files in `%APPDATA%\mpv` that differ are saved to `_backup_<date>` first, and so is an old `gopro_triage.*` version.
3. For the timeline and clip cutting: Python 3.9+ and ffmpeg with ffprobe (`winget install Gyan.FFmpeg`).

**Linux / macOS** — not tested, but the Lua script is platform-independent: copy the contents of `mpv/` to `~/.config/mpv/`.

## Usage

Drag a folder of videos into the mpv window. The first unreviewed file opens; the status is shown in the top right corner. Subfolders (e.g. one per day) come after the folder's own files, each with its own `_triage.csv`.

| Keys | Action |
|---|---|
| `h` `v` `p` `b` | highlight / scenery / people / bad |
| `f` `s` `c` `w` `d` | fish / shot / catch / wildlife / dive |
| `j` `t` `x` `r` | jump / trick / crash / ride |
| `i` … `o` | clip start … end (a mark in between becomes the clip's label) |
| `n` / `N` | note at the moment / for the file |
| `0`–`3` | file rating |
| `Enter` | file reviewed → next unreviewed one |
| `u` | undo the latest action (a CSV row after a second `u`); inside an open clip — its marks first, then its start |
| `Del` | delete a nearby mark / clip / note (±3 s), also from earlier sessions — after a second `Del`; `u` brings it back |
| `+` `-` `Tab` `Backspace` | speed ×2 / ÷2 / 1x↔fast / 1x |
| `PgUp` `PgDn` | previous / next mark |
| `a` | activity (spearfishing / kitesurfing / other); remembered in the folder's CSV |
| `L` | original ↔ low-resolution proxy (GoPro .LRV / DJI .LRF) |
| `F1` | help |

Details are in [docs/triage-format.md](docs/triage-format.md).

## Editing timeline

```
py tools\make_timeline.py "D:\Footage\2026-09_Turkey" --min-rating 2
py tools\make_timeline.py "D:\Footage\2026-09_Turkey" --full
```

Next to `_triage.csv` you get `_selects.fcpxml` (the selected segments back to back) and `_selects.edl` (markers for labels and notes, colored by label). In Resolve:

1. **File → Import → Timeline…** → `_selects.fcpxml`.
2. Right-click this timeline in the Media Pool → **Timelines → Import → Timeline Markers from EDL…** → `_selects.edl`. Resolve ignores markers in FCPXML, so they come as a separate file.

The segments link to the original videos, so their edges can be dragged out to the start and end of the file: the pads only set the initial width. `--full` additionally writes `_full.fcpxml` and `_full.edl` — every marked file in full with all its markup as markers. The segment plan is the same as for cutting: the same options and `make_clips.conf`.

### Markers on clips

Timeline markers from the `.edl` stay where they are on the timeline: once you move, trim or delete clips, they no longer point at the right moments. Markers on the clips themselves move with the clips, and the markup times are already counted from the start of each file, exactly like clip markers.

```
py tools\resolve_markers.py "D:\Footage\2026-09_Turkey"
```

reads every `_triage.csv` under the folder and writes three entries to Resolve's **Workspace → Scripts** menu with the markers built in (Resolve runs menu scripts in a sandbox without file access, so the data has to be inside them). Restart Resolve once when the entries are new, then:

| Entry | What it does |
|---|---|
| `camera_triage - timeline markers` | puts the markers on every clip of the current timeline |
| `camera_triage - media pool markers` | puts them on every clip in the Media Pool: clips you add to a timeline later come with their markers |
| `camera_triage - remove markers` | removes the markers these entries added (yours stay) |

The markers are the same as in the `.edl`, with the same colors: labels, `i`/`o` clips with their duration, notes. After changing the markup in mpv, run the command again and click the entry again: its markers are replaced. The report is in **Workspace → Console**.

## Cutting clips

Clip files are for sharing, watching on a phone, or keeping the best moments apart from the raw footage. For editing the timeline (above) is more convenient.

```
py tools\make_clips.py "D:\Footage\2026-09_Turkey" --dry-run
py tools\make_clips.py "D:\Footage\2026-09_Turkey" --min-rating 2
```

The clips appear in a `Clips\` subfolder next to the videos: `GX011151_00-01-20_14s_fish+shot.mp4`.
To cut straight into a project, give a full path: `--out "D:\02_Project\Clips"`. The videos and `_triage.csv` stay in the footage folder, no need to copy them to the project.
Pads around marks and other permanent options are in `tools\make_clips.conf`.

## Archive report

```
py tools\make_report.py "D:\Footage" --probe
```

The script walks all subfolders, reads every `_triage.csv` and checks which videos are on disk, including folders without markup. A single folder works too. Three tables appear in the first given folder (or in `--out`), from general to detailed:

| File | Row | Purpose |
|---|---|---|
| `_report_folders.csv` | folder + a TOTAL row | what was shot and when, how many videos are reviewed, ratings 0–3, duration, marks by label; which folders are not fully marked up |
| `_report_files.csv` | video | status (reviewed / rated, no Enter / started / not opened), rating, duration, shooting time, marks by label, notes for the file, whether the file is on disk |
| `_report_events.csv` | a `_triage.csv` row | every mark, clip and note in the archive in the `_triage.csv` columns + folder, file rating and status — for filters like "every `catch` in files rated 3" |

If the given folder is too broad and contains copies (videos or `_triage.csv` copied into a project, an export, a backup), the report recognizes them. Copies are flagged in the `copy_of` column, a folder made only of copies gets the status "copy: …", and TOTAL counts each video once. To skip such folders entirely use `--skip Clips,02_Project,03_Export`.

`--probe` reads the duration and shooting time of videos that were never opened via ffprobe. `--xlsx` builds the same tables as one workbook `_report.xlsx`: sheets, header filters, numbers as numbers. It requires `py -m pip install openpyxl`. The report is a snapshot; it can be re-saved and rebuilt as often as you like.

## Important

Only the script writes `_triage.csv`. You can view it in OnlyOffice / Excel but **must not save it**: a spreadsheet changes the time format and the delimiter. For tables and filters use `tools\make_report.py` (above).

## Tests

After changing the scripts — from the repository folder:

```
py -m unittest discover tests
```

About 10 seconds, nothing to install (standard `unittest`). What is checked:

| File | What |
|---|---|
| `test_plan.py` | the shared segment plan: pads, merging, exclusions, filters, GoPro chapter order, config, CSV format check |
| `test_make_clips.py` | clip names, `--out`, `--prune` with filters deletes nothing extra |
| `test_make_timeline.py` | timecodes (including drop frame), FCPXML: segments back to back and on frame boundaries, markers inside; the EDL matches the FCPXML |
| `test_make_report.py` | video and folder statuses, copies in a project and an export, TOTAL without duplicates, `--probe` keeps known values |
| `test_labels.py` | the label list in `camera_triage.lua` (keys, F1 help) matches `triage_plan.py` (report, Resolve colors) |
| `test_resolve_markers.py` | markers on clips: what is baked into the menu scripts; the scripts themselves in Resolve's own Lua (`fuscript`, if Resolve is installed) under the same sandbox as Resolve's menu, against a fake Resolve — frames, colors, durations, reruns, proxies, remove |
| `test_integration.py` | all three tools on real tiny videos (requires ffmpeg) |
| `test_mpv_lua.py` | `camera_triage.lua` in a windowless mpv: chapter order, `Del`, `u` (also inside an open clip), a mark, rating + Enter, CSV sorting, subfolder order (requires mpv and ffmpeg; mpv is looked up in PATH, in `C:\Program Files\MPV Player` or via the `MPV` variable) |

Without ffmpeg or mpv the corresponding tests are skipped (`skipped`), the rest still run.

## License

MIT — see [LICENSE](LICENSE).
