# =============================================================================
# backlight.py - Laptop screen brightness
# =============================================================================
# Reads /sys/class/backlight/<dev>/brightness (world-readable) and writes
# through systemd-logind's Session.SetBrightness over D-Bus. That is the
# same trick brightnessctl and ~/.local/bin/lock-dim use: no root, no udev
# rule, no 'video' group, and logind only allows it for the active local
# session, so a background process or SSH login cannot dim your screen.
#
# Percent <-> raw value uses a curve (EXPONENT) instead of a straight line.
# Eyes are far more sensitive at the dark end, so with a linear mapping
# 1-20 % feels like most of the range and 50-100 % barely changes.
#
# Only GLib/Gio are imported here (no GTK), so the `screen brightness +5`
# command used by the brightness keys starts fast.
# =============================================================================

from __future__ import annotations

from pathlib import Path
from typing import Optional

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

SYSFS = Path("/sys/class/backlight")
EXPONENT = 2.0
MIN_PERCENT = 1         # never 0: a black panel looks like the laptop died


class Backlight:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.name = path.name
        self.max = int((path / "max_brightness").read_text())

    @classmethod
    def find(cls) -> Optional["Backlight"]:
        """The firmware/raw device with the largest range (intel_backlight here).

        Some laptops also expose 'acpi_video0' with a coarse 0-10 range;
        the largest max_brightness is the most precise one.
        """
        best = None
        for path in sorted(SYSFS.glob("*")):
            try:
                device = cls(path)
            except (OSError, ValueError):
                continue
            if device.max > 0 and (best is None or device.max > best.max):
                best = device
        return best

    # --- raw values ----------------------------------------------------------

    def raw(self) -> int:
        return int((self.path / "brightness").read_text())

    def set_raw(self, value: int) -> None:
        value = max(1, min(self.max, int(value)))
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        bus.call_sync(
            "org.freedesktop.login1",
            "/org/freedesktop/login1/session/auto",
            "org.freedesktop.login1.Session",
            "SetBrightness",
            GLib.Variant("(ssu)", ("backlight", self.name, value)),
            None, Gio.DBusCallFlags.NONE, 2000, None)

    # --- percent -------------------------------------------------------------

    def to_percent(self, raw: int) -> int:
        return max(MIN_PERCENT, round(100 * (raw / self.max) ** (1 / EXPONENT)))

    def to_raw(self, percent: float) -> int:
        percent = max(MIN_PERCENT, min(100, percent))
        return max(1, round(self.max * (percent / 100) ** EXPONENT))

    def percent(self) -> int:
        return self.to_percent(self.raw())

    def set_percent(self, percent: float) -> None:
        self.set_raw(self.to_raw(percent))

    def step(self, delta: int) -> int:
        """Change brightness by `delta` percent; returns the new percent.

        At the dark end several percent values round to the same raw value,
        so a plain +1 % could do nothing. Make sure every step moves at least
        one raw unit in the requested direction.
        """
        current = self.raw()
        target = self.to_raw(self.to_percent(current) + delta)
        if target == current and delta:
            target = current + (1 if delta > 0 else -1)
        target = max(1, min(self.max, target))
        self.set_raw(target)
        return self.to_percent(target)


# -----------------------------------------------------------------------------
# Command line: `screen brightness [N | +N | -N]`
# -----------------------------------------------------------------------------

def cli(args: list[str]) -> int:
    import subprocess

    device = Backlight.find()
    if device is None:
        print("No backlight found in /sys/class/backlight (desktop or external monitor).")
        return 1
    values = [a for a in args if not a.startswith("--")]
    if not values:
        print(device.percent())
        return 0

    arg = values[0].rstrip("%")
    try:
        if arg[:1] in "+-":
            new = device.step(int(arg))
        else:
            device.set_percent(int(arg))
            new = device.percent()
    except ValueError:
        print("Usage: screen brightness [PERCENT | +STEP | -STEP]")
        return 2
    except GLib.Error as error:
        print(f"logind refused the change: {error.message}")
        return 1

    # On-screen feedback for the brightness keys. The synchronous hint makes
    # swaync replace the previous bubble instead of stacking a new one.
    if "--notify" in args:
        subprocess.run(
            ["notify-send", "-a", "Brightness", "-t", "1200",
             "-i", "display-brightness-symbolic",
             "-h", f"int:value:{new}",
             "-h", "string:x-canonical-private-synchronous:brightness",
             f"Brightness {new}%"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        print(new)
    return 0
