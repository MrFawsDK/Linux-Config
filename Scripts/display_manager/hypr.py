# =============================================================================
# hypr.py - Talking to Hyprland
# =============================================================================
# Reading:  `hyprctl -j monitors all` (JSON, includes disabled monitors).
# Writing:  `hyprctl eval '<lua>'` with one hl.monitor{...} call per monitor.
#           This user's Hyprland runs the Lua config, where
#           `hyprctl keyword monitor ...` fails with "keyword can't work with
#           non-legacy parsers", so eval is the only runtime path.
# Events:   Hyprland's event socket (.socket2.sock) announces hotplug, so the
#           window updates when a monitor is plugged in or removed.
#
# Security note: monitor names/descriptions come from the monitor's EDID,
# i.e. from whatever is plugged into the HDMI port. They end up inside Lua
# code that Hyprland executes, so every string goes through lua_str(), which
# escapes quotes, backslashes and control characters. Without that, a
# crafted EDID description like `x" }) os.execute("...") --` would be code.
# =============================================================================

from __future__ import annotations

import json
import math
import os
import subprocess
from dataclasses import dataclass, field, replace
from typing import Optional

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib, GObject


class HyprError(Exception):
    """hyprctl failed or Hyprland rejected the Lua."""


# Transform values as Hyprland numbers them (wl_output_transform).
TRANSFORMS = [
    (0, "Normal"),
    (1, "90°"),
    (2, "180°"),
    (3, "270°"),
    (4, "Flipped"),
    (5, "Flipped 90°"),
    (6, "Flipped 180°"),
    (7, "Flipped 270°"),
]

# Connector prefixes of built-in panels. Used to never save an internal
# panel as disabled (that would boot to a black screen without a monitor).
INTERNAL_PREFIXES = ("eDP", "LVDS", "DSI")


# -----------------------------------------------------------------------------
# Data model
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class Mode:
    width: int
    height: int
    refresh: float

    @classmethod
    def parse(cls, text: str) -> Optional["Mode"]:
        """'1920x1200@60.00Hz' -> Mode(1920, 1200, 60.0)."""
        try:
            size, _, rate = text.removesuffix("Hz").partition("@")
            width, height = size.split("x")
            return cls(int(width), int(height), float(rate or 60))
        except ValueError:
            return None

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"

    def __str__(self) -> str:
        # The format Hyprland accepts in `mode`, e.g. "1920x1200@60.00".
        return f"{self.width}x{self.height}@{self.refresh:.2f}"


@dataclass
class Settings:
    """What we can change on one monitor. Compared to detect pending edits."""
    enabled: bool = True
    mode: Optional[Mode] = None
    x: int = 0
    y: int = 0
    scale: float = 1.0
    transform: int = 0
    mirror: str = ""            # name of the monitor to mirror, "" = off

    def copy(self) -> "Settings":
        return replace(self)


@dataclass
class Monitor:
    name: str
    description: str
    make: str
    model: str
    serial: str
    modes: list[Mode]
    physical_mm: tuple[int, int]
    focused: bool
    live: Settings = field(default_factory=Settings)

    @property
    def internal(self) -> bool:
        return self.name.startswith(INTERNAL_PREFIXES)

    @property
    def title(self) -> str:
        if self.internal:
            return "Built-in Display"
        label = f"{self.make} {self.model}".strip()
        return label or self.name

    def resolutions(self) -> list[str]:
        """Unique 'WxH' strings, largest first (modes come sorted that way)."""
        seen: list[str] = []
        for mode in self.modes:
            if mode.resolution not in seen:
                seen.append(mode.resolution)
        return seen

    def rates(self, resolution: str) -> list[Mode]:
        return sorted((m for m in self.modes if m.resolution == resolution),
                      key=lambda m: -m.refresh)

    @classmethod
    def from_json(cls, data: dict) -> "Monitor":
        modes = [m for m in map(Mode.parse, data.get("availableModes", [])) if m]
        current = Mode(int(data["width"]), int(data["height"]), float(data["refreshRate"]))
        # Use the matching entry from availableModes so the combo boxes find it
        # (59.999 Hz from the current state vs. "60.00" in the list).
        current = min(modes, default=current,
                      key=lambda m: (m.resolution != current.resolution,
                                     abs(m.refresh - current.refresh)))
        mirror = data.get("mirrorOf", "none")
        live = Settings(
            enabled=not data.get("disabled", False),
            mode=current,
            x=int(data["x"]),
            y=int(data["y"]),
            scale=float(data["scale"]),
            transform=int(data["transform"]),
            mirror="" if mirror in ("none", "", None) else str(mirror),
        )
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            make=data.get("make", ""),
            model=data.get("model", ""),
            serial=data.get("serial", ""),
            modes=modes or [current],
            physical_mm=(int(data.get("physicalWidth", 0)), int(data.get("physicalHeight", 0))),
            focused=bool(data.get("focused")),
            live=live,
        )


