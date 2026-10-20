# =============================================================================
# ui.py - Main window and sidebar pages
# =============================================================================
# Layout: an Adw.NavigationSplitView with a sidebar (page list) and a content
# area. Each sidebar entry is one Page subclass:
#
#   OverviewPage      current connection, addresses, Wi-Fi switch, NM status
#   WifiPage          scan results, connect/disconnect, hidden networks
#   SavedPage         all saved profiles: connect, edit, forget
#   EthernetVpnPage   wired adapters and profiles, VPN profiles
#   DiagnosticsPage   connectivity checks, service status, adapters, log
#
# Pages never poll. The backend emits a debounced 'changed' signal whenever
# NetworkManager state changes; the visible page redraws immediately and the
# others are marked stale and redraw when shown. Pages that rebuild lists
# compare a "signature" of the displayed data first, so a burst of
# irrelevant notifications does not make rows flicker.
# =============================================================================

from __future__ import annotations

import time
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("NM", "1.0")
from gi.repository import Adw, Gio, GLib, Gtk, NM, Pango

from . import APP_NAME, APP_VERSION
from .backend import NetworkBackend, device_state_label
from .connections import (CATEGORY_ETHERNET, CATEGORY_ICONS, CATEGORY_OTHER,
                          CATEGORY_TITLES, CATEGORY_VPN, CATEGORY_WIFI,
                          ProfileManager, SavedProfile, installed_vpn_plugins,
                          summarize_profile)
from .editor import ConnectionEditor
from .wifi import (Security, WifiManager, WifiNetwork, collect_networks,
                   signal_icon_name, signal_level, validate_wifi_password)


# -----------------------------------------------------------------------------
# Widget helpers
# -----------------------------------------------------------------------------

def action_row(title: str, subtitle: str = "") -> Adw.ActionRow:
    """ActionRow with markup disabled.

    SSIDs and profile names are user data and may contain '&' or '<', which
    would break (or be interpreted as) Pango markup.
    """
    row = Adw.ActionRow(title=title, subtitle=subtitle)
    row.set_use_markup(False)
    return row


def property_row(title: str, value: str = "") -> Adw.ActionRow:
    """Small title above a selectable value, used for status details."""
    row = action_row(title, value or "—")
    row.add_css_class("property")
    row.set_subtitle_selectable(True)
    return row


def badge(text: str, kind: str = "") -> Gtk.Label:
    """Pill-shaped status label (styles defined in theme.py)."""
    label = Gtk.Label(label=text, valign=Gtk.Align.CENTER)
    label.add_css_class("badge")
    if kind:
        label.add_css_class(kind)
    return label


def pill_button(label: str, callback: Callable, suggested: bool = False,
                destructive: bool = False) -> Gtk.Button:
    button = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
    button.add_css_class("pill" if suggested else "flat")
    if suggested:
        button.add_css_class("suggested-action")
    if destructive:
        button.add_css_class("destructive-action")
    button.connect("clicked", lambda *_: callback())
    return button


def icon_button(icon: str, tooltip: str, callback: Callable,
                destructive: bool = False) -> Gtk.Button:
    button = Gtk.Button(icon_name=icon, tooltip_text=tooltip, valign=Gtk.Align.CENTER)
    button.add_css_class("flat")
    if destructive:
        button.add_css_class("error")
    button.connect("clicked", lambda *_: callback())
    return button


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

    @property
    def rows(self) -> list[Gtk.Widget]:
        return self._rows


def join(*parts: str) -> str:
    return " · ".join(p for p in parts if p)


# -----------------------------------------------------------------------------
# Page base class
# -----------------------------------------------------------------------------

class Page(Adw.NavigationPage):
    """A content page: header bar + scrollable preferences page.

    Subclasses build their widgets in __init__ and implement refresh().
    """

    icon_name = "network-wireless-symbolic"

    def __init__(self, window: "MainWindow", title: str, tag: str) -> None:
        super().__init__(title=title, tag=tag)
        self.window = window
        self.backend: NetworkBackend = window.backend
        self.stale = True

        self.toolbar = Adw.ToolbarView()
        self.header = Adw.HeaderBar()
        self.toolbar.add_top_bar(self.header)

        # Shown on every page while the NetworkManager daemon is not running.
        self.nm_banner = Adw.Banner(title="NetworkManager is not running. "
                                          "Start it with: systemctl start NetworkManager")
        self.toolbar.add_top_bar(self.nm_banner)

        self.content = Adw.PreferencesPage()
        self.toolbar.set_content(self.content)
        self.set_child(self.toolbar)

    def refresh(self) -> None:
        """Re-read state from the backend and update widgets."""
        self.nm_banner.set_revealed(not self.backend.nm_running)
        self.stale = False


# -----------------------------------------------------------------------------
# Overview page
# -----------------------------------------------------------------------------

