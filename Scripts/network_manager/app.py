# =============================================================================
# app.py - Application object and startup
# =============================================================================
# Adw.Application gives us:
#   * single-instance behaviour: launching the app again (e.g. from the
#     Hyprland launcher or Waybar) raises the existing window instead of
#     starting a second copy. This works via the D-Bus name APP_ID.
#   * the libadwaita style manager, used by theme.py.
#
# The application ID also becomes the Wayland app_id, which is what the
# .desktop file's StartupWMClass refers to so Hyprland matches the window
# to its launcher entry.
# =============================================================================

from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("NM", "1.0")
from gi.repository import Adw, Gio, GLib, Gtk

from . import APP_ID, APP_NAME
from .backend import NetworkBackend, friendly_error
from .theme import apply_theme


class NetworkManagerApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        GLib.set_application_name(APP_NAME)
        self.backend: NetworkBackend | None = None
        self.startup_error: str | None = None

    # -------------------------------------------------------------------------
    # GApplication lifecycle
    # -------------------------------------------------------------------------

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        apply_theme()
        Gtk.Window.set_default_icon_name("network-wireless")

        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("window.close", ["<Control>w"])
        self.set_accels_for_action("win.refresh", ["F5", "<Control>r"])

        # Connecting to NetworkManager can only fail if the system D-Bus
        # itself is unavailable; a stopped NM daemon is handled by the UI.
        try:
            self.backend = NetworkBackend()
        except GLib.Error as error:
            self.startup_error = friendly_error(error)

    def do_activate(self) -> None:
        window = self.get_active_window()
        if window is None:
            if self.backend is None:
                window = self._error_window()
            else:
                # Imported here so a GTK/libadwaita import problem in the UI
                # module is reported after the theme is set up.
                from .ui import MainWindow
                window = MainWindow(self, self.backend)
        window.present()

    def _error_window(self) -> Adw.ApplicationWindow:
        """Fallback window when the NetworkManager client cannot start."""
        window = Adw.ApplicationWindow(application=self, title=APP_NAME,
                                       default_width=560, default_height=420)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(Adw.StatusPage(
            icon_name="network-error-symbolic",
            title="Cannot Reach NetworkManager",
            description=self.startup_error or "Unknown error"))
        window.set_content(toolbar)
        return window


def main(argv: list[str] | None = None) -> int:
    """Entry point used by Scripts/wifi-gui.py."""
    app = NetworkManagerApp()
    return app.run(argv if argv is not None else sys.argv)
