#!/usr/bin/env bash
# Bluetooth setup (Arch): installerer bluez, starter service, fjerner rfkill-block
set -euo pipefail
sudo pacman -S --needed bluez bluez-utils blueman
sudo systemctl enable --now bluetooth.service
rfkill unblock bluetooth
bluetoothctl power on