class OverviewPage(Page):
    icon_name = "go-home-symbolic"

    # The systemd service state is fetched over D-Bus; do it at most this often.
    SERVICE_REFRESH_S = 5

    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window, "Overview", "overview")
        self._updating = False
        self._service_checked_at = 0.0
        self._service_text = "Checking…"

        # --- Status hero card ---
        hero_group = Adw.PreferencesGroup()
        self.hero = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        self.hero.add_css_class("card")
        self.hero.add_css_class("status-hero")
        self.hero_icon = Gtk.Image(pixel_size=56)
        self.hero_icon.add_css_class("hero-icon")
        self.hero.append(self.hero_icon)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                           valign=Gtk.Align.CENTER, hexpand=True)
        self.hero_title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.hero_title.add_css_class("hero-title")
        self.hero_subtitle = Gtk.Label(xalign=0, wrap=True)
        self.hero_subtitle.add_css_class("dim-label")
        text_box.append(self.hero_title)
        text_box.append(self.hero_subtitle)
        self.hero.append(text_box)

        self.hero_buttons = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        self.hero.append(self.hero_buttons)
        hero_group.add(self.hero)
        self.content.add(hero_group)

        # --- Connection details ---
        self.connection_group = Adw.PreferencesGroup(title="Connection")
        self.row_network = property_row("Network")
        self.row_interface = property_row("Interface")
        self.row_type = property_row("Type")
        self.row_status = property_row("Status")
        self.row_signal = property_row("Signal")
        for row in (self.row_network, self.row_interface, self.row_type,
                    self.row_status, self.row_signal):
            self.connection_group.add(row)
        self.content.add(self.connection_group)

        self.address_group = Adw.PreferencesGroup(title="Addresses")
        self.row_ipv4 = property_row("IPv4 address")
        self.row_gateway = property_row("Gateway")
        self.row_dns = property_row("DNS servers")
        self.row_ipv6 = property_row("IPv6 address")
        for row in (self.row_ipv4, self.row_gateway, self.row_dns, self.row_ipv6):
            self.address_group.add(row)
        self.content.add(self.address_group)

        # --- Wi-Fi radio ---
        radio_group = Adw.PreferencesGroup(title="Wireless")
        self.wifi_switch = Adw.SwitchRow(title="Wi-Fi")
        self.wifi_switch.connect("notify::active", self._on_wifi_switch)
        radio_group.add(self.wifi_switch)
        self.content.add(radio_group)

        # --- NetworkManager daemon ---
        nm_group = Adw.PreferencesGroup(title="NetworkManager")
        self.row_service = property_row("Service")
        self.row_nm_state = property_row("Network state")
        self.row_connectivity = property_row("Internet connectivity")
        for row in (self.row_service, self.row_nm_state, self.row_connectivity):
            nm_group.add(row)
        self.content.add(nm_group)

    def _set_hero_buttons(self, buttons: list[Gtk.Widget]) -> None:
        child = self.hero_buttons.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.hero_buttons.remove(child)
            child = next_child
        for button in buttons:
            self.hero_buttons.append(button)

    def refresh(self) -> None:
        super().refresh()
        backend = self.backend
        current = backend.current_connection()

        for css in ("connected", "connecting", "disconnected"):
            self.hero.remove_css_class(css)

        if current is not None:
            details = backend.describe_active(current)
            connecting = details.state != NM.ActiveConnectionState.ACTIVATED
            self.hero.add_css_class("connecting" if connecting else "connected")
            if details.type_name == NM.SETTING_WIRELESS_SETTING_NAME:
                icon = signal_icon_name(details.signal or 0)
            elif details.type_name in (NM.SETTING_VPN_SETTING_NAME, NM.SETTING_WIREGUARD_SETTING_NAME):
                icon = "network-vpn-symbolic"
            else:
                icon = "network-wired-symbolic"
            self.hero_icon.set_from_icon_name(icon)
            self.hero_title.set_text(details.ssid or details.name)
            self.hero_subtitle.set_text(join(details.state_label, details.type_label,
                                             details.interface))
            self._set_hero_buttons([
                pill_button("Disconnect", lambda: backend.deactivate(current, self.window.report)),
            ])

            self.row_network.set_subtitle(
                details.ssid if details.ssid and details.ssid == details.name
                else join(details.ssid, f"Profile “{details.name}”"))
            self.row_interface.set_subtitle(details.interface or "—")
            self.row_type.set_subtitle(details.type_label)
            self.row_status.set_subtitle(details.state_label)
            self.row_signal.set_visible(details.signal is not None)
            if details.signal is not None:
                self.row_signal.set_subtitle(f"{details.signal}%")
            self.row_ipv4.set_subtitle(", ".join(details.ipv4.addresses) or "—")
            self.row_gateway.set_subtitle(details.ipv4.gateway or details.ipv6.gateway or "—")
            self.row_dns.set_subtitle(", ".join(details.ipv4.dns + details.ipv6.dns) or "—")
            self.row_ipv6.set_subtitle(", ".join(details.ipv6.addresses) or "—")
            self.connection_group.set_visible(True)
            self.address_group.set_visible(True)
        else:
            self.hero.add_css_class("disconnected")
            self.hero_icon.set_from_icon_name("network-offline-symbolic")
            self.hero_title.set_text("Not connected")
            self.hero_subtitle.set_text("Connect to a saved network or choose one on the Wi-Fi page.")
            self._set_hero_buttons([
                pill_button("Choose Network", lambda: self.window.show_page("wifi")),
                pill_button("Connect", self._connect_best, suggested=True),
            ])
            self.connection_group.set_visible(False)
            self.address_group.set_visible(False)

        # Wi-Fi switch: update without triggering our own handler.
        self._updating = True
        self.wifi_switch.set_active(backend.wifi_enabled())
        self._updating = False
        hardware_on = backend.wifi_hardware_enabled()
        self.wifi_switch.set_sensitive(hardware_on and backend.nm_running)
        self.wifi_switch.set_subtitle("" if hardware_on else
                                      "Blocked by a hardware switch or airplane mode (rfkill)")

        self.row_nm_state.set_subtitle(backend.state_label() if backend.nm_running else "Not running")
        self.row_connectivity.set_subtitle(backend.connectivity_label())
        self._refresh_service()

    def _refresh_service(self) -> None:
        now = time.monotonic()
        if now - self._service_checked_at < self.SERVICE_REFRESH_S:
            self.row_service.set_subtitle(self._service_text)
            return
        self._service_checked_at = now

        def done(result, error):
            if error:
                self._service_text = f"Unknown ({error})"
            else:
                active_state, sub_state = result
                self._service_text = f"{active_state} ({sub_state})"
                if self.backend.nm_running:
                    self._service_text += f" · version {self.backend.nm_version}"
            self.row_service.set_subtitle(self._service_text)

        self.backend.get_service_status(done)

    def _on_wifi_switch(self, row, _pspec) -> None:
        if self._updating:
            return
        self.backend.set_wifi_enabled(row.get_active(), self.window.report)

    def _connect_best(self) -> None:
        """Let NM pick the best saved profile for the most useful device."""
        device = None
        if self.backend.wifi_enabled():
            device = self.backend.primary_wifi_device()
        if device is None:
            wired = [d for d in self.backend.ethernet_devices() if d.get_carrier()]
            device = wired[0] if wired else None
        if device is None:
            self.window.toast("No available network adapter to connect with.")
            return
        self.backend.activate(None, device, None, "the best available network",
                              self.window.report_activation)


