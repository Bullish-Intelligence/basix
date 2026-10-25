-- The default Hyprland config of Basix. Hyprland reads it from /etc/xdg/hypr/hyprland.lua
-- when the learner has no hyprland.lua or hyprland.conf in ~/.config/hypr.
local terminal = "foot"
local browser  = "firefox"
local mod      = "SUPER"

hl.monitor({ output = "", mode = "preferred", position = "auto", scale = "auto" })

hl.config({
    general = { gaps_in = 5, gaps_out = 20, border_size = 2, layout = "dwindle" },
    decoration = { rounding = 10 },
    misc = { force_default_wallpaper = 0, disable_hyprland_logo = true },
    input = { kb_layout = "us", follow_mouse = 1 },
})

hl.bind(mod .. " + Q", hl.dsp.exec_cmd(terminal))
hl.bind(mod .. " + B", hl.dsp.exec_cmd(browser))
hl.bind(mod .. " + R", hl.dsp.exec_cmd("noctalia msg panel-toggle launcher"))
hl.bind(mod .. " + C", hl.dsp.window.close())
hl.bind(mod .. " + V", hl.dsp.window.float({ action = "toggle" }))
hl.bind(mod .. " + left",  hl.dsp.focus({ direction = "left" }))
hl.bind(mod .. " + right", hl.dsp.focus({ direction = "right" }))
hl.bind(mod .. " + up",    hl.dsp.focus({ direction = "up" }))
hl.bind(mod .. " + down",  hl.dsp.focus({ direction = "down" }))
for i = 1, 10 do
    local key = i % 10
    hl.bind(mod .. " + " .. key,         hl.dsp.focus({ workspace = i }))
    hl.bind(mod .. " + SHIFT + " .. key, hl.dsp.window.move({ workspace = i }))
end
hl.bind(mod .. " + mouse:272", hl.dsp.window.drag(),   { mouse = true })
hl.bind(mod .. " + mouse:273", hl.dsp.window.resize(), { mouse = true })
