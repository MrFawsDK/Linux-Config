# =============================================================================
# app.py - Application object and startup
# =============================================================================
# Adw.Application gives us single-instance behaviour (running `screen`
# again raises the open window, via the D-Bus name APP_ID) and the
# libadwaita style manager used by theme.py. APP_ID is also the Wayland
# app_id that the .desktop file's StartupWMClass refers to.
# =============================================================================

from __future__ import annotations

import shutil
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

from . import APP_ID, APP_NAME
from .theme import apply_theme


class DisplayManagerApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        GLib.set_application_name(APP_NAME)
        self.events = None

    # -------------------------------------------------------------------------
    # GApplication lifecycle
    # -------------------------------------------------------------------------

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        apply_theme()
        Gtk.Window.set_default_icon_name("video-display")

        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("window.close", ["<Control>w", "Escape"])
        self.set_accels_for_action("win.refresh", ["F5", "<Control>r"])
        self.set_accels_for_action("win.brightness-up", ["<Control>Up", "<Control>plus"])
        self.set_accels_for_action("win.brightness-down", ["<Control>Down", "<Control>minus"])

        if shutil.which("hyprctl"):
            from .hypr import HyprEvents
            self.events = HyprEvents()

    def do_activate(self) -> None:
        window = self.get_active_window()
        if window is None:
            if self.events is None:
                window = self._error_window()
            else:
                from .ui import MainWindow
                window = MainWindow(self, self.events)
        window.present()

    def do_shutdown(self) -> None:
        if self.events is not None:
            self.events.stop()
        Adw.Application.do_shutdown(self)

    def _error_window(self) -> Adw.ApplicationWindow:
        """Fallback window when hyprctl is not available."""
        window = Adw.ApplicationWindow(application=self, title=APP_NAME,
                                       default_width=560, default_height=420)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(Adw.StatusPage(
            icon_name="video-display-symbolic",
            title="Hyprland Not Found",
            description="This app configures monitors through hyprctl and "
                        "only works inside a Hyprland session."))
        window.set_content(toolbar)
        return window


def main(argv: list[str] | None = None) -> int:
    """Entry point used by Scripts/display-gui.py."""
    app = DisplayManagerApp()
    return app.run(argv if argv is not None else sys.argv)
