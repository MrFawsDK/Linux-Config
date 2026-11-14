# =============================================================================
# MrFaws Sound Manager
# =============================================================================
# A GTK4 / libadwaita volume control for PipeWire, built for Arch Linux +
# Hyprland with a Dracula theme. It replaces pavucontrol for everyday use:
# pick an output or microphone, set the volume, control each app, and
# switch sound card profiles.
#
# All audio operations go through pipewire-pulse with `pactl` (JSON output),
# and live updates come from `pactl subscribe`. Nothing here edits PipeWire
# or WirePlumber configuration files.
#
# Module overview (see Scripts/README.md for details):
#   app.py      Application object, startup and single-instance handling
#   theme.py    Dracula colour palette and CSS
#   backend.py  pactl wrapper: state model, live updates, volume/default changes
#   meter.py    Microphone level meter (parec reader)
#   ui.py       Main window and all sidebar pages
# =============================================================================

APP_ID = "dk.mrfaws.SoundManager"
APP_NAME = "MrFaws Sound Manager"
APP_VERSION = "1.0.0"
