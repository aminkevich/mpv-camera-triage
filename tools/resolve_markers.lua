-- resolve_markers.lua — the part of the Resolve menu scripts that runs inside DaVinci Resolve.
--
-- Resolve runs menu scripts in a sandbox: no file access (io, dofile, require), no way to start
-- programs. So tools/resolve_markers.py reads _triage.csv itself and writes the menu scripts
-- (Workspace → Scripts → camera_triage - …) as: the marker data, then this file, then one call.
-- This file must therefore use only plain Lua and the Resolve API.
--
-- DATA: [file path in upper case, "\" separators] = { {t, duration, color, name, note}, ... } —
-- times in seconds from the start of the file, sorted; duration 0 = a point marker.

local M = {}
local TAG = "camera_triage"       -- customData of our markers: a rerun replaces them, remove deletes them

-- Clip markers on timeline clips count frames from the start of the source file (as in the Media
-- Pool); "item" would count from the first frame used on the timeline instead.
M.TIMELINE_FRAMES = "source"

local function list(t)
    -- Resolve returns lists as tables {[1] = a, [2] = b, ...}, which may also hold other fields
    -- (a number, e.g. a count): keep only the objects under numeric keys, in order
    local pairs_ = {}
    for k, v in pairs(t or {}) do
        if type(k) == "number" and (type(v) == "userdata" or type(v) == "table") then
            pairs_[#pairs_ + 1] = { k, v }
        end
    end
    table.sort(pairs_, function(a, b) return a[1] < b[1] end)
    local out = {}
    for i, kv in ipairs(pairs_) do out[i] = kv[2] end
    return out
end

local function timeline_items(timeline)
    local items = {}
    for i = 1, (timeline:GetTrackCount("video") or 0) do
        for _, item in ipairs(list(timeline:GetItemListInTrack("video", i))) do items[#items + 1] = item end
    end
    return items
end

local function media_pool_clips(folder, out)
    out = out or {}
    for _, clip in ipairs(list(folder:GetClipList())) do out[#out + 1] = clip end
    for _, sub in ipairs(list(folder:GetSubFolderList())) do media_pool_clips(sub, out) end
    return out
end

-- remove the markers this script added; → how many
local function clear(item)
    local n = 0
    for frame, info in pairs(item:GetMarkers() or {}) do
        if type(info) == "table" and info.customData == TAG and item:DeleteMarkerAtFrame(frame) then
            n = n + 1
        end
    end
    return n
end

-- Resolve reports 59.94 / 29.97 / 23.976; the exact rates are 60000/1001 etc.
function M.exact_fps(fps)
    fps = tonumber(fps)
    if not fps or fps <= 0 then return nil end
    for _, n in ipairs({ 24, 30, 48, 60, 120, 240 }) do
        if math.abs(fps - n * 1000 / 1001) < 0.01 then return n * 1000 / 1001 end
    end
    return fps
end

function M.key(path)
    return (tostring(path or ""):gsub("/", "\\"):upper())
end

-- markers of one file → {frame, length, color, name, note}; one marker per frame (Resolve's rule),
-- so a marker on an occupied frame moves to the next one; nothing past the end of the file
function M.plan(markers, fps, frames)
    local out, used = {}, {}
    for _, m in ipairs(markers) do
        local f = math.max(0, math.floor(m[1] * fps + 0.5))
        while used[f] do f = f + 1 end
        if not frames or f < frames then
            used[f] = true
            local length = 1
            if m[2] and m[2] > 0 then length = math.max(1, math.floor(m[2] * fps + 0.5)) end
            if frames then length = math.max(1, math.min(length, frames - f)) end
            out[#out + 1] = { f, length, m[3], m[4], m[5] or "" }
        end
    end
    return out
end

-- put the markers of mp_item's file on item (the clip itself or a timeline clip using it)
local function mark(item, mp_item, data)
    local markers = data[M.key(mp_item:GetClipProperty("File Path"))]
    local fps = M.exact_fps(mp_item:GetClipProperty("FPS"))
    if not markers or not fps then return nil end
    local frames = tonumber(mp_item:GetClipProperty("Frames") or "")
    local shift = 0
    if item ~= mp_item and M.TIMELINE_FRAMES == "item" then shift = math.floor(item:GetLeftOffset() or 0) end
    clear(item)
    local added, failed = 0, 0
    for _, m in ipairs(M.plan(markers, fps, frames)) do
        if item:AddMarker(m[1] - shift, m[3], m[4], m[5], m[2], TAG) then added = added + 1 else failed = failed + 1 end
    end
    return added, failed
end

function M.run(resolve, mode, data, baked)
    local project = resolve:GetProjectManager():GetCurrentProject()
    if not project then print("camera_triage: no project is open"); return end
    local timeline = project:GetCurrentTimeline()
    local pool = {}
    if mode ~= "timeline" then pool = media_pool_clips(project:GetMediaPool():GetRootFolder()) end

    if mode == "remove" then
        local removed = 0
        for _, clip in ipairs(pool) do removed = removed + clear(clip) end
        if timeline then
            for _, item in ipairs(timeline_items(timeline)) do
                if item:GetMediaPoolItem() then removed = removed + clear(item) end   -- not titles, generators
            end
        end
        print("camera_triage: removed " .. removed .. " marker(s) added by this script")
        return
    end

    local targets, where = {}, nil
    if mode == "timeline" then
        if not timeline then print("camera_triage: no timeline is open"); return end
        for _, item in ipairs(timeline_items(timeline)) do
            local mp = item:GetMediaPoolItem()
            if mp then targets[#targets + 1] = { item, mp } end
        end
        where = 'timeline "' .. timeline:GetName() .. '"'
    else
        for _, clip in ipairs(pool) do targets[#targets + 1] = { clip, clip } end
        where = "Media Pool"
    end

    local done, markers, failed, skipped, seen = 0, 0, 0, {}, {}
    for _, t in ipairs(targets) do
        local added, bad = mark(t[1], t[2], data)
        if added then
            done, markers, failed = done + 1, markers + added, failed + bad
        else
            local name = t[1]:GetName()
            if not seen[name] then seen[name] = true; skipped[#skipped + 1] = name end
        end
    end
    print("camera_triage: " .. where .. " — " .. done .. " clip(s) marked, " .. markers .. " marker(s)"
        .. (failed > 0 and (", " .. failed .. " not accepted by Resolve") or "")
        .. "  (markup baked " .. tostring(baked) .. ")")
    if #skipped > 0 then
        table.sort(skipped)
        local shown = {}
        for i = 1, math.min(10, #skipped) do shown[i] = skipped[i] end
        print("  without markup (" .. #skipped .. "): " .. table.concat(shown, ", ")
            .. (#skipped > 10 and (" and " .. (#skipped - 10) .. " more") or "")
            .. " — run py tools\\resolve_markers.py <footage folder> again if they are marked")
    end
end

return M