# -----------------------------------------------------------------------------
# Wi-Fi page
# -----------------------------------------------------------------------------

class WifiPage(Page):
    icon_name = "network-wireless-symbolic"

    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window, "Wi-Fi", "wifi")
        self.wifi: WifiManager = window.wifi
        self._signature = None
        self._auto_scanned = False

        # --- Header buttons ---
        hidden_button = Gtk.Button(icon_name="list-add-symbolic",
                                   tooltip_text="Connect to hidden network…")
        hidden_button.connect("clicked", lambda *_: self._connect_hidden())
        self.header.pack_end(hidden_button)

        self.scan_button = Gtk.Button(icon_name="view-refresh-symbolic",
                                      tooltip_text="Scan for networks")
        self.scan_button.connect("clicked", lambda *_: self.scan())
        self.scan_spinner = Adw.Spinner(visible=False)
        self.header.pack_end(self.scan_button)
        self.header.pack_end(self.scan_spinner)

        # --- Wi-Fi off banner ---
        self.off_banner = Adw.Banner(title="Wi-Fi is turned off", button_label="Turn On")
        self.off_banner.connect("button-clicked",
                                lambda *_: self.backend.set_wifi_enabled(True, self.window.report))
        self.toolbar.add_top_bar(self.off_banner)

        # --- No adapter ---
        self.empty = Adw.StatusPage(icon_name="network-wireless-disabled-symbolic",
                                    title="No Wi-Fi Adapter",
                                    description="NetworkManager does not see a wireless device.")

        # --- Network list ---
        self.network_group = RowGroup(title="Available Networks")
        self.scan_label = Gtk.Label(valign=Gtk.Align.CENTER)
        self.scan_label.add_css_class("hint")
        self.network_group.set_header_suffix(self.scan_label)
        self.content.add(self.network_group)

        self.hidden_group = Adw.PreferencesGroup()
        self.hidden_row = action_row("Hidden networks nearby",
                                     "Networks that do not broadcast their name")
        self.hidden_row.add_prefix(Gtk.Image(icon_name="network-wireless-signal-none-symbolic"))
        self.hidden_row.add_suffix(pill_button("Connect…", self._connect_hidden))
        self.hidden_group.add(self.hidden_row)
        self.content.add(self.hidden_group)

    @property
    def device(self) -> Optional[NM.DeviceWifi]:
        return self.backend.primary_wifi_device()

    # -------------------------------------------------------------------------
    # Scanning
    # -------------------------------------------------------------------------

    def scan(self) -> None:
        device = self.device
        if device is None or self.wifi.scanning:
            return
        self.scan_button.set_sensitive(False)
        self.scan_spinner.set_visible(True)
        self.scan_label.set_text("Scanning…")

        def done(error):
            self.scan_button.set_sensitive(True)
            self.scan_spinner.set_visible(False)
            if error:
                self.window.toast(f"Scan: {error}")
            self._signature = None  # force a redraw with fresh results
            self.refresh()

        self.wifi.request_scan(device, done)

    def on_shown(self) -> None:
        """First visit: rescan if NM's results are older than a minute."""
        if self._auto_scanned:
            return
        self._auto_scanned = True
        device = self.device
        if device is None or not self.backend.wifi_enabled():
            return
        result = collect_networks(device, [])
        if result.last_scan_age_s is None or result.last_scan_age_s > 60:
            self.scan()

    # -------------------------------------------------------------------------
    # Drawing
    # -------------------------------------------------------------------------

    def refresh(self) -> None:
        super().refresh()
        device = self.device
        wifi_on = self.backend.wifi_enabled()
        self.off_banner.set_revealed(self.backend.nm_running and not wifi_on)

        if device is None:
            self.toolbar.set_content(self.empty)
            self.scan_button.set_sensitive(False)
            return
        self.toolbar.set_content(self.content)
        self.scan_button.set_sensitive(wifi_on and not self.wifi.scanning)

        result = collect_networks(device, self.backend.client.get_connections())
        if not self.wifi.scanning:
            age = result.last_scan_age_s
            if age is None:
                self.scan_label.set_text("")
            elif age < 60:
                self.scan_label.set_text("Updated just now")
            else:
                self.scan_label.set_text(f"Updated {age // 60} min ago")

        device_busy = NM.DeviceState.PREPARE <= device.get_state() < NM.DeviceState.ACTIVATED
        signature = (wifi_on, device_busy, result.hidden_ap_count, tuple(
            (n.key, signal_level(n.strength), n.active, n.ap_count,
             tuple(c.get_uuid() for c in n.saved), tuple(sorted(n.bands)))
            for n in result.networks))
        if signature == self._signature:
            return
        self._signature = signature

        rows = [self._network_row(n, device, device_busy) for n in result.networks]
        if not rows:
            rows = [action_row("No networks found",
                               "Turn on Wi-Fi or scan again." if wifi_on else "Wi-Fi is turned off.")]
        self.network_group.set_rows(rows)
        self.hidden_row.set_title(
            f"{result.hidden_ap_count} hidden network{'s' if result.hidden_ap_count != 1 else ''} nearby"
            if result.hidden_ap_count else "Hidden network")
        self.hidden_row.set_subtitle("Connect to a network that does not broadcast its name")

    def _network_row(self, network: WifiNetwork, device: NM.DeviceWifi,
                     device_busy: bool) -> Adw.ActionRow:
        saved_text = ""
        profile = network.preferred_profile
        if profile is not None:
            saved_text = ("Saved" if profile.get_id() == network.ssid
                          else f"Saved as “{profile.get_id()}”")
        extra_aps = f"{network.ap_count} access points" if network.ap_count > 1 else ""
        row = action_row(network.ssid, join(network.security.label,
                                            " / ".join(sorted(network.bands)),
                                            extra_aps, saved_text))

        icon = Gtk.Image(icon_name=signal_icon_name(network.strength),
                         tooltip_text=f"Signal {network.strength}%")
        icon.add_css_class(f"signal-{signal_level(network.strength)}")
        row.add_prefix(icon)

        if network.security.is_secure:
            lock = Gtk.Image(icon_name="changes-prevent-symbolic",
                             tooltip_text=network.security.label)
            lock.add_css_class("dim-label")
            row.add_suffix(lock)

        if network.active and device_busy:
            row.add_suffix(badge("Connecting…", "busy"))
            row.add_suffix(pill_button("Cancel", lambda: self.wifi.disconnect(device, self.window.report)))
        elif network.active:
            row.add_suffix(badge("Connected", "connected"))
            row.add_suffix(pill_button("Disconnect",
                                       lambda: self.wifi.disconnect(device, self.window.report)))
        else:
            row.add_suffix(pill_button("Connect", lambda: self._connect(network, device),
                                       suggested=True))

        if profile is not None:
            row.add_suffix(icon_button("document-edit-symbolic", "Edit saved profile",
                                       lambda: self.window.open_editor(remote=profile)))
        return row

    # -------------------------------------------------------------------------
    # Connecting
    # -------------------------------------------------------------------------

    def _connect(self, network: WifiNetwork, device: NM.DeviceWifi) -> None:
        if network.is_saved:
            self.wifi.connect_saved(network, device, self.window.report_activation)
        elif network.security in (Security.OPEN, Security.OWE):
            self.wifi.connect_new(network, device, None, self._on_new_result)
        elif network.security.needs_password:
            self._ask_password(network, device)
        elif network.security is Security.ENTERPRISE:
            # 802.1X needs identity, method and CA settings: use the editor.
            self.window.open_editor(ssid=network.ssid, security_mode="enterprise",
                                    connect_after_save=True, device=device)
        else:
            self.window.toast("WEP networks are obsolete and insecure and are not "
                              "supported by this app.")

    def _ask_password(self, network: WifiNetwork, device: NM.DeviceWifi) -> None:
        dialog = Adw.AlertDialog(heading=f"Connect to {network.ssid}",
                                 body=f"{network.security.label} · enter the network password")
        entry = Gtk.PasswordEntry(show_peek_icon=True, activates_default=True,
                                  placeholder_text="Password")
        hint = Gtk.Label(xalign=0, wrap=True)
        hint.add_css_class("hint")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(entry)
        box.append(hint)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("connect", "Connect")
        dialog.set_response_appearance("connect", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("connect")
        dialog.set_close_response("cancel")
        dialog.set_response_enabled("connect", False)

        def on_changed(*_):
            error = validate_wifi_password(network.security, entry.get_text())
            hint.set_text(error or "")
            dialog.set_response_enabled("connect", error is None)

        entry.connect("changed", on_changed)
        on_changed()

        def on_response(_dialog, response):
            if response == "connect":
                password = entry.get_text()
                # Clear the widget straight away; the password now only lives
                # in the NM setting until it is sent over D-Bus.
                entry.set_text("")
                self.wifi.connect_new(network, device, password, self._on_new_result)

        dialog.connect("response", on_response)
        dialog.present(self.window)
        entry.grab_focus()

    def _on_new_result(self, ok: bool, message: str, new_uuid: Optional[str]) -> None:
        """Report a quick-connect result; offer to forget a failed new profile."""
        if ok or not new_uuid or self.window.profiles.by_uuid(new_uuid) is None:
            self.window.toast(message)
            return
        self.window.toast(message, button="Forget Profile",
                          on_button=lambda: self.window.profiles.delete(new_uuid, self.window.report))

    def _connect_hidden(self) -> None:
        self.window.open_editor(hidden=True, security_mode="wpa-psk",
                                connect_after_save=True, device=self.device)


# -----------------------------------------------------------------------------
# Saved connections page
# -----------------------------------------------------------------------------

def build_profile_row(window: "MainWindow", profile: SavedProfile) -> Adw.ActionRow:
    """Row for one saved profile, shared by the Saved and Ethernet & VPN pages."""
    # Only the non-default facts are listed, to keep subtitles short:
    # the SSID when it differs from the name, manual-only and non-zero priority.
    subtitle = join(
        f"SSID “{profile.detail}”" if profile.category == CATEGORY_WIFI
        and profile.detail and profile.detail != profile.name else
        (profile.detail if profile.category != CATEGORY_WIFI else ""),
        profile.security,
        "" if profile.autoconnect else "Manual only",
        f"Priority {profile.priority}" if profile.priority else "",
        profile.last_used_text,
    )
    row = action_row(profile.name, subtitle)
    row.add_prefix(Gtk.Image(icon_name=CATEGORY_ICONS[profile.category]))

    if profile.is_busy:
        row.add_suffix(badge("Connecting…", "busy"))
    elif profile.is_active:
        row.add_suffix(badge("Active", "connected"))

    if not profile.can_activate:
        warning = Gtk.Image(icon_name="dialog-warning-symbolic", tooltip_text=profile.note)
        warning.add_css_class("warning")
        row.add_suffix(warning)
        if profile.category == CATEGORY_VPN:
            row.set_subtitle(join(row.get_subtitle(), profile.note))
    elif profile.is_active or profile.is_busy:
        row.add_suffix(pill_button("Disconnect",
                                   lambda: window.profiles.deactivate(profile.uuid, window.report)))
    else:
        row.add_suffix(pill_button("Connect",
                                   lambda: window.profiles.activate(profile.uuid,
                                                                    window.report_activation)))

    if profile.editable:
        edit = icon_button("document-edit-symbolic", "Edit",
                           lambda: window.open_editor(remote=profile.remote))
        row.add_suffix(edit)
        row.add_suffix(icon_button("user-trash-symbolic", "Forget…",
                                   lambda: window.confirm_delete(profile), destructive=True))
        row.set_activatable_widget(edit)
    return row


class SavedPage(Page):
    icon_name = "starred-symbolic"

    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window, "Saved Networks", "saved")
        self._signature = None

        add_menu = Gio.Menu()
        add_menu.append("Wi-Fi Profile…", "win.new-wifi")
        add_menu.append("Ethernet Profile…", "win.new-ethernet")
        add_button = Gtk.MenuButton(icon_name="list-add-symbolic", menu_model=add_menu,
                                    tooltip_text="Add profile")
        self.header.pack_end(add_button)

        # Search box filters by name, SSID and details.
        search_group = Adw.PreferencesGroup()
        self.search = Gtk.SearchEntry(placeholder_text="Search saved profiles")
        self.search.connect("search-changed", lambda *_: self._apply_filter())
        search_group.add(self.search)
        self.content.add(search_group)

        self.groups: dict[str, RowGroup] = {}
        for category in (CATEGORY_WIFI, CATEGORY_ETHERNET, CATEGORY_VPN, CATEGORY_OTHER):
            group = RowGroup(title=CATEGORY_TITLES[category])
            self.groups[category] = group
            self.content.add(group)
        self.groups[CATEGORY_WIFI].set_description(
            "Profiles are identified by their UUID, so renaming is safe.")

        self.empty = Adw.StatusPage(icon_name="starred-symbolic", title="No Saved Profiles",
                                    description="Connect to a network to save it.")
        self._search_texts: dict[Gtk.Widget, str] = {}

    def refresh(self) -> None:
        super().refresh()
        profiles = self.window.profiles.profiles()
        signature = tuple((p.uuid, p.name, p.detail, p.security, p.autoconnect, p.priority,
                           p.last_used, p.active_state, p.can_activate) for p in profiles)
        if signature == self._signature:
            return
        self._signature = signature

        self.toolbar.set_content(self.content if profiles else self.empty)
        self._search_texts = {}
        for category, group in self.groups.items():
            rows = []
            for profile in (p for p in profiles if p.category == category):
                row = build_profile_row(self.window, profile)
                self._search_texts[row] = f"{profile.name} {profile.detail} {profile.security}".lower()
                rows.append(row)
            group.set_rows(rows)
            group.set_visible(bool(rows))
        self._apply_filter()

    def _apply_filter(self) -> None:
        query = self.search.get_text().strip().lower()
        for group in self.groups.values():
            visible_rows = 0
            for row in group.rows:
                visible = not query or query in self._search_texts.get(row, "")
                row.set_visible(visible)
                visible_rows += visible
            group.set_visible(visible_rows > 0)


