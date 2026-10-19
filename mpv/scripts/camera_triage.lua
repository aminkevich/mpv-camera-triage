-- camera_triage.lua — быстрая ручная разметка (триаж) футажа экшн-камер в mpv
-- Версия 3.2. Нужна mpv 0.38 или новее.
-- Работает с любыми видео. Понимает прокси низкого разрешения: GoPro .LRV и DJI .LRF
-- (можно смотреть их вместо тяжёлых MP4), для GoPro — имена глав (GX01…).
--
-- Разметка пишется в _triage.csv В ПАПКЕ С ВИДЕО (переезжает в архив вместе с футажом).
--
-- Клавиши:
--   h v p b     highlight / scenery / people / bad          (точечные метки)
--   f s c w d   fish / shot / catch / wildlife / dive (точка — момент во время нырка; i … d … o — весь нырок)
--   j t x r     jump / trick / crash / ride
--   i  o        начало / конец клипа (метка, нажатая между i и o, становится меткой клипа)
--   n           заметка к моменту           N  заметка к файлу
--               (после отмены заметки через u её текст подставится в следующую n / N —
--                так заметку легко перенести в другое место; в поле ввода работает Ctrl+V)
--   0 1 2 3     оценка файла (0 мусор … 3 топ)
--   ENTER       файл просмотрен → записать оценку → следующий НЕпросмотренный
--               (открытый клип при этом закрывается в текущей позиции)
--   u           отменить: открытый клип (i) — сразу; запись в CSV — после подтверждения (u ещё раз)
--   + -         скорость ×2 / ÷2       TAB  1x ↔ быстро       BACKSPACE  1x
--   PgUp PgDn   прыжок к предыдущей / следующей метке (метки видны засечками на полосе прокрутки)
--   a           сменить активность (запоминается в CSV папки)   F1 подсказка   F11 полный экран
--   L           переключить оригинал ↔ прокси (LRV/LRF) для текущего и следующих файлов
--
-- Статус (файл, оценка, метки, скорость ≠ 1x) постоянно виден в правом верхнем углу.
-- Место в файле запоминается при переходе на другой файл и при выходе (продолжение с того же места).
-- В конце каждого файла пауза и напоминание про оценку.
-- Уход с файла без ENTER: оценка сохраняется как file_partial, незакрытый клип отбрасывается.
-- При выходе из mpv CSV сортируется: по файлу, внутри файла — по времени.
--
-- Колонки CSV:
--   logged_at;event;activity;file;src;time_ms;end_ms;time_hms;label;rating;note;
--   speed_pct;duration_ms;duration_hms;camera_time
-- event: mark (точка) | clip (отрезок i/o) | note (заметка; без time_ms — заметка к файлу)
--        file_done (просмотрен полностью + оценка) | file_partial (оценка без ENTER)

local mp      = require 'mp'
local utils   = require 'mp.utils'
local options = require 'mp.options'
local has_input, input = pcall(require, 'mp.input')

local o = {
    csv_name    = "_triage.csv",
    activity    = "spearfishing",
    reaction_s  = 0.5,     -- поправка на реакцию (сек реального времени × скорость; на паузе не применяется)
    fast_speed  = 4,       -- скорость для TAB
    prefer_proxy = false,  -- true: вместо MP4 открывать лёгкий прокси (GoPro .LRV / DJI .LRF), если он есть рядом
    prefer_lrv  = false,   -- устаревшее имя prefer_proxy (v3.0), работает так же
    skip_done   = true,    -- в плейлисте пропускать уже просмотренные файлы
    skip_exts   = "thm,jpg,jpeg,gpr,dng,wav,csv,txt,md,ini,srt,xml",
    osd_s       = 1.5,
    status_bar  = true,    -- постоянный статус в правом верхнем углу
    show_marks  = true,    -- при просмотре всплывают ранее поставленные метки
    sort_on_exit = true,   -- при выходе сортировать CSV по файлу и времени
}
options.read_options(o, "camera_triage")
local use_proxy = o.prefer_proxy or o.prefer_lrv     -- текущий режим; клавиша L переключает
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
    "i / o  начало / конец клипа     n заметка к моменту   N к файлу",
    "0-3 оценка файла     ENTER готово → следующий     u отменить",
    "+ - скорость   TAB 1x↔быстро   BACKSPACE 1x   a активность",
    "PgUp/PgDn  к предыдущей / следующей метке",
    "Пауза ПРОБЕЛ   Кадр . ,   Перемотка стрелки   < > файлы",
    "L  оригинал ↔ прокси низкого разрешения (LRV / LRF)",
    "F1 скрыть подсказку   F11 полный экран",
}, "\n")

