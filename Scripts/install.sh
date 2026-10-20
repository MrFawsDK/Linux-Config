#!/usr/bin/env bash
# =============================================================================
# install.sh - make MrFaws Network Manager easy to start
# =============================================================================
# Run once (and again after moving the repo):  ~/Config/Scripts/install.sh
#
#   * checks the required packages and offers the pacman command if missing
#   * links the command `mrfaws-network` into ~/.local/bin (already in PATH)
#   * installs the app launcher (rofi / app menu) with the correct path
#
# Nothing is copied: the command and launcher point at this repo, so edits
# here take effect immediately. Re-running the script is safe.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
ENTRY="$SCRIPT_DIR/wifi-gui.py"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"
COMMAND="mrfaws-network"
DESKTOP="mrfaws-wifi.desktop"

# --- Dependencies -------------------------------------------------------------
missing=()
for pkg in networkmanager python python-gobject gtk4 libadwaita; do
    pacman -Q "$pkg" &>/dev/null || missing+=("$pkg")
done
if ((${#missing[@]})); then
    echo "Missing packages: ${missing[*]}"
    echo "Install them with:  sudo pacman -S --needed ${missing[*]}"
    exit 1
fi

# --- Command in PATH ----------------------------------------------------------
chmod +x "$ENTRY"
mkdir -p "$BIN_DIR"
ln -sfn "$ENTRY" "$BIN_DIR/$COMMAND"
echo "Command:  $BIN_DIR/$COMMAND -> $ENTRY"

# --- App launcher -------------------------------------------------------------
# The Exec line is rewritten to this repo's actual location.
mkdir -p "$APP_DIR"
sed "s|^Exec=.*|Exec=/usr/bin/python3 $ENTRY|" "$SCRIPT_DIR/$DESKTOP" > "$APP_DIR/$DESKTOP"
command -v update-desktop-database &>/dev/null && update-desktop-database "$APP_DIR" || true
echo "Launcher: $APP_DIR/$DESKTOP"

case ":$PATH:" in
    *":$BIN_DIR:"*) echo "Done. Start it with:  $COMMAND" ;;
    *) echo "Done. Note: $BIN_DIR is not in PATH; add it to ~/.bashrc to use '$COMMAND'." ;;
esac