# -----------------------------------------------------------------------------
# Ethernet & VPN page
# -----------------------------------------------------------------------------

def build_device_expander(window: "MainWindow", device: NM.Device,
                          expanded: set[str], with_actions: bool) -> Adw.ExpanderRow:
    """Expandable row with adapter details (Ethernet & Diagnostics pages)."""
    info = window.backend.describe_device(device)
    row = Adw.ExpanderRow(title=info.interface,
                          subtitle=join(info.type_label, info.state_label, info.connection_name))
    row.set_use_markup(False)
    icon = {"Ethernet": "network-wired-symbolic", "Wi-Fi": "network-wireless-symbolic",
            "WireGuard": "network-vpn-symbolic"}.get(info.type_label, "network-workgroup-symbolic")
    row.add_prefix(Gtk.Image(icon_name=icon))

    details = [("Type", info.type_label), ("State", info.state_label),
               ("Driver", info.driver), ("MAC address", info.hw_address),
               ("MTU", str(info.mtu) if info.mtu else ""),
               ("Managed by NetworkManager", "Yes" if info.managed else "No"),
               ("Connection", info.connection_name)]
    details += list(info.extra.items())
    details += [("IPv4", ", ".join(info.ipv4.addresses)),
                ("IPv4 gateway", info.ipv4.gateway),
                ("IPv4 DNS", ", ".join(info.ipv4.dns)),
                ("IPv6", ", ".join(info.ipv6.addresses))]
    for title, value in details:
        if value:
            row.add_row(property_row(title, value))

    if with_actions and info.managed:
        if device.get_active_connection() is not None:
            row.add_suffix(pill_button("Disconnect",
                                       lambda: window.backend.disconnect_device(device, window.report)))
        elif device.get_state() >= NM.DeviceState.DISCONNECTED:
            row.add_suffix(pill_button(
                "Connect",
                lambda: window.backend.activate(None, device, None, info.interface,
                                                window.report_activation),
                suggested=True))

    # Remember which rows the user opened so a refresh does not collapse them.
    row.set_expanded(info.interface in expanded)
    row.connect("notify::expanded",
                lambda r, _p: expanded.add(info.interface) if r.get_expanded()
                else expanded.discard(info.interface))
    return row


