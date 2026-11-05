-- resolve_markers.lua — _triage.csv markup → markers ON CLIPS in DaVinci Resolve.
--
-- Runs inside Resolve from Workspace → Scripts → camera_triage … (install.ps1 creates the menu
-- entries). The free Resolve lists only Lua scripts in that menu, so the work is split:
--   1. here: collect the clips (current timeline or the whole Media Pool) with their file path,
--      frame rate and length;
--   2. resolve_markers.py --plan (run once with the Python bundled with Resolve): reads
--      _triage.csv next to each file and answers with the markers — the same ones as the .edl
--      of make_timeline.py, in source frames from the start of the file;
--   3. here: replace this script's markers on each clip with the new ones.
-- Markers placed by hand are not touched: ours carry the custom data "camera_triage".
-- Output goes to Workspace → Console.

local M = {}
local TAG = "camera_triage"

local function list(t)
    -- Resolve returns lists as tables {[1] = a, [2] = b, ...}
    local out = {}
    for _, v in pairs(t or {}) do out[#out + 1] = v end
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

local function split(line)
    local fields = {}
    for f in (line .. "\t"):gmatch("(.-)\t") do fields[#fields + 1] = f end
    return fields
end

local function clean(text)
    return (tostring(text or ""):gsub("[\t\r\n]+", " "))
end

-- ask resolve_markers.py for the markers: request → answer (files in %TEMP%)
local function ask_python(tools, python, request_lines)
    local tmp = os.getenv("TEMP") or os.getenv("TMP") or "."
    local request, answer = tmp .. "\\camera_triage_request.tsv", tmp .. "\\camera_triage_answer.tsv"
    local f = assert(io.open(request, "wb"))
    f:write(table.concat(request_lines, "\n"), "\n")
    f:close()
    os.remove(answer)
    local cmd = string.format('""%s" "%s\\resolve_markers.py" --plan "%s" "%s" 2>&1"', python, tools, request, answer)
    local p = io.popen(cmd)
    local output = p and p:read("*a") or ""
    if p then p:close() end
    if output:match("%S") then print(output) end
    local result = io.open(answer, "rb")
    if not result then
        print("camera_triage: no answer from " .. python .. " — is the Python path in the menu script right?")
        return nil
    end
    local lines = {}
    for line in result:lines() do lines[#lines + 1] = (line:gsub("\r$", "")) end
    result:close()
    os.remove(request)
    os.remove(answer)
    return lines
end

function M.run(resolve, mode, tools, python)
    local project = resolve:GetProjectManager():GetCurrentProject()
    if not project then print("camera_triage: no project is open"); return end
    local timeline = project:GetCurrentTimeline()
    local pool = media_pool_clips(project:GetMediaPool():GetRootFolder())

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
            if mp then targets[#targets + 1] = { item = item, mp = mp } end   -- not titles, generators
        end
        where = 'timeline "' .. timeline:GetName() .. '"'
    else
        for _, clip in ipairs(pool) do targets[#targets + 1] = { item = clip, mp = clip } end
        where = "Media Pool"
    end
    if #targets == 0 then print("camera_triage: " .. where .. " has no clips"); return end

    local request = {}
    for i, t in ipairs(targets) do
        request[#request + 1] = table.concat({ i, clean(t.mp:GetClipProperty("File Path")),
            clean(t.mp:GetClipProperty("FPS")), clean(t.mp:GetClipProperty("Frames")) }, "\t")
    end
    local answer = ask_python(tools, python, request)
    if not answer then return end

    local done, markers, failed, skipped, seen = 0, 0, 0, {}, {}
    for _, line in ipairs(answer) do
        local f = split(line)
        local t = targets[tonumber(f[1]) or 0]
        if t and f[2] == "NONE" then
            local name = t.item:GetName()
            if not seen[name] then seen[name] = true; skipped[#skipped + 1] = name end
        elseif t and f[2] == "MARKUP" then
            clear(t.item)
            done = done + 1
        elseif t and #f >= 6 then
            if t.item:AddMarker(tonumber(f[2]), f[4], f[5], f[6], tonumber(f[3]), TAG) then
                markers = markers + 1
            else
                failed = failed + 1
            end
        end
    end
    print("camera_triage: " .. where .. " — " .. done .. " clip(s) marked, " .. markers .. " marker(s)"
        .. (failed > 0 and (", " .. failed .. " not accepted by Resolve") or ""))
    if #skipped > 0 then
        table.sort(skipped)
        local shown = {}
        for i = 1, math.min(10, #skipped) do shown[i] = skipped[i] end
        print("  without markup (" .. #skipped .. "): " .. table.concat(shown, ", ")
            .. (#skipped > 10 and (" and " .. (#skipped - 10) .. " more") or ""))
    end
end

return M
