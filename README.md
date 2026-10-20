# MrFaws Config

My personal Arch Linux + Hyprland configurations.

## Overview

All config files live directly in this folder, named after the application they belong to. There are two subfolders: `Wallpapers/` holds image assets, and `Scripts/` holds my own scripts (see [Scripts/README.md](Scripts/README.md)).

```text
Config/
├── bashrc                       Bash: aliases, editor, PATH, Starship init
├── bash_profile                 Bash: login shell (sources ~/.bashrc)
├── bash_logout                  Bash: logout
├── dolphinrc                    Dolphin file manager settings
├── gitconfig                    Git user name and email
├── hyprland.lua                 Hyprland: monitors, autostart, Dracula borders, input (dk), keybinds
├── hyprlock.conf                Hyprlock: purple "secure session" lock screen
├── kitty.conf                   Kitty terminal
├── kitty-dracula.conf           Kitty Dracula color theme (included by kitty.conf)
├── nanorc                       Nano: line numbers, syntax highlighting, soft wrap
├── starship.toml                Starship: Dracula-palette powerline prompt
├── vscode-settings.json         VS Code: Dracula theme and editor preferences
├── waybar-config.jsonc          Waybar main config
├── waybar-modules.jsonc         Waybar module definitions
├── waybar-modules-custom.jsonc  Waybar custom module definitions
├── waybar-style.css             Waybar stylesheet
├── Wallpapers/
│   └── mrfaws-purple-hacker.png Wallpaper used by Hyprlock
├── Scripts/
│   ├── wifi-gui.py              MrFaws Network Manager (GTK4 NetworkManager GUI)
│   ├── install.sh               Installs the `mrfaws-network` command and launcher
│   ├── mrfaws-wifi.desktop      Launcher for the Network Manager
│   ├── network_manager/         Network Manager source package
│   └── legacy/                  Archived V1 Wi-Fi Manager
└── README.md
```

## Original paths

Some files were renamed here so the folder is clear. When restoring, copy them back under the **original name**, because other files refer to them by that name.

| File here | Original path |
|---|---|
| `bashrc` | `~/.bashrc` |
| `bash_profile` | `~/.bash_profile` |
| `bash_logout` | `~/.bash_logout` |
| `dolphinrc` | `~/.config/dolphinrc` |
| `gitconfig` | `~/.gitconfig` |
| `hyprland.lua` | `~/.config/hypr/hyprland.lua` |
| `hyprlock.conf` | `~/.config/hypr/hyprlock.conf` |
| `kitty.conf` | `~/.config/kitty/kitty.conf` |
| `kitty-dracula.conf` | `~/.config/kitty/dracula.conf` |
| `nanorc` | `~/.nanorc` |
| `starship.toml` | `~/.config/starship.toml` |
| `vscode-settings.json` | `~/.config/Code/User/settings.json` |
| `waybar-config.jsonc` | `~/.config/waybar/config.jsonc` |
| `waybar-modules.jsonc` | `~/.config/waybar/Modules` |
| `waybar-modules-custom.jsonc` | `~/.config/waybar/ModulesCustom` |
| `waybar-style.css` | `~/.config/waybar/style.css` |
| `Wallpapers/mrfaws-purple-hacker.png` | `~/.config/hypr/wallpapers/mrfaws-purple-hacker.png` |
| `Scripts/mrfaws-wifi.desktop` | `~/.local/share/applications/mrfaws-wifi.desktop` (installed by `Scripts/install.sh`) |

## Requirements

Main packages used by these configs: `hyprland`, `hyprlock`, `waybar`, `kitty`, `starship`, `swaync`, `network-manager-applet`, `rofi`, `dolphin`, `grim`, `slurp`, `wl-clipboard`, `brightnessctl`, `playerctl`, `pavucontrol`, `ttf-jetbrains-mono-nerd`.

The Network Manager script additionally needs `networkmanager`, `python-gobject`, `gtk4` and `libadwaita` (1.7+).

## Restoring

Back up any existing files first, then run:

```bash
mkdir -p ~/.config/hypr/wallpapers ~/.config/waybar ~/.config/kitty ~/.config/Code/User ~/.local/share/applications

cp bashrc                      ~/.bashrc
cp bash_profile                ~/.bash_profile
cp bash_logout                 ~/.bash_logout
cp nanorc                      ~/.nanorc
cp gitconfig                   ~/.gitconfig
cp dolphinrc                   ~/.config/dolphinrc
cp starship.toml               ~/.config/starship.toml
cp hyprland.lua                ~/.config/hypr/hyprland.lua
cp hyprlock.conf               ~/.config/hypr/hyprlock.conf
cp Wallpapers/*.png            ~/.config/hypr/wallpapers/
cp kitty.conf                  ~/.config/kitty/kitty.conf
cp kitty-dracula.conf          ~/.config/kitty/dracula.conf
cp vscode-settings.json        ~/.config/Code/User/settings.json
cp waybar-config.jsonc         ~/.config/waybar/config.jsonc
cp waybar-modules.jsonc        ~/.config/waybar/Modules
cp waybar-modules-custom.jsonc ~/.config/waybar/ModulesCustom
cp waybar-style.css            ~/.config/waybar/style.css
Scripts/install.sh            # mrfaws-network command + launcher
```

Notes:
- `kitty.conf` contains `include dracula.conf`, so the theme must be installed as `dracula.conf`.
- `waybar-config.jsonc` includes `$HOME/.config/waybar/Modules` and `ModulesCustom`, so those files must keep their original names when installed.
- `hyprlock.conf` loads the wallpaper from `/home/faws/.config/hypr/wallpapers/`. Change that path if your username is different.
- Edit `gitconfig` with your own name and email before using it.
- `Scripts/` is run in place: `Scripts/install.sh` points the `mrfaws-network` command and the launcher at the repo's current location. Re-run it if you move the repo.
- Reload Hyprland with `hyprctl reload`, and restart Waybar to apply changes.
