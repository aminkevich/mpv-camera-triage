# `_triage.csv` markup format (camera_triage v3)

## Principles

- The markup is manual: a person watches the video and marks moments. Marks, clips and ratings are structured data and can serve as ground truth for automatic tools, for example to train and evaluate automatic triage. Notes are for people and are not used by automation.
- The file lives **in the video folder** and moves to the archive with it. The CSV contains no paths.
- `file` is always the name of the original video (MP4), even if an LRV/LRF proxy was watched; see the `src` column.
- `file_done` means the file was watched to the end: anything not marked in it counts as "nothing interesting". This is needed to measure the recall of SpearCut's automatic markup.
- No `file_done` row — the file is not fully marked up.
- The CSV is the single source of truth, and only the script writes it. Manual edits belong in a text editor (Notepad, VS Code), not in a spreadsheet.
- While you work, rows are appended to the end (safe on a crash); the file is rewritten only on undo (`u`) and delete (`Del`). On exit from mpv the file is sorted: by video → events by time → notes for the file → `file_partial` / `file_done`.

## Columns (`;` delimiter, UTF-8 with BOM)

`logged_at;event;activity;file;src;time_ms;end_ms;time_hms;label;rating;note;speed_pct;duration_ms;duration_hms;camera_time`

| event | meaning | filled in |
|---|---|---|
| `mark` | point mark | time_ms, time_hms, label, speed_pct |
| `clip` | range `i` … `o` | time_ms, end_ms, time_hms, label (the last mark between i and o), speed_pct |
| `note` | note; with time_ms — at a moment, without — for the whole file | note (+ time_ms, speed_pct for a moment) |
| `file_done` | file reviewed (Enter) | rating, duration_ms, duration_hms, camera_time |
| `file_partial` | rated, but left the file without Enter | rating, duration, camera_time |

- If a file has several `file_done` rows, the last one counts (re-rating).
- `time_hms`, `duration_hms` are `hh:mm:ss` for people; the exact values are in `*_ms`.
- `speed_pct` is the playback speed at the moment of the mark, an accuracy indicator (a mark at 400 % is coarser).
- `camera_time` is the recording time from the metadata. GoPro writes the local time of the camera clock; other cameras may write UTC.

## Labels

The list is fixed. A new label is added by changing the script, not by editing the CSV; ideas are written down as notes first.

A new label is added in two places: `LABELS` and the `HELP` text in `camera_triage.lua` (the key), and `LABELS` in `tools/triage_plan.py` (the marker color in Resolve; the report columns and the `pad.<label>` check come from there too). The test `tests/test_labels.py` checks that the lists match. Labels missing from the list (old or renamed) are not lost by the report: they get columns at the end of the tables.

| Label | Key | When to use |
|---|---|---|
| `highlight` | h | interesting, but no category fits |
| `scenery` | v | beautiful: landscape under or above water, corals, seaweed, terrain |
| `people` | p | people, interaction, funny moments |
| `bad` | b | definitely discard: murky, camera in a pocket, too dark (a negative example, produces no clips) |
| `fish` | f | a fish in the frame (any, including morays) |
| `shot` | s | a shot |
| `catch` | c | the catch |
| `wildlife` | w | animals other than fish: turtle, octopus, ray, cuttlefish, dolphin |
| `dive` | d | **as a point** — an interesting moment during a dive; **inside a clip** `i` (descent) … `d` … `o` (ascent) — the whole dive. Told apart by `event`: `mark` or `clip` |
| `jump` `trick` `crash` `ride` | j t x r | kitesurfing |

The activity (spearfishing / kitesurfing / other) is written to every row and changed with the `a` key. When a folder is opened, it is taken from the latest row of its `_triage.csv`, so there is no need to pick the activity again after restarting mpv.

**File rating**

| | |
|---|---|
| 0 | never watch again / junk |
| 1 | there is something |
| 2 | an ordinary decent file (most of them) |
| 3 | rare, really strong |

## Behavior in mpv