local skip_set = {}
for e in o.skip_exts:gmatch("[^,%s]+") do skip_set[e:lower()] = true end

local activity = o.activity
local activity_dir = nil   -- папка, для которой активность уже выбрана
local undone_note = nil    -- текст последней отменённой заметки (подставится в следующую)
local cur = nil        -- текущий файл
local index = {}       -- [dir] = { done, rating, marks (кол-во), points (список) }; ключ — имя MP4 в верхнем регистре
local written = {}     -- стек записей этой сессии для undo: { path = ..., dir = ... }
local touched = {}     -- CSV, в которые писали в этой сессии (для сортировки при выходе)

---------------------------------------------------------------- helpers

local refresh_ui            -- определяется ниже (оверлей статуса и подсказки)
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

-- Прокси → оригинал:
--   GoPro: GL010123.LRV → GX010123.MP4 / GH010123.MP4
--   DJI и прочие: DJI_20261018_0001_D.LRF → DJI_20261018_0001_D.MP4 (то же имя)
local function main_file_for(dir, fname)
    local up = fname:upper()
    local ch, num = up:match("^GL(%d%d)(%d%d%d%d)%.LRV$")
    if ch then
        for _, p in ipairs({ "GX", "GH" }) do
            local cand = p .. ch .. num .. ".MP4"
            if exists(dir, cand) then return cand end
        end
        return "GX" .. ch .. num .. ".MP4"   -- лучшая догадка, если MP4 рядом нет
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

-- Оригинал → прокси, если он лежит рядом (nil — прокси нет):
--   GoPro: GX010123.MP4 → GL010123.LRV;  DJI и прочие: <имя>.MP4 → <имя>.LRF / <имя>.LRV
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

-- "2026-09-18T09:55:29.000000Z" → "2026-09-18 09:55:29". GoPro пишет сюда местное время часов камеры
-- (несмотря на "Z"); другие камеры могут писать UTC — значение сохраняется как есть, без пересчёта.
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

