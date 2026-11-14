# =============================================================================
# backend.py - PipeWire state model and commands (via pactl)
# =============================================================================
# PipeWire speaks the PulseAudio protocol through pipewire-pulse, so `pactl`
# can read and change everything this app needs:
#
#   reading   `pactl -f json info` and `pactl -f json list <type>` give the
#             full state as JSON; it is parsed into the dataclasses below.
#   watching  `pactl subscribe` prints one line per server event. Any line
#             schedules a (debounced) reload, after which the backend emits
#             'changed'. The UI never polls.
#   writing   set-default, set-volume, set-mute, move streams and card
#             profiles are short pactl calls run asynchronously.
#
# Volume changes while dragging a slider would otherwise start dozens of
# processes per second, so they are coalesced per target: at most one
# set-volume call is in flight, and only the newest value is sent next.
# =============================================================================

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Optional

from gi.repository import Gio, GLib, GObject

# The level meter's own recording stream uses this client name, so it can be
# hidden from the Apps page.
METER_CLIENT_NAME = "MrFaws Sound Meter"

# pactl's 100 % volume.
VOLUME_NORM = 65536

# Volume is capped at 100 %, matching the volume keys in hyprland.lua
# (`wpctl set-volume -l 1`), so the slider and the keys always agree.
MAX_VOLUME = 100

RELOAD_DELAY_MS = 60
RESUBSCRIBE_DELAY_S = 2


# -----------------------------------------------------------------------------
# Data model
# -----------------------------------------------------------------------------

@dataclass
class Device:
    """An output (sink) or input (source)."""
    kind: str               # "sink" or "source"
    index: int
    name: str               # pactl/PipeWire node name, used for commands
    title: str              # short human name, e.g. "Speaker", "Headphones"
    subtitle: str           # hardware name, e.g. "Meteor Lake-P HD Audio"
    icon: str
    volume: int             # percent
    mute: bool
    available: bool         # False for an unplugged HDMI port / headset jack
    state: str = ""         # RUNNING, IDLE, SUSPENDED


@dataclass
class Stream:
    """An app playing (sink-input) or recording (source-output) sound."""
    kind: str               # "sink-input" or "source-output"
    index: int
    app: str
    title: str              # media title, e.g. the YouTube video
    icon: str               # icon theme name of the app (may not exist)
    volume: int
    mute: bool
    device: int             # index of the sink/source it is connected to
    paused: bool


@dataclass
class Profile:
    name: str
    description: str
    available: bool
    priority: int
    label: str = ""         # description made unique, see _profile_labels()


@dataclass
class Card:
    index: int
    name: str
    title: str
    icon: str
    active_profile: str
    profiles: list[Profile] = field(default_factory=list)


def volume_percent(volume: dict) -> int:
    """Average of all channels, in percent of VOLUME_NORM."""
    values = [ch.get("value", 0) for ch in volume.values()] if volume else []
    if not values:
        return 0
    return round(sum(values) / len(values) / VOLUME_NORM * 100)


def _device_icon(kind: str, port_type: str, port_name: str, props: dict) -> str:
    bus = props.get("device.bus", "")
    form = props.get("device.form_factor", "")
    text = f"{port_type} {port_name} {form}".lower()
    if kind == "source":
        if "headset" in text or bus == "bluetooth":
            return "audio-headset-symbolic"
        return "audio-input-microphone-symbolic"
    if "hdmi" in text or "displayport" in text:
        return "video-display-symbolic"
    if "headset" in text:
        return "audio-headset-symbolic"
    if "headphone" in text or bus == "bluetooth":
        return "audio-headphones-symbolic"
    if bus == "usb":
        return "audio-card-symbolic"
    return "audio-speakers-symbolic"


def _clean(text: str) -> str:
    for junk in (" Output", " Input", " (Stereo)", " (Mono)"):
        text = text.replace(junk, "")
    return text.strip()