- Status in the top right corner: file, reviewed or not, rating, number of marks and notes, "reviewed N of M"; speed ≠ 1x in large type; an open clip.
- Marks and notes are ticks on the seek bar; `PgUp`/`PgDn` jump between them; during playback they pop up.
- At the end of every file playback pauses with a reminder about the rating.
- `Enter` moves to the next **unreviewed** file. On an already reviewed file with an unchanged rating no new row is written.
- The position in a file is remembered when switching to another file and on exit.
- `u` undoes the latest action: an open clip start is cancelled right away; a CSV row after a second `u` within 4 s. Marks placed inside an open clip are undone first, newest first; the clip stays open and takes its label from the mark that is left, and the next `u` cancels its start. The text of an undone note is prefilled into the next `n` / `N` — this is how a note is moved to another place.
- `Del`: deletes the mark, clip (by its start or end) or note at a moment nearest to the current position within ±3 s — including ones from earlier sessions. The first press shows what will be deleted, a second press within 4 s removes the row from the CSV. `u` brings it back. Handy: `PgUp`/`PgDn` to the mark → `Del` → `Del`. Notes for the file (`N`) cannot be deleted this way — only in a text editor.
- Leaving a file without Enter: the rating is saved as `file_partial`, an unclosed clip is discarded.
- Proxy: if a light copy lies next to the MP4 (GoPro `.LRV`, DJI `.LRF`), it can be watched instead of the original — `prefer_proxy=yes` in the settings or the `L` key on the fly. The CSV still gets the MP4 name, and `src` gets `lrv`/`lrf`.
- Folder: all videos are in the list, only junk is dropped (photos, THM, duplicate LRVs). The first unreviewed file is opened.
- Subfolders: with `subfolders=recursive` (the default) their videos go into the list right away, after the folder's own files, folder by folder in name order (`Day2\…`, then `Mid\…`). So "reviewed N of M", the first unreviewed file and `Enter` see the whole tree. Each subfolder gets its own `_triage.csv`, and the activity is taken from it when you get there. `lazy` is mpv's default: a subfolder is opened only when playback reaches it; `ignore` skips subfolders.
- GoPro chapters: a long recording is split into files GX010123, GX020123, … (chapter, then recording number). mpv sorts by name, so the continuation of a recording ends up at the end of the list; the script puts the chapters of one recording in sequence (`gopro_order=yes`, the default). Only GoPro files of the same folder are swapped among themselves; other files and subfolders are not moved. The CSV gets the same order after sorting on exit.

## Archive report (`tools/make_report.py`)

- Walk: all subfolders except `Clips` (`--skip`). A folder goes into the report if it has a `_triage.csv` or videos (MP4/MOV; LRV/LRF proxies do not count as separate videos).
- Video status: `reviewed` — has `file_done`; `rated, no Enter` — only `file_partial`; `started` — has marks or notes but no rating; `not opened` — the video is on disk but not in the markup.
- The rating is the last `file_done`, as for cutting. `rating_draft` is a rating from a `file_partial` made after it (re-rating without Enter).
- Folder status: `complete` (all videos reviewed), `partial (N of M)`, `not marked up` (no `_triage.csv`), `CSV in another format` (re-saved in a spreadsheet — cannot be read).
- Label columns (`highlight` … `ride`) count point marks and `i`/`o` clips with that label. `duration_*` is the sum of known durations, `reviewed_*` of reviewed videos only; `unknown_duration` is how many videos have an unknown duration (not opened and no `--probe`, or no file on disk).
- `_report_events.csv` is all `_triage.csv` files combined without changes: a `folder` column on the left, `file_rating`, `file_status` and `copy_of` on the right.
- Copies: the same video in several folders (a copy in a project, an export, a backup, a copy of one `_triage.csv`) is recognized by its file name plus one of these signs: size (video on disk), time of the first markup row for the file (a copy of `_triage.csv`, including a file that was only started), shooting time and duration from the markup (if there is no time — the exact duration). The name alone is not enough: after formatting the card a GoPro writes `GX010001` again.
  - The primary copy is where the video is on disk and the folder has the most videos (the original footage folder); on a tie — the first by path.
  - The others get `copy_of` = the primary's folder, marked "(markup differs)" if the copy was marked further or re-rated.
  - The primary's `copies` is how many other copies there are. A folder's `copies` is how many copies it holds; if all its videos are copies, the status is "copy: <folder>".
  - Folder rows show them as they are. TOTAL counts unique videos: each copy once, by the markup of the primary copy.
- The report does not change `_triage.csv`. Report files are overwritten on every run; if one of them is open in Excel, it is skipped with a warning.

## Editing timeline (`tools/make_timeline.py`)

