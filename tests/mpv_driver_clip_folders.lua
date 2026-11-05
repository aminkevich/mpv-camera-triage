-- Test "user" for test_mpv_lua.py (ClipUndoAndSubfoldersTest): u inside an open clip,
-- then walking a folder with subfolders using > (playlist-next).
local mp = require 'mp'
local csv = mp.get_opt("driver-csv")
local BS = string.char(92)
local function rel(p) return (p:gsub("^.*Trip[/" .. BS .. "]", ""):gsub(BS, "/")) end

local function dump(tag)
    local f = io.open(csv, "rb")
    local data = f and f:read("*a") or ""
    if f then f:close() end
    for line in data:gmatch("[^\n]+") do
        if not line:find("logged_at", 1, true) then mp.msg.info("CSV " .. tag .. " " .. line) end
    end
    mp.msg.info("CSV " .. tag .. " END")
end

local function key(k) return function() mp.commandv("keypress", k) end end
local function seek(t) return function() mp.commandv("seek", tostring(t), "absolute+exact") end end

local steps = {
    seek(1), key("i"), seek(2), key("f"), seek(3), key("s"),   -- clip open from 1 s, marks fish and shot
    key("u"), key("u"),                                         -- undoes shot, not the clip start
    function() dump("after_undo") end,
    seek(4), key("o"),                                          -- the clip is still open: close it
    function() dump("after_close") end,
}
for _ = 1, 8 do steps[#steps + 1] = function() mp.commandv("playlist-next", "force") end end
steps[#steps + 1] = function() mp.commandv("quit") end

mp.register_event("file-loaded", function()
    mp.msg.info("PLAYING " .. rel(mp.get_property("path") or ""))
end)

local started = false
mp.register_event("file-loaded", function()
    if started then return end
    started = true
    local pl = {}
    for _, e in ipairs(mp.get_property_native("playlist")) do pl[#pl + 1] = rel(e.filename) end
    mp.msg.info("PLAYLIST " .. table.concat(pl, ","))
    local i = 0
    local function nxt()
        i = i + 1
        if steps[i] then steps[i](); mp.add_timeout(0.4, nxt) end
    end
    mp.add_timeout(0.8, nxt)
end)