def _parse_device(kind: str, raw: dict) -> Device:
    props = raw.get("properties", {})
    active = raw.get("active_port") or ""
    port = next((p for p in raw.get("ports", []) if p.get("name") == active), None)

    # Built-in sound cards describe nodes as "<chip name> <port>", which is
    # long and repetitive. Show the port ("Speaker", "Digital Microphone")
    # as the title and the chip as the subtitle. Bluetooth/USB devices have
    # a proper product name in their description, so keep that.
    hardware = props.get("device.description", "")
    if props.get("device.api") == "alsa" and port is not None:
        title = _clean(port.get("description", "")) or _clean(raw.get("description", ""))
        subtitle = hardware
    else:
        title = _clean(raw.get("description", "")) or raw.get("name", "")
        subtitle = _clean(port.get("description", "")) if port else ""

    return Device(
        kind=kind,
        index=raw["index"],
        name=raw["name"],
        title=title,
        subtitle=subtitle if subtitle != title else "",
        icon=_device_icon(kind, port.get("type", "") if port else "",
                          active, props),
        volume=volume_percent(raw.get("volume", {})),
        mute=bool(raw.get("mute")),
        available=port is None or port.get("availability") != "not available",
        state=raw.get("state", ""),
    )


def _parse_stream(kind: str, raw: dict) -> Optional[Stream]:
    props = raw.get("properties", {})
    app = props.get("application.name") or props.get("node.name") or "Unknown app"
    if app == METER_CLIENT_NAME:
        return None
    # Peak-detect streams from other mixers (e.g. pavucontrol) are not apps.
    if props.get("stream.monitor") == "true" or props.get("resample.peaks") == "true":
        return None
    binary = props.get("application.process.binary", "")
    title = props.get("media.name", "")
    if title == app or title.lower() in ("playback", "playstream", "record", "recstream"):
        title = ""
    return Stream(
        kind=kind,
        index=raw["index"],
        app=app,
        title=title,
        icon=props.get("application.icon_name") or binary.lower() or "",
        volume=volume_percent(raw.get("volume", {})),
        mute=bool(raw.get("mute")),
        device=raw.get("sink" if kind == "sink-input" else "source", -1),
        paused=bool(raw.get("corked")),
    )


def _profile_labels(profiles: list[Profile]) -> None:
    """Give profiles that share a base description a short, distinct label.

    UCM sound cards (most modern laptops) describe every profile as "Play
    HiFi quality Music (<port list>)", which is long and only differs in the
    port list, e.g. "(HDMI1, Headphones, Mic1)" vs "(HDMI1, Mic1, Speaker)".
    The ports they do not have in common become the label: "Headphones"
    vs "Speaker".
    """
    def split(text: str) -> tuple[str, set[str]]:
        if "(" not in text or not text.endswith(")"):
            return text, set()
        base, _, rest = text.partition("(")
        return base.strip(), {p.strip() for p in rest[:-1].split(",")}

    groups: dict[str, list[tuple[Profile, set[str]]]] = {}
    for profile in profiles:
        base, ports = split(profile.description)
        groups.setdefault(base, []).append((profile, ports))
    for base, group in groups.items():
        common = set.intersection(*(ports for _p, ports in group))
        for profile, ports in group:
            extra = sorted(ports - common)
            profile.label = ", ".join(extra) if len(group) > 1 and extra else profile.description


def _parse_card(raw: dict) -> Card:
    props = raw.get("properties", {})
    profiles = [
        Profile(name=name,
                description=info.get("description", name),
                available=bool(info.get("available", True)),
                priority=int(info.get("priority", 0)))
        for name, info in raw.get("profiles", {}).items()
    ]
    profiles.sort(key=lambda p: (p.name == "off", -p.priority))
    _profile_labels(profiles)
    return Card(
        index=raw["index"],
        name=raw["name"],
        title=props.get("device.description") or raw["name"],
        icon="bluetooth-symbolic" if props.get("device.bus") == "bluetooth"
        else "audio-card-symbolic",
        active_profile=raw.get("active_profile", ""),
        profiles=profiles,
    )