- The segment plan is the same as for cutting below (the shared module `tools/triage_plan.py`): pads, merging, `exclude`, `point_exclude`, `--labels`, `--min-rating`, `--no-marks`, `make_clips.conf`. The difference: instead of clip files an FCPXML 1.8 timeline is written that links to the original videos.
- `_selects.fcpxml` next to `_triage.csv`: timeline "Selects <folder>", segments back to back in recording order (GoPro chapters in sequence). A segment is named `GX010123 00:01:20 fish+shot ★2`.
- Markers: point marks (marker name = label, including `bad`), `i`/`o` clips (a marker with a duration, "clip dive"), notes at moments ("✎ text").
- Resolve (tested on 21.1.2) ignores markers in FCPXML but reads them, with colors, from an `.edl`. So an `.edl` with the same name is written next to it — the timeline markers in Resolve's format. Import them as a second step: right-click the timeline in the Media Pool → Timelines → Import → Timeline Markers from EDL. Its timecodes assume the timeline starts at 01:00:00:00, as in the `.fcpxml`.
- Marker colors: fish — blue, shot — red, catch — green, wildlife — Cyan, dive — Sky, scenery — Mint, people — pink, highlight — yellow, bad — Cocoa, jump — purple, trick — Fuchsia, crash — Rose, ride — Lavender; notes at moments — Cream, for the file — Sand.
- `--full` also writes `_full.fcpxml` and `_full.edl`: every marked file in full, all markup as markers, notes for the file as a marker at the start. The `--labels` filter does not apply to it, the rating does.
- Times are frame-accurate: the frame rate, duration and timecode of each source come from ffprobe (GoPro writes a time-of-day timecode, and Resolve finds frames by it). The timeline frame rate is the most common one among the sources, the start is 01:00:00:00.
- No MP4 — the LRV/LRF proxy goes on the timeline (with a warning). Video paths in the file are absolute; after moving the folder use Relink Media in Resolve.
- Timeline files are overwritten on every run; edits made in Resolve live in the Resolve project, not in the `.fcpxml`.

## Cutting (`tools/make_clips.py`)

- A point mark → a segment −5 / +5 s (by default); an `i`/`o` clip → its bounds ± 0.5 s; the clip's labels are all point marks inside it.
- Settings: `tools/make_clips.conf` (or `--config PATH`): global pads (`pad_before`, `pad_after`) and per-label pads (`pad.shot = 6/3`, `pad.scenery = 4`; labels without their own line use the global ones), `clip_pad`, `merge_gap`, `exclude`, `point_exclude`, `min_rating`, `out`, `reencode_crf`, `reencode_preset`. Priority: built-in values → config → command line; `--pad-before` / `--pad-after` also override per-label pads. The resulting settings are printed on startup.
- `out` / `--out`: a subfolder name next to the videos (`Clips`) or a full path (`D:\02_Project\Clips`). With a full path the videos and `_triage.csv` stay in the footage folder and need not be copied to the project. With several marked footage folders the clips go to subfolders by their paths from the given folder (`D:\02_Project\Clips\Turkey\…`), otherwise identical file names from different cards would mix. The `_clips.csv` index lies next to the clips.
- `bad` produces no clips and does not appear in names (`--exclude`); a clip `i` … `b` … `o` with no other labels is not cut. If the points of some label are not wanted as clips — `--point-exclude dive`.
- Close segments (≤ 2 s) are merged: `GX011151_00-01-20_14s_fish+shot.mp4` in `Clips/`, index `Clips/_clips.csv`.
- Index: `clip;source;src;start_ms;end_ms;duration_ms;labels;rating;activity;marks;notes`; `src` is what was cut from (`mp4`/`lrv`/`lrf`), `marks`/`notes` are the marks and notes inside the clip (`fish@35000 | bad@45000`, time in ms from the start of the source).
- No re-encoding by default (`-c copy`): fast and lossless; a clip can only start on a keyframe, so the start moves back to the previous one (usually ≤ ~1 s), and the index records the actual start. That is enough for cutting by markup.
- `--reencode` is an export for compatibility: H.264 8-bit + AAC, plays everywhere (phones, messengers, players without HEVC / 10-bit), starts exactly on a frame. Very slow on 4K — the CPU encodes.
- Files rated 0 are skipped by default; `--min-rating 0` takes everything.
- Filters: `--min-rating N`, `--labels fish,shot` (selects clips, does not change their names or bounds), `--pad-before`, `--pad-after`.
- The clip name contains the start, duration and labels, so changing `pad_before` or `pad_after` gives a new name and the clip is cut again.
- A repeated run leaves finished clips alone; a clip cut from a proxy is re-cut once the MP4 appears. `--prune` deletes clips listed in the index from `Clips/` that are not in the current plan (after changes to the markup or the pads, or when a file is rated 0). The filters `--labels`, `--no-marks`, `--min-rating` only choose what to cut now: with `--prune` clips with other labels and from lower-rated files are not deleted.
- If `_clips.csv` is open in another program (Excel locks the file), the script warns before cutting; the clips are cut, and the index is written to `_clips.pending.csv` and moved to `_clips.csv` on the next run. A lost index is restored by running again.
