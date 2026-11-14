# =============================================================================
# layout.py - Drag-and-drop monitor arrangement
# =============================================================================
# A custom widget that draws every enabled, non-mirrored monitor as a
# rectangle in Hyprland's logical coordinates (pixels / scale), scaled to fit
# the widget. Click selects a monitor, drag moves it.
#
# Drawing uses GTK4's Gtk.Snapshot (GSK render nodes) rather than a
# Gtk.DrawingArea + cairo: from Python, cairo needs the extra python-cairo
# package (without it: "Couldn't find foreign struct converter for
# 'cairo.Context'"), while snapshots work with the packages already installed.
#
# On release the dragged monitor snaps against the edge of the nearest other
# monitor (left/right/above/below, with top/bottom or left/right edges lined
# up when close), so you cannot leave gaps or overlaps that make the cursor
# get stuck between screens. Afterwards the whole layout is shifted so the
# top-left corner is 0x0, which keeps the saved positions tidy.
#
# The widget does not own any state: it reads and writes the Settings
# objects the window passes in and calls on_changed / on_selected.
# =============================================================================

from __future__ import annotations

import math
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, Graphene, Gsk, Gtk, Pango

from . import theme
from .hypr import Monitor, Settings, logical_size

SNAP_FRACTION = 0.12    # edges closer than 12 % of the monitor size line up


def _rgba(hex_color: str) -> Gdk.RGBA:
    color = Gdk.RGBA()
    color.parse(hex_color)
    return color


def _rect(x: float, y: float, w: float, h: float) -> Graphene.Rect:
    return Graphene.Rect().init(x, y, w, h)


class Rect:
    def __init__(self, x: float, y: float, w: float, h: float) -> None:
        self.x, self.y, self.w, self.h = x, y, w, h

    def overlaps(self, other: "Rect") -> bool:
        return (self.x < other.x + other.w and other.x < self.x + self.w and
                self.y < other.y + other.h and other.y < self.y + self.h)


def snap_position(moving: Rect, others: list[Rect]) -> tuple[int, int]:
    """Closest position where `moving` touches one of `others` edge to edge."""
    if not others:
        return (0, 0)
    best: Optional[tuple[float, float, float]] = None
    for o in others:
        # Slide along the touching edge, but keep at least 1px of shared edge.
        cy = min(max(moving.y, o.y - moving.h + 1), o.y + o.h - 1)
        cx = min(max(moving.x, o.x - moving.w + 1), o.x + o.w - 1)
        # Line up edges that are already nearly aligned.
        ty, tx = SNAP_FRACTION * moving.h, SNAP_FRACTION * moving.w
        for aligned in (o.y, o.y + o.h - moving.h):
            if abs(cy - aligned) < ty:
                cy = aligned
        for aligned in (o.x, o.x + o.w - moving.w):
            if abs(cx - aligned) < tx:
                cx = aligned
        candidates = [
            (o.x + o.w, cy),            # right of o
            (o.x - moving.w, cy),       # left of o
            (cx, o.y - moving.h),       # above o
            (cx, o.y + o.h),            # below o
        ]
        for x, y in candidates:
            test = Rect(x, y, moving.w, moving.h)
            if any(test.overlaps(r) for r in others):
                continue
            dist = math.hypot(x - moving.x, y - moving.y)
            if best is None or dist < best[0]:
                best = (dist, x, y)
    if best is None:
        # Every edge is blocked (crowded layout): put it right of everything.
        return (round(max(r.x + r.w for r in others)), round(min(r.y for r in others)))
    return (round(best[1]), round(best[2]))