-- nil = файла нет; true = наш формат; false = чужой/старый формат
local function header_state(path)
    local first = read_first_line(path)
    if first == nil or first == "" then return nil end   -- нет файла / пустой файл → запишем шапку
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
                -- активность папки = активность самой поздней записи (по logged_at, не по порядку строк)
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
                                          ev = ev, label = label }
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
        osd("⚠ _triage.csv в другом формате (старая версия скрипта или\n" ..
            "пересохранён в OnlyOffice/Excel). Запись остановлена.\n" ..
            "Переименуй или удали этот CSV.", 8)
        return false
    end
    local f, err = io.open(path, "ab")
    if not f then
        osd("⚠ Не могу записать CSV:\n" .. tostring(err) .. "\nФайл открыт в Excel?", 6)
        return false
    end
    if hs == nil then
        local sz = f:seek("end")
        if sz and sz > 0 then f:write("\n") end         -- пустая строка/мусор без шапки
        f:write(BOM .. HEADER_LINE .. "\n")
    end
    local out = {}
    for i = 1, #HEADER do out[i] = csv_field(fields[HEADER[i]]) end
    f:write(table.concat(out, ";") .. "\n")
    f:close()
    written[#written + 1] = { path = path, dir = cur.dir }
    touched[path] = true
    return true
end

local function base_row(event, with_speed)
    return {
        logged_at = os.date("%Y-%m-%d %H:%M:%S"),
        event     = event,
        activity  = activity,
        file      = cur.file,
        src       = cur.src,
        -- скорость важна только для событий с временем (точность метки)
        speed_pct = with_speed and math.floor(mp.get_property_number("speed", 1) * 100 + 0.5) or nil,
    }
end

-- Сортировка CSV: по файлу; внутри файла — события со временем (по времени), заметки к файлу,
-- затем file_partial / file_done. При равенстве — порядок записи. Пишем во временный файл и подменяем.
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
                rows[#rows + 1] = { line = line, file = (r[col.file] or ""):upper(),
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
        mp.msg.error("camera_triage: не удалось заменить " .. path .. ", данные в " .. tmp)
    end
end

---------------------------------------------------------------- marks on timeline

-- Метки текущего файла → «главы» mpv: засечки на полосе прокрутки, PgUp/PgDn прыгают по ним
local function update_chapters()
    if not cur then return end
    local pts = (index[cur.dir].points or {})[cur.key] or {}
    local ch = {}
    for _, p in ipairs(pts) do
        if p.ev == "clip" then
            ch[#ch + 1] = { time = p.t, title = "▶ клип " .. p.label }
            if p.e then ch[#ch + 1] = { time = p.e, title = "◀ конец клипа" } end
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

---------------------------------------------------------------- UI: статус + подсказка

local ui = mp.create_osd_overlay("ass-events")
local help_on = false

local function ass_escape(t)
    return (t:gsub("\\", "\\\239\187\160"):gsub("{", "\\{"):gsub("}", "\\}"):gsub("\n", "\\N"))
end

local count_done              -- определяется ниже (сколько файлов плейлиста просмотрено)

local function status_lines()
    if not cur then return {} end
    local ix = index[cur.dir] or {}
    local pos = mp.get_property_number("playlist-pos", 0) + 1
    local count = mp.get_property_number("playlist-count", 1)
    local done = ix.done and ix.done[cur.key]
    local lines = {
        string.format("[%d/%d]  %s%s", pos, count, cur.file,
            PROXY_EXT[cur.src] and ("  [" .. cur.src:upper() .. " — прокси]") or ""),
        (done and "✓ просмотрен" or "не просмотрен") ..
            "   оценка " .. (cur.rating and tostring(cur.rating) or "—") ..
            "   меток " .. ((ix.marks and ix.marks[cur.key]) or 0) ..
            "   заметок " .. ((ix.notes and ix.notes[cur.key]) or 0),
        activity .. "   просмотрено " .. count_done() .. " из " .. count,
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
            extra[#extra + 1] = "{\\fs20\\1c&H5050FF&}" .. ass_escape("● клип открыт с " .. hms(cur.in_t) .. "  (o / u)")
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
    if not cur then osd("Нет загруженного файла"); return false end
    return true
end

-- время метки: при воспроизведении вычитаем реакцию × скорость, на паузе — точно
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
    osd("▶ IN  " .. hms(t) .. "     o — конец клипа,  u — отменить")
end

local function close_clip(t)
    local row = base_row("clip", true)
    row.time_ms, row.end_ms, row.time_hms, row.label = ms(cur.in_t), ms(t), hms(cur.in_t), cur.in_label
    if not append_row(row) then return false end
    local msg = "■ Клип " .. hms(cur.in_t) .. " – " .. hms(t) .. (cur.in_label and ("  " .. cur.in_label) or "")
    cur.in_t, cur.in_label = nil, nil
    reload_marks()
    osd(msg)
    return true
end

local function set_out()
    if not need_file() then return end
    if not cur.in_t then osd("Сначала начало клипа: i"); return end
    local t = mark_time()
    if not t then return end
    if t <= cur.in_t then osd("Конец раньше начала — клип не записан", 2.5); return end
    close_clip(t)
end

local function set_rating(n)
    if not need_file() then return end
    cur.rating = n
    osd("Оценка файла: " .. n .. "  " .. string.rep("★", n) .. "     ENTER — готово")
end

local function ask_text(prompt, on_text, default_text)
    if not has_input then
        osd("Для заметок нужна mpv 0.38 или новее", 4)
        return
    end
    local was_paused = mp.get_property_bool("pause")
    mp.set_property_bool("pause", true)
    local function restore()
        if not was_paused then mp.set_property_bool("pause", false) end
    end
    local submitted = false
    input.get({
        prompt = prompt .. " ",
        default_text = default_text,
        cursor_position = default_text and (#default_text + 1) or nil,
        submit = function(text)
            submitted = true
            input.terminate()
            restore()
            text = (text or ""):gsub("[\r\n]+", " ")
            if text:match("%S") then
                on_text(text)
            else
                osd("✎ Пустая заметка — ничего не записано")
            end
        end,
        closed = function()
            restore()
            if not submitted then osd("✎ Заметка отменена — ничего не записано") end
        end,
    })
end

local function note_moment()
    if not need_file() then return end
    local t = mark_time() or 0
    local file_key = cur.key
    local prefill = undone_note
    ask_text("Заметка @ " .. hms(t) .. ":", function(text)
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
    ask_text("Заметка к файлу:", function(text)
        undone_note = nil
        if not (cur and cur.key == file_key) then return end
        local row = base_row("note")      -- без time_ms = заметка ко всему файлу
        row.note = text
        if append_row(row) then reload_marks(); osd("✎ (файл) " .. text) end
    end, prefill)
end

local undo_armed = nil      -- { line, at }: ждём подтверждения отмены
local function undo()
    if cur and cur.in_t then
        local t = cur.in_t
        cur.in_t, cur.in_label = nil, nil
        osd("↶ Начало клипа " .. hms(t) .. " отменено")
        return
    end
    local last_w = written[#written]
    if not last_w then osd("Нечего отменять (в этой сессии)"); return end
    local f = io.open(last_w.path, "rb")
    if not f then osd("⚠ CSV не найден"); return end
    local data = f:read("*a")
    f:close()
    local body = data:gsub("\r?\n$", "")
    local last = body:match("([^\n]*)$") or ""
    local rest = body:sub(1, #body - #last)
    -- подтверждение: первое u только показывает, что будет отменено
    local r0 = parse_csv_line(last)
    local what = (r0[2] or "") .. " " .. ((r0[2] == "note") and (r0[11] or "") or (r0[9] or "")) ..
                 " " .. (r0[10] or "") .. " " .. (r0[8] or "")
    if (r0[4] or ""):upper() ~= (cur and cur.key or "") then what = what .. "   (файл " .. (r0[4] or "") .. ")" end
    local now = mp.get_time()
    if not (undo_armed and undo_armed.line == last and now - undo_armed.at < 4) then
        undo_armed = { line = last, at = now }
        osd("u ещё раз — отменить:  " .. what, 4)
        return
    end
    undo_armed = nil
    local w, err = io.open(last_w.path, "wb")
    if not w then osd("⚠ Не могу записать CSV: " .. tostring(err), 5); return end
    w:write(rest)
    w:close()
    table.remove(written)
    load_index(last_w.dir, true)
    local r = parse_csv_line(last)
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
        osd("↶ Отменено: " .. what .. "\nn / N в новом месте — текст подставится", 4)
    else
        osd("↶ Отменено: " .. what)
    end
end

-- просмотрен ли файл записи плейлиста
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

-- индекс следующего непросмотренного файла после pos (по кругу), nil — таких нет
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
        osd("Сначала оценка 0–3, потом ENTER", 2.5)
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
        -- уже просмотрен и оценка не менялась — повторная строка file_done не нужна
        cur.finished = true
        msg = "✓ Уже просмотрен (оценка " .. cur.rating .. ")"
    else
        local row = base_row("file_done")
        row.rating, row.duration_ms, row.duration_hms, row.camera_time =
            cur.rating, cur.duration_ms, cur.duration_hms, cur.camera_time
        if not append_row(row) then return end
        cur.finished = true
        cur.saved_rating = cur.rating
        ix.done[cur.key] = true
        ix.rating[cur.key] = tostring(cur.rating)
        msg = "✓ Готово (оценка " .. cur.rating .. ")"
    end
    local pos = mp.get_property_number("playlist-pos", 0)
    local nxt = find_undone(pos)
    if nxt then
        osd(msg .. " → следующий непросмотренный")
        mp.commandv("playlist-play-index", tostring(nxt))
    else
        osd(msg .. ((mp.get_property_number("playlist-count", 1) > 1)
            and ".  Все файлы в списке просмотрены." or "."), 5)
    end
end

local function cycle_activity()
    local i = 1
    for k, a in ipairs(ACTIVITIES) do
        if a == activity then i = k end
    end
    activity = ACTIVITIES[i % #ACTIVITIES + 1]
    osd("Активность: " .. activity)
end

local function set_speed(s)
    s = math.max(0.25, math.min(16, s))
    mp.set_property_number("speed", s)
    osd(string.format("Скорость %gx", s))
end

local function toggle_fast()
    local s = mp.get_property_number("speed", 1)
    if math.abs(s - 1) < 0.01 then set_speed(o.fast_speed) else set_speed(1) end
end

---------------------------------------------------------------- playlist / file events

-- Что делать с записью плейлиста: "junk" (не видео / прокси, чей оригинал тоже в списке),
-- "done" (уже просмотрен), nil (оставить).
-- in_list — множество полных путей плейлиста (верхний регистр).
local function entry_kind(p, in_list)
    local path, dir, fname, ext = split(p)
    local info = utils.file_info(path)
    if not info or info.is_dir then return nil end          -- папки mpv раскроет сам
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

local pl_sig = nil          -- плейлист, который уже почищен
local start_msg = nil       -- сообщение при открытии папки (сколько уже просмотрено)
local skip_current = false  -- текущий файл надо пропустить (уже просмотрен / не видео)
local skip_to = nil         -- куда перейти вместо текущего
local open_src = nil        -- "lrv"/"lrf", если вместо MP4 открыт прокси
local resume_after_reload = nil  -- переоткрытие по L: { key, t, pause, in_t, in_label }

-- Плейлист чистится ОДИН раз, до открытия файла (а не удалением текущей записи на лету).
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
        -- 1) убираем только мусор (фото, THM, прокси при наличии оригинала); просмотренные остаются
        local cur_i = mp.get_property_number("playlist-pos", 0)
        local cur_junk = false
        for i = #pl - 1, 0, -1 do                            -- с конца, чтобы индексы не съезжали
            if entry_kind(pl[i + 1].filename, in_list) == "junk" then
                if i == cur_i then cur_junk = true else mp.commandv("playlist-remove", tostring(i)) end
            end
        end
        local left = mp.get_property_native("playlist") or {}
        -- если текущий сам мусор, его уберём при следующем открытии (текущий удалять нельзя)
        pl_sig = (not cur_junk) and playlist_sig(left) or nil
        cur_i = mp.get_property_number("playlist-pos", 0)
        -- 2) начинаем с первого НЕпросмотренного файла
        local first_undone, ndone, nvideo = nil, 0, 0
        for i, e in ipairs(left) do
            if entry_kind(e.filename, in_list) ~= "junk" then
                nvideo = nvideo + 1
                if entry_done(e.filename) then ndone = ndone + 1
                elseif not first_undone then first_undone = i - 1 end
            end
        end
        if ndone > 0 then
            start_msg = "Просмотрено " .. ndone .. " из " .. nvideo ..
                (first_undone and " — начинаю с первого непросмотренного" or " — все файлы, повторный просмотр")
        end
        local target = nil
        if cur_junk then
            target = first_undone or ((cur_i + 1) % #left)
        elseif o.skip_done and first_undone and first_undone ~= cur_i and entry_done(p) then
            target = first_undone
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
            activity_msg = "Активность из _triage.csv этой папки: " .. activity
        end
    end

    cur = {
        dir = dir, fname = fname, file = file, key = key, src = open_src or ext,
        csv = utils.join_path(dir, o.csv_name),
        rating = tonumber(ix.rating[key] or ""),   -- при повторном просмотре подставляем прежнюю оценку
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
        -- переоткрыт клавишей L: то же место, та же пауза, открытый клип сохраняется
        cur.in_t, cur.in_label = rr.in_t, rr.in_label
        cur.last_t = rr.t
        mp.commandv("seek", tostring(rr.t), "absolute+exact")
        mp.set_property_bool("pause", rr.pause)
        refresh_ui()
        return
    end
    if ix.bad then
        osd("⚠ _triage.csv в этой папке в другом формате — запись будет остановлена.\n" ..
            "Переименуй или удали его.", 8)
    else
        local start = mp.get_property_number("time-pos", 0)
        if activity_msg then
            osd((start_msg and (start_msg .. "\n") or "") .. activity_msg, 4)
            start_msg = nil
        elseif start_msg then
            osd(start_msg, 4)
            start_msg = nil
        elseif start > 1 then
            osd(cur.file .. "   ↻ продолжаю с " .. hms(start), 2.5)
        else
            osd(cur.file, 2)
        end
    end
end)

-- Уход с файла без ENTER (>, <, закрытие mpv): оценку не теряем, пишем file_partial.
mp.register_event("end-file", function()
    local c = cur
    if not c then return end
    if c.finished then cur = nil; return end
    local msgs = {}
    if c.in_t then
        msgs[#msgs + 1] = "незакрытый клип с " .. hms(c.in_t) .. " отброшен"
        c.in_t = nil
    end
    if c.rating ~= nil and c.rating ~= c.saved_rating then
        local row = base_row("file_partial")
        row.rating, row.duration_ms, row.duration_hms, row.camera_time =
            c.rating, c.duration_ms, c.duration_hms, c.camera_time
        if append_row(row) then
            index[c.dir].rating[c.key] = tostring(c.rating)
            msgs[#msgs + 1] = "оценка сохранена как черновик (без ENTER)"
        end
    elseif not (index[c.dir] and index[c.dir].done[c.key]) then
        msgs[#msgs + 1] = "не отмечен как просмотренный"
    end
    cur = nil
    if #msgs > 0 and not resume_after_reload then
        mp.osd_message("⚠ " .. c.file .. ": " .. table.concat(msgs, ", "), 3)
    end
    refresh_ui()
end)

-- Запомнить место в файле при уходе на другой файл (<, >) и при выходе — средствами mpv
-- (watch_later; работает и после перезапуска). Досмотренный / только начатый файл — не запоминаем.
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

-- Не переходить к следующему файлу автоматически: в конце каждого файла пауза и напоминание.
mp.set_property("keep-open", "always")
mp.observe_property("eof-reached", "bool", function(_, eof)
    if not eof or not cur then return end
    local ix = index[cur.dir] or { done = {} }
    if ix.done[cur.key] and cur.rating == cur.saved_rating then
        osd("■ Конец файла (уже просмотрен).   ENTER — к непросмотренному,  > — следующий", 3600)
    else
        osd("■ Конец файла.  Оценка 0–3 → ENTER" ..
            (cur.rating and ("   (сейчас: " .. cur.rating .. ")") or "") ..
            "\n> — следующий без отметки", 3600)
    end
end)

-- Ранее поставленные метки всплывают, когда воспроизведение через них проходит
mp.observe_property("time-pos", "number", function(_, t)
    if not cur or not t then return end
    local last = cur.last_t or t
    cur.last_t = t
    if not o.show_marks then return end
    local step = t - last
    local speed = mp.get_property_number("speed", 1)
    if step <= 0 or step > 1 + speed then return end   -- перемотка/прыжок — не показываем
    local pts = (index[cur.dir].points or {})[cur.key]
    if not pts then return end
    for _, p in ipairs(pts) do
        if p.t > last and p.t <= t then
            local what
            if p.ev == "clip" then
                what = "▶ клип " .. p.label .. "  " .. hms(p.t) .. "–" .. hms(p.e or p.t)
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
-- L: переключить оригинал ↔ прокси. Текущий файл переоткрывается с того же места.
local function toggle_proxy()
    use_proxy = not use_proxy
    local msg = use_proxy and "Режим: ПРОКСИ (низкое разрешение, LRV/LRF)" or "Режим: ОРИГИНАЛ (полное разрешение)"
    if not cur then osd(msg, 3); return end
    local has_px = proxy_for(cur.dir, cur.file) ~= nil
    if use_proxy and not has_px then
        osd(msg .. "\nУ этого файла прокси нет — остаётся оригинал", 4)
        return
    end
    local t = mp.get_property_number("time-pos", 0)
    local was_paused = mp.get_property_bool("pause")
    resume_after_reload = { key = cur.key, t = t, pause = was_paused,
                            in_t = cur.in_t, in_label = cur.in_label }
    cur.in_t, cur.in_label = nil, nil       -- не считать «брошенным» при переоткрытии
    osd(msg, 3)
    mp.commandv("playlist-play-index", tostring(mp.get_property_number("playlist-pos", 0)))
end
bind("L", "proxy-toggle", toggle_proxy)

bind("F1", "help", function() help_on = not help_on; refresh_ui() end)
bind("F11", "fullscreen", function() mp.command("cycle fullscreen") end)

mp.msg.info("camera_triage v3.0: CSV = <папка видео>/" .. o.csv_name)
