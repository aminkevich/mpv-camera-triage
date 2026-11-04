-- camera_triage.lua — fast manual triage of action-camera footage in mpv
-- Version 3.4. Requires mpv 0.38 or newer.
-- Works with any video. Understands low-resolution proxies: GoPro .LRV and DJI .LRF
-- (you can watch them instead of the heavy MP4s), and GoPro chapter names (GX01…).
--
-- Markup is written to _triage.csv IN THE VIDEO FOLDER (it moves to the archive with the footage).
--
-- Keys:
--   h v p b     highlight / scenery / people / bad          (point marks)
--   f s c w d   fish / shot / catch / wildlife / dive (a point is a moment during a dive; i … d … o is the whole dive)
--   j t x r     jump / trick / crash / ride
--   i  o        clip start / end (a mark pressed between i and o becomes the clip's label)
--   n           note at the current moment     N  note for the whole file
--               Text is never lost: if a note is undone with u or the input box is closed
--               with Esc, the unfinished text is prefilled into the next n / N.
--               This makes it easy to move a note to another place. Ctrl+V works in the input box.
--   0 1 2 3     file rating (0 junk … 3 top)
--   ENTER       file reviewed → write the rating → next UNreviewed file
--               (an open clip is closed at the current position)
--   u           undo: an open clip (i) right away; a CSV row after confirmation (u again)
--   Del         delete the nearest mark / clip / note (±3 s), including ones from earlier sessions;
--               after confirmation (Del again). u brings the deleted row back
--   Chapters of one GoPro recording (GX01…, GX02…) follow each other in the playlist (gopro_order)
--   + -         speed ×2 / ÷2          TAB  1x ↔ fast         BACKSPACE  1x
--   PgUp PgDn   jump to the previous / next mark (marks are shown as ticks on the seek bar)
--   a           change activity (remembered in the folder's CSV)   F1 help   F11 full screen
--   L           switch original ↔ proxy (LRV/LRF) for the current and following files
--
-- A status line (file, rating, marks, speed ≠ 1x) is always visible in the top right corner.
-- The position in a file is remembered when switching to another file and on exit (resume where you left off).
-- At the end of every file playback pauses with a reminder about the rating.
-- Leaving a file without ENTER: the rating is saved as file_partial, an unclosed clip is discarded.
-- On exit from mpv the CSV is sorted: by file, and by time within a file.
--
-- CSV columns:
--   logged_at;event;activity;file;src;time_ms;end_ms;time_hms;label;rating;note;
--   speed_pct;duration_ms;duration_hms;camera_time
-- event: mark (point) | clip (i/o range) | note (note; without time_ms it is a note for the file)
--        file_done (fully reviewed + rating) | file_partial (rating without ENTER)

local mp      = require 'mp'
local utils   = require 'mp.utils'
local options = require 'mp.options'
local has_input, input = pcall(require, 'mp.input')

local o = {
    csv_name    = "_triage.csv",
    activity    = "spearfishing",
    reaction_s  = 0.5,     -- reaction-time correction (seconds of real time × speed; not applied while paused)
    fast_speed  = 4,       -- speed for TAB
    prefer_proxy = false,  -- true: open the light proxy (GoPro .LRV / DJI .LRF) instead of the MP4 if it is next to it
    prefer_lrv  = false,   -- old name of prefer_proxy (v3.0), works the same
    skip_done   = true,    -- skip already reviewed files in the playlist
    skip_exts   = "thm,jpg,jpeg,gpr,dng,wav,csv,txt,md,ini,srt,xml",
    osd_s       = 1.5,
    status_bar  = true,    -- permanent status in the top right corner
    show_marks  = true,    -- earlier marks pop up during playback
    sort_on_exit = true,   -- sort the CSV by file and time on exit
    gopro_order = true,    -- chapters of one GoPro recording (GX01…, GX02…) follow each other in the playlist
}
options.read_options(o, "camera_triage")
local use_proxy = o.prefer_proxy or o.prefer_lrv     -- current mode; the L key toggles it
local PROXY_EXT = { lrv = true, lrf = true }

local ACTIVITIES = { "spearfishing", "kitesurfing", "other" }
local LABELS = {
    { "h", "highlight" }, { "v", "scenery" }, { "p", "people" },  { "b", "bad" },
    { "f", "fish" },      { "s", "shot" },    { "c", "catch" },   { "w", "wildlife" },
    { "d", "dive" },
    { "j", "jump" },      { "t", "trick" },   { "x", "crash" },   { "r", "ride" },
}
local HEADER = { "logged_at", "event", "activity", "file", "src",
                 "time_ms", "end_ms", "time_hms", "label", "rating", "note",
                 "speed_pct", "duration_ms", "duration_hms", "camera_time" }
local HEADER_LINE = table.concat(HEADER, ";")
local BOM = "\239\187\191"

local HELP = table.concat({
    "h highlight  v scenery  p people  b bad",
    "f fish  s shot  c catch  w wildlife  d dive",
    "j jump  t trick  x crash  r ride",
    "i / o  clip start / end     n note at moment   N note for file",
    "0-3 file rating     ENTER done → next     u undo",
    "Del  delete a nearby mark (±3 s), also from earlier sessions; u brings it back",
    "+ - speed   TAB 1x↔fast   BACKSPACE 1x   a activity",
    "PgUp/PgDn  previous / next mark",
    "Pause SPACE   Frame . ,   Seek arrows   < > files",
    "L  original ↔ low-resolution proxy (LRV / LRF)",
    "F1 hide help   F11 full screen",
}, "\n")

local skip_set = {}
for e in o.skip_exts:gmatch("[^,%s]+") do skip_set[e:lower()] = true end

local activity = o.activity
local activity_dir = nil   -- folder for which the activity has already been chosen
local undone_note = nil    -- text of the last undone note (prefilled into the next one)
local cur = nil        -- current file
local index = {}       -- [dir] = { done, rating, marks (count), points (list) }; key is the MP4 name in upper case
local written = {}     -- stack of this session's writes for undo: { path = ..., dir = ... }
local touched = {}     -- CSVs written in this session (sorted on exit)

---------------------------------------------------------------- helpers

local refresh_ui            -- defined below (status and help overlay)
local function osd(msg, dur)
    mp.osd_message(msg, dur or o.osd_s)
    if refresh_ui then refresh_ui() end
end

local function ms(t) return math.floor(t * 1000 + 0.5) end

local function hms(t)
    t = math.floor(t + 0.5)
    return string.format("%02d:%02d:%02d", math.floor(t / 3600), math.floor((t % 3600) / 60), t % 60)
end

local function csv_field(v)
    if v == nil then return "" end
    local s = tostring(v)
    if s:find('[;"\r\n]') then
        s = '"' .. (s:gsub('"', '""')) .. '"'
    end
    return s
end

local function parse_csv_line(line)
    local res, field, inq, i, n = {}, {}, false, 1, #line
    while i <= n do
        local ch = line:sub(i, i)
        if inq then
            if ch == '"' then
                if line:sub(i + 1, i + 1) == '"' then
                    field[#field + 1] = '"'; i = i + 1
                else
                    inq = false
                end
            else
                field[#field + 1] = ch
            end
        elseif ch == '"' then
            inq = true
        elseif ch == ';' then
            res[#res + 1] = table.concat(field); field = {}
        else
            field[#field + 1] = ch
        end
        i = i + 1
    end
    res[#res + 1] = table.concat(field)
    return res
end

local function exists(dir, name)
    return utils.file_info(utils.join_path(dir, name)) ~= nil
end

-- Proxy → original:
--   GoPro: GL010123.LRV → GX010123.MP4 / GH010123.MP4
--   DJI and others: DJI_20261018_0001_D.LRF → DJI_20261018_0001_D.MP4 (same name)
local function main_file_for(dir, fname)
    local up = fname:upper()
    local ch, num = up:match("^GL(%d%d)(%d%d%d%d)%.LRV$")
    if ch then
        for _, p in ipairs({ "GX", "GH" }) do
            local cand = p .. ch .. num .. ".MP4"
            if exists(dir, cand) then return cand end
        end
        return "GX" .. ch .. num .. ".MP4"   -- best guess when there is no MP4 next to it
    end
    local base = fname:match("^(.+)%.[Ll][Rr][VvFf]$")
    if base then
        for _, e in ipairs({ ".MP4", ".mp4", ".MOV", ".mov" }) do
            if exists(dir, base .. e) then return base .. e end
        end
        return base .. ".MP4"
    end
    return fname
end

-- Original → proxy, if it is next to it (nil — no proxy):
--   GoPro: GX010123.MP4 → GL010123.LRV;  DJI and others: <name>.MP4 → <name>.LRF / <name>.LRV
local function proxy_for(dir, fname)
    local up = fname:upper()
    local cands = {}
    local ch, num = up:match("^G[HX](%d%d)(%d%d%d%d)%.MP4$")
    if ch then
        cands = { "GL" .. ch .. num .. ".LRV" }
    else
        local base = fname:match("^(.+)%.[MmQq][PpOo][4Vv]$")
        if base then cands = { base .. ".LRF", base .. ".LRV" } end
    end
    for _, c in ipairs(cands) do
        if exists(dir, c) then return c end
    end
    return nil
end

-- GoPro splits a long recording into chapters: GX010123, GX020123, … (the first two digits are
-- the chapter, the last four the recording number); older cameras: GOPR0123 (1st chapter), GP010123 (2nd), …
-- Sort key "recording, then chapter" so that chapters of one recording follow each other; not GoPro — nil.
local function gopro_key(fname)
    local up = fname:upper()
    local ch, num = up:match("^G[HXL](%d%d)(%d%d%d%d)%.")
    if not ch then ch, num = up:match("^GP(%d%d)(%d%d%d%d)%.") end
    if not ch then
        num = up:match("^GOPR(%d%d%d%d)%.")
        ch = num and "00"
    end
    if not num then return nil end
    return num .. "_" .. ch .. "_" .. up
end

local function full_path(p)
    local np = mp.command_native({ "normalize-path", p })
    if np and np ~= "" then return np end
    if p:match("^%a:[\\/]") or p:match("^[\\/]") then return p end
    return utils.join_path(mp.get_property("working-directory", ""), p)
end

local function split(p)
    local path = full_path(p)
    local dir, fname = utils.split_path(path)
    dir = dir:gsub("[\\/]+$", "")
    return path, dir, fname, (fname:match("%.([^.]+)$") or ""):lower()
end

-- "2026-09-18T09:55:29.000000Z" → "2026-09-18 09:55:29". GoPro writes the camera clock's local time
-- here (despite the "Z"); other cameras may write UTC — the value is stored as is, without conversion.
local function camera_time()
    local raw = mp.get_property("metadata/by-key/creation_time")
    if not raw then return nil end
    local d, t = raw:match("^(%d%d%d%d%-%d%d%-%d%d)[T ](%d%d:%d%d:%d%d)")
    if d then return d .. " " .. t end
    return raw
end

---------------------------------------------------------------- CSV

local function read_first_line(path)
    local f = io.open(path, "rb")
    if not f then return nil end
    local first = (f:read("*l") or ""):gsub("\r$", ""):gsub("^" .. BOM, "")
    f:close()
    return first
end

-- nil = no file; true = our format; false = foreign/old format
local function header_state(path)
    local first = read_first_line(path)
    if first == nil or first == "" then return nil end   -- no file / empty file → the header will be written
    return first == HEADER_LINE
end

local function load_index(dir, force)
    if index[dir] and not force then return index[dir] end
    local ix = { done = {}, rating = {}, marks = {}, notes = {}, points = {}, bad = false,
                 activity = nil, activity_at = "" }
    index[dir] = ix
    local path = utils.join_path(dir, o.csv_name)
    if header_state(path) == false then ix.bad = true; return ix end
    local f = io.open(path, "rb")
    if not f then return ix end
    local col
    for raw in f:lines() do
        local line = raw:gsub("\r$", ""):gsub("^" .. BOM, "")
        if line ~= "" then
            local row = parse_csv_line(line)
            if not col then
                col = {}
                for i, name in ipairs(row) do col[name] = i end
            else
                local k = (row[col.file] or ""):upper()
                -- the folder's activity = activity of the latest row (by logged_at, not by row order)
                local at, act = row[col.logged_at] or "", row[col.activity] or ""
                if act ~= "" and at >= ix.activity_at then ix.activity, ix.activity_at = act, at end
                local ev = row[col.event]
                if ev == "file_done" then
                    ix.done[k] = true
                    ix.rating[k] = row[col.rating]
                elseif ev == "file_partial" then
                    ix.rating[k] = row[col.rating]
                elseif ev == "mark" or ev == "clip" or ev == "note" then
                    if ev == "note" then
                        ix.notes[k] = (ix.notes[k] or 0) + 1
                    else
                        ix.marks[k] = (ix.marks[k] or 0) + 1
                    end
                    local t = tonumber(row[col.time_ms] or "")
                    if t then
                        local pts = ix.points[k] or {}
                        ix.points[k] = pts
                        local label = (ev == "note") and (row[col.note] or "") or (row[col.label] or "")
                        pts[#pts + 1] = { t = t / 1000, e = tonumber(row[col.end_ms] or ""),
                                          ev = ev, label = label, line = line }
                        if pts[#pts].e then pts[#pts].e = pts[#pts].e / 1000 end
                    end
                end
            end
        end
    end
    f:close()
    for _, pts in pairs(ix.points) do
        table.sort(pts, function(a, b) return a.t < b.t end)
    end
    return ix
end

local function append_row(fields)
    local path = cur.csv
    local hs = header_state(path)
    if hs == false then
        osd("⚠ _triage.csv has a different format (old script version or\n" ..
            "re-saved in OnlyOffice/Excel). Writing stopped.\n" ..
            "Rename or delete this CSV.", 8)
        return false
    end
    local f, err = io.open(path, "ab")
    if not f then
        osd("⚠ Cannot write the CSV:\n" .. tostring(err) .. "\nIs it open in Excel?", 6)
        return false
    end
    if hs == nil then
        local sz = f:seek("end")
        if sz and sz > 0 then f:write("\n") end         -- blank line / garbage without a header
        f:write(BOM .. HEADER_LINE .. "\n")
    end
    local out = {}
    for i = 1, #HEADER do out[i] = csv_field(fields[HEADER[i]]) end
    local line = table.concat(out, ";")
    f:write(line .. "\n")
    f:close()
    written[#written + 1] = { path = path, dir = cur.dir, line = line }
    touched[path] = true
    return true
end

-- Remove one row from the CSV (the first exact match).
-- true — removed; false, text — no such row; nil, text — could not read/write.
local function remove_line(path, line)
    local f = io.open(path, "rb")
    if not f then return nil, "CSV not found" end
    local data = f:read("*a")
    f:close()
    local out, removed = {}, false
    for raw in data:gmatch("[^\n]+") do
        if not removed and raw:gsub("\r$", "") == line then removed = true
        else out[#out + 1] = raw end
    end
    if not removed then return false, "the row has already been changed or removed" end
    local w, err = io.open(path, "wb")
    if not w then return nil, tostring(err) end
    w:write(table.concat(out, "\n") .. "\n")
    w:close()
    return true
end

local function base_row(event, with_speed)
    return {
        logged_at = os.date("%Y-%m-%d %H:%M:%S"),
        event     = event,
        activity  = activity,
        file      = cur.file,
        src       = cur.src,
        -- speed matters only for events with a time (mark accuracy)
        speed_pct = with_speed and math.floor(mp.get_property_number("speed", 1) * 100 + 0.5) or nil,
    }
end

-- CSV sort: by file (GoPro chapters follow each other by recording); within a file — events with a time
-- (by time), notes for the file, then file_partial / file_done. Ties keep the write order.
-- Writes to a temporary file and swaps it in.
local function sort_csv(path)
    if header_state(path) ~= true then return end
    local f = io.open(path, "rb")
    if not f then return end
    local data = f:read("*a")
    f:close()
    local rows, header = {}, nil
    local col = {}
    for i, name in ipairs(HEADER) do col[name] = i end
    for raw in data:gmatch("[^\n]+") do
        local line = raw:gsub("\r$", ""):gsub("^" .. BOM, "")
        if line ~= "" then
            if not header then
                header = line
            else
                local r = parse_csv_line(line)
                local ev = r[col.event] or ""
                local t = tonumber(r[col.time_ms] or "")
                local grp = (ev == "file_done" or ev == "file_partial") and 2 or (t and 0 or 1)
                local file = r[col.file] or ""
                rows[#rows + 1] = { line = line, file = gopro_key(file) or file:upper(),
                                    grp = grp, t = t or 0, n = #rows }
            end
        end
    end
    table.sort(rows, function(a, b)
        if a.file ~= b.file then return a.file < b.file end
        if a.grp ~= b.grp then return a.grp < b.grp end
        if a.t ~= b.t then return a.t < b.t end
        return a.n < b.n
    end)
    local out = { BOM .. header }
    for _, r in ipairs(rows) do out[#out + 1] = r.line end
    local tmp = path .. ".tmp"
    local w = io.open(tmp, "wb")
    if not w then return end
    w:write(table.concat(out, "\n") .. "\n")
    w:close()
    os.remove(path)
    if not os.rename(tmp, path) then
        mp.msg.error("camera_triage: could not replace " .. path .. ", the data is in " .. tmp)
    end
end

---------------------------------------------------------------- marks on timeline

-- Marks of the current file → mpv "chapters": ticks on the seek bar, PgUp/PgDn jump between them
local function update_chapters()
    if not cur then return end
    local pts = (index[cur.dir].points or {})[cur.key] or {}
    local ch = {}
    for _, p in ipairs(pts) do
        if p.ev == "clip" then
            ch[#ch + 1] = { time = p.t, title = "▶ clip " .. p.label }
            if p.e then ch[#ch + 1] = { time = p.e, title = "◀ clip end" } end
        elseif p.ev == "note" then
            ch[#ch + 1] = { time = p.t, title = "✎ " .. p.label }
        else
            ch[#ch + 1] = { time = p.t, title = p.label }
        end
    end
    table.sort(ch, function(a, b) return a.time < b.time end)
    mp.set_property_native("chapter-list", ch)
end

local function reload_marks()
    if not cur then return end
    load_index(cur.dir, true)
    update_chapters()
end

---------------------------------------------------------------- UI: status + help

local ui = mp.create_osd_overlay("ass-events")
local help_on = false

local function ass_escape(t)
    return (t:gsub("\\", "\\\239\187\160"):gsub("{", "\\{"):gsub("}", "\\}"):gsub("\n", "\\N"))
end

local count_done              -- defined below (how many playlist files are reviewed)

local function status_lines()
    if not cur then return {} end
    local ix = index[cur.dir] or {}
    local pos = mp.get_property_number("playlist-pos", 0) + 1
    local count = mp.get_property_number("playlist-count", 1)
    local done = ix.done and ix.done[cur.key]
    local lines = {
        string.format("[%d/%d]  %s%s", pos, count, cur.file,
            PROXY_EXT[cur.src] and ("  [" .. cur.src:upper() .. " proxy]") or ""),
        (done and "✓ reviewed" or "not reviewed") ..
            "   rating " .. (cur.rating and tostring(cur.rating) or "—") ..
            "   marks " .. ((ix.marks and ix.marks[cur.key]) or 0) ..
            "   notes " .. ((ix.notes and ix.notes[cur.key]) or 0),
        activity .. "   reviewed " .. count_done() .. " of " .. count,
    }
    return lines
end

refresh_ui = function()
    local events = {}
    if cur and o.status_bar then
        local txt = ass_escape(table.concat(status_lines(), "\n"))
        events[#events + 1] = "{\\an9\\pos(1268,10)\\fs17\\bord1.5\\shad0\\1c&HFFFFFF&\\3c&H000000&}" .. txt
        local extra = {}
        local speed = mp.get_property_number("speed", 1)
        if math.abs(speed - 1) > 0.01 then
            extra[#extra + 1] = "{\\fs34\\1c&H00D7FF&}" .. ass_escape(string.format("▶▶ %gx", speed))
        end
        if cur.in_t then
            extra[#extra + 1] = "{\\fs20\\1c&H5050FF&}" .. ass_escape("● clip open since " .. hms(cur.in_t) .. "  (o / u)")
        end
        if #extra > 0 then
            events[#events + 1] = "{\\an9\\pos(1268,58)\\bord2\\shad0\\3c&H000000&}" .. table.concat(extra, "\\N")
        end
    end
    if help_on then
        events[#events + 1] = "{\\an1\\pos(16,640)\\fs19\\bord2\\shad0\\1c&HFFFFFF&\\3c&H000000&}" .. ass_escape(HELP)
    end
    if #events == 0 then ui:remove(); return end
    ui.res_x, ui.res_y = 1280, 720
    ui.data = table.concat(events, "\n")
    ui:update()
end

---------------------------------------------------------------- actions

local function need_file()
    if not cur then osd("No file loaded"); return false end
    return true
end

-- mark time: during playback subtract reaction × speed, while paused — exact
local function mark_time()
    local t = mp.get_property_number("time-pos")
    if not t then return nil end
    if not mp.get_property_bool("pause") then
        t = t - o.reaction_s * mp.get_property_number("speed", 1)
    end
    return math.max(0, t)
end

local function add_mark(label)
    if not need_file() then return end
    local t = mark_time()
    if not t then return end
    local row = base_row("mark", true)
    row.time_ms, row.time_hms, row.label = ms(t), hms(t), label
    if append_row(row) then
        if cur.in_t then cur.in_label = label end
        reload_marks()
        osd("● " .. label .. "  @ " .. hms(t))
    end
end

local function set_in()
    if not need_file() then return end
    local t = mark_time()
    if not t then return end
    cur.in_t, cur.in_label = t, nil
    osd("▶ IN  " .. hms(t) .. "     o — clip end,  u — cancel")
end

local function close_clip(t)
    local row = base_row("clip", true)
    row.time_ms, row.end_ms, row.time_hms, row.label = ms(cur.in_t), ms(t), hms(cur.in_t), cur.in_label
    if not append_row(row) then return false end
    local msg = "■ Clip " .. hms(cur.in_t) .. " – " .. hms(t) .. (cur.in_label and ("  " .. cur.in_label) or "")
    cur.in_t, cur.in_label = nil, nil
    reload_marks()
    osd(msg)
    return true
end

local function set_out()
    if not need_file() then return end
    if not cur.in_t then osd("Set the clip start first: i"); return end
    local t = mark_time()
    if not t then return end
    if t <= cur.in_t then osd("End is before start — clip not written", 2.5); return end
    close_clip(t)
end

local function set_rating(n)
    if not need_file() then return end
    cur.rating = n
    osd("File rating: " .. n .. "  " .. string.rep("★", n) .. "     ENTER — done")
end

local function ask_text(prompt, on_text, default_text)
    if not has_input then
        osd("Notes require mpv 0.38 or newer", 4)
        return
    end
    local was_paused = mp.get_property_bool("pause")
    mp.set_property_bool("pause", true)
    local function restore()
        if not was_paused then mp.set_property_bool("pause", false) end
    end
    local submitted = false
    local draft = default_text or ""          -- last typed text (for Esc)
    input.get({
        prompt = prompt .. " ",
        default_text = default_text,
        cursor_position = default_text and (#default_text + 1) or nil,
        edited = function(text) draft = text or "" end,
        submit = function(text)
            submitted = true
            input.terminate()
            restore()
            text = (text or ""):gsub("[\r\n]+", " ")
            if text:match("%S") then
                on_text(text)
            else
                osd("✎ Empty note — nothing written")
            end
        end,
        closed = function(text)
            restore()
            if submitted then return end
            local t = ((type(text) == "string") and text or draft):gsub("[\r\n]+", " ")
            if t:match("%S") then
                undone_note = t     -- draft: prefilled into the next note
                osd("✎ Note not written — text kept:\n«" .. t .. "»\nn / N at the right place will insert it", 4)
            else
                undone_note = nil
                osd("✎ Note cancelled — nothing written")
            end
        end,
    })
end

local function note_moment()
    if not need_file() then return end
    local t = mark_time() or 0
    local file_key = cur.key
    local prefill = undone_note
    ask_text("Note @ " .. hms(t) .. ":", function(text)
        undone_note = nil
        if not (cur and cur.key == file_key) then return end
        local row = base_row("note", true)
        row.time_ms, row.time_hms, row.note = ms(t), hms(t), text
        if append_row(row) then reload_marks(); osd("✎ " .. text) end
    end, prefill)
end

local function note_file()
    if not need_file() then return end
    local file_key = cur.key
    local prefill = undone_note
    ask_text("Note for the file:", function(text)
        undone_note = nil
        if not (cur and cur.key == file_key) then return end
        local row = base_row("note")      -- no time_ms = note for the whole file
        row.note = text
        if append_row(row) then reload_marks(); osd("✎ (file) " .. text) end
    end, prefill)
end

-- CSV row (parsed) → "fish  00:01:29", for the u and Del confirmations
local function describe_row(r)
    local what = (r[2] or "") .. " " .. ((r[2] == "note") and (r[11] or "") or (r[9] or "")) ..
                 " " .. (r[10] or "") .. " " .. (r[8] or "")
    if (r[4] or ""):upper() ~= (cur and cur.key or "") then what = what .. "   (file " .. (r[4] or "") .. ")" end
    return what
end

local undo_armed = nil      -- { line, at }: waiting for undo confirmation
local function undo()
    if cur and cur.in_t then
        local t = cur.in_t
        cur.in_t, cur.in_label = nil, nil
        osd("↶ Clip start " .. hms(t) .. " cancelled")
        return
    end
    local last_w = written[#written]
    if not last_w then osd("Nothing to undo (in this session)"); return end
    local last = last_w.line
    local r = parse_csv_line(last)
    local what = describe_row(r)
    -- confirmation: the first u only shows what will be undone
    local now = mp.get_time()
    if not (undo_armed and undo_armed.line == last and now - undo_armed.at < 4) then
        undo_armed = { line = last, at = now }
        osd((last_w.deleted and "u again — restore deleted:  " or "u again — undo:  ") .. what, 4)
        return
    end
    undo_armed = nil
    if last_w.deleted then
        -- undoing a delete (Del): the row goes back to the end of the CSV and is sorted into place on exit
        local f, err = io.open(last_w.path, "ab")
        if not f then osd("⚠ Cannot write the CSV: " .. tostring(err), 5); return end
        f:write(last .. "\n")
        f:close()
        table.remove(written)
        load_index(last_w.dir, true)
        if cur and cur.dir == last_w.dir then update_chapters() end
        osd("↶ Restored: " .. what)
        return
    end
    local ok, err = remove_line(last_w.path, last)
    if ok == nil then osd("⚠ Cannot write the CSV: " .. err, 5); return end
    table.remove(written)
    if not ok then osd("⚠ Not undone: " .. err, 5); return end
    load_index(last_w.dir, true)
    if cur and cur.dir == last_w.dir then
        update_chapters()
        local k = (r[4] or ""):upper()
        if k == cur.key and (r[2] == "file_done" or r[2] == "file_partial") then
            cur.finished = false
            cur.saved_rating = tonumber(index[cur.dir].rating[k] or "")
        end
    end
    if r[2] == "note" and (r[11] or "") ~= "" then
        undone_note = r[11]
        osd("↶ Undone: " .. what .. "\nn / N at a new place — the text will be prefilled", 4)
    else
        osd("↶ Undone: " .. what)
    end
end

-- Del: delete the mark, clip or note nearest to the current position — including ones from earlier sessions.
-- A clip is matched by its start or end. Handy: PgUp/PgDn to the mark → Del → Del.
-- u brings the deleted row back (like a normal undo).
local DEL_WINDOW = 3          -- seconds from the current position
local del_armed = nil         -- { line, at }: waiting for delete confirmation
local function delete_near()
    if not need_file() then return end
    local ix = index[cur.dir]
    if ix.bad then osd("⚠ _triage.csv has a different format — cannot delete", 4); return end
    local t = mp.get_property_number("time-pos", 0)
    local best, bd = nil, nil
    for _, p in ipairs((ix.points or {})[cur.key] or {}) do
        local d = math.abs(t - p.t)
        if p.e then d = math.min(d, math.abs(t - p.e)) end
        if d <= DEL_WINDOW and (not bd or d < bd) then best, bd = p, d end
    end
    if not best then
        osd("No marks nearby (±" .. DEL_WINDOW .. " s).  PgUp/PgDn — go to a mark, then Del", 3)
        return
    end
    local what = describe_row(parse_csv_line(best.line))
    local now = mp.get_time()
    if not (del_armed and del_armed.line == best.line and now - del_armed.at < 4) then
        del_armed = { line = best.line, at = now }
        osd("Del again — delete:  " .. what, 4)
        return
    end
    del_armed = nil
    local ok, err = remove_line(cur.csv, best.line)
    if not ok then osd("⚠ Not deleted: " .. err, 5); reload_marks(); return end
    -- if this row was written in this session, u must no longer "undo" it — instead u restores it
    for i = #written, 1, -1 do
        local w = written[i]
        if not w.deleted and w.path == cur.csv and w.line == best.line then table.remove(written, i); break end
    end
    written[#written + 1] = { path = cur.csv, dir = cur.dir, line = best.line, deleted = true }
    touched[cur.csv] = true
    reload_marks()
    osd("✕ Deleted: " .. what .. "     u — restore")
end

-- is the playlist entry's file reviewed
local function entry_done(filename)
    local _, dir, fname, ext = split(filename)
    local file = PROXY_EXT[ext] and main_file_for(dir, fname) or fname
    return load_index(dir).done[file:upper()] == true
end

count_done = function()
    local n = 0
    for _, e in ipairs(mp.get_property_native("playlist") or {}) do
        if entry_done(e.filename) then n = n + 1 end
    end
    return n
end

-- index of the next unreviewed file after pos (wrapping around), nil — none left
local function find_undone(pos)
    local pl = mp.get_property_native("playlist") or {}
    local n = #pl
    for step = 1, n - 1 do
        local i = (pos + step) % n
        if not entry_done(pl[i + 1].filename) then return i end
    end
    return nil
end

local function mark_done()
    if not need_file() then return end
    if cur.rating == nil then
        osd("Rate 0–3 first, then ENTER", 2.5)
        return
    end
    if cur.in_t then
        local t = mark_time()
        if t and t > cur.in_t then
            if not close_clip(t) then return end
        else
            cur.in_t, cur.in_label = nil, nil
        end
    end
    local ix = index[cur.dir]
    local msg
    if ix.done[cur.key] and cur.rating == cur.saved_rating then
        -- already reviewed and the rating has not changed — no need for another file_done row
        cur.finished = true
        msg = "✓ Already reviewed (rating " .. cur.rating .. ")"
    else
        local row = base_row("file_done")
        row.rating, row.duration_ms, row.duration_hms, row.camera_time =
            cur.rating, cur.duration_ms, cur.duration_hms, cur.camera_time
        if not append_row(row) then return end
        cur.finished = true
        cur.saved_rating = cur.rating
        ix.done[cur.key] = true
        ix.rating[cur.key] = tostring(cur.rating)
        msg = "✓ Done (rating " .. cur.rating .. ")"
    end
    local pos = mp.get_property_number("playlist-pos", 0)
    local nxt = find_undone(pos)
    if nxt then
        osd(msg .. " → next unreviewed")
        mp.commandv("playlist-play-index", tostring(nxt))
    else
        osd(msg .. ((mp.get_property_number("playlist-count", 1) > 1)
            and ".  All files in the list are reviewed." or "."), 5)
    end
end

local function cycle_activity()
    local i = 1
    for k, a in ipairs(ACTIVITIES) do
        if a == activity then i = k end
    end
    activity = ACTIVITIES[i % #ACTIVITIES + 1]
    osd("Activity: " .. activity)
end

local function set_speed(s)
    s = math.max(0.25, math.min(16, s))
    mp.set_property_number("speed", s)
    osd(string.format("Speed %gx", s))
end

local function toggle_fast()
    local s = mp.get_property_number("speed", 1)
    if math.abs(s - 1) < 0.01 then set_speed(o.fast_speed) else set_speed(1) end
end

---------------------------------------------------------------- playlist / file events

-- What to do with a playlist entry: "junk" (not a video / a proxy whose original is also in the list),
-- "done" (already reviewed), nil (keep).
-- in_list — set of full playlist paths (upper case).
local function entry_kind(p, in_list)
    local path, dir, fname, ext = split(p)
    local info = utils.file_info(path)
    if not info or info.is_dir then return nil end          -- mpv expands folders itself
    if skip_set[ext] then return "junk" end
    if PROXY_EXT[ext] then
        local main = utils.join_path(dir, main_file_for(dir, fname))
        if in_list[main:upper()] then return "junk" end
    end
    local file = PROXY_EXT[ext] and main_file_for(dir, fname) or fname
    if load_index(dir).done[file:upper()] then return "done" end
    return nil
end

local function playlist_sig(pl)
    if #pl == 0 then return "" end
    return #pl .. "|" .. (pl[1].filename or "") .. "|" .. (pl[#pl].filename or "")
end

-- GoPro chapters in recording order: mpv sorts by name (GX010123, GX010124, …, GX020123),
-- and the continuation of a recording ends up at the end. Only GoPro files are moved, into the
-- slots they already occupy; other files stay put. true — the order changed.
local function reorder_gopro()
    local pl = mp.get_property_native("playlist") or {}
    local slots, items = {}, {}
    for i, e in ipairs(pl) do
        local _, dir, fname = split(e.filename)
        local k = gopro_key(fname)
        if k then
            slots[#slots + 1] = i
            items[#items + 1] = { f = e.filename, k = dir:upper() .. "|" .. k }
        end
    end
    if #items < 2 then return false end
    table.sort(items, function(a, b) return a.k < b.k end)
    local want, have = {}, {}
    for i, e in ipairs(pl) do want[i], have[i] = e.filename, e.filename end
    for j, i in ipairs(slots) do want[i] = items[j].f end
    local moved = false
    for i = 1, #want do
        if have[i] ~= want[i] then
            local j = i + 1
            while j <= #have and have[j] ~= want[i] do j = j + 1 end
            if j > #have then break end
            mp.commandv("playlist-move", tostring(j - 1), tostring(i - 1))
            table.insert(have, i, table.remove(have, j))
            moved = true
        end
    end
    return moved
end

local pl_sig = nil          -- playlist that has already been cleaned
local start_msg = nil       -- message when a folder is opened (how many are already reviewed)
local skip_current = false  -- the current file must be skipped (already reviewed / not a video)
local skip_to = nil         -- where to go instead of the current file
local open_src = nil        -- "lrv"/"lrf" if a proxy was opened instead of the MP4
local resume_after_reload = nil  -- reopen by L: { key, t, pause, in_t, in_label }

-- The playlist is cleaned ONCE, before the file opens (not by removing the current entry on the fly).
mp.add_hook("on_load", 50, function()
    skip_current, open_src = false, nil
    local p = mp.get_property("path")
    if not p then return end
    local path, dir, fname, ext = split(p)
    local info = utils.file_info(path)
    if not info or info.is_dir then return end

    local pl = mp.get_property_native("playlist") or {}
    if #pl > 1 and playlist_sig(pl) ~= pl_sig then
        local in_list = {}
        for _, e in ipairs(pl) do in_list[full_path(e.filename):upper()] = true end
        -- 1) remove only junk (photos, THM, proxies that have an original); reviewed files stay
        local cur_i = mp.get_property_number("playlist-pos", 0)
        local cur_junk = false
        for i = #pl - 1, 0, -1 do                            -- from the end so the indexes do not shift
            if entry_kind(pl[i + 1].filename, in_list) == "junk" then
                if i == cur_i then cur_junk = true else mp.commandv("playlist-remove", tostring(i)) end
            end
        end
        local was_first = cur_i == 0                         -- before cleaning: a whole folder was opened
        local reordered = o.gopro_order and reorder_gopro()
        local left = mp.get_property_native("playlist") or {}
        -- if the current entry is junk itself, it is removed on the next open (the current one cannot be removed)
        pl_sig = (not cur_junk) and playlist_sig(left) or nil
        cur_i = mp.get_property_number("playlist-pos", 0)
        -- 2) start with the first UNreviewed file
        local first_undone, ndone, nvideo = nil, 0, 0
        for i, e in ipairs(left) do
            if entry_kind(e.filename, in_list) ~= "junk" then
                nvideo = nvideo + 1
                if entry_done(e.filename) then ndone = ndone + 1
                elseif not first_undone then first_undone = i - 1 end
            end
        end
        if ndone > 0 then
            start_msg = "Reviewed " .. ndone .. " of " .. nvideo ..
                (first_undone and " — starting with the first unreviewed" or " — all files, reviewing again")
        end
        local target = nil
        if cur_junk then
            target = first_undone or ((cur_i + 1) % #left)
        elseif o.skip_done and first_undone and first_undone ~= cur_i and entry_done(p) then
            target = first_undone
        elseif reordered and was_first and cur_i ~= 0 then
            -- a folder was opened (the first file was playing), and after reordering another file is first
            target = (o.skip_done and first_undone) or 0
            if target == cur_i then target = nil end
        end
        if target and #left > 1 then
            skip_current, skip_to = true, target
            return
        end
    end

    if use_proxy and (ext == "mp4" or ext == "mov") then
        local px = proxy_for(dir, fname)
        if px then
            mp.set_property("stream-open-filename", utils.join_path(dir, px))
            open_src = (px:match("%.([^.]+)$") or "lrv"):lower()
        end
    end
end)

mp.register_event("file-loaded", function()
    cur = nil
    local p = mp.get_property("path")
    if not p then return end
    local _, dir, fname, ext = split(p)
    if skip_current then
        skip_current = false
        mp.commandv("playlist-play-index", tostring(skip_to or 0))
        skip_to = nil
        return
    end
    local file = PROXY_EXT[ext] and main_file_for(dir, fname) or fname
    local key = file:upper()
    local ix = load_index(dir)
    local dur = mp.get_property_number("duration")
    local activity_msg = nil
    if dir ~= activity_dir then
        activity_dir = dir
        if ix.activity and ix.activity ~= activity then
            activity = ix.activity
            activity_msg = "Activity from this folder's _triage.csv: " .. activity
        end
    end

    cur = {
        dir = dir, fname = fname, file = file, key = key, src = open_src or ext,
        csv = utils.join_path(dir, o.csv_name),
        rating = tonumber(ix.rating[key] or ""),   -- when reviewing again, prefill the previous rating
        camera_time = camera_time(),
        duration_ms = dur and ms(dur) or nil,
        duration_hms = dur and hms(dur) or nil,
    }
    cur.saved_rating = cur.rating
    cur.last_t = 0
    update_chapters()
    local rr = resume_after_reload
    resume_after_reload = nil
    if rr and rr.key == key then
        -- reopened by L: same position, same pause state; an open clip and an unsaved rating are kept
        cur.in_t, cur.in_label = rr.in_t, rr.in_label
        cur.rating, cur.saved_rating = rr.rating, rr.saved_rating
        cur.last_t = rr.t
        mp.commandv("seek", tostring(rr.t), "absolute+exact")
        mp.set_property_bool("pause", rr.pause)
        refresh_ui()
        return
    end
    if ix.bad then
        osd("⚠ _triage.csv in this folder has a different format — writing will be stopped.\n" ..
            "Rename or delete it.", 8)
    else
        local start = mp.get_property_number("time-pos", 0)
        if activity_msg then
            osd((start_msg and (start_msg .. "\n") or "") .. activity_msg, 4)
            start_msg = nil
        elseif start_msg then
            osd(start_msg, 4)
            start_msg = nil
        elseif start > 1 then
            osd(cur.file .. "   ↻ resuming from " .. hms(start), 2.5)
        else
            osd(cur.file, 2)
        end
    end
end)

-- Leaving a file without ENTER (>, <, closing mpv): keep the rating, write file_partial.
mp.register_event("end-file", function()
    local c = cur
    if not c then return end
    if c.finished then cur = nil; return end
    if resume_after_reload and resume_after_reload.key == c.key then
        cur = nil       -- reopen by L: the rating and clip carry over, no file_partial needed
        return
    end
    local msgs = {}
    if c.in_t then
        msgs[#msgs + 1] = "unclosed clip from " .. hms(c.in_t) .. " discarded"
        c.in_t = nil
    end
    if c.rating ~= nil and c.rating ~= c.saved_rating then
        local row = base_row("file_partial")
        row.rating, row.duration_ms, row.duration_hms, row.camera_time =
            c.rating, c.duration_ms, c.duration_hms, c.camera_time
        if append_row(row) then
            index[c.dir].rating[c.key] = tostring(c.rating)
            msgs[#msgs + 1] = "rating saved as a draft (without ENTER)"
        end
    elseif not (index[c.dir] and index[c.dir].done[c.key]) then
        msgs[#msgs + 1] = "not marked as reviewed"
    end
    cur = nil
    if #msgs > 0 and not resume_after_reload then
        mp.osd_message("⚠ " .. c.file .. ": " .. table.concat(msgs, ", "), 3)
    end
    refresh_ui()
end)

-- Remember the position in a file when switching to another file (<, >) and on exit — with mpv's
-- own watch_later (works after a restart too). A finished / just started file is not remembered.
mp.add_hook("on_unload", 50, function()
    if not cur then return end
    local pos = mp.get_property_number("time-pos")
    local dur = mp.get_property_number("duration")
    if cur.finished or mp.get_property_bool("eof-reached")
        or not pos or pos < 3 or (dur and pos > dur - 3) then
        mp.command("delete-watch-later-config")
    else
        mp.command("write-watch-later-config")
    end
end)

mp.register_event("shutdown", function()
    if not o.sort_on_exit then return end
    for path in pairs(touched) do sort_csv(path) end
end)

-- Do not move to the next file automatically: pause with a reminder at the end of every file.
mp.set_property("keep-open", "always")
mp.observe_property("eof-reached", "bool", function(_, eof)
    if not eof or not cur then return end
    local ix = index[cur.dir] or { done = {} }
    if ix.done[cur.key] and cur.rating == cur.saved_rating then
        osd("■ End of file (already reviewed).   ENTER — to an unreviewed one,  > — next", 3600)
    else
        osd("■ End of file.  Rating 0–3 → ENTER" ..
            (cur.rating and ("   (now: " .. cur.rating .. ")") or "") ..
            "\n> — next without marking", 3600)
    end
end)

-- Earlier marks pop up when playback passes them
mp.observe_property("time-pos", "number", function(_, t)
    if not cur or not t then return end
    local last = cur.last_t or t
    cur.last_t = t
    if not o.show_marks then return end
    local step = t - last
    local speed = mp.get_property_number("speed", 1)
    if step <= 0 or step > 1 + speed then return end   -- seek/jump — do not show
    local pts = (index[cur.dir].points or {})[cur.key]
    if not pts then return end
    for _, p in ipairs(pts) do
        if p.t > last and p.t <= t then
            local what
            if p.ev == "clip" then
                what = "▶ clip " .. p.label .. "  " .. hms(p.t) .. "–" .. hms(p.e or p.t)
            elseif p.ev == "note" then
                what = "✎ " .. p.label .. "  @ " .. hms(p.t)
            else
                what = "◆ " .. p.label .. "  @ " .. hms(p.t)
            end
            osd(what, 2)
        end
    end
end)

mp.observe_property("speed", "number", function() refresh_ui() end)
mp.observe_property("playlist-count", "number", function() refresh_ui() end)

---------------------------------------------------------------- key bindings

local function bind(key, name, fn) mp.add_forced_key_binding(key, "triage-" .. name, fn) end

for _, kv in ipairs(LABELS) do
    local key, label = kv[1], kv[2]
    bind(key, "mark-" .. label, function() add_mark(label) end)
end
for n = 0, 3 do
    bind(tostring(n), "rate-" .. n, function() set_rating(n) end)
end
bind("i", "in", set_in)
bind("o", "out", set_out)
bind("n", "note", note_moment)
bind("N", "file-note", note_file)
bind("u", "undo", undo)
bind("DEL", "delete-mark", delete_near)
bind("a", "activity", cycle_activity)
bind("ENTER", "done", mark_done)
bind("KP_ENTER", "done-kp", mark_done)
bind("+", "faster", function() set_speed(mp.get_property_number("speed", 1) * 2) end)
bind("-", "slower", function() set_speed(mp.get_property_number("speed", 1) / 2) end)
bind("KP_ADD", "faster-kp", function() set_speed(mp.get_property_number("speed", 1) * 2) end)
bind("KP_SUBTRACT", "slower-kp", function() set_speed(mp.get_property_number("speed", 1) / 2) end)
bind("TAB", "fast-toggle", toggle_fast)
bind("BS", "speed-reset", function() set_speed(1) end)
for k, s in pairs({ F2 = 1, F3 = 2, F4 = 4, F5 = 8 }) do
    bind(k, "speed-" .. k, function() set_speed(s) end)
end
-- L: switch original ↔ proxy. The current file is reopened at the same position.
local function toggle_proxy()
    use_proxy = not use_proxy
    local msg = use_proxy and "Mode: PROXY (low resolution, LRV/LRF)" or "Mode: ORIGINAL (full resolution)"
    if not cur then osd(msg, 3); return end
    local has_px = proxy_for(cur.dir, cur.file) ~= nil
    if use_proxy and not has_px then
        osd(msg .. "\nThis file has no proxy — staying on the original", 4)
        return
    end
    local t = mp.get_property_number("time-pos", 0)
    local was_paused = mp.get_property_bool("pause")
    resume_after_reload = { key = cur.key, t = t, pause = was_paused,
                            in_t = cur.in_t, in_label = cur.in_label,
                            rating = cur.rating, saved_rating = cur.saved_rating }
    osd(msg, 3)
    mp.commandv("playlist-play-index", tostring(mp.get_property_number("playlist-pos", 0)))
end
bind("L", "proxy-toggle", toggle_proxy)

bind("F1", "help", function() help_on = not help_on; refresh_ui() end)
bind("F11", "fullscreen", function() mp.command("cycle fullscreen") end)

mp.msg.info("camera_triage v3.4: CSV = <video folder>/" .. o.csv_name)
