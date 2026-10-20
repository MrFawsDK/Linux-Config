# MrFaws Network Manager

A Dracula-themed GTK4 / libadwaita front end for NetworkManager, built for
Arch Linux + Hyprland. Version 2 replaces the old nmcli-based Wi-Fi Manager.

All network operations go through **libnm** (NetworkManager's own client
library) over D-Bus. The app never edits files in
`/etc/NetworkManager/system-connections` and never calls `nmcli`.

## Features

| Page | What it does |
|---|---|
| Overview | Current connection, IP/gateway/DNS, Wi-Fi on/off switch, NetworkManager status |
| Wi-Fi | Scan, connect/disconnect, password prompt, hidden networks, enterprise (802.1X) networks |
| Saved Networks | Every saved profile: search, connect, edit, forget, add Wi-Fi/Ethernet profiles |
| Ethernet & VPN | Wired adapters and profiles, VPN/WireGuard profiles, installed VPN plugins |
| Diagnostics | Connectivity/DNS/HTTPS checks, service state, adapter details, activity log |

The connection editor has General, Security (Wi-Fi), IPv4 and IPv6 tabs and
supports WPA/WPA2, WPA3, Enhanced Open, and Enterprise with PEAP, TTLS and TLS.

## Requirements

```bash
sudo pacman -S --needed networkmanager python python-gobject gtk4 libadwaita
```

- libadwaita **1.7 or newer** (the editor uses `Adw.InlineViewSwitcher`).
- Optional: `network-manager-applet` (nm-applet) as secret agent / keyring storage,
  and `networkmanager-openvpn` etc. for classic VPN profiles. WireGuard is built in.
- A polkit agent must run in the Hyprland session, so NetworkManager can ask
  for authorisation when needed (e.g. `polkit-kde-agent` or `hyprpolkitagent`).

## Running

```bash
python3 ~/Config/Scripts/wifi-gui.py
```

Starting it again while it is open just raises the existing window
(single instance via the D-Bus name `dk.mrfaws.NetworkManager`).

Keyboard shortcuts: `F5` / `Ctrl+R` refresh, `Ctrl+W` close window, `Ctrl+Q` quit.

### Launcher

`mrfaws-wifi.desktop` is the application launcher. Install it with:

```bash
cp ~/Config/Scripts/mrfaws-wifi.desktop ~/.local/share/applications/
```

`StartupWMClass=dk.mrfaws.NetworkManager` matches the app's Wayland app_id,
so Hyprland links the window to the launcher entry. The `Exec` line uses the
absolute path `/home/faws/Config/Scripts/wifi-gui.py`; change it if the repo
lives somewhere else.

## Layout

```text
Scripts/
├── wifi-gui.py              Entry point (what the launcher runs)
├── mrfaws-wifi.desktop      Application launcher
├── network_manager/         V2 package
│   ├── __init__.py          App ID, name and version
│   ├── app.py               Adw.Application, startup, single instance
│   ├── theme.py             Dracula palette and CSS
│   ├── backend.py           NM.Client wrapper, status, diagnostics, activity log
│   ├── wifi.py              Scanning, network grouping, quick-connect
│   ├── connections.py       Saved profiles: list/activate/update/add/delete
│   ├── editor.py            Connection editor dialog
│   └── ui.py                Main window and sidebar pages
└── legacy/
    └── wifi_gui_v1.py       Archived V1 (GTK3 + nmcli), not used by V2
```

## Security and safety

- Passwords are sent to NetworkManager inside the D-Bus message, never on a
  command line, and are never written to the activity log.
- Password fields start empty; empty means "keep the saved password". Saved
  secrets are never read back into the UI.
- Editing starts from a clone of the stored profile and only changes the
  properties the editor shows, so other settings (e.g. school-specific
  802.1X options) are kept.
- Profiles are identified by UUID, so renamed profiles keep working.
- For enterprise networks, server certificate validation is only turned off
  when you explicitly choose "No CA certificate is required", and a warning
  stays visible while it is off.
- The Diagnostics DNS/HTTPS checks contact `archlinux.org`, and only when you
  press Run.

## V1 (legacy)

V1 is archived unchanged in `legacy/wifi_gui_v1.py` (also in Git history,
commit `53d8d11`). It is not imported by V2. Reasons for replacing it are
listed in the header of that file.