def logical_size(settings: Settings) -> tuple[int, int]:
    """Size in layout coordinates: pixels / scale, swapped when rotated 90/270.

    Hyprland positions monitors in these logical pixels, so a 1920x1200
    panel at scale 1.5 is 1280x800 wide in the arrangement.
    """
    if settings.mode is None:
        return (0, 0)
    width = round(settings.mode.width / settings.scale)
    height = round(settings.mode.height / settings.scale)
    return (height, width) if settings.transform % 2 else (width, height)


def valid_scales(mode: Optional[Mode], current: float) -> list[float]:
    """Scales that give a whole-pixel logical size, between 1x and 3x.

    Wayland fractional scaling works in 1/120 steps, and Hyprland nudges any
    scale that does not divide the resolution evenly to one that does (with a
    "invalid scale" notification). Offering only clean values avoids that.
    For 1920x1200 this gives 1, 1.2, 1.25, 1.33, 1.5, 1.6, 1.67, 2, 2.4, ...
    """
    if mode is None:
        return [current]
    g = math.gcd(mode.width, mode.height)
    scales: dict[float, float] = {}     # rounded key -> exact value (4/3 stays 4/3)
    for k in range(1, g + 1):
        s = g / k
        if not (1.0 <= s <= 3.0 and abs(s * 120 - round(s * 120)) < 1e-6):
            continue
        # Hide odd steps like 1.0667 or 1.875: keep multiples of 5 % and thirds.
        if abs(s * 20 - round(s * 20)) < 1e-6 or abs(s * 3 - round(s * 3)) < 1e-6:
            scales[round(s, 4)] = s
    # Always keep the current value selectable, even if it is unusual.
    if not any(abs(s - current) < 1e-3 for s in scales.values()):
        scales[round(current, 4)] = current
    return sorted(scales.values())


# -----------------------------------------------------------------------------
# hyprctl
# -----------------------------------------------------------------------------

