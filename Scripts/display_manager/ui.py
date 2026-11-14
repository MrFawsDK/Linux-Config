# =============================================================================
# ui.py - Main window
# =============================================================================
# One scrollable page, top to bottom:
#
#   Brightness    big slider + presets for the laptop backlight (if any)
#   Arrangement   drag-and-drop layout of the monitors (layout.py)
#   Displays      list of every monitor, including ones that are off
#   <selected>    on/off, resolution, refresh rate, scale, rotation, mirror,
#                 position of the monitor picked in the list or the layout
#
# Brightness changes apply immediately. Monitor changes are collected in
# self.pending and only sent to Hyprland when you press Apply. Like GNOME,
# Apply then asks "Keep these settings?" and reverts by itself after
# CONFIRM_SECONDS, so a mode your monitor cannot show (black screen) fixes
# itself without you having to find the button. Keep = saved to
# ~/.config/hypr/monitors.lua (store.py).
# =============================================================================

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk, Pango

from . import APP_NAME, APP_VERSION, store
from .backlight import Backlight
from .hypr import (TRANSFORMS, HyprError, HyprEvents, Monitor, Settings, apply,
                   list_monitors, logical_size, valid_scales, validate)
from .layout import LayoutView, Rect, normalize, snap_position

CONFIRM_SECONDS = 15


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def same(a: Settings, b: Settings) -> bool:
    """Equal for our purposes (scale from hyprctl is float32-ish)."""
    if a.enabled != b.enabled:
        return False
    if not a.enabled:
        return True
    return (a.mode == b.mode and a.x == b.x and a.y == b.y and
            abs(a.scale - b.scale) < 1e-3 and a.transform == b.transform and
            a.mirror == b.mirror)


def badge(text: str, kind: str = "") -> Gtk.Label:
    label = Gtk.Label(label=text, valign=Gtk.Align.CENTER)
    label.add_css_class("badge")
    if kind:
        label.add_css_class(kind)
    return label


def scale_label(mode, scale: float, transform: int) -> str:
    w, h = logical_size(Settings(mode=mode, scale=scale, transform=transform))
    return f"{round(scale * 100)} %  ·  looks like {w}x{h}"


# -----------------------------------------------------------------------------
# Brightness card
# -----------------------------------------------------------------------------

class BrightnessCard(Gtk.Box):
    """Big brightness slider. Follows changes from the brightness keys.

    While the user drags, writes are coalesced (one D-Bus call per WRITE_MS)
    and the 1 s poll is ignored for HOLD_S, so the slider never jumps back
    underneath the pointer.
    """

    HOLD_S = 1.2
    WRITE_MS = 40
    PRESETS = (10, 25, 50, 75, 100)

    def __init__(self, device: Backlight, on_error) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.device = device
        self.on_error = on_error
        self._updating = False
        self._touched_at = 0.0
        self._pending: Optional[float] = None
        self._write_source = 0
        self.add_css_class("card")
        self.add_css_class("hero")

        top = Gtk.Box(spacing=16)
        icon = Gtk.Image(icon_name="display-brightness-symbolic", pixel_size=32,
                         valign=Gtk.Align.CENTER)
        icon.add_css_class("hero-icon")
        top.append(icon)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                        valign=Gtk.Align.CENTER, hexpand=True)
        title = Gtk.Label(label="Brightness", xalign=0)
        title.add_css_class("hero-title")
        subtitle = Gtk.Label(label=f"Built-in display · {device.name}", xalign=0)
        subtitle.add_css_class("dim-label")
        texts.append(title)
        texts.append(subtitle)
        top.append(texts)
        self.append(top)

        row = Gtk.Box(spacing=12)
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, 100, 1)
        self.scale.set_increments(1, 5)
        self.scale.set_draw_value(False)
        self.scale.set_hexpand(True)
        self.scale.add_css_class("brightness")
        self.scale.connect("value-changed", self._on_value_changed)
        row.append(self.scale)
        self.label = Gtk.Label(xalign=1)
        self.label.add_css_class("percent-label")
        row.append(self.label)
        self.append(row)

        # WrapBox: the presets flow onto a second line in a narrow tile.
        presets = Adw.WrapBox(child_spacing=8, line_spacing=8)
        for value in self.PRESETS:
            button = Gtk.Button(label=f"{value}%")
            button.add_css_class("preset")
            button.connect("clicked", lambda _b, v=value: self.scale.set_value(v))
            presets.append(button)
        self.append(presets)

        self.poll()

    def _on_value_changed(self, scale: Gtk.Scale) -> None:
        value = round(scale.get_value())
        self.label.set_label(f"{value}%")
        if self._updating:
            return
        self._touched_at = time.monotonic()
        self._pending = value
        if not self._write_source:
            self._write_source = GLib.timeout_add(self.WRITE_MS, self._flush)

    def _flush(self) -> bool:
        self._write_source = 0
        value, self._pending = self._pending, None
        if value is not None:
            try:
                self.device.set_percent(value)
            except GLib.Error as error:
                self.on_error(f"logind refused the brightness change: {error.message}")
        return GLib.SOURCE_REMOVE

    def step(self, delta: int) -> None:
        self.scale.set_value(self.scale.get_value() + delta)

    def poll(self) -> bool:
        """Re-read sysfs (called every second by the window)."""
        if time.monotonic() - self._touched_at < self.HOLD_S:
            return GLib.SOURCE_CONTINUE
        try:
            value = self.device.percent()
        except (OSError, ValueError):
            return GLib.SOURCE_CONTINUE
        if value != round(self.scale.get_value()):
            self._updating = True
            self.scale.set_value(value)
            self._updating = False
        self.label.set_label(f"{round(self.scale.get_value())}%")
        return GLib.SOURCE_CONTINUE


