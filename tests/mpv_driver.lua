-- Test "user" for test_mpv_lua.py: presses camera_triage.lua keys step by step
-- and writes to the mpv log what the test checks (playlist order, CSV, current file).
local mp = require 'mp'
local csv = mp.get_opt("driver-csv")
local BS = string.char(92)

local function base(p) return (p:gsub(".*[/" .. BS .. BS .. "]", "")) end

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
    function()
        local pl = {}
        for _, e in ipairs(mp.get_property_native("playlist")) do pl[#pl + 1] = base(e.filename) end
        mp.msg.info("PLAYLIST " .. table.concat(pl, ","))
        mp.msg.info("PLAYING " .. base(mp.get_property("path")))
    end,
    seek(5.5), key("DEL"), key("DEL"),            -- mark fish@5 from an "earlier session": Del, Del
    function() dump("after_del") end,
    key("u"), key("u"),                           -- restore the deleted row
    function() dump("after_undo") end,
    seek(12), key("s"),                           -- new shot mark while paused — exactly 12 s
    key("2"), key("ENTER"),                       -- rating → next unreviewed
    function() mp.msg.info("PLAYING " .. base(mp.get_property("path") or "")) end,
    function() mp.commandv("quit") end,
}

local started = false
mp.register_event("file-loaded", function()
    if started then return end
    started = true
    local i = 0
    local function nxt()
        i = i + 1
        if steps[i] then steps[i](); mp.add_timeout(0.4, nxt) end
    end
    mp.add_timeout(0.8, nxt)
end)