class EthernetVpnPage(Page):
    icon_name = "network-wired-symbolic"

    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window, "Ethernet & VPN", "ethernet")
        self._signature = None
        self._expanded: set[str] = set()

        self.adapter_group = RowGroup(title="Ethernet Adapters")
        self.wired_group = RowGroup(title="Ethernet Profiles")
        self.vpn_group = RowGroup(title="VPN Profiles")
        for group in (self.adapter_group, self.wired_group, self.vpn_group):
            self.content.add(group)

    def refresh(self) -> None:
        super().refresh()
        devices = self.backend.ethernet_devices()
        profiles = self.window.profiles.profiles()
        wired = [p for p in profiles if p.category == CATEGORY_ETHERNET]
        vpns = [p for p in profiles if p.category == CATEGORY_VPN]
        plugins = installed_vpn_plugins()

        signature = (
            tuple((d.get_iface(), d.get_state(), d.get_carrier(), d.get_speed(),
                   bool(d.get_active_connection())) for d in devices),
            tuple((p.uuid, p.name, p.active_state, p.autoconnect, p.priority, p.last_used)
                  for p in wired + vpns),
            tuple(plugins))
        if signature == self._signature:
            return
        self._signature = signature

        self.adapter_group.set_rows(
            [build_device_expander(self.window, d, self._expanded, with_actions=True)
             for d in devices]
            or [action_row("No Ethernet adapters", "NetworkManager does not see a wired device.")])

        self.wired_group.set_rows([build_profile_row(self.window, p) for p in wired]
                                  or [action_row("No saved Ethernet profiles")])

        self.vpn_group.set_description(
            "Installed VPN plugins: " + (", ".join(plugins) if plugins else "none") +
            ". WireGuard is built into NetworkManager.")
        self.vpn_group.set_rows([build_profile_row(self.window, p) for p in vpns]
                                or [action_row("No saved VPN profiles",
                                               "Import or create VPN profiles with nmcli or "
                                               "nm-connection-editor; they will appear here.")])


