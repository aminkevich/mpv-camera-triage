-- Fake Resolve for test_resolve_markers.py: runs tools/resolve_markers.lua (with the real
-- resolve_markers.py behind it) and prints the markers that ended up on each clip.
-- Environment: CT_TOOLS, CT_PYTHON, CT_DIR (folder with _triage.csv), CT_MODES ("timeline,remove").
local tools, python, dir = os.getenv("CT_TOOLS"), os.getenv("CT_PYTHON"), os.getenv("CT_DIR")

local function clip(name, fps, frames)
    local c = { markers = {}, path = dir .. "\\" .. name, name = name }
    function c:GetName() return self.name end
    function c:GetClipProperty(key)
        if key == "File Path" then return self.path end
        if key == "FPS" then return fps end
        if key == "Frames" then return tostring(frames) end
    end
    function c:GetMediaPoolItem() return self end
    function c:GetMarkers()
        local t = {}
        for k, v in pairs(self.markers) do t[k] = v end
        return t
    end
    function c:AddMarker(frame, color, name, note, duration, custom)
        if self.markers[frame] then return false end
        self.markers[frame] = { color = color, name = name, note = note, duration = duration, customData = custom }
        return true
    end
    function c:DeleteMarkerAtFrame(frame)
        if not self.markers[frame] then return false end
        self.markers[frame] = nil
        return true
    end
    return c
end

local gopro = clip("GX010123.MP4", 59.94, 3600)
local proxy = clip("DJI_0001.LRF", 30, 900)
local other = clip("C0001.MP4", 25, 500)
local title = { GetName = function() return "Title" end, GetMediaPoolItem = function() return nil end }
gopro:AddMarker(100, "Green", "my own", "", 1, nil)              -- placed by hand: must stay

local timeline = {
    GetName = function() return "Selects Turkey" end,
    GetTrackCount = function(_, kind) return kind == "video" and 2 or 0 end,
    GetItemListInTrack = function(_, _, i)
        if i == 1 then return { [1] = gopro, [2] = other, [3] = title } end
        return { [1] = proxy }
    end,
}
local root = { GetClipList = function() return { [1] = gopro, [2] = proxy } end,
               GetSubFolderList = function() return {} end }
local project = { GetCurrentTimeline = function() return timeline end,
                  GetMediaPool = function() return { GetRootFolder = function() return root end } end }
local resolve = { GetProjectManager = function() return { GetCurrentProject = function() return project end } end }

local M = dofile(tools .. "\\resolve_markers.lua")
local launcher = os.getenv("CT_LAUNCHER")          -- a menu launcher made by install.ps1
for mode in os.getenv("CT_MODES"):gmatch("[^,]+") do
    if launcher then
        _G.resolve = resolve                        -- as Resolve defines it for menu scripts
        dofile(launcher)
    else
        M.run(resolve, mode, tools, python)
    end
    for _, c in ipairs({ gopro, proxy, other }) do
        local frames = {}
        for f in pairs(c.markers) do frames[#frames + 1] = f end
        table.sort(frames)
        for _, f in ipairs(frames) do
            local m = c.markers[f]
            print(table.concat({ "MARKER", mode, c.name, f, m.color, m.name, m.duration, m.note or "" }, "|"))
        end
    end
end