def hyprctl(*args: str) -> str:
    try:
        result = subprocess.run(["hyprctl", *args], capture_output=True,
                                text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HyprError(f"hyprctl failed: {error}") from error
    if result.returncode != 0:
        raise HyprError((result.stderr or result.stdout).strip() or "hyprctl failed")
    return result.stdout


def list_monitors() -> list[Monitor]:
    try:
        data = json.loads(hyprctl("-j", "monitors", "all"))
    except json.JSONDecodeError as error:
        raise HyprError(f"Unexpected hyprctl output: {error}") from error
    monitors = [Monitor.from_json(item) for item in data]
    # Built-in panel first, then by connector name: stable order in the UI.
    monitors.sort(key=lambda m: (not m.internal, m.name))
    return monitors


def lua_str(value: str) -> str:
    """Quote a string as a Lua literal, safe against injection (see header)."""
    out = []
    for ch in value:
        if ch in ('"', "\\"):
            out.append("\\" + ch)
        elif ord(ch) < 32 or ord(ch) == 127:
            out.append(f"\\{ord(ch):03d}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def lua_number(value: float) -> str:
    # Full precision on purpose: 1.333333 would make 1920 px -> 1440.0003
    # logical px, and Hyprland would "correct" it with a warning.
    return repr(float(value))


def monitor_lua(output: str, settings: Settings) -> str:
    """One hl.monitor{...} call. `output` is a name or 'desc:...' selector."""
    if not settings.enabled:
        return f"hl.monitor({{ output = {lua_str(output)}, disabled = true }})"
    parts = [
        f"output = {lua_str(output)}",
        f"mode = {lua_str(str(settings.mode) if settings.mode else 'preferred')}",
        f"position = {lua_str(f'{settings.x}x{settings.y}')}",
        f"scale = {lua_number(settings.scale)}",
        f"transform = {int(settings.transform)}",
    ]
    if settings.mirror:
        parts.append(f"mirror = {lua_str(settings.mirror)}")
    return "hl.monitor({ " + ", ".join(parts) + " })"


def apply(changes: dict[str, Settings]) -> None:
    """Apply settings for several monitors in one eval (one Lua chunk).

    Enabled monitors are applied before disabled ones, so there is never a
    moment where Hyprland has no active output at all.
    """
    if not changes:
        return
    ordered = sorted(changes.items(), key=lambda item: not item[1].enabled)
    chunk = "\n".join(monitor_lua(name, s) for name, s in ordered)
    output = hyprctl("eval", chunk).strip()
    if output and output != "ok":
        raise HyprError(output)


def validate(monitors: list[Monitor], settings: dict[str, Settings]) -> Optional[str]:
    """Return a human-readable problem with a planned layout, or None."""
    enabled = [n for n, s in settings.items() if s.enabled]
    if not enabled:
        return "At least one display must stay on."
    if all(settings[n].mirror for n in enabled):
        return "At least one display must show its own picture (not mirror)."
    for name, s in settings.items():
        if not s.enabled or not s.mirror:
            continue
        target = settings.get(s.mirror)
        if target is None or not target.enabled:
            return f"{name} mirrors a display that is off."
        if target.mirror:
            return f"{name} cannot mirror {s.mirror}, which is itself a mirror."
    return None


# -----------------------------------------------------------------------------
# Live events
# -----------------------------------------------------------------------------

class HyprEvents(GObject.Object):
    """Emits 'changed' (debounced) when monitors are added or removed."""

    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    WATCHED = ("monitoradded", "monitoraddedv2", "monitorremoved",
               "monitorremovedv2", "configreloaded")

    def __init__(self) -> None:
        super().__init__()
        self._stream: Optional[Gio.DataInputStream] = None
        self._cancel = Gio.Cancellable()
        self._debounce = 0
        self._connect()

    def _connect(self) -> None:
        signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if not signature or not runtime:
            return  # not inside Hyprland: no live updates, the app still works
        path = os.path.join(runtime, "hypr", signature, ".socket2.sock")
        try:
            conn = Gio.SocketClient().connect(Gio.UnixSocketAddress.new(path), None)
        except GLib.Error:
            return
        self._conn = conn  # keep the connection object alive
        self._stream = Gio.DataInputStream.new(conn.get_input_stream())
        self._read_next()

    def _read_next(self) -> None:
        self._stream.read_line_async(GLib.PRIORITY_DEFAULT, self._cancel, self._on_line)

    def _on_line(self, stream, result) -> None:
        try:
            line, _length = stream.read_line_finish_utf8(result)
        except GLib.Error:
            return  # cancelled or socket closed (Hyprland exited)
        if line is None:
            return
        event = line.split(">>", 1)[0]
        if event in self.WATCHED:
            # Hotplug sends several events in a row; collapse them into one.
            if self._debounce:
                GLib.source_remove(self._debounce)
            self._debounce = GLib.timeout_add(400, self._emit)
        self._read_next()

    def _emit(self) -> bool:
        self._debounce = 0
        self.emit("changed")
        return GLib.SOURCE_REMOVE

    def stop(self) -> None:
        self._cancel.cancel()