# -----------------------------------------------------------------------------
# Main window
# -----------------------------------------------------------------------------

class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, events: HyprEvents) -> None:
        super().__init__(application=app, title=APP_NAME,
                         default_width=760, default_height=860)
        self.set_size_request(300, 400)
        self.events = events
        self.monitors: list[Monitor] = []
        self.pending: dict[str, Settings] = {}
        self.selected = ""
        self._syncing = False
        self._confirming = False

        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)
        toolbar = Adw.ToolbarView()
        self.toast_overlay.set_child(toolbar)

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Displays", subtitle="MrFaws"))
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh (F5)")
        refresh.set_action_name("win.refresh")
        header.pack_start(refresh)
        about = Gtk.Button(icon_name="help-about-symbolic", tooltip_text="About")
        about.connect("clicked", lambda *_: self._show_about())
        header.pack_end(about)
        toolbar.add_top_bar(header)

        self.hook_banner = Adw.Banner(
            title="hyprland.lua does not load monitors.lua: saved layouts won't survive a reboot")
        toolbar.add_top_bar(self.hook_banner)

        page = Adw.PreferencesPage()
        toolbar.set_content(page)

        # --- Brightness -------------------------------------------------------
        self.backlight = Backlight.find()
        self.brightness: Optional[BrightnessCard] = None
        if self.backlight is not None:
            group = Adw.PreferencesGroup()
            self.brightness = BrightnessCard(self.backlight, self.toast)
            group.add(self.brightness)
            page.add(group)
            self._poll_source = GLib.timeout_add_seconds(1, self.brightness.poll)

        # --- Arrangement ------------------------------------------------------
        self.layout_group = Adw.PreferencesGroup(
            title="Arrangement",
            description="Drag the displays to match how they stand on your desk.")
        frame = Gtk.Frame()
        frame.add_css_class("layout-card")
        self.layout_view = LayoutView(self._select, self._on_layout_changed)
        frame.set_child(self.layout_view)
        self.layout_group.add(frame)
        page.add(self.layout_group)

        # --- Display list -----------------------------------------------------
        self.list_group = Adw.PreferencesGroup(title="Displays")
        self._list_rows: list[Gtk.Widget] = []
        page.add(self.list_group)

        # --- Settings of the selected display ----------------------------------
        self.settings_group = Adw.PreferencesGroup()
        page.add(self.settings_group)
        self._build_settings_rows()

        # --- Bottom bar: Reset / Apply -----------------------------------------
        bar = Gtk.ActionBar()
        self.status = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.status.add_css_class("hint")
        bar.pack_start(self.status)
        self.apply_button = Gtk.Button(label="Apply")
        self.apply_button.add_css_class("suggested-action")
        self.apply_button.connect("clicked", lambda *_: self._apply())
        bar.pack_end(self.apply_button)
        self.reset_button = Gtk.Button(label="Reset")
        self.reset_button.connect("clicked", lambda *_: self._reset())
        bar.pack_end(self.reset_button)
        toolbar.add_bottom_bar(bar)

        self._install_actions()
        self._events_handler = events.connect("changed", self._on_hypr_changed)
        self.connect("close-request", self._on_close)
        self.reload()

    # -------------------------------------------------------------------------
    # Settings rows (created once, updated by _sync_settings)
    # -------------------------------------------------------------------------

    def _build_settings_rows(self) -> None:
        g = self.settings_group

        self.enabled_row = Adw.SwitchRow(title="On",
                                         subtitle="Turned off displays stay off until you turn them back on (not saved)")
        self.enabled_row.connect("notify::active", self._on_enabled)
        g.add(self.enabled_row)

        self.resolution_row = Adw.ComboRow(title="Resolution")
        self.resolution_row.connect("notify::selected", self._on_resolution)
        g.add(self.resolution_row)

        self.rate_row = Adw.ComboRow(title="Refresh Rate")
        self.rate_row.connect("notify::selected", self._on_rate)
        g.add(self.rate_row)

        self.scale_row = Adw.ComboRow(title="Scale",
                                      subtitle="Bigger = larger text and windows")
        self.scale_row.connect("notify::selected", self._on_scale)
        g.add(self.scale_row)

        self.transform_row = Adw.ComboRow(title="Rotation")
        self.transform_row.set_model(Gtk.StringList.new([label for _v, label in TRANSFORMS]))
        self.transform_row.connect("notify::selected", self._on_transform)
        g.add(self.transform_row)

        self.mirror_row = Adw.ComboRow(title="Mirror",
                                       subtitle="Show the same picture as another display")
        self.mirror_row.connect("notify::selected", self._on_mirror)
        g.add(self.mirror_row)

        self.x_row = Adw.SpinRow.new_with_range(-32768, 32768, 1)
        self.x_row.set_title("Position X")
        self.x_row.connect("notify::value", self._on_position)
        g.add(self.x_row)
        self.y_row = Adw.SpinRow.new_with_range(-32768, 32768, 1)
        self.y_row.set_title("Position Y")
        self.y_row.connect("notify::value", self._on_position)
        g.add(self.y_row)

        self._scale_values: list[float] = []
        self._rate_values: list = []
        self._mirror_values: list[str] = []

    # -------------------------------------------------------------------------
    # Loading state
    # -------------------------------------------------------------------------

    def reload(self) -> None:
        try:
            self.monitors = list_monitors()
        except HyprError as error:
            self.toast(str(error))
            return
        self.pending = {m.name: m.live.copy() for m in self.monitors}
        names = [m.name for m in self.monitors]
        if self.selected not in names:
            focused = next((m.name for m in self.monitors if m.focused), None)
            self.selected = focused or (names[0] if names else "")
        self.hook_banner.set_revealed(not store.is_hooked())
        self._refresh_all()

    def _monitor(self, name: str) -> Optional[Monitor]:
        return next((m for m in self.monitors if m.name == name), None)

    def _dirty(self) -> dict[str, Settings]:
        return {m.name: self.pending[m.name] for m in self.monitors
                if not same(self.pending[m.name], m.live)}

    # -------------------------------------------------------------------------
    # Redrawing
    # -------------------------------------------------------------------------

    def _refresh_all(self) -> None:
        self._rebuild_list()
        self.layout_view.set_data(self.monitors, self.pending, self.selected)
        self.layout_group.set_visible(bool(self.monitors))
        self._sync_settings()
        self._update_bar()

    def _rebuild_list(self) -> None:
        for row in self._list_rows:
            self.list_group.remove(row)
        self._list_rows = []
        for monitor in self.monitors:
            s = self.pending[monitor.name]
            subtitle = monitor.name
            if s.enabled and s.mode:
                subtitle += f"  ·  {s.mode.resolution} @ {s.mode.refresh:.0f} Hz"
            row = Adw.ActionRow(title=monitor.title, subtitle=subtitle, activatable=True)
            row.set_use_markup(False)
            icon = "computer-symbolic" if monitor.internal else "video-display-symbolic"
            row.add_prefix(Gtk.Image(icon_name=icon))
            if not s.enabled:
                row.add_suffix(badge("Off", "off"))
            elif s.mirror:
                row.add_suffix(badge(f"Mirrors {s.mirror}"))
            elif monitor.focused:
                row.add_suffix(badge("Active", "active"))
            if monitor.name == self.selected:
                row.add_suffix(Gtk.Image(icon_name="object-select-symbolic"))
            row.connect("activated", lambda _r, n=monitor.name: self._select(n))
            self.list_group.add(row)
            self._list_rows.append(row)
        self.list_group.set_visible(len(self.monitors) > 1)

    def _sync_settings(self) -> None:
        """Put the selected monitor's pending settings into the rows."""
        monitor = self._monitor(self.selected)
        self.settings_group.set_visible(monitor is not None)
        if monitor is None:
            return
        s = self.pending[monitor.name]
        self._syncing = True
        try:
            self.settings_group.set_title(monitor.title)
            self.settings_group.set_description(
                f"{monitor.name} · {monitor.description}" if monitor.description else monitor.name)

            # Turning off is only offered with more than one display.
            self.enabled_row.set_visible(len(self.monitors) > 1)
            self.enabled_row.set_active(s.enabled)
            for row in (self.resolution_row, self.rate_row, self.scale_row,
                        self.transform_row, self.mirror_row, self.x_row, self.y_row):
                row.set_sensitive(s.enabled)

            resolutions = monitor.resolutions()
            self.resolution_row.set_model(Gtk.StringList.new(resolutions))
            if s.mode and s.mode.resolution in resolutions:
                self.resolution_row.set_selected(resolutions.index(s.mode.resolution))

            self._rate_values = monitor.rates(s.mode.resolution) if s.mode else []
            self.rate_row.set_model(Gtk.StringList.new(
                [f"{m.refresh:.2f} Hz" for m in self._rate_values]))
            if s.mode in self._rate_values:
                self.rate_row.set_selected(self._rate_values.index(s.mode))

            self._scale_values = valid_scales(s.mode, s.scale)
            self.scale_row.set_model(Gtk.StringList.new(
                [scale_label(s.mode, v, s.transform) for v in self._scale_values]))
            nearest = min(range(len(self._scale_values)),
                          key=lambda i: abs(self._scale_values[i] - s.scale))
            self.scale_row.set_selected(nearest)

            self.transform_row.set_selected(s.transform)

            # Mirror targets: other displays that are on and not mirrors.
            mirrored_by = [n for n, o in self.pending.items() if o.enabled and o.mirror == monitor.name]
            self._mirror_values = [""] + [
                m.name for m in self.monitors
                if m.name != monitor.name and self.pending[m.name].enabled
                and not self.pending[m.name].mirror]
            self.mirror_row.set_model(Gtk.StringList.new(
                ["Off"] + [f"{self._monitor(n).title} ({n})" for n in self._mirror_values[1:]]))
            self.mirror_row.set_selected(
                self._mirror_values.index(s.mirror) if s.mirror in self._mirror_values else 0)
            self.mirror_row.set_visible(len(self.monitors) > 1)
            if mirrored_by:
                self.mirror_row.set_sensitive(False)
                self.mirror_row.set_subtitle(f"{', '.join(mirrored_by)} mirrors this display")
            else:
                self.mirror_row.set_subtitle("Show the same picture as another display")

            placed = [o for o in self.pending.values() if o.enabled and not o.mirror]
            show_position = len(placed) > 1 and not s.mirror
            self.x_row.set_visible(show_position)
            self.y_row.set_visible(show_position)
            self.x_row.set_value(s.x)
            self.y_row.set_value(s.y)
        finally:
            self._syncing = False

    def _update_bar(self) -> None:
        dirty = self._dirty()
        self.apply_button.set_sensitive(bool(dirty) and not self._confirming)
        self.reset_button.set_sensitive(bool(dirty) and not self._confirming)
        if dirty:
            self.status.set_label(f"Unapplied changes: {', '.join(dirty)}")
        else:
            self.status.set_label("Changes are tested for 15 s before they are saved")

    def _changed(self, resnap: bool = False) -> None:
        """Called after any edit of the selected monitor's pending settings."""
        if resnap:
            self._resnap(self.selected)
        normalize(self.pending)
        self._refresh_all()

    def _resnap(self, name: str) -> None:
        """After a size change, move `name` back against its neighbours."""
        s = self.pending[name]
        if not s.enabled or s.mirror:
            return
        others = [Rect(o.x, o.y, *logical_size(o)) for n, o in self.pending.items()
                  if n != name and o.enabled and not o.mirror]
        if others:
            s.x, s.y = snap_position(Rect(s.x, s.y, *logical_size(s)), others)

    # -------------------------------------------------------------------------
    # Row handlers
    # -------------------------------------------------------------------------

    def _select(self, name: str) -> None:
        if name != self.selected:
            self.selected = name
            self._refresh_all()

    def _on_layout_changed(self) -> None:
        self._refresh_all()

    def _current(self) -> Optional[Settings]:
        if self._syncing:
            return None
        return self.pending.get(self.selected)

    def _on_enabled(self, row, _pspec) -> None:
        s = self._current()
        if s is None or s.enabled == row.get_active():
            return
        s.enabled = row.get_active()
        if s.enabled:
            # Coming back on: start to the right of the others, then snap.
            s.mirror = ""
            others = [o for n, o in self.pending.items()
                      if n != self.selected and o.enabled and not o.mirror]
            s.x = max((o.x + logical_size(o)[0] for o in others), default=0)
            s.y = 0
        else:
            for other in self.pending.values():
                if other.mirror == self.selected:
                    other.mirror = ""
        self._changed(resnap=s.enabled)

    def _on_resolution(self, row, _pspec) -> None:
        s = self._current()
        monitor = self._monitor(self.selected)
        if s is None or monitor is None:
            return
        resolutions = monitor.resolutions()
        index = row.get_selected()
        if index >= len(resolutions) or (s.mode and s.mode.resolution == resolutions[index]):
            return
        s.mode = monitor.rates(resolutions[index])[0]      # highest refresh rate
        scales = valid_scales(s.mode, s.scale)
        if not any(abs(v - s.scale) < 1e-3 for v in scales):
            s.scale = min(scales, key=lambda v: abs(v - s.scale))
        self._changed(resnap=True)

    def _on_rate(self, row, _pspec) -> None:
        s = self._current()
        index = row.get_selected()
        if s is None or index >= len(self._rate_values):
            return
        if s.mode != self._rate_values[index]:
            s.mode = self._rate_values[index]
            self._changed()

    def _on_scale(self, row, _pspec) -> None:
        s = self._current()
        index = row.get_selected()
        if s is None or index >= len(self._scale_values):
            return
        if abs(s.scale - self._scale_values[index]) > 1e-6:
            s.scale = self._scale_values[index]
            self._changed(resnap=True)

    def _on_transform(self, row, _pspec) -> None:
        s = self._current()
        index = row.get_selected()
        if s is None or index >= len(TRANSFORMS) or s.transform == TRANSFORMS[index][0]:
            return
        s.transform = TRANSFORMS[index][0]
        self._changed(resnap=True)

    def _on_mirror(self, row, _pspec) -> None:
        s = self._current()
        index = row.get_selected()
        if s is None or index >= len(self._mirror_values):
            return
        if s.mirror != self._mirror_values[index]:
            s.mirror = self._mirror_values[index]
            self._changed(resnap=not s.mirror)

    def _on_position(self, _row, _pspec) -> None:
        s = self._current()
        if s is None:
            return
        s.x, s.y = int(self.x_row.get_value()), int(self.y_row.get_value())
        # No snapping or normalising here: typed values are taken literally.
        self.layout_view.queue_draw()
        self._update_bar()

    # -------------------------------------------------------------------------
    # Apply / confirm / revert
    # -------------------------------------------------------------------------

    def _reset(self) -> None:
        self.pending = {m.name: m.live.copy() for m in self.monitors}
        self._refresh_all()

    def _apply(self) -> None:
        problem = validate(self.monitors, self.pending)
        if problem:
            self.toast(problem)
            return
        changes = {n: s.copy() for n, s in self._dirty().items()}
        if not changes:
            return
        snapshot = {m.name: m.live.copy() for m in self.monitors}
        try:
            apply(changes)
        except HyprError as error:
            self.toast(f"Hyprland rejected the change: {error}")
            self._try_revert(snapshot, changes)
            return
        self._confirming = True
        self._update_bar()
        # Give Hyprland a moment to finish the modeset before reading back.
        GLib.timeout_add(400, self._after_apply, snapshot, changes)

    def _after_apply(self, snapshot, changes) -> bool:
        self.reload()
        self._ask_keep(snapshot, changes)
        return GLib.SOURCE_REMOVE

    def _ask_keep(self, snapshot: dict[str, Settings], changes: dict[str, Settings]) -> None:
        dialog = Adw.AlertDialog(heading="Keep These Display Settings?")
        dialog.add_response("revert", "Revert")
        dialog.add_response("keep", "Keep Changes")
        dialog.set_response_appearance("keep", Adw.ResponseAppearance.SUGGESTED)
        # Enter and Escape both revert. A stray Enter (or a key pressed while
        # this dialog grabbed focus) must never keep a layout you cannot see.
        # Found the hard way in testing: with "keep" as default, typing in
        # another window confirmed a scale change.
        dialog.set_default_response("revert")
        dialog.set_close_response("revert")

        state = {"left": CONFIRM_SECONDS, "timer": 0}

        def update_body() -> None:
            dialog.set_body(f"Reverting to the previous settings in {state['left']} s.")

        def tick() -> bool:
            state["left"] -= 1
            if state["left"] <= 0:
                state["timer"] = 0
                dialog.force_close()       # emits the close response = revert
                return GLib.SOURCE_REMOVE
            update_body()
            return GLib.SOURCE_CONTINUE

        update_body()
        state["timer"] = GLib.timeout_add_seconds(1, tick)

        def on_response(_dialog, response: str) -> None:
            if state["timer"]:
                GLib.source_remove(state["timer"])
                state["timer"] = 0
            self._confirming = False
            if response == "keep":
                self._save()
            else:
                self._try_revert(snapshot, changes)
            self._update_bar()

        dialog.connect("response", on_response)
        dialog.present(self)

    def _try_revert(self, snapshot: dict[str, Settings], changes: dict[str, Settings]) -> None:
        try:
            apply({n: snapshot[n] for n in changes if n in snapshot})
        except HyprError as error:
            self.toast(f"Revert failed: {error}. Run `hyprctl reload` to restore the saved setup.")
        GLib.timeout_add(400, self._reload_once)

    def _reload_once(self) -> bool:
        self.reload()
        return GLib.SOURCE_REMOVE

    def _save(self) -> None:
        # Save what Hyprland actually runs now (it may round a scale).
        try:
            self.monitors = list_monitors()
            path = store.save(self.monitors, {m.name: m.live for m in self.monitors})
        except (HyprError, store.StoreError, OSError) as error:
            self.toast(f"Applied, but not saved: {error}")
            return
        if store.is_hooked():
            self.toast(f"Saved to {str(path).replace(str(Path.home()), '~', 1)}")
        else:
            self.toast("Saved, but hyprland.lua does not load monitors.lua yet")
        self.reload()

    # -------------------------------------------------------------------------
    # Events, actions, misc
    # -------------------------------------------------------------------------

    def _on_hypr_changed(self, _events) -> None:
        if self._confirming:
            return
        had_edits = bool(self._dirty())
        self.reload()
        self.toast("Displays changed" + (": unapplied edits were reset" if had_edits else ""))

    def _install_actions(self) -> None:
        refresh = Gio.SimpleAction.new("refresh", None)
        refresh.connect("activate", lambda *_: self.reload())
        self.add_action(refresh)
        for name, delta in (("brightness-up", 5), ("brightness-down", -5)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a, d=delta:
                           self.brightness.step(d) if self.brightness else None)
            self.add_action(action)

    def _on_close(self, _window) -> bool:
        if self.brightness is not None:
            GLib.source_remove(self._poll_source)
        self.events.disconnect(self._events_handler)
        return False

    def toast(self, message: str) -> None:
        self.toast_overlay.add_toast(Adw.Toast(title=GLib.markup_escape_text(message)))

    def _show_about(self) -> None:
        about = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon="video-display",
            version=APP_VERSION,
            developer_name="MrFaws",
            comments="Dracula-themed display settings for Hyprland: brightness, "
                     "resolution, scale, rotation, mirroring and arrangement.")
        about.present(self)
