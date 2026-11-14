# =============================================================================
# MrFaws Display Manager
# =============================================================================
# A GTK4 / libadwaita display settings app for Hyprland, built for Arch Linux
# with a Dracula theme: laptop brightness, resolution, refresh rate, scale,
# rotation, mirroring and a drag-and-drop arrangement of the monitors.
#
# Monitor changes are applied live with `hyprctl eval 'hl.monitor{...}'`
# (Hyprland 0.56 Lua config, `hyprctl keyword` no longer works) and, once
# confirmed, saved to ~/.config/hypr/monitors.lua, which hyprland.lua loads.
# Brightness goes through systemd-logind over D-Bus, so no root is needed.
#
# Module overview (see Scripts/README.md for details):
#   app.py        Application object, startup and single-instance handling
#   theme.py      Dracula colour palette and CSS
#   hypr.py       hyprctl wrapper: monitor model, apply, live hotplug events
#   store.py      Writes the saved layout to ~/.config/hypr/monitors.lua
#   backlight.py  Laptop backlight via logind (also used by the CLI)
#   layout.py     Drag-and-drop monitor arrangement widget
#   ui.py         Main window
# =============================================================================

APP_ID = "dk.mrfaws.DisplayManager"
APP_NAME = "MrFaws Display Manager"
APP_VERSION = "1.0.0"