class LayoutView(Gtk.Widget):
    PADDING = 24
    HEIGHT = 230

    def __init__(self, on_selected: Callable[[str], None],
                 on_changed: Callable[[], None]) -> None:
        super().__init__(hexpand=True)
        self.add_css_class("layout-view")
        self.on_selected = on_selected
        self.on_changed = on_changed
        self.monitors: list[Monitor] = []
        self.settings: dict[str, Settings] = {}
        self.selected = ""
        self._dragging = ""
        self._drag_origin = (0, 0)
        self._view = (1.0, 0.0, 0.0)    # zoom, offset x, offset y (frozen while dragging)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_begin)
        drag.connect("drag-update", self._on_update)
        drag.connect("drag-end", self._on_end)
        self.add_controller(drag)

    # -------------------------------------------------------------------------
    # Data
    # -------------------------------------------------------------------------

    def set_data(self, monitors: list[Monitor], settings: dict[str, Settings],
                 selected: str) -> None:
        self.monitors = monitors
        self.settings = settings
        self.selected = selected
        self.queue_draw()

    def _placed(self) -> list[tuple[Monitor, Rect]]:
        """Monitors that occupy space in the layout (on and not mirroring)."""
        out = []
        for monitor in self.monitors:
            s = self.settings.get(monitor.name)
            if s and s.enabled and not s.mirror:
                w, h = logical_size(s)
                out.append((monitor, Rect(s.x, s.y, w, h)))
        return out

    def _compute_view(self, width: int, height: int) -> tuple[float, float, float]:
        placed = self._placed()
        if not placed:
            return (1.0, 0.0, 0.0)
        min_x = min(r.x for _, r in placed)
        min_y = min(r.y for _, r in placed)
        max_x = max(r.x + r.w for _, r in placed)
        max_y = max(r.y + r.h for _, r in placed)
        span_w, span_h = max(1, max_x - min_x), max(1, max_y - min_y)
        zoom = min((width - 2 * self.PADDING) / span_w,
                   (height - 2 * self.PADDING) / span_h)
        zoom = max(zoom, 0.01)
        off_x = (width - span_w * zoom) / 2 - min_x * zoom
        off_y = (height - span_h * zoom) / 2 - min_y * zoom
        return (zoom, off_x, off_y)

    # -------------------------------------------------------------------------
    # Drawing
    # -------------------------------------------------------------------------

    def do_measure(self, orientation, _for_size):
        if orientation == Gtk.Orientation.VERTICAL:
            return (self.HEIGHT, self.HEIGHT, -1, -1)
        return (120, 400, -1, -1)

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        if not self._dragging:
            self._view = self._compute_view(self.get_width(), self.get_height())
        zoom, off_x, off_y = self._view

        for monitor, r in self._placed():
            x, y = off_x + r.x * zoom + 2, off_y + r.y * zoom + 2
            w, h = r.w * zoom - 4, r.h * zoom - 4
            if w <= 0 or h <= 0:
                continue
            selected = monitor.name == self.selected
            mirrored_by = [n for n, s in self.settings.items()
                           if s.enabled and s.mirror == monitor.name]

            radius = min(10.0, w / 2, h / 2)
            rounded = Gsk.RoundedRect()
            rounded.init_from_rect(_rect(x, y, w, h), radius)
            snapshot.push_rounded_clip(rounded)
            snapshot.append_color(_rgba(theme.SURFACE if selected else theme.BACKGROUND), _rect(x, y, w, h))
            snapshot.pop()
            border = 3.0 if selected else 1.5
            color = _rgba(theme.PURPLE if selected else theme.COMMENT)
            snapshot.append_border(rounded, [border] * 4, [color] * 4)

            s = self.settings[monitor.name]
            lines = [(monitor.name, theme.PURPLE if selected else theme.FOREGROUND, True),
                     (f"{s.mode.resolution if s.mode else '?'} · {s.scale:.3g}x",
                      theme.COMMENT, False)]
            if mirrored_by:
                lines.append(("+ " + ", ".join(mirrored_by), theme.CYAN, False))
            self._text(snapshot, x, y, w, h, lines)

    def _text(self, snapshot: Gtk.Snapshot, x, y, w, h, lines) -> None:
        layouts = []
        for text, color, bold in lines:
            layout = self.create_pango_layout(text)
            desc = Pango.FontDescription.from_string("Sans Bold 11" if bold else "Sans 9")
            layout.set_font_description(desc)
            layout.set_width(int(max(1, w - 12) * Pango.SCALE))
            layout.set_ellipsize(Pango.EllipsizeMode.END)
            layout.set_alignment(Pango.Alignment.CENTER)
            layouts.append((layout, color))
        total = sum(l.get_pixel_size()[1] for l, _ in layouts)
        if total > h:
            layouts = layouts[:1]   # tiny rectangle: name only
            total = layouts[0][0].get_pixel_size()[1]
        cy = y + (h - total) / 2
        for layout, color in layouts:
            snapshot.save()
            snapshot.translate(Graphene.Point().init(x + 6, cy))
            snapshot.append_layout(layout, _rgba(color))
            snapshot.restore()
            cy += layout.get_pixel_size()[1]

    # -------------------------------------------------------------------------
    # Mouse
    # -------------------------------------------------------------------------

    def _hit(self, px: float, py: float) -> Optional[str]:
        zoom, off_x, off_y = self._view
        for monitor, r in reversed(self._placed()):
            if (off_x + r.x * zoom <= px <= off_x + (r.x + r.w) * zoom and
                    off_y + r.y * zoom <= py <= off_y + (r.y + r.h) * zoom):
                return monitor.name
        return None

    def _on_begin(self, _gesture, px: float, py: float) -> None:
        name = self._hit(px, py)
        if name is None:
            return
        self.selected = name
        self.on_selected(name)
        if len(self._placed()) > 1:
            self._dragging = name
            s = self.settings[name]
            self._drag_origin = (s.x, s.y)
        self.queue_draw()

    def _on_update(self, _gesture, dx: float, dy: float) -> None:
        if not self._dragging:
            return
        zoom = self._view[0]
        s = self.settings[self._dragging]
        s.x = round(self._drag_origin[0] + dx / zoom)
        s.y = round(self._drag_origin[1] + dy / zoom)
        self.queue_draw()

    def _on_end(self, _gesture, _dx: float, _dy: float) -> None:
        if not self._dragging:
            return
        name, self._dragging = self._dragging, ""
        placed = self._placed()
        moving = next(r for m, r in placed if m.name == name)
        others = [r for m, r in placed if m.name != name]
        s = self.settings[name]
        s.x, s.y = snap_position(moving, others)
        normalize(self.settings)
        self.queue_draw()
        self.on_changed()


def normalize(settings: dict[str, Settings]) -> None:
    """Shift all placed monitors so the layout starts at 0x0."""
    placed = [s for s in settings.values() if s.enabled and not s.mirror]
    if not placed:
        return
    dx = min(s.x for s in placed)
    dy = min(s.y for s in placed)
    for s in placed:
        s.x -= dx
        s.y -= dy
