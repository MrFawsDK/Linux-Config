# =============================================================================
# ui.py - Main window and sidebar pages
# =============================================================================
# Layout: an Adw.NavigationSplitView with a sidebar (page list) and a content
# area, like MrFaws Network Manager. Each sidebar entry is one Page subclass:
#
#   OutputPage   volume of the current output + pick speakers/headphones/HDMI
#   InputPage    microphone volume, live level meter + pick a microphone
#   AppsPage     per-app volume, mute and output/input choice
#   BluetoothPage  pair / connect Bluetooth headphones and speakers (BlueZ)
#   CardsPage    sound card profiles, open pavucontrol, restart audio
#
# Pages never poll. The backend emits 'changed' after each PipeWire event;
# the visible page updates immediately and the others are marked stale and
# update when shown. Lists are only rebuilt when their structure changes
# (a device or app appears, disappears or is renamed); volume and mute
# changes update the existing widgets, so a slider being dragged is never
# replaced underneath the pointer.
# =============================================================================

from __future__ import annotations

import shutil
import time
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from . import APP_NAME, APP_VERSION
from .backend import MAX_VOLUME, Card, Device, SoundBackend, Stream
from .bluetooth import BluetoothBackend, BtDevice
from .meter import LevelMeter


# -----------------------------------------------------------------------------
# Widget helpers
# -----------------------------------------------------------------------------

def action_row(title: str, subtitle: str = "") -> Adw.ActionRow:
    """ActionRow with markup disabled.

    Device names and media titles are user data and may contain '&' or '<',
    which would break (or be interpreted as) Pango markup.
    """
    row = Adw.ActionRow(title=title, subtitle=subtitle)
    row.set_use_markup(False)
    return row


def badge(text: str, kind: str = "") -> Gtk.Label:
    """Pill-shaped status label (styles defined in theme.py)."""
    label = Gtk.Label(label=text, valign=Gtk.Align.CENTER)
    label.add_css_class("badge")
    if kind:
        label.add_css_class(kind)
    return label


def volume_icon(kind: str, volume: int, mute: bool) -> str:
    if kind in ("source", "source-output"):
        if mute or volume == 0:
            return "microphone-sensitivity-muted-symbolic"
        if volume < 34:
            return "microphone-sensitivity-low-symbolic"
        if volume < 67:
            return "microphone-sensitivity-medium-symbolic"
        return "microphone-sensitivity-high-symbolic"
    if mute or volume == 0:
        return "audio-volume-muted-symbolic"
    if volume < 34:
        return "audio-volume-low-symbolic"
    if volume < 67:
        return "audio-volume-medium-symbolic"
    return "audio-volume-high-symbolic"


def app_icon(name: str) -> str:
    """The app's own icon if the icon theme has it, else a generic one."""
    theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    for candidate in (name, name.lower(), f"{name.lower()}-symbolic"):
        if candidate and theme.has_icon(candidate):
            return candidate
    return "audio-x-generic-symbolic"