# -----------------------------------------------------------------------------
# Diagnostics page
# -----------------------------------------------------------------------------

class DiagnosticsPage(Page):
    icon_name = "utilities-terminal-symbolic"

    # Host used for the DNS/HTTPS checks. Only contacted when the user
    # clicks "Run". (archlinux.org is also NM's connectivity host on Arch.)
    TEST_HOST = "archlinux.org"
    TEST_PORT = 443
    LOG_ROWS = 50

    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window, "Diagnostics", "diagnostics")
        self._adapter_signature = None
        self._unit_checked_at = 0.0
        self._expanded: set[str] = set()

        refresh_button = Gtk.Button(icon_name="view-refresh-symbolic",
                                    tooltip_text="Refresh status")
        refresh_button.connect("clicked", lambda *_: self._manual_refresh())
        self.header.pack_end(refresh_button)

        # --- Quick checks ---
        self.checks_group = Adw.PreferencesGroup(
            title="Connectivity Checks",
            description=f"The DNS and HTTPS checks contact {self.TEST_HOST}.")
        run_all = Gtk.Button(label="Run All", valign=Gtk.Align.CENTER)
        run_all.add_css_class("pill")
        run_all.connect("clicked", lambda *_: self._run_all())
        self.checks_group.set_header_suffix(run_all)

        self.check_nm = self._check_row("NetworkManager connectivity check",
                                        self._run_nm_check)
        self.check_dns = self._check_row(f"DNS lookup ({self.TEST_HOST})", self._run_dns)
        self.check_tcp = self._check_row(f"HTTPS reachability ({self.TEST_HOST}:{self.TEST_PORT})",
                                         self._run_tcp)
        self.content.add(self.checks_group)

        # --- Service ---
        service_group = Adw.PreferencesGroup(title="NetworkManager Service")
        self.row_unit = property_row("systemd unit (NetworkManager.service)")
        self.row_daemon = property_row("Daemon")
        self.row_networking = property_row("Networking")
        self.row_state = property_row("Overall state")
        self.row_connectivity = property_row("Connectivity")
        self.row_check_enabled = property_row("Automatic connectivity checking")
        for row in (self.row_unit, self.row_daemon, self.row_networking, self.row_state,
                    self.row_connectivity, self.row_check_enabled):
            service_group.add(row)
        self.content.add(service_group)

        # --- Adapters ---
        self.adapter_group = RowGroup(title="Network Adapters")
        self.content.add(self.adapter_group)

        # --- Activity log ---
        self.log_group = RowGroup(title="Activity Log",
                                  description="Results of operations in this session. "
                                              "Passwords are never recorded.")
        clear = Gtk.Button(label="Clear", valign=Gtk.Align.CENTER)
        clear.add_css_class("flat")
        clear.connect("clicked", lambda *_: self.backend.clear_log())
        self.log_group.set_header_suffix(clear)
        self.content.add(self.log_group)
        self.backend.connect("log-added", lambda *_: self._refresh_log())
        self._refresh_log()

    def _check_row(self, title: str, runner: Callable) -> Adw.ActionRow:
        row = action_row(title, "Not run yet")
        row.add_suffix(pill_button("Run", runner))
        self.checks_group.add(row)
        return row

    @staticmethod
    def _set_result(row: Adw.ActionRow, ok: Optional[bool], text: str) -> None:
        row.remove_css_class("log-success")
        row.remove_css_class("log-error")
        if ok is True:
            row.add_css_class("log-success")
        elif ok is False:
            row.add_css_class("log-error")
        row.set_subtitle(text)

    def _run_nm_check(self) -> None:
        self._set_result(self.check_nm, None, "Running…")

        def done(label, error):
            ok = error is None and label == "Full internet access"
            text = error or label
            if error is None and not self.backend.connectivity_check_available():
                text += " (connectivity checking is disabled in NetworkManager)"
            self._set_result(self.check_nm, ok, text)
            self.backend.add_log(f"Connectivity check: {text}", "success" if ok else "error")

        self.backend.check_connectivity(done)

    def _run_dns(self) -> None:
        self._set_result(self.check_dns, None, "Resolving…")

        def done(addresses, error):
            text = error or ", ".join(addresses[:4])
            self._set_result(self.check_dns, error is None, text)
            self.backend.add_log(f"DNS lookup {self.TEST_HOST}: {text}",
                                 "success" if error is None else "error")

        self.backend.resolve_host(self.TEST_HOST, done)

    def _run_tcp(self) -> None:
        self._set_result(self.check_tcp, None, "Connecting…")

        def done(elapsed_ms, error):
            text = error or f"Reachable in {elapsed_ms} ms"
            self._set_result(self.check_tcp, error is None, text)
            self.backend.add_log(f"HTTPS {self.TEST_HOST}: {text}",
                                 "success" if error is None else "error")

        self.backend.check_tcp(self.TEST_HOST, self.TEST_PORT, done)

    def _run_all(self) -> None:
        self._run_nm_check()
        self._run_dns()
        self._run_tcp()

    def _manual_refresh(self) -> None:
        self._adapter_signature = None
        self._unit_checked_at = 0.0
        self.refresh()
        self.window.toast("Status refreshed")

    def refresh(self) -> None:
        super().refresh()
        backend = self.backend
        client = backend.client

        def unit_done(result, error):
            self.row_unit.set_subtitle(error or f"{result[0]} ({result[1]})")

        # The systemd query is a D-Bus round trip; limit it like the Overview.
        now = time.monotonic()
        if now - self._unit_checked_at >= OverviewPage.SERVICE_REFRESH_S:
            self._unit_checked_at = now
            backend.get_service_status(unit_done)
        self.row_daemon.set_subtitle(f"Running · version {backend.nm_version}"
                                     if backend.nm_running else "Not running")
        self.row_networking.set_subtitle("Enabled" if client.networking_get_enabled() else "Disabled")
        self.row_state.set_subtitle(backend.state_label())
        self.row_connectivity.set_subtitle(backend.connectivity_label())
        self.row_check_enabled.set_subtitle(
            "Enabled" if client.connectivity_check_get_enabled()
            else ("Disabled" if backend.connectivity_check_available() else "Not configured"))

        devices = backend.devices()
        signature = tuple((d.get_iface(), d.get_state(), d.get_mtu(),
                           tuple(backend.describe_device(d).ipv4.addresses),
                           bool(d.get_active_connection())) for d in devices)
        if signature != self._adapter_signature:
            self._adapter_signature = signature
            self.adapter_group.set_rows(
                [build_device_expander(self.window, d, self._expanded, with_actions=False)
                 for d in devices])

    def _refresh_log(self) -> None:
        rows = []
        for entry in reversed(self.backend.log[-self.LOG_ROWS:]):
            row = action_row(entry.message, entry.time_text)
            if entry.kind in ("success", "error"):
                row.add_css_class(f"log-{entry.kind}")
            rows.append(row)
        self.log_group.set_rows(rows or [action_row("No activity yet")])


