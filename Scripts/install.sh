#!/usr/bin/env bash
# =============================================================================
# install.sh - make the MrFaws apps easy to start
# =============================================================================
# Run once (and again after moving the repo):  ~/Config/Scripts/install.sh
#
#   * enables the bluetooth service for the Bluetooth page in `sound`
#   * checks the required packages and offers the pacman command if missing
#   * links the commands `network`, `sound` and `screen` into ~/.local/bin (already in PATH)
#   * installs the app launchers (rofi / app menu) with the correct path
#
# Nothing is copied: the command and launcher point at this repo, so edits
# here take effect immediately. Re-running the script is safe.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"

# command : entry script : launcher
APPS=(
    "network:wifi-gui.py:mrfaws-wifi.desktop"
    "sound:sound-gui.py:mrfaws-sound.desktop"
    "screen:display-gui.py:mrfaws-display.desktop"
)

# --- Dependencies -------------------------------------------------------------
missing=()
for pkg in networkmanager libpulse pipewire-pulse bluez bluez-utils python python-gobject gtk4 libadwaita hyprland; do
    pacman -Q "$pkg" &>/dev/null || missing+=("$pkg")
done
if ((${#missing[@]})); then
    echo "Missing packages: ${missing[*]}"
    echo "Install them with:  sudo pacman -S --needed ${missing[*]}"
    exit 1
fi

# --- Bluetooth service (used by the Bluetooth page in `sound`) ----------------
if ! systemctl is-active --quiet bluetooth.service; then
    echo "Enabling bluetooth.service (sudo)..."
    sudo systemctl enable --now bluetooth.service || echo "Could not start bluetooth.service"
fi
command -v rfkill &>/dev/null && rfkill unblock bluetooth || true

# --- Commands in PATH and app launchers --------------------------------------
# The launcher Exec lines are rewritten to this repo's actual location.
mkdir -p "$BIN_DIR" "$APP_DIR"
for app in "${APPS[@]}"; do
    IFS=: read -r command entry desktop <<< "$app"
    chmod +x "$SCRIPT_DIR/$entry"
    ln -sfn "$SCRIPT_DIR/$entry" "$BIN_DIR/$command"
    echo "Command:  $BIN_DIR/$command -> $SCRIPT_DIR/$entry"
    sed "s|^Exec=.*|Exec=/usr/bin/python3 $SCRIPT_DIR/$entry|" \
        "$SCRIPT_DIR/$desktop" > "$APP_DIR/$desktop"
    echo "Launcher: $APP_DIR/$desktop"
done

# Remove the link from before `network` was renamed (only if it is ours).
OLD_LINK="$BIN_DIR/mrfaws-network"
if [[ -L "$OLD_LINK" && "$(readlink -f "$OLD_LINK")" == "$SCRIPT_DIR/wifi-gui.py" ]]; then
    rm "$OLD_LINK"
fi
# Remove the link from before the Display Manager command was renamed to `screen`.
OLD_LINK="$BIN_DIR/display"
if [[ -L "$OLD_LINK" && "$(readlink -f "$OLD_LINK")" == "$SCRIPT_DIR/display-gui.py" ]]; then
    rm "$OLD_LINK"
fi
command -v update-desktop-database &>/dev/null && update-desktop-database "$APP_DIR" || true

case ":$PATH:" in
    *":$BIN_DIR:"*) echo "Done. Start them with:  network  /  sound  /  screen" ;;
    *) echo "Done. Note: $BIN_DIR is not in PATH; add it to ~/.bashrc to use the commands." ;;
esac
