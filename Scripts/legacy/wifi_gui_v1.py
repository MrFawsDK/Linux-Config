# =============================================================================
# LEGACY: MrFaws Wi-Fi Manager V1 (archived in V2)
# =============================================================================
# This file is a copy of the original Scripts/wifi-gui.py (V1, commit
# 53d8d11 on main), unchanged apart from this header. It is kept here for
# reference only.
#
# Reason for retirement (replaced in V2 by the network_manager package):
#   - Passed Wi-Fi passwords as nmcli command-line arguments, which are
#     readable by other local processes through /proc/<pid>/cmdline.
#   - Activated saved profiles by name instead of UUID, so renamed
#     profiles (where name != SSID) could not be activated.
#   - Merged networks by SSID only, hiding duplicate SSIDs with different
#     security types.
#   - No support for hidden SSIDs or enterprise (802.1X) networks.
#   - Parsed nmcli text output in background threads instead of using
#     NetworkManager's libnm API on the GTK main loop.
#
# This file is NOT imported or executed by V2. It can still be started
# manually for comparison with:  python3 Scripts/legacy/wifi_gui_v1.py
# (V1 uses GTK3 and runs as its own separate process.)
# =============================================================================

#!/usr/bin/env python3

import subprocess
import threading
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib

CSS = """
window, list, scrolledwindow {
    background: #282a36;
}
label {
    color: #f8f8f2;
}
button {
    background-image: none;
    background: #44475a;
    color: #f8f8f2;
    border: 1px solid #6272a4;
    border-radius: 8px;
    padding: 9px;
}
button:hover {
    background: #6272a4;
    border-color: #bd93f9;
}
entry {
    background: #44475a;
    color: #f8f8f2;
    border-radius: 6px;
}
list row {
    background: #282a36;
    padding: 8px;
    border-bottom: 1px solid #44475a;
}
.title {
    color: #bd93f9;
    font-size: 24px;
    font-weight: bold;
}
.subtitle {
    color: #8be9fd;
}
"""

def split_nmcli(line):
    fields, current, escaped = [], "", False
    for char in line:
        if escaped:
            current += char
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append(current)
            current = ""
        else:
            current += char
    fields.append(current)
    return fields


class WifiApp(Gtk.Window):
    def __init__(self):
        super().__init__(title="MrFaws Wi-Fi")
        self.set_default_size(560, 540)
        self.set_border_width(20)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.add(root)

        title = Gtk.Label(label="Network manager | WI-FI")
        title.get_style_context().add_class("title")
        root.pack_start(title, False, False, 0)

        subtitle = Gtk.Label(label="Made by the one and only MrFaws")
        subtitle.get_style_context().add_class("subtitle")
        root.pack_start(subtitle, False, False, 0)

        self.status = Gtk.Label(label="Ready")
        root.pack_start(self.status, False, False, 0)

        self.refresh_button = Gtk.Button(label="↻  Scan for networks/Wi-Fi")
        self.refresh_button.connect("clicked", self.refresh)
        root.pack_start(self.refresh_button, False, False, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        root.pack_start(scroll, True, True, 0)

        self.networks = Gtk.ListBox()
        scroll.add(self.networks)

        self.refresh()

    def set_status(self, message):
        self.status.set_text(message)
        return False

    def refresh(self, *_):
        self.refresh_button.set_sensitive(False)
        self.set_status("Scanning for Wi-Fi networks...")
        threading.Thread(target=self.scan, daemon=True).start()

    def scan(self):
        result = subprocess.run(
            ["nmcli", "-t", "--escape", "yes",
             "-f", "IN-USE,SSID,SIGNAL,SECURITY",
             "device", "wifi", "list", "--rescan", "yes"],
            capture_output=True, text=True
        )

        if result.returncode:
            GLib.idle_add(self.set_status, result.stderr.strip())
            GLib.idle_add(self.refresh_button.set_sensitive, True)
            return

        found = {}
        for line in result.stdout.splitlines():
            fields = split_nmcli(line)
            if len(fields) != 4:
                continue

            active, ssid, signal, security = fields
            if not ssid:
                continue

            try:
                strength = int(signal)
            except ValueError:
                strength = 0

            if ssid not in found or strength > found[ssid][1]:
                found[ssid] = (active == "*", strength, security)

        GLib.idle_add(self.render_networks, found)

    def render_networks(self, found):
        for child in self.networks.get_children():
            self.networks.remove(child)

        for ssid, (active, signal, security) in sorted(
            found.items(), key=lambda item: -item[1][1]
        ):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

            name = Gtk.Label(label=ssid)
            name.set_xalign(0)
            name.set_ellipsize(3)
            row.pack_start(name, True, True, 0)

            details = Gtk.Label(
                label=f"{signal}%  {'Open' if security == '--' else 'Secure'}"
            )
            row.pack_start(details, False, False, 0)

            button = Gtk.Button(label="Connected" if active else "Connect")
            button.set_sensitive(not active)
            button.connect("clicked", self.connect_network, ssid, security)
            row.pack_start(button, False, False, 0)

            self.networks.add(row)

        self.networks.show_all()
        self.refresh_button.set_sensitive(True)
        self.set_status(f"{len(found)} networks found")

    def connect_network(self, button, ssid, security):
        if "802.1X" in security or "EAP" in security:
            self.set_status("Enterprise Wi-Fi: use an existing saved profile.")

        password = None

        if security != "--" and "802.1X" not in security and "EAP" not in security:
            dialog = Gtk.Dialog(
                title=f"Connect to {ssid}",
                transient_for=self,
                flags=Gtk.DialogFlags.MODAL
            )
            dialog.add_buttons(
                "Cancel", Gtk.ResponseType.CANCEL,
                "Connect", Gtk.ResponseType.OK
            )

            entry = Gtk.Entry()
            entry.set_visibility(False)
            entry.set_placeholder_text("Wi-Fi password (blank if saved)")
            entry.set_margin_top(12)
            entry.set_margin_bottom(12)
            entry.set_margin_start(12)
            entry.set_margin_end(12)
            dialog.get_content_area().pack_start(entry, True, True, 0)
            dialog.show_all()

            response = dialog.run()
            password = entry.get_text()
            dialog.destroy()

            if response != Gtk.ResponseType.OK:
                return

        self.set_status(f"Connecting to {ssid}...")
        threading.Thread(
            target=self.do_connect,
            args=(ssid, password),
            daemon=True
        ).start()

    def do_connect(self, ssid, password):
        # Reuse an existing NetworkManager connection first.
        if not password:
            command = ["nmcli", "--wait", "20",
                       "connection", "up", "id", ssid]
        else:
            command = ["nmcli", "--wait", "20",
                       "device", "wifi", "connect", ssid,
                       "password", password]

        result = subprocess.run(command, capture_output=True, text=True)

        # For new open networks, create a connection if none is saved.
        if result.returncode and password is None:
            result = subprocess.run(
                ["nmcli", "--wait", "20",
                 "device", "wifi", "connect", ssid],
                capture_output=True, text=True
            )

        if result.returncode == 0:
            GLib.idle_add(self.set_status, f"Connected to {ssid}!")
            GLib.idle_add(self.refresh)
        else:
            message = result.stderr.strip() or "Connection failed"
            GLib.idle_add(self.set_status, message[:140])


if __name__ == "__main__":
    window = WifiApp()
    window.connect("destroy", Gtk.main_quit)
    window.show_all()
    Gtk.main()
