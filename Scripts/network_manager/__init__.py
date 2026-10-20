# =============================================================================
# MrFaws Network Manager V2
# =============================================================================
# A GTK4 / libadwaita front end for NetworkManager, built for Arch Linux +
# Hyprland with a Dracula theme. All network operations go through libnm
# (NetworkManager's own client library) over D-Bus; nothing here edits
# configuration files or calls nmcli.
#
# Module overview (see Scripts/README.md for details):
#   app.py          Application object, startup and single-instance handling
#   theme.py        Dracula colour palette and CSS
#   backend.py      NM.Client wrapper, status queries, diagnostics, activity log
#   wifi.py         Wi-Fi scanning, network grouping and quick-connect
#   connections.py  Saved connection profiles (list/activate/edit/delete)
#   editor.py       Connection editor dialog
#   ui.py           Main window and all sidebar pages
# =============================================================================

APP_ID = "dk.mrfaws.NetworkManager"
APP_NAME = "MrFaws Network Manager"
APP_VERSION = "2.0.0"