class VolumeControl(Gtk.Box):
    """Mute button + slider + percentage.

    `on_volume(percent)` and `on_mute(muted)` are called on user input only.
    For a short while after the user touches the slider, external updates
    are ignored, so the echo of our own (coalesced) volume writes does not
    make the slider jump back while it is being dragged.
    """

    HOLD_S = 0.6

    def __init__(self, kind: str, on_volume: Callable[[int], None],
                 on_mute: Callable[[bool], None], big: bool = False) -> None:
        super().__init__(spacing=10, hexpand=True, valign=Gtk.Align.CENTER)
        self.kind = kind
        self.on_volume = on_volume
        self.on_mute = on_mute
        self._mute = False
        self._updating = False
        self._touched_at = 0.0
        if big:
            self.add_css_class("big")

        self.mute_button = Gtk.Button(valign=Gtk.Align.CENTER, tooltip_text="Mute")
        self.mute_button.add_css_class("flat")
        self.mute_button.add_css_class("circular")
        self.mute_button.add_css_class("mute-button")
        self.mute_button.connect("clicked", lambda *_: self.on_mute(not self._mute))
        self.append(self.mute_button)

        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, MAX_VOLUME, 1)
        self.scale.set_increments(1, 5)
        self.scale.set_draw_value(False)
        self.scale.set_hexpand(True)
        self.scale.add_css_class("volume")
        if big:
            self.scale.add_css_class("big")
        self.scale.connect("value-changed", self._on_value_changed)
        self.append(self.scale)

        self.label = Gtk.Label(xalign=1)
        self.label.add_css_class("volume-label")
        self.append(self.label)

    def _on_value_changed(self, scale: Gtk.Scale) -> None:
        value = round(scale.get_value())
        self._show(value)
        if not self._updating:
            self._touched_at = time.monotonic()
            self.on_volume(value)

    def _show(self, volume: int) -> None:
        self.label.set_label(f"{volume}%")
        self.mute_button.set_icon_name(volume_icon(self.kind, volume, self._mute))

    def set_state(self, volume: int, mute: bool) -> None:
        self._mute = mute
        if mute:
            self.add_css_class("muted")
            self.mute_button.add_css_class("muted")
            self.mute_button.set_tooltip_text("Unmute")
        else:
            self.remove_css_class("muted")
            self.mute_button.remove_css_class("muted")
            self.mute_button.set_tooltip_text("Mute")
        if time.monotonic() - self._touched_at > self.HOLD_S:
            self._updating = True
            self.scale.set_value(volume)
            self._updating = False
        self._show(round(self.scale.get_value()))


