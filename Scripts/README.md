# MrFaws Scripts

Dracula-themed GTK4 / libadwaita apps for Arch Linux + Hyprland:

| Command | App | Section |
|---|---|---|
| `network` | MrFaws Network Manager | [below](#mrfaws-network-manager) |
| `sound` | MrFaws Sound Manager | [below](#mrfaws-sound-manager) |
| `screen` | MrFaws Display Manager | [below](#mrfaws-display-manager) |

Install all of them with `~/Config/Scripts/install.sh`.

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

## Install and run

Run the installer once (and again if you move the repo):

```bash
~/Config/Scripts/install.sh
```

It checks the packages, links the commands `network`, `sound` and `screen`
into `~/.local/bin` and installs the app launchers. Then start the app with:

```bash
network
```

or from rofi / the app menu ("MrFaws Network Manager"). Without installing:
`python3 ~/Config/Scripts/wifi-gui.py`.

Starting it again while it is open just raises the existing window
(single instance via the D-Bus name `dk.mrfaws.NetworkManager`).

Keyboard shortcuts: `F5` / `Ctrl+R` refresh, `Ctrl+W` close window, `Ctrl+Q` quit.

### Launcher

`mrfaws-wifi.desktop` is the application launcher; `install.sh` copies it to
`~/.local/share/applications/` and rewrites its `Exec` line to the repo's
real location. `StartupWMClass=dk.mrfaws.NetworkManager` matches the app's
Wayland app_id, so Hyprland links the window to the launcher entry.

## Layout

```text
Scripts/
├── wifi-gui.py              Network Manager entry point
├── sound-gui.py             Sound Manager entry point
├── display-gui.py           Display Manager entry point (+ `screen brightness` CLI)
├── install.sh               Installs the `network` / `sound` / `screen` commands and launchers
├── mrfaws-wifi.desktop      Network Manager launcher
├── mrfaws-sound.desktop     Sound Manager launcher
├── mrfaws-display.desktop   Display Manager launcher
├── sound_manager/           Sound Manager package (see below)
├── display_manager/         Display Manager package (see below)
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

# MrFaws Sound Manager

A Dracula-themed volume control for PipeWire that replaces pavucontrol for
everyday use. Start it with `sound`, from the app menu, or by **clicking the
volume in Waybar** (right-click mutes).

| Page | What it does |
|---|---|
| Output | Big volume slider + mute for the current output; pick Speaker / Headphones / HDMI / Bluetooth with one click |
| Input | Microphone volume + mute, a live level meter to test it, pick a microphone |
| Apps | Every app playing or recording sound: its own volume, mute, and which device it uses |
| Bluetooth | Tænd/sluk, par høretelefoner/højttalere, connect/disconnect, glem enhed. Scanner kun mens siden er åben |
| Sound Cards | Card profile (e.g. Speaker vs Headphones), Restart Audio, open pavucontrol |

Made to be simple:

- Unplugged outputs (empty HDMI ports, headset jack) are folded away under
  "Not Plugged In" instead of cluttering the list.
- Picking a device makes it the default; playing apps follow it. A muted
  device is unmuted when you pick it.
- Volume is capped at 100 %, the same as the volume keys in `hyprland.lua`.
- The mic level meter only records while the Input page is open.
- `Ctrl+M` mutes the output, `F5` refreshes, `Ctrl+W` / `Ctrl+Q` close.

It talks to PipeWire through pipewire-pulse with `pactl` (JSON output) and
updates live from `pactl subscribe`. It never edits PipeWire or WirePlumber
config files. Bluetooth kræver `bluez` + `bluez-utils` og `sudo systemctl enable --now bluetooth`; pairing-agenten kører kun mens du pairer (ellers ville den acceptere alt i nærheden), så enheder med PIN (tastaturer) skal paires med `bluetoothctl`. Requirements: `pipewire-pulse` and `libpulse` (for `pactl` /
`parec`) on top of the GTK packages above.

```text
sound_manager/
├── __init__.py   App ID, name and version
├── app.py        Adw.Application, startup, single instance (dk.mrfaws.SoundManager)
├── theme.py      Dracula palette (shared with network_manager) and CSS
├── backend.py    pactl wrapper: state model, live updates, coalesced volume writes
├── meter.py      Microphone level meter (parec at 8 kHz)
├── bluetooth.py  BlueZ over D-Bus (ingen ekstra Python-pakker), midlertidig pairing-agent
└── ui.py         Main window and the five pages
```

# MrFaws Display Manager

Dracula-themed display settings for Hyprland. Start it with `screen` or from
the app menu ("MrFaws Display Manager").

| Section | What it does |
|---|---|
| Brightness | Big slider + 10/25/50/75/100 % presets for the laptop panel; follows the brightness keys live |
| Arrangement | Drag the monitors into place; they snap edge to edge, no gaps or overlaps |
| Displays | Every connected monitor, including ones that are turned off (shown with 2+ monitors) |
| Selected display | On/off, resolution, refresh rate, scale, rotation, mirror, exact X/Y position |

### Apply, test, keep

Monitor edits are collected first and sent to Hyprland when you press
**Apply**. A dialog then asks *Keep These Display Settings?* and **reverts by
itself after 15 s**, so a mode your monitor can't show fixes itself.
Enter and Escape also mean Revert. **Keep Changes** saves the layout to
`~/.config/hypr/monitors.lua`, which `hyprland.lua` loads at startup:

```lua
-- in hyprland.lua, right after the catch-all hl.monitor rule
do
    local ok, err = pcall(dofile, os.getenv("HOME") .. "/.config/hypr/monitors.lua")
    if not ok and not tostring(err):find("No such file") then
        hl.notification.create({ text = "monitors.lua: " .. tostring(err), timeout = 10000, color = "rgb(ff5555)" })
    end
end
```

The app shows a banner if that line is missing. Delete `monitors.lua` to go
back to automatic setup (`preferred` / `auto` / `auto`).

### Brightness from the command line / keys

```bash
screen brightness              # print current percent
screen brightness 40           # set 40 %
screen brightness +5           # step up (-5 = down)
screen brightness +5 --notify  # same, with an on-screen bubble (swaync)
```

`hyprland.lua` binds `XF86MonBrightnessUp/Down` to `screen brightness ±5 --notify`
(they used to call `brightnessctl`, which isn't installed). This path never
imports GTK, so a key press takes about 70 ms. In the window: `Ctrl+↑/↓` = ±5 %.

Percent uses a squared curve (`EXPONENT` in `backlight.py`): more steps at the
dark end where eyes notice them. Minimum is 1 raw step; it never goes to 0.

### How it works

- **Monitors**: read with `hyprctl -j monitors all`, changed with
  `hyprctl eval 'hl.monitor({...})'`. Hyprland 0.56's Lua config rejects
  `hyprctl keyword`, so eval is the only runtime path.
- **Brightness**: systemd-logind `Session.SetBrightness` over D-Bus (same as
  brightnessctl and `lock-dim`). No root, no udev rule, no `video` group.
- **Hotplug**: listens on Hyprland's event socket (`.socket2.sock`) and
  reloads when a monitor is added or removed.
- **Scales**: only scales giving whole logical pixels are offered (1, 1.2,
  1.25, 4/3, 1.5, 1.6, 5/3, 2 ... for 1920x1200), sent at full precision,
  so Hyprland doesn't "correct" them with a warning.

### Safety and security

- **EDID injection**: monitor names/descriptions come from the monitor's
  EDID, i.e. from whatever is plugged in, and end up inside Lua that
  Hyprland runs. Every string is escaped (`lua_str` in `hypr.py`); a
  description like `x" }) os.execute("...") --` stays a harmless string.
- Turning a display **off is never saved**. A saved "disabled" laptop panel
  would boot to a black screen without an external monitor.
- You can't turn off or mirror away the last display, or create mirror chains.
- `monitors.lua` is validated with `luac -p` and written atomically
  (temp file + rename). The previous version is kept as `monitors.lua.bak`.
- A broken `monitors.lua` can't break the rest of `hyprland.lua`
  (keybinds keep working) thanks to `pcall`; you get a red notification.
- External monitors are saved as `desc:<description>`, so settings follow
  the monitor to another port or dock. Identical monitors fall back to the
  connector name.

```text
display_manager/
├── __init__.py   App ID, name and version
├── app.py        Adw.Application, startup, single instance (dk.mrfaws.DisplayManager)
├── theme.py      Dracula palette (shared with the other apps) and CSS
├── hypr.py       hyprctl wrapper: monitor model, Lua generation/escaping, apply, hotplug events
├── store.py      Writes ~/.config/hypr/monitors.lua (validated, atomic)
├── backlight.py  Laptop backlight via logind + the `screen brightness` CLI
├── layout.py     Drag-and-drop arrangement widget (Gtk.Snapshot, no pycairo needed)
└── ui.py         Main window, apply/confirm/revert
```

Not included yet: external-monitor brightness (needs `ddcutil` + i2c access),
night light (needs `hyprsunset`), VRR and HDR/10-bit settings.