# -----------------------------------------------------------------------------
# Backend
# -----------------------------------------------------------------------------

class SoundBackend(GObject.Object):
    """Holds the current audio state and runs pactl commands.

    Emits 'changed' after every reload. Command errors are passed to the
    `on_error` callback (the window shows them as toasts).
    """

    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    LISTS = ("sinks", "sources", "sink-inputs", "source-outputs", "cards")

    def __init__(self) -> None:
        super().__init__()
        self.connected = False
        self.loaded = False
        self.default_sink = ""
        self.default_source = ""
        self.sinks: list[Device] = []
        self.sources: list[Device] = []
        self.playback: list[Stream] = []
        self.recording: list[Stream] = []
        self.cards: list[Card] = []
        self.on_error: Callable[[str], None] = lambda message: None

        self._reload_source = 0
        self._reloading = False
        self._reload_again = False
        self._subscriber: Optional[Gio.Subprocess] = None
        self._stopped = False

        # Coalesced volume writes: key -> newest pending args / in-flight flag.
        self._pending: dict[str, list[str]] = {}
        self._in_flight: set[str] = set()

        self._subscribe()
        self.reload()

    # -------------------------------------------------------------------------
    # Lookups
    # -------------------------------------------------------------------------

    @property
    def output(self) -> Optional[Device]:
        return next((d for d in self.sinks if d.name == self.default_sink), None)

    @property
    def input(self) -> Optional[Device]:
        return next((d for d in self.sources if d.name == self.default_source), None)

    def device_by_index(self, kind: str, index: int) -> Optional[Device]:
        devices = self.sinks if kind == "sink" else self.sources
        return next((d for d in devices if d.index == index), None)

    # -------------------------------------------------------------------------
    # Reading state
    # -------------------------------------------------------------------------

    def queue_reload(self) -> None:
        if not self._reload_source:
            self._reload_source = GLib.timeout_add(RELOAD_DELAY_MS, self._on_reload_timeout)

    def _on_reload_timeout(self) -> bool:
        self._reload_source = 0
        self.reload()
        return GLib.SOURCE_REMOVE

    def reload(self) -> None:
        """Re-read the full state with one pactl call per list."""
        if self._reloading:
            self._reload_again = True
            return
        self._reloading = True
        results: dict[str, object] = {}
        commands = {"info": ["info"], **{name: ["list", name] for name in self.LISTS}}

        def done(key: str, ok: bool, output: str) -> None:
            try:
                results[key] = json.loads(output) if ok else None
            except json.JSONDecodeError:
                results[key] = None
            if len(results) == len(commands):
                self._apply(results)

        for key, args in commands.items():
            self._run(["pactl", "-f", "json", *args],
                      lambda ok, out, err, key=key: done(key, ok, out))

    def _apply(self, results: dict) -> None:
        self._reloading = False
        info = results.get("info")
        self.connected = isinstance(info, dict)
        if self.connected:
            self.default_sink = info.get("default_sink_name", "")
            self.default_source = info.get("default_source_name", "")
            self.sinks = [_parse_device("sink", d) for d in results.get("sinks") or []]
            self.sources = [_parse_device("source", d) for d in results.get("sources") or []
                            if not d["name"].endswith(".monitor")
                            and d.get("properties", {}).get("device.class") != "monitor"]
            self.playback = [s for s in (_parse_stream("sink-input", d)
                                         for d in results.get("sink-inputs") or []) if s]
            self.recording = [s for s in (_parse_stream("source-output", d)
                                          for d in results.get("source-outputs") or []) if s]
            self.cards = [_parse_card(c) for c in results.get("cards") or []]
        self.loaded = True
        self.emit("changed")
        if self._reload_again:
            self._reload_again = False
            self.reload()

    # -------------------------------------------------------------------------
    # Live updates
    # -------------------------------------------------------------------------

    def _subscribe(self) -> None:
        try:
            self._subscriber = Gio.Subprocess.new(
                ["pactl", "subscribe"],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE)
        except GLib.Error:
            self._retry_subscribe()
            return
        stream = Gio.DataInputStream.new(self._subscriber.get_stdout_pipe())
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self._on_event, stream)

    def _on_event(self, stream, result, _data) -> None:
        try:
            line, _length = stream.read_line_finish_utf8(result)
        except GLib.Error:
            line = None
        if line is None:
            # pactl exited: PipeWire was restarted or stopped.
            self._subscriber = None
            if not self._stopped:
                self.reload()
                self._retry_subscribe()
            return
        # 'client' events fire for every pactl call we run ourselves; they
        # never change what the app shows, so ignore them to avoid loops.
        if " on client " not in line:
            self.queue_reload()
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self._on_event, stream)

    def _retry_subscribe(self) -> None:
        def retry():
            if not self._stopped:
                self._subscribe()
                self.reload()
            return GLib.SOURCE_REMOVE
        GLib.timeout_add_seconds(RESUBSCRIBE_DELAY_S, retry)

    def stop(self) -> None:
        self._stopped = True
        if self._subscriber is not None:
            self._subscriber.force_exit()

    # -------------------------------------------------------------------------
    # Commands
    # -------------------------------------------------------------------------

    def set_default(self, device: Device) -> None:
        verb = "set-default-sink" if device.kind == "sink" else "set-default-source"
        self._command([verb, device.name])
        # If the user picked a device whose volume is still muted, they almost
        # certainly expect to hear something.
        if device.mute:
            self.set_mute(device.kind, device.index, False)

    def set_volume(self, kind: str, index: int, percent: int) -> None:
        percent = max(0, min(MAX_VOLUME, int(percent)))
        self._coalesced(f"{kind}:{index}",
                        [f"set-{kind}-volume", str(index), f"{percent}%"])

    def set_mute(self, kind: str, index: int, mute: bool) -> None:
        self._command([f"set-{kind}-mute", str(index), "1" if mute else "0"])

    def move_stream(self, stream: Stream, device_index: int) -> None:
        verb = "move-sink-input" if stream.kind == "sink-input" else "move-source-output"
        self._command([verb, str(stream.index), str(device_index)])

    def set_profile(self, card: Card, profile: str) -> None:
        self._command(["set-card-profile", card.name, profile])

    def toggle_output_mute(self) -> None:
        output = self.output
        if output is not None:
            self.set_mute("sink", output.index, not output.mute)

    # -------------------------------------------------------------------------
    # Process helpers
    # -------------------------------------------------------------------------

    def _command(self, args: list[str], finished: Optional[Callable[[], None]] = None) -> None:
        def done(ok: bool, _out: str, err: str) -> None:
            if not ok:
                self.on_error(err.strip().splitlines()[-1] if err.strip()
                              else f"pactl {args[0]} failed")
            if finished is not None:
                finished()
        self._run(["pactl", *args], done)

    def _coalesced(self, key: str, args: list[str]) -> None:
        if key in self._in_flight:
            self._pending[key] = args
            return
        self._in_flight.add(key)

        def finished() -> None:
            self._in_flight.discard(key)
            pending = self._pending.pop(key, None)
            if pending is not None:
                self._coalesced(key, pending)

        self._command(args, finished)

    @staticmethod
    def _run(argv: list[str], callback: Callable[[bool, str, str], None]) -> None:
        try:
            process = Gio.Subprocess.new(
                argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE)
        except GLib.Error as error:
            callback(False, "", error.message)
            return

        def on_done(proc, result) -> None:
            try:
                _ok, out, err = proc.communicate_utf8_finish(result)
            except GLib.Error as error:
                callback(False, "", error.message)
                return
            callback(proc.get_successful(), out or "", err or "")

        process.communicate_utf8_async(None, None, on_done)