class RowGroup(Adw.PreferencesGroup):
    """PreferencesGroup whose rows can be replaced wholesale."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._rows: list[Gtk.Widget] = []

    def set_rows(self, rows: list[Gtk.Widget]) -> None:
        for row in self._rows:
            self.remove(row)
        self._rows = list(rows)
        for row in self._rows:
            self.add(row)


# -----------------------------------------------------------------------------
# Page base class
# -----------------------------------------------------------------------------

class Page(Adw.NavigationPage):
    """A content page: header bar + scrollable preferences page.

    Subclasses build their widgets in __init__ and implement refresh().
    """

    icon_name = "audio-speakers-symbolic"

    def __init__(self, window: "MainWindow", title: str, tag: str) -> None:
        super().__init__(title=title, tag=tag)
        self.window = window
        self.backend: SoundBackend = window.backend
        self.stale = True

        self.toolbar = Adw.ToolbarView()
        self.header = Adw.HeaderBar()
        self.toolbar.add_top_bar(self.header)

        # Shown on every page while pipewire-pulse cannot be reached.
        self.banner = Adw.Banner(title="Cannot reach PipeWire. Try “Restart audio” "
                                       "on the Sound Cards page.")
        self.toolbar.add_top_bar(self.banner)

        self.content = Adw.PreferencesPage()
        self.toolbar.set_content(self.content)
        self.set_child(self.toolbar)

    def refresh(self) -> None:
        """Re-read state from the backend and update widgets."""
        self.banner.set_revealed(self.backend.loaded and not self.backend.connected)
        self.stale = False

    def on_shown(self) -> None:
        pass

    def on_hidden(self) -> None:
        pass


# -----------------------------------------------------------------------------
# Output / Input pages
# -----------------------------------------------------------------------------

class DevicePage(Page):
    """Shared layout of the Output and Input pages."""

    kind = "sink"
    empty_title = "No Output Found"
    choose_title = "Output Device"
    choose_hint = "Apps that are playing move to the device you pick."

    def __init__(self, window: "MainWindow", title: str, tag: str) -> None:
        super().__init__(window, title, tag)
        self._list_signature = None

        # --- Hero card: current device + big volume slider ---
        hero_group = Adw.PreferencesGroup()
        self.hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.hero.add_css_class("card")
        self.hero.add_css_class("device-hero")
        if self.kind == "source":
            self.hero.add_css_class("input")

        top = Gtk.Box(spacing=16)
        self.hero_icon = Gtk.Image(pixel_size=32, valign=Gtk.Align.CENTER)
        self.hero_icon.add_css_class("hero-icon")
        top.append(self.hero_icon)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                        valign=Gtk.Align.CENTER, hexpand=True)
        self.hero_title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.hero_title.add_css_class("hero-title")
        self.hero_subtitle = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.hero_subtitle.add_css_class("dim-label")
        texts.append(self.hero_title)
        texts.append(self.hero_subtitle)
        top.append(texts)
        self.hero.append(top)

        self.volume = VolumeControl(self.kind, self._on_volume, self._on_mute, big=True)
        self.hero.append(self.volume)
        self.add_hero_extras(self.hero)
        hero_group.add(self.hero)
        self.content.add(hero_group)

        # --- Device choice ---
        self.devices_group = RowGroup(title=self.choose_title, description=self.choose_hint)
        self.content.add(self.devices_group)

        self.unplugged_group = Adw.PreferencesGroup()
        self.unplugged_row = Adw.ExpanderRow(title="Not Plugged In")
        self.unplugged_row.add_css_class("dim-label")
        self.unplugged_group.add(self.unplugged_row)
        self._unplugged_rows: list[Gtk.Widget] = []
        self.content.add(self.unplugged_group)

    def add_hero_extras(self, hero: Gtk.Box) -> None:
        pass

    def current(self) -> Optional[Device]:
        return self.backend.output if self.kind == "sink" else self.backend.input

    def devices(self) -> list[Device]:
        return self.backend.sinks if self.kind == "sink" else self.backend.sources

    def _on_volume(self, percent: int) -> None:
        device = self.current()
        if device is not None:
            self.backend.set_volume(self.kind, device.index, percent)

    def _on_mute(self, mute: bool) -> None:
        device = self.current()
        if device is not None:
            self.backend.set_mute(self.kind, device.index, mute)

    def refresh(self) -> None:
        super().refresh()
        device = self.current()

        # Hero
        if device is None:
            self.hero_icon.set_from_icon_name(
                "audio-volume-muted-symbolic" if self.kind == "sink"
                else "microphone-disabled-symbolic")
            self.hero_title.set_label(self.empty_title)
            self.hero_subtitle.set_label("Plug in a device or check the Sound Cards page.")
            self.volume.set_sensitive(False)
            self.volume.set_state(0, False)
        else:
            self.hero_icon.set_from_icon_name(device.icon)
            self.hero_title.set_label(device.title)
            self.hero_subtitle.set_label(device.subtitle or " ")
            self.volume.set_sensitive(True)
            self.volume.set_state(device.volume, device.mute)
        if device is not None and device.mute:
            self.hero.add_css_class("muted")
        else:
            self.hero.remove_css_class("muted")

        # Device lists, rebuilt only when something structural changed.
        devices = self.devices()
        signature = [(d.index, d.title, d.subtitle, d.icon, d.available,
                      device is not None and d.index == device.index) for d in devices]
        if signature == self._list_signature:
            return
        self._list_signature = signature

        plugged = [d for d in devices if d.available or (device and d.index == device.index)]
        unplugged = [d for d in devices if d not in plugged]
        self.devices_group.set_rows([self._device_row(d, device) for d in plugged]
                                    or [action_row("Nothing connected")])

        for row in self._unplugged_rows:
            self.unplugged_row.remove(row)
        self._unplugged_rows = []
        for d in unplugged:
            row = action_row(d.title, d.subtitle)
            row.add_prefix(Gtk.Image(icon_name=d.icon))
            self.unplugged_row.add_row(row)
            self._unplugged_rows.append(row)
        self.unplugged_row.set_subtitle(f"{len(unplugged)} hidden")
        self.unplugged_group.set_visible(bool(unplugged))

    def _device_row(self, d: Device, current: Optional[Device]) -> Adw.ActionRow:
        selected = current is not None and d.index == current.index
        row = action_row(d.title, d.subtitle)
        row.add_css_class("device-row")
        icon = Gtk.Image(icon_name=d.icon)
        icon.add_css_class("device-icon")
        row.add_prefix(icon)
        if selected:
            row.add_css_class("selected")
            check = Gtk.Image(icon_name="object-select-symbolic")
            check.add_css_class("check")
            row.add_suffix(check)
        else:
            row.set_activatable(True)
            row.connect("activated", lambda *_: self.backend.set_default(d))
            row.set_tooltip_text("Use this device")
        return row


class OutputPage(DevicePage):
    icon_name = "audio-speakers-symbolic"
    kind = "sink"

    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window, "Output", "output")


class InputPage(DevicePage):
    icon_name = "audio-input-microphone-symbolic"
    kind = "source"
    empty_title = "No Microphone Found"
    choose_title = "Microphone"
    choose_hint = "Apps that are recording move to the microphone you pick."

    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window, "Input", "input")
        self.meter = LevelMeter(self.level.set_value)
        self._shown = False

    def add_hero_extras(self, hero: Gtk.Box) -> None:
        box = Gtk.Box(spacing=12)
        icon = Gtk.Image(icon_name="audio-input-microphone-symbolic")
        icon.add_css_class("dim-label")
        box.append(icon)
        self.level = Gtk.LevelBar(min_value=0, max_value=1, hexpand=True,
                                  valign=Gtk.Align.CENTER,
                                  tooltip_text="Live microphone level — speak to test")
        self.level.add_css_class("mic-level")
        # Replace GTK's default offsets with Dracula-coloured ones (theme.py).
        for name in ("low", "high", "full"):
            self.level.remove_offset_value(name)
        # A block gets the class of the first offset above its value:
        # normal (green) up to 85 %, then high (orange), then full (red).
        self.level.add_offset_value("normal", 0.85)
        self.level.add_offset_value("high", 0.97)
        self.level.add_offset_value("full", 1.0)
        box.append(self.level)
        hint = Gtk.Label(label="Level")
        hint.add_css_class("hint")
        hint.add_css_class("volume-label")
        hint.set_xalign(1)
        box.append(hint)
        hero.append(box)

    def refresh(self) -> None:
        super().refresh()
        self._update_meter()

    def on_shown(self) -> None:
        self._shown = True
        self._update_meter()

    def on_hidden(self) -> None:
        self._shown = False
        self.meter.stop()

    def _update_meter(self) -> None:
        device = self.current()
        if self._shown and device is not None:
            self.meter.start(device.name)
        else:
            self.meter.stop()


# -----------------------------------------------------------------------------
# Apps page
# -----------------------------------------------------------------------------

class AppRow(Adw.PreferencesRow):
    """One playing/recording app: name, device picker and volume slider."""

    def __init__(self, backend: SoundBackend, stream: Stream,
                 devices: list[Device]) -> None:
        super().__init__(activatable=False, focusable=False)
        self.backend = backend
        self.stream = stream
        self._devices = devices
        self._updating = False
        device_kind = "sink" if stream.kind == "sink-input" else "source"

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.add_css_class("app-row")

        top = Gtk.Box(spacing=12)
        icon = Gtk.Image(icon_name=app_icon(stream.icon), pixel_size=28,
                         valign=Gtk.Align.CENTER)
        icon.add_css_class("app-icon")
        top.append(icon)

        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER,
                        hexpand=True)
        name = Gtk.Label(label=stream.app, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        name.add_css_class("app-name")
        texts.append(name)
        if stream.title:
            title = Gtk.Label(label=stream.title, xalign=0,
                              ellipsize=Pango.EllipsizeMode.END,
                              tooltip_text=stream.title)
            title.add_css_class("dim-label")
            title.add_css_class("caption")
            texts.append(title)
        top.append(texts)

        if stream.paused:
            top.append(badge("Paused", "paused"))

        # Device picker. Only shown when there is an actual choice.
        if len(devices) > 1:
            self.picker = Gtk.DropDown.new_from_strings([d.title for d in devices])
            self.picker.set_valign(Gtk.Align.CENTER)
            self.picker.set_tooltip_text("Play on" if device_kind == "sink" else "Record from")
            self.picker.add_css_class("flat")
            self._select_current()
            self.picker.connect("notify::selected", self._on_picked)
            top.append(self.picker)
        else:
            self.picker = None
        box.append(top)

        self.volume = VolumeControl(
            stream.kind,
            lambda v: backend.set_volume(stream.kind, stream.index, v),
            lambda m: backend.set_mute(stream.kind, stream.index, m))
        self.volume.set_state(stream.volume, stream.mute)
        box.append(self.volume)
        self.set_child(box)

    def _select_current(self) -> None:
        for position, device in enumerate(self._devices):
            if device.index == self.stream.device:
                self._updating = True
                self.picker.set_selected(position)
                self._updating = False
                return

    def _on_picked(self, dropdown, _pspec) -> None:
        if self._updating:
            return
        position = dropdown.get_selected()
        if 0 <= position < len(self._devices):
            self.backend.move_stream(self.stream, self._devices[position].index)

    def update(self, stream: Stream) -> None:
        self.stream = stream
        self.volume.set_state(stream.volume, stream.mute)


class AppsPage(Page):
    icon_name = "view-app-grid-symbolic"

    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window, "Apps", "apps")
        self._signature = None
        self._rows: dict[tuple[str, int], AppRow] = {}

        self.empty = Adw.StatusPage(icon_name="audio-x-generic-symbolic",
                                    title="No Apps Are Playing",
                                    description="Start some music or a video and it shows "
                                                "up here with its own volume.")
        self.empty.add_css_class("compact")
        self.empty_group = Adw.PreferencesGroup()
        self.empty_group.add(self.empty)
        self.content.add(self.empty_group)

        self.playback_group = RowGroup(title="Playing")
        self.content.add(self.playback_group)
        self.recording_group = RowGroup(title="Recording",
                                        description="Apps that are using a microphone.")
        self.content.add(self.recording_group)

    def refresh(self) -> None:
        super().refresh()
        b = self.backend
        outputs = [d for d in b.sinks if d.available]
        inputs = [d for d in b.sources if d.available]
        signature = (
            [(s.index, s.app, s.title, s.icon, s.paused, s.device) for s in b.playback],
            [(s.index, s.app, s.title, s.icon, s.paused, s.device) for s in b.recording],
            [(d.index, d.title) for d in outputs], [(d.index, d.title) for d in inputs],
        )
        if signature == self._signature:
            for stream in b.playback + b.recording:
                row = self._rows.get((stream.kind, stream.index))
                if row is not None:
                    row.update(stream)
            return
        self._signature = signature

        def pickable(devices: list[Device], kind: str, stream: Stream) -> list[Device]:
            # Keep the stream's current device in the list even if it is
            # unplugged, so the picker always shows where it plays now.
            current = b.device_by_index(kind, stream.device)
            if current is not None and current not in devices:
                return devices + [current]
            return devices

        self._rows = {}
        playback = [AppRow(b, s, pickable(outputs, "sink", s)) for s in b.playback]
        recording = [AppRow(b, s, pickable(inputs, "source", s)) for s in b.recording]
        for row in playback + recording:
            self._rows[(row.stream.kind, row.stream.index)] = row
        self.playback_group.set_rows(playback)
        self.recording_group.set_rows(recording)
        self.playback_group.set_visible(bool(playback))
        self.recording_group.set_visible(bool(recording))
        self.empty_group.set_visible(not playback)


# -----------------------------------------------------------------------------
# Bluetooth page
# -----------------------------------------------------------------------------

class BluetoothPage(Page):
    """Power switch, paired devices and a live list of devices nearby.

    Lives in Sound because Bluetooth is mostly headphones and speakers: once
    one connects, PipeWire adds it and it shows up on the Output page.
    """

    icon_name = "bluetooth-symbolic"

    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window, "Bluetooth", "bluetooth")
        self.bt: BluetoothBackend = window.bluetooth
        self._signature = None
        self._updating = False

        self.missing = Adw.StatusPage(
            icon_name="bluetooth-disabled-symbolic", title="Bluetooth Is Not Available",
            description="Install BlueZ and start the service:\n"
                        "sudo pacman -S bluez bluez-utils\n"
                        "sudo systemctl enable --now bluetooth")
        self.missing.add_css_class("compact")
        self.missing_group = Adw.PreferencesGroup()
        self.missing_group.add(self.missing)
        self.content.add(self.missing_group)

        self.power_group = Adw.PreferencesGroup()
        self.power_row = Adw.SwitchRow(title="Bluetooth")
        self.power_row.add_prefix(Gtk.Image(icon_name="bluetooth-symbolic"))
        self.power_row.connect("notify::active", self._on_power)
        self.power_group.add(self.power_row)
        self.content.add(self.power_group)

        self.paired_group = RowGroup(title="My Devices")
        self.content.add(self.paired_group)

        self.nearby_group = RowGroup(title="Nearby",
                                     description="Put your headphones in pairing mode. "
                                                 "Tap a device to pair.")
        self.spinner = Gtk.Spinner(valign=Gtk.Align.CENTER)
        self.nearby_group.set_header_suffix(self.spinner)
        self.content.add(self.nearby_group)

        self.bt.connect("changed", lambda *_: self._refresh_if_visible())
        self._shown = False

    def _refresh_if_visible(self) -> None:
        self.stale = True
        if self._shown:
            self.refresh()

    def on_shown(self) -> None:
        self._shown = True
        self.bt.set_scanning(True)
        self.refresh()

    def on_hidden(self) -> None:
        self._shown = False
        self.bt.set_scanning(False)   # scanning drains battery; only while open

    def _on_power(self, row, _pspec) -> None:
        if not self._updating:
            self.bt.set_powered(row.get_active())

    def refresh(self) -> None:
        super().refresh()
        bt = self.bt
        usable = bt.installed and bt.adapter_path is not None
        self.missing_group.set_visible(bt.loaded and not usable)
        self.power_group.set_visible(usable)
        if not usable:
            self.paired_group.set_visible(False)
            self.nearby_group.set_visible(False)
            return

        self._updating = True
        self.power_row.set_active(bt.powered)
        self.power_row.set_subtitle(bt.adapter_name + (" — visible as this name" if bt.powered else ""))
        self._updating = False
        self.spinner.set_spinning(bt.discovering)

        paired = [d for d in bt.devices if d.paired]
        nearby = [d for d in bt.devices if not d.paired and d.nearby]
        signature = ([(d.path, d.name, d.icon, d.connected, d.path in bt.busy) for d in paired],
                     [(d.path, d.name, d.icon, d.path in bt.busy) for d in nearby],
                     bt.powered)
        if signature == self._signature:
            return
        self._signature = signature

        self.paired_group.set_rows([self._paired_row(d) for d in paired]
                                   or [action_row("No paired devices")])
        self.nearby_group.set_rows([self._nearby_row(d) for d in nearby]
                                   or [action_row("Searching…" if bt.powered else "Bluetooth is off")])
        self.paired_group.set_visible(bt.powered)
        self.nearby_group.set_visible(bt.powered)

    def _paired_row(self, d: BtDevice) -> Adw.ActionRow:
        busy = d.path in self.bt.busy
        row = action_row(d.name, "Connecting…" if busy else ("Connected" if d.connected else "Not connected"))
        row.add_prefix(Gtk.Image(icon_name=d.icon))
        if d.connected:
            row.add_css_class("selected")
        if busy:
            row.add_suffix(Gtk.Spinner(spinning=True, valign=Gtk.Align.CENTER))
        else:
            toggle = Gtk.Button(label="Disconnect" if d.connected else "Connect",
                                valign=Gtk.Align.CENTER)
            toggle.add_css_class("flat")
            toggle.connect("clicked", lambda *_: self.bt.disconnect_device(d) if d.connected
                           else self.bt.connect_device(d))
            row.add_suffix(toggle)
            forget = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                                tooltip_text="Forget this device")
            forget.add_css_class("flat")
            forget.connect("clicked", lambda *_: self.bt.forget_device(d))
            row.add_suffix(forget)
        return row

    def _nearby_row(self, d: BtDevice) -> Adw.ActionRow:
        busy = d.path in self.bt.busy
        row = action_row(d.name, "Pairing…" if busy else d.address)
        row.add_prefix(Gtk.Image(icon_name=d.icon))
        if busy:
            row.add_suffix(Gtk.Spinner(spinning=True, valign=Gtk.Align.CENTER))
        else:
            row.set_activatable(True)
            row.connect("activated", lambda *_: self.bt.pair_device(d))
            row.set_tooltip_text("Pair and connect")
        return row


# -----------------------------------------------------------------------------
# Sound cards page
# -----------------------------------------------------------------------------

class CardsPage(Page):
    icon_name = "audio-card-symbolic"

    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window, "Sound Cards", "cards")
        self._signature = None
        self._groups: list[Adw.PreferencesGroup] = []

        tools = Adw.PreferencesGroup(title="Troubleshooting")
        restart = action_row("Restart Audio",
                             "Restarts PipeWire and WirePlumber. Fixes most “no sound” problems.")
        restart.add_prefix(Gtk.Image(icon_name="view-refresh-symbolic"))
        restart.set_activatable(True)
        restart.connect("activated", lambda *_: self.window.restart_audio())
        tools.add(restart)

        if shutil.which("pavucontrol"):
            advanced = action_row("Advanced Mixer", "Opens pavucontrol for every detail.")
            advanced.add_prefix(Gtk.Image(icon_name="preferences-system-symbolic"))
            advanced.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
            advanced.set_activatable(True)
            advanced.connect("activated", lambda *_: self.window.spawn(["pavucontrol"]))
            tools.add(advanced)
        self.tools = tools

    def refresh(self) -> None:
        super().refresh()
        cards = self.backend.cards
        signature = [(c.index, c.title, c.active_profile,
                      [(p.name, p.available) for p in c.profiles]) for c in cards]
        if signature == self._signature:
            return
        self._signature = signature

        for group in self._groups:
            self.content.remove(group)
        self._groups = []
        for card in cards:
            group = Adw.PreferencesGroup(
                title=GLib.markup_escape_text(card.title),
                description="The profile decides which outputs and inputs the card offers.")
            group.add(self._profile_row(card))
            self._groups.append(group)
        if not cards:
            group = Adw.PreferencesGroup()
            group.add(action_row("No sound cards found"))
            self._groups.append(group)
        self._groups.append(self.tools)
        for group in self._groups:
            self.content.add(group)

    def _profile_row(self, card: Card) -> Adw.ComboRow:
        profiles = [p for p in card.profiles if p.available or p.name == card.active_profile]
        row = Adw.ComboRow(title="Profile", use_subtitle=False)
        row.add_prefix(Gtk.Image(icon_name=card.icon))
        row.set_model(Gtk.StringList.new([p.label for p in profiles]))
        for position, profile in enumerate(profiles):
            if profile.name == card.active_profile:
                row.set_selected(position)

        def on_selected(combo, _pspec):
            position = combo.get_selected()
            if 0 <= position < len(profiles) and profiles[position].name != card.active_profile:
                self.backend.set_profile(card, profiles[position].name)

        row.connect("notify::selected", on_selected)
        return row


# -----------------------------------------------------------------------------
# Main window
# -----------------------------------------------------------------------------

class MainWindow(Adw.ApplicationWindow):
    """Sidebar navigation, toasts and shared actions."""

    def __init__(self, app: Adw.Application, backend: SoundBackend) -> None:
        super().__init__(application=app, title=APP_NAME,
                         default_width=900, default_height=640)
        self.set_size_request(360, 480)
        self.backend = backend
        backend.on_error = self.toast
        self.bluetooth = BluetoothBackend()
        self.bluetooth.on_error = self.toast

        self.toast_overlay = Adw.ToastOverlay()
        self.split = Adw.NavigationSplitView(min_sidebar_width=200, max_sidebar_width=260)
        self.toast_overlay.set_child(self.split)
        self.set_content(self.toast_overlay)

        self.pages: dict[str, Page] = {}
        for page_class in (OutputPage, InputPage, AppsPage, BluetoothPage, CardsPage):
            page = page_class(self)
            self.pages[page.get_tag()] = page
        self._current: Optional[Page] = None

        self._build_sidebar()
        self._install_actions()

        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 600sp"))
        breakpoint.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(breakpoint)

        self._changed_handler = backend.connect("changed", self._on_backend_changed)
        self.connect("close-request", self._on_close)
        self.show_page("output")

    # -------------------------------------------------------------------------
    # Sidebar navigation
    # -------------------------------------------------------------------------

    def _build_sidebar(self) -> None:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Sound", subtitle="MrFaws"))
        about = Gtk.Button(icon_name="help-about-symbolic", tooltip_text="About")
        about.connect("clicked", lambda *_: self._show_about())
        header.pack_end(about)
        toolbar.add_top_bar(header)

        self.sidebar_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.sidebar_list.add_css_class("navigation-sidebar")
        self._sidebar_rows: dict[str, Gtk.ListBoxRow] = {}
        for tag, page in self.pages.items():
            box = Gtk.Box(spacing=12, margin_top=2, margin_bottom=2)
            box.append(Gtk.Image(icon_name=page.icon_name))
            box.append(Gtk.Label(label=page.get_title(), xalign=0))
            row = Gtk.ListBoxRow(child=box)
            row.page_tag = tag
            self.sidebar_list.append(row)
            self._sidebar_rows[tag] = row
        self.sidebar_list.connect("row-activated", lambda _l, row: self.show_page(row.page_tag))

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      child=self.sidebar_list, vexpand=True)
        toolbar.set_content(scroller)
        self.split.set_sidebar(Adw.NavigationPage(title=APP_NAME, tag="sidebar", child=toolbar))

    def show_page(self, tag: str) -> None:
        page = self.pages[tag]
        self.sidebar_list.select_row(self._sidebar_rows[tag])
        if page is not self._current:
            if self._current is not None:
                self._current.on_hidden()
            self._current = page
            self.split.set_content(page)
        self.split.set_show_content(True)
        if page.stale:
            page.refresh()
        page.on_shown()

    def _on_backend_changed(self, _backend) -> None:
        # Redraw the visible page now; others redraw when next shown.
        for page in self.pages.values():
            page.stale = True
        if self._current is not None:
            self._current.refresh()

    def _on_close(self, _window) -> bool:
        for page in self.pages.values():
            page.on_hidden()
        self.backend.disconnect(self._changed_handler)
        return False

    # -------------------------------------------------------------------------
    # Actions
    # -------------------------------------------------------------------------

    def _install_actions(self) -> None:
        refresh = Gio.SimpleAction.new("refresh", None)
        refresh.connect("activate", lambda *_: self.backend.reload())
        self.add_action(refresh)

        mute = Gio.SimpleAction.new("toggle-mute", None)
        mute.connect("activate", lambda *_: self.backend.toggle_output_mute())
        self.add_action(mute)

    def toast(self, message: str) -> None:
        self.toast_overlay.add_toast(Adw.Toast(title=GLib.markup_escape_text(message)))

    def spawn(self, argv: list[str]) -> None:
        try:
            Gio.Subprocess.new(argv, Gio.SubprocessFlags.NONE)
        except GLib.Error as error:
            self.toast(error.message)

    def restart_audio(self) -> None:
        self.toast("Restarting audio…")
        try:
            process = Gio.Subprocess.new(
                ["systemctl", "--user", "restart", "wireplumber", "pipewire", "pipewire-pulse"],
                Gio.SubprocessFlags.STDERR_PIPE)
        except GLib.Error as error:
            self.toast(error.message)
            return

        def done(proc, result):
            proc.wait_finish(result)
            self.toast("Audio restarted" if proc.get_successful()
                       else "Could not restart audio")
            self.backend.queue_reload()

        process.wait_async(None, done)

    def _show_about(self) -> None:
        about = Adw.AboutDialog(application_name=APP_NAME, version=APP_VERSION,
                                developer_name="MrFaws",
                                application_icon="audio-volume-high",
                                comments="Dracula-themed volume control for PipeWire.")
        about.present(self)