# -----------------------------------------------------------------------------
# Main window
# -----------------------------------------------------------------------------

class MainWindow(Adw.ApplicationWindow):
    """Sidebar navigation, toasts and shared actions (editor, delete)."""

    def __init__(self, app: Adw.Application, backend: NetworkBackend) -> None:
        super().__init__(application=app, title=APP_NAME,
                         default_width=1040, default_height=740)
        self.set_size_request(360, 480)
        self.backend = backend
        self.wifi = WifiManager(backend)
        self.profiles = ProfileManager(backend)

        self.toast_overlay = Adw.ToastOverlay()
        self.split = Adw.NavigationSplitView(min_sidebar_width=220, max_sidebar_width=280)
        self.toast_overlay.set_child(self.split)
        self.set_content(self.toast_overlay)

        self.pages: dict[str, Page] = {}
        for page_class in (OverviewPage, WifiPage, SavedPage, EthernetVpnPage, DiagnosticsPage):
            page = page_class(self)
            self.pages[page.get_tag()] = page
        self._current: Optional[Page] = None

        self._build_sidebar()
        self._install_actions()

        # Collapse the sidebar into a back-navigable list on narrow windows
        # (e.g. when tiled small in Hyprland).
        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 640sp"))
        breakpoint.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(breakpoint)

        backend.connect("changed", self._on_backend_changed)
        self.show_page("overview")

    # -------------------------------------------------------------------------
    # Sidebar navigation
    # -------------------------------------------------------------------------

    def _build_sidebar(self) -> None:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        title = Adw.WindowTitle(title="Network", subtitle="MrFaws")
        header.set_title_widget(title)

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
        self.sidebar_list.connect("row-activated", self._on_sidebar_activated)

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      child=self.sidebar_list, vexpand=True)
        toolbar.set_content(scroller)
        self.split.set_sidebar(Adw.NavigationPage(title=APP_NAME, tag="sidebar", child=toolbar))

    def _on_sidebar_activated(self, _listbox, row) -> None:
        self.show_page(row.page_tag)

    def show_page(self, tag: str) -> None:
        page = self.pages[tag]
        self.sidebar_list.select_row(self._sidebar_rows[tag])
        if page is not self._current:
            self._current = page
            self.split.set_content(page)
        self.split.set_show_content(True)
        if page.stale:
            page.refresh()
        if hasattr(page, "on_shown"):
            page.on_shown()

    def _on_backend_changed(self, _backend) -> None:
        # Redraw the visible page now; others redraw when next shown.
        for page in self.pages.values():
            page.stale = True
        if self._current is not None:
            self._current.refresh()

    # -------------------------------------------------------------------------
    # Actions
    # -------------------------------------------------------------------------

    def _install_actions(self) -> None:
        new_wifi = Gio.SimpleAction.new("new-wifi", None)
        new_wifi.connect("activate", lambda *_: self.open_editor(
            new_type=NM.SETTING_WIRELESS_SETTING_NAME, security_mode="wpa-psk"))
        self.add_action(new_wifi)

        new_wired = Gio.SimpleAction.new("new-ethernet", None)
        new_wired.connect("activate", lambda *_: self.open_editor(
            new_type=NM.SETTING_WIRED_SETTING_NAME))
        self.add_action(new_wired)

        refresh = Gio.SimpleAction.new("refresh", None)
        refresh.connect("activate", lambda *_: self.backend.queue_changed())
        self.add_action(refresh)

    def open_editor(self, **kwargs) -> None:
        """Open the connection editor (see ConnectionEditor for arguments)."""
        editor = ConnectionEditor(self.backend, self.profiles,
                                  on_finished=lambda msg, ok: self.toast(msg), **kwargs)
        editor.present(self)

    def confirm_delete(self, profile: SavedProfile) -> None:
        """Ask before permanently deleting a profile."""
        remote = self.profiles.by_uuid(profile.uuid)
        if remote is None:
            return
        current = summarize_profile(remote, self.backend)
        body = ("This permanently deletes the saved profile and any password stored "
                "with it. You can reconnect later, but you will need to enter the "
                "settings again.")
        if current.is_active or current.is_busy:
            body += "\n\nThis connection is active and will be disconnected."
        dialog = Adw.AlertDialog(heading=f"Forget “{current.name}”?", body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Forget")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_response(_dialog, response):
            if response == "delete":
                self.profiles.delete(profile.uuid, self.report)

        dialog.connect("response", on_response)
        dialog.present(self)

    def _show_about(self) -> None:
        about = Adw.AboutDialog(application_name=APP_NAME, version=APP_VERSION,
                                developer_name="MrFaws",
                                application_icon="network-wireless",
                                comments="A Dracula-themed NetworkManager front end "
                                         "for Arch Linux + Hyprland.")
        about.present(self)

    # -------------------------------------------------------------------------
    # Feedback
    # -------------------------------------------------------------------------

    def toast(self, message: str, button: Optional[str] = None,
              on_button: Optional[Callable[[], None]] = None) -> None:
        toast = Adw.Toast(title=GLib.markup_escape_text(message), timeout=5)
        if button and on_button:
            toast.set_button_label(button)
            toast.set_timeout(10)
            toast.connect("button-clicked", lambda *_: on_button())
        self.toast_overlay.add_toast(toast)

    def report(self, _result, error: Optional[str]) -> None:
        """Generic callback for backend operations: show errors as toasts."""
        if error:
            self.toast(error)

    def report_activation(self, ok: bool, message: str) -> None:
        self.toast(message)
