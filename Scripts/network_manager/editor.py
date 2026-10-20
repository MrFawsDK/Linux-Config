# =============================================================================
# editor.py - Connection editor dialog
# =============================================================================
# An Adw.Dialog with tabs (General / Security / IPv4 / IPv6) covering the
# most-used parts of nm-connection-editor for Wi-Fi and Ethernet profiles.
# VPN and other profile types get the General tab only (name, automatic
# connection, priority); their plugin-specific settings are left untouched.
#
# Principles:
#   * Every visible control is wired to a real NetworkManager property.
#     Options the editor cannot handle (e.g. WEP, EAP-FAST, embedded
#     certificates) are shown as "Keep existing ..." and left unchanged
#     instead of offering a fake control.
#   * Saving starts from a fresh clone of the stored profile and only
#     overwrites the properties the editor manages, so anything else in the
#     profile (school-specific options, routes, cloned MAC, ...) survives.
#   * Password fields start empty. Empty means "keep the saved password";
#     secrets are never read back into the UI.
#   * CA certificate validation is only turned off when the user explicitly
#     switches on "No CA certificate is required", and a warning stays
#     visible while it is off.
#   * Final validation is done by NetworkManager itself (normalize() and the
#     daemon's own checks); local checks just give friendlier messages.
# =============================================================================

from __future__ import annotations

import os
import socket
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("NM", "1.0")
from gi.repository import Adw, Gio, GLib, Gtk, NM

from .backend import NetworkBackend, connection_type_label, friendly_error, ssid_to_text
from .connections import ProfileManager


class EditorError(Exception):
    """A user-correctable problem in the form (shown in the error banner)."""


# -----------------------------------------------------------------------------
# Choice lists
# -----------------------------------------------------------------------------
# Each list is (value, label). The value is what gets written to NM.

WIFI_SECURITY_CHOICES = [
    ("open", "None (open network)"),
    ("owe", "Enhanced Open (OWE)"),
    ("wpa-psk", "WPA/WPA2 Personal"),
    ("sae", "WPA3 Personal"),
    ("enterprise", "WPA/WPA2/WPA3 Enterprise (802.1X)"),
]

EAP_CHOICES = [
    ("peap", "Protected EAP (PEAP)"),
    ("ttls", "Tunneled TLS (TTLS)"),
    ("tls", "TLS (certificate)"),
]

# Inner (phase 2) authentication. "auth:" maps to 802-1x.phase2-auth,
# "autheap:" to 802-1x.phase2-autheap (EAP-based inner methods for TTLS).
PEAP_INNER_CHOICES = [
    ("auth:mschapv2", "MSCHAPv2"),
    ("auth:gtc", "GTC"),
    ("auth:md5", "MD5"),
]
TTLS_INNER_CHOICES = [
    ("auth:pap", "PAP"),
    ("auth:mschapv2", "MSCHAPv2"),
    ("auth:mschap", "MSCHAP"),
    ("auth:chap", "CHAP"),
    ("autheap:mschapv2", "EAP-MSCHAPv2"),
    ("autheap:md5", "EAP-MD5"),
    ("autheap:gtc", "EAP-GTC"),
]

IPV4_METHOD_CHOICES = [
    ("auto", "Automatic (DHCP)"),
    ("manual", "Manual"),
    ("link-local", "Link-local only"),
    ("shared", "Shared to other computers"),
    ("disabled", "Disabled"),
]

IPV6_METHOD_CHOICES = [
    ("auto", "Automatic"),
    ("dhcp", "Automatic, DHCP only"),
    ("manual", "Manual"),
    ("link-local", "Link-local only"),
    ("ignore", "Ignore"),
    ("disabled", "Disabled"),
]

IPV6_PRIVACY_CHOICES = [
    (-1, "Default"),
    (0, "Disabled"),
    (1, "Enabled, prefer public address"),
    (2, "Enabled, prefer temporary address"),
]

CERT_FILE_PATTERNS = ["*.pem", "*.crt", "*.cer", "*.der", "*.p12", "*.pfx", "*.key"]


# -----------------------------------------------------------------------------
# Small reusable rows
# -----------------------------------------------------------------------------

class ChoiceRow(Adw.ComboRow):
    """A ComboRow that maps its items to NM values instead of indexes.

    If a profile contains a value we do not list (e.g. an unusual phase 2
    method), it is appended as "Keep existing (...)" so loading and saving
    the profile never changes it by accident.
    """

    def __init__(self, title: str, choices: list, current=None,
                 subtitle: Optional[str] = None) -> None:
        super().__init__(title=title)
        if subtitle:
            self.set_subtitle(subtitle)
        self._values: list = []
        self.set_choices(choices, current)

    def set_choices(self, choices: list, current=None) -> None:
        choices = list(choices)
        values = [value for value, _label in choices]
        if current is not None and current not in values:
            choices.append((current, f"Keep existing ({current})"))
            values.append(current)
        self._values = values
        self.set_model(Gtk.StringList.new([label for _value, label in choices]))
        self.set_value(current if current is not None else values[0])

    def get_value(self):
        index = self.get_selected()
        if 0 <= index < len(self._values):
            return self._values[index]
        return None

    def set_value(self, value) -> None:
        if value in self._values:
            self.set_selected(self._values.index(value))


def info_row(title: str, value: str) -> Adw.ActionRow:
    """Read-only 'property' row: small title, selectable value."""
    row = Adw.ActionRow(title=title, subtitle=value or "—")
    row.add_css_class("property")
    row.set_subtitle_selectable(True)
    return row


class FileRow(Adw.ActionRow):
    """Certificate / key file picker with immediate validation.

    State:
      path        - chosen file path, or None
      description - shown when the profile holds a non-file value
                    (embedded blob or PKCS#11 URI) that we keep as-is
      changed     - True once the user picked or cleared a file; only then
                    is the NM property rewritten on save
    """

    def __init__(self, title: str, validator: Optional[Callable[[str], Optional[str]]] = None,
                 on_changed: Optional[Callable[[], None]] = None) -> None:
        super().__init__(title=title)
        self.path: Optional[str] = None
        self.description: Optional[str] = None
        self.changed = False
        self._validator = validator
        self._on_changed = on_changed
        self.error_handler: Optional[Callable[[str], None]] = None

        choose = Gtk.Button(icon_name="document-open-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text="Choose file…")
        choose.add_css_class("flat")
        choose.connect("clicked", self._on_choose)
        self.clear_button = Gtk.Button(icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER,
                                       tooltip_text="Remove")
        self.clear_button.add_css_class("flat")
        self.clear_button.connect("clicked", self._on_clear)
        self.add_suffix(choose)
        self.add_suffix(self.clear_button)
        self._refresh()

    def load(self, scheme: NM.Setting8021xCKScheme, get_path: Callable[[], Optional[str]],
             get_uri: Callable[[], Optional[str]]) -> None:
        """Show the value currently stored in the profile.

        The path/URI getters are passed as callables because libnm asserts
        (nm-CRITICAL) if get_*_path() is called when the scheme is not PATH.
        """
        self.changed = False
        self.path = None
        self.description = None
        if scheme == NM.Setting8021xCKScheme.PATH:
            self.path = get_path()
        elif scheme == NM.Setting8021xCKScheme.BLOB:
            self.description = "Embedded in profile (kept)"
        elif scheme == NM.Setting8021xCKScheme.PKCS11:
            uri = get_uri()
            self.description = f"PKCS#11: {uri}" if uri else "PKCS#11 token (kept)"
        self._refresh()

    def has_value(self) -> bool:
        return bool(self.path or self.description)

    def _refresh(self) -> None:
        if self.path:
            self.set_subtitle(self.path)
        elif self.description:
            self.set_subtitle(self.description)
        else:
            self.set_subtitle("None selected")
        self.clear_button.set_sensitive(self.has_value())

    def _on_choose(self, _button) -> None:
        dialog = Gtk.FileDialog(title=self.get_title(), modal=True)
        file_filter = Gtk.FileFilter(name="Certificates and keys")
        for pattern in CERT_FILE_PATTERNS:
            file_filter.add_pattern(pattern)
        all_files = Gtk.FileFilter(name="All files")
        all_files.add_pattern("*")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(file_filter)
        filters.append(all_files)
        dialog.set_filters(filters)
        if self.path:
            dialog.set_initial_folder(Gio.File.new_for_path(os.path.dirname(self.path)))
        dialog.open(self.get_root(), None, self._on_chosen)

    def _on_chosen(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return  # cancelled
        path = gfile.get_path() if gfile else None
        if not path:
            return
        if self._validator:
            error = self._validator(path)
            if error:
                if self.error_handler:
                    self.error_handler(error)
                return
        self.path = path
        self.description = None
        self.changed = True
        self._refresh()
        if self._on_changed:
            self._on_changed()

    def _on_clear(self, _button) -> None:
        self.path = None
        self.description = None
        self.changed = True
        self._refresh()
        if self._on_changed:
            self._on_changed()


# -----------------------------------------------------------------------------
# Validation helpers
# -----------------------------------------------------------------------------

def _split_list(text: str) -> list[str]:
    """Split a comma/space separated list ("1.1.1.1, 8.8.8.8")."""
    return [item for item in text.replace(",", " ").split() if item]


def _validate_ca_file(path: str) -> Optional[str]:
    """Let libnm parse the certificate (PEM or DER) to check it is usable."""
    try:
        NM.Setting8021x.new().set_ca_cert(path, NM.Setting8021xCKScheme.PATH,
                                          NM.Setting8021xCKFormat.UNKNOWN)
    except GLib.Error as error:
        return f"Not a valid CA certificate: {friendly_error(error)}"
    return None


def _validate_client_cert(path: str) -> Optional[str]:
    try:
        NM.Setting8021x.new().set_client_cert(path, NM.Setting8021xCKScheme.PATH,
                                              NM.Setting8021xCKFormat.UNKNOWN)
    except GLib.Error as error:
        return f"Not a valid client certificate: {friendly_error(error)}"
    return None


def _parse_addresses(text: str, family: int, label: str) -> list[NM.IPAddress]:
    """Parse 'addr/prefix, addr/prefix' into NM.IPAddress objects."""
    max_prefix = 32 if family == socket.AF_INET else 128
    example = "192.168.1.10/24" if family == socket.AF_INET else "2001:db8::10/64"
    result = []
    for item in _split_list(text):
        address, slash, prefix_text = item.partition("/")
        if not slash:
            raise EditorError(f"{label}: add a prefix length to '{item}', e.g. {example}.")
        try:
            prefix = int(prefix_text)
        except ValueError:
            raise EditorError(f"{label}: '{prefix_text}' is not a valid prefix length.")
        if not 1 <= prefix <= max_prefix:
            raise EditorError(f"{label}: prefix must be between 1 and {max_prefix}.")
        if not NM.utils_ipaddr_valid(family, address):
            raise EditorError(f"{label}: '{address}' is not a valid address.")
        result.append(NM.IPAddress.new(family, address, prefix))
    return result


def _parse_ip_list(text: str, family: int, label: str) -> list[str]:
    items = _split_list(text)
    for item in items:
        if not NM.utils_ipaddr_valid(family, item):
            raise EditorError(f"{label}: '{item}' is not a valid address.")
    return items


# -----------------------------------------------------------------------------
# IP settings page (shared by IPv4 and IPv6)
# -----------------------------------------------------------------------------

class IPPage(Adw.PreferencesPage):
    """Method, addresses, gateway, DNS and routing options for one family."""

    def __init__(self, family: int, setting: Optional[NM.SettingIPConfig]) -> None:
        super().__init__()
        self.family = family
        self.is_v4 = family == socket.AF_INET
        label = "IPv4" if self.is_v4 else "IPv6"
        choices = IPV4_METHOD_CHOICES if self.is_v4 else IPV6_METHOD_CHOICES

        method = setting.get_method() if setting else "auto"

        # --- Method ---
        method_group = Adw.PreferencesGroup(title=f"{label} method")
        self.method_row = ChoiceRow("Method", choices, method)
        self.method_row.connect("notify::selected", lambda *_: self._update_visibility())
        method_group.add(self.method_row)
        self.add(method_group)

        # --- Addresses ---
        self.address_group = Adw.PreferencesGroup(title="Addresses")
        example = "192.168.1.10/24" if self.is_v4 else "2001:db8::10/64"
        self.addresses_row = Adw.EntryRow(title=f"Addresses (e.g. {example})")
        self.gateway_row = Adw.EntryRow(title="Gateway")
        self.address_group.add(self.addresses_row)
        self.address_group.add(self.gateway_row)
        self.add(self.address_group)

        # --- DNS ---
        self.dns_group = Adw.PreferencesGroup(
            title="DNS",
            description="Separate multiple entries with commas.")
        self.dns_row = Adw.EntryRow(title="DNS servers")
        self.search_row = Adw.EntryRow(title="Search domains")
        self.ignore_auto_dns_row = Adw.SwitchRow(
            title="Use only these DNS servers",
            subtitle="Ignore DNS servers provided automatically (DHCP / router advertisements)")
        self.dns_group.add(self.dns_row)
        self.dns_group.add(self.search_row)
        self.dns_group.add(self.ignore_auto_dns_row)
        self.add(self.dns_group)

        # --- Routing / privacy ---
        self.advanced_group = Adw.PreferencesGroup(title="Advanced")
        self.never_default_row = Adw.SwitchRow(
            title="Use only for resources on this network",
            subtitle="Never use this connection as the default route")
        self.advanced_group.add(self.never_default_row)
        self.privacy_row: Optional[ChoiceRow] = None
        if not self.is_v4:
            privacy = (int(setting.get_ip6_privacy())
                       if isinstance(setting, NM.SettingIP6Config) else -1)
            self.privacy_row = ChoiceRow("Privacy extensions", IPV6_PRIVACY_CHOICES, privacy)
            self.advanced_group.add(self.privacy_row)
        self.add(self.advanced_group)

        # Load current values.
        if setting is not None:
            self.addresses_row.set_text(", ".join(
                f"{setting.get_address(i).get_address()}/{setting.get_address(i).get_prefix()}"
                for i in range(setting.get_num_addresses())))
            self.gateway_row.set_text(setting.get_gateway() or "")
            self.dns_row.set_text(", ".join(setting.get_dns(i) for i in range(setting.get_num_dns())))
            self.search_row.set_text(", ".join(
                setting.get_dns_search(i) for i in range(setting.get_num_dns_searches())))
            self.ignore_auto_dns_row.set_active(setting.get_ignore_auto_dns())
            self.never_default_row.set_active(setting.get_never_default())
        self._update_visibility()

    def _update_visibility(self) -> None:
        method = self.method_row.get_value()
        configurable = method not in ("disabled", "ignore")
        self.address_group.set_visible(configurable)
        self.dns_group.set_visible(configurable)
        self.advanced_group.set_visible(configurable)
        # With automatic methods, extra static addresses are allowed in NM
        # and are added alongside the automatic ones.
        if method == "manual":
            self.address_group.set_description(None)
        else:
            self.address_group.set_description(
                "Optional. Static addresses are added in addition to automatic ones.")
        self.ignore_auto_dns_row.set_visible(method in ("auto", "dhcp"))

    def apply(self, connection: NM.Connection) -> None:
        """Write the form into the connection's ipv4/ipv6 setting."""
        label = "IPv4" if self.is_v4 else "IPv6"
        if self.is_v4:
            setting = connection.get_setting_ip4_config()
            if setting is None:
                setting = NM.SettingIP4Config.new()
                connection.add_setting(setting)
        else:
            setting = connection.get_setting_ip6_config()
            if setting is None:
                setting = NM.SettingIP6Config.new()
                connection.add_setting(setting)

        method = self.method_row.get_value()
        setting.set_property("method", method)
        if method in ("disabled", "ignore"):
            return  # leave address/DNS properties untouched

        addresses = _parse_addresses(self.addresses_row.get_text(), self.family,
                                     f"{label} addresses")
        if method == "manual" and not addresses:
            raise EditorError(f"{label}: manual configuration needs at least one address.")
        gateway = self.gateway_row.get_text().strip()
        if gateway and not NM.utils_ipaddr_valid(self.family, gateway):
            raise EditorError(f"{label} gateway '{gateway}' is not a valid address.")
        dns = _parse_ip_list(self.dns_row.get_text(), self.family, f"{label} DNS")

        setting.clear_addresses()
        for address in addresses:
            setting.add_address(address)
        setting.set_property("gateway", gateway or None)

        setting.clear_dns()
        for server in dns:
            setting.add_dns(server)
        setting.clear_dns_searches()
        for domain in _split_list(self.search_row.get_text()):
            setting.add_dns_search(domain)
        if method in ("auto", "dhcp"):
            setting.set_property("ignore-auto-dns", self.ignore_auto_dns_row.get_active())
        setting.set_property("never-default", self.never_default_row.get_active())
        if self.privacy_row is not None:
            setting.set_property("ip6-privacy",
                                 NM.SettingIP6ConfigPrivacy(self.privacy_row.get_value()))


# -----------------------------------------------------------------------------
# The editor dialog
# -----------------------------------------------------------------------------

class ConnectionEditor(Adw.Dialog):
    """Edit an existing profile (remote=...) or create a new one.

    Arguments for new profiles:
      new_type            NM connection type, Wi-Fi or Ethernet
      ssid, hidden        prefill for Wi-Fi
      security_mode       prefill ('open', 'wpa-psk', 'sae', 'enterprise', ...)
      connect_after_save  activate the profile after saving ("Save & Connect")
      device              device to activate on (optional)
    on_finished(message, ok) is called once after saving or failing.
    """

    def __init__(self, backend: NetworkBackend, profiles: ProfileManager, *,
                 remote: Optional[NM.RemoteConnection] = None,
                 new_type: str = NM.SETTING_WIRELESS_SETTING_NAME,
                 ssid: Optional[str] = None,
                 hidden: bool = False,
                 security_mode: Optional[str] = None,
                 connect_after_save: bool = False,
                 device: Optional[NM.Device] = None,
                 on_finished: Optional[Callable[[str, bool], None]] = None) -> None:
        super().__init__()
        self.backend = backend
        self.profiles = profiles
        self.remote = remote
        self.device = device
        self.connect_after_save = connect_after_save
        self.on_finished = on_finished

        # The form is loaded from a clone so nothing touches the live object.
        if remote is not None:
            self.source: NM.Connection = NM.SimpleConnection.new_clone(remote)
            self.type_name = remote.get_connection_type() or ""
        else:
            self.source = NM.SimpleConnection.new()
            self.type_name = new_type

        self.is_wifi = self.type_name == NM.SETTING_WIRELESS_SETTING_NAME
        self.is_ethernet = self.type_name == NM.SETTING_WIRED_SETTING_NAME
        self.has_ip_pages = self.is_wifi or self.is_ethernet

        title = (remote.get_id() if remote is not None
                 else f"New {connection_type_label(self.type_name)} Connection")
        self.set_title(title)
        self.set_content_width(600)
        self.set_content_height(720)

        self._build_layout(connect_after_save)
        self._build_general_page(ssid, hidden)
        if self.is_wifi:
            self._build_security_page(security_mode)
        if self.has_ip_pages:
            self.ipv4_page = IPPage(socket.AF_INET, self.source.get_setting_ip4_config())
            self.stack.add_titled_with_icon(self.ipv4_page, "ipv4", "IPv4",
                                            "network-workgroup-symbolic")
            self.ipv6_page = IPPage(socket.AF_INET6, self.source.get_setting_ip6_config())
            self.stack.add_titled_with_icon(self.ipv6_page, "ipv6", "IPv6",
                                            "network-workgroup-symbolic")

    # -------------------------------------------------------------------------
    # Layout
    # -------------------------------------------------------------------------

    def _build_layout(self, connect_after_save: bool) -> None:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)

        self.save_button = Gtk.Button(label="Save & Connect" if connect_after_save else "Save")
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", self._on_save)
        header.pack_end(self.save_button)

        self.spinner = Adw.Spinner(visible=False)
        header.pack_end(self.spinner)

        header.set_title_widget(Adw.WindowTitle(title=self.get_title()))
        toolbar.add_top_bar(header)

        # OLD IMPLEMENTATION - disabled in V2 development
        # Reason: a ViewSwitcher in the header bar did not fit next to the
        # Cancel/Save buttons when Hyprland tiles the window narrow; the tabs
        # now sit in their own bar below the header (see below).
        #
        # self.stack = Adw.ViewStack()
        # switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        # header.set_title_widget(switcher)
        # toolbar.add_top_bar(header)

        # Tabs: an inline (segmented) switcher in its own top bar, which
        # shrinks gracefully on narrow windows.
        self.stack = Adw.ViewStack()
        switcher = Adw.InlineViewSwitcher(stack=self.stack,
                                          display_mode=Adw.InlineViewSwitcherDisplayMode.BOTH,
                                          margin_start=12, margin_end=12,
                                          margin_top=6, margin_bottom=6)
        switcher.add_css_class("flat")
        toolbar.add_top_bar(switcher)

        # Validation and save errors appear in this banner.
        self.banner = Adw.Banner()
        toolbar.add_top_bar(self.banner)

        toolbar.set_content(self.stack)
        self.set_child(toolbar)

    def _show_error(self, message: str) -> None:
        self.banner.set_title(message)
        self.banner.set_revealed(True)

    def _set_busy(self, busy: bool) -> None:
        self.save_button.set_sensitive(not busy)
        self.spinner.set_visible(busy)
        if busy:
            self.banner.set_revealed(False)

    # -------------------------------------------------------------------------
    # General tab
    # -------------------------------------------------------------------------

    def _build_general_page(self, preset_ssid: Optional[str], preset_hidden: bool) -> None:
        page = Adw.PreferencesPage()
        s_con = self.source.get_setting_connection()

        # --- Identity ---
        identity = Adw.PreferencesGroup(title="Connection")
        default_name = preset_ssid or ("Wired connection" if self.is_ethernet else "")
        self.name_row = Adw.EntryRow(title="Name")
        self.name_row.set_text(s_con.get_id() if s_con else default_name)
        identity.add(self.name_row)

        self.ssid_row: Optional[Adw.EntryRow] = None
        self.hidden_row: Optional[Adw.SwitchRow] = None
        self.original_ssid_bytes: Optional[GLib.Bytes] = None
        self.original_ssid_text = ""
        if self.is_wifi:
            s_wifi = self.source.get_setting_wireless()
            self.original_ssid_bytes = s_wifi.get_ssid() if s_wifi else None
            self.original_ssid_text = ssid_to_text(self.original_ssid_bytes)
            self.ssid_row = Adw.EntryRow(title="Network name (SSID)")
            self.ssid_row.set_text(self.original_ssid_text or preset_ssid or "")
            identity.add(self.ssid_row)
            self.hidden_row = Adw.SwitchRow(
                title="Hidden network",
                subtitle="The network does not broadcast its name")
            self.hidden_row.set_active(s_wifi.get_hidden() if s_wifi else preset_hidden)
            identity.add(self.hidden_row)
            # Keep the name in sync with the SSID while creating a profile,
            # until the user types a name of their own.
            if self.remote is None:
                self.ssid_row.connect("changed", self._on_ssid_changed_for_name)
        page.add(identity)

        # --- Automatic connection ---
        auto = Adw.PreferencesGroup(
            title="Automatic connection",
            description="When several saved networks are available, the one "
                        "with the highest priority is chosen.")
        self.autoconnect_row = Adw.SwitchRow(title="Connect automatically")
        self.autoconnect_row.set_active(s_con.get_autoconnect() if s_con else True)
        auto.add(self.autoconnect_row)
        self.priority_row = Adw.SpinRow.new_with_range(-999, 999, 1)
        self.priority_row.set_title("Priority")
        self.priority_row.set_subtitle("Higher numbers are preferred (default 0)")
        self.priority_row.set_value(s_con.get_autoconnect_priority() if s_con else 0)
        auto.add(self.priority_row)
        page.add(auto)

        # --- Device / link ---
        self.interface_row: Optional[Adw.EntryRow] = None
        self.mtu_row: Optional[Adw.SpinRow] = None
        if self.has_ip_pages:
            link = Adw.PreferencesGroup(title="Device")
            example = "wlan0" if self.is_wifi else "enp3s0"
            self.interface_row = Adw.EntryRow(title=f"Restrict to interface (optional, e.g. {example})")
            self.interface_row.set_text((s_con.get_interface_name() if s_con else None) or "")
            link.add(self.interface_row)
            self.mtu_row = Adw.SpinRow.new_with_range(0, 65535, 1)
            self.mtu_row.set_title("MTU")
            self.mtu_row.set_subtitle("0 = automatic")
            link_setting = (self.source.get_setting_wireless() if self.is_wifi
                            else self.source.get_setting_wired())
            self.mtu_row.set_value(link_setting.get_mtu() if link_setting else 0)
            link.add(self.mtu_row)
            page.add(link)

        # --- Read-only information ---
        if self.remote is not None:
            info = Adw.PreferencesGroup(title="Details")
            info.add(info_row("Type", connection_type_label(self.type_name)))
            info.add(info_row("UUID", self.remote.get_uuid()))
            info.add(info_row("Stored in", self.remote.get_filename() or "Memory only"))
            if not self.has_ip_pages:
                info.set_description(
                    "Only general options can be edited for this connection type. "
                    "All other settings are kept unchanged.")
            page.add(info)

        self.stack.add_titled_with_icon(page, "general", "General", "emblem-system-symbolic")

    def _on_ssid_changed_for_name(self, entry: Adw.EntryRow) -> None:
        # Follow the SSID only while the name is still empty or matches the
        # previously auto-filled value.
        previous = getattr(self, "_auto_name", "")
        if self.name_row.get_text() in ("", previous):
            self._auto_name = entry.get_text()
            self.name_row.set_text(self._auto_name)

    # -------------------------------------------------------------------------
    # Security tab (Wi-Fi only)
    # -------------------------------------------------------------------------

    def _current_security_mode(self) -> str:
        wsec = self.source.get_setting_wireless_security()
        if wsec is None:
            return "open"
        key_mgmt = wsec.get_key_mgmt()
        if key_mgmt in ("wpa-psk", "sae", "owe"):
            return key_mgmt
        if key_mgmt == "wpa-eap":
            return "enterprise"
        # WEP, dynamic WEP, Suite-B... -> kept unchanged
        return key_mgmt or "unknown"

    def _build_security_page(self, preset_mode: Optional[str]) -> None:
        page = Adw.PreferencesPage()
        self.original_mode = self._current_security_mode() if self.remote is not None else None
        start_mode = self.original_mode or preset_mode or "wpa-psk"
        wsec = self.source.get_setting_wireless_security()
        s8021x = self.source.get_setting_802_1x()

        # --- Security type ---
        type_group = Adw.PreferencesGroup(title="Security")
        self.security_row = ChoiceRow("Security", WIFI_SECURITY_CHOICES, start_mode)
        self.security_row.connect("notify::selected", lambda *_: self._update_security_visibility())
        type_group.add(self.security_row)
        page.add(type_group)

        # --- Personal (PSK / SAE) ---
        self.personal_group = Adw.PreferencesGroup(title="Password")
        self.psk_row = Adw.PasswordEntryRow(title="Password")
        self.personal_group.add(self.psk_row)
        if self.remote is not None:
            self.personal_group.set_description("Leave empty to keep the saved password.")
        page.add(self.personal_group)
        if wsec is not None:
            self._describe_secret_storage(self.personal_group, wsec.get_psk_flags())

        # --- Enterprise: method and credentials ---
        current_eap = None
        if s8021x is not None and s8021x.get_num_eap_methods() > 0:
            methods = [s8021x.get_eap_method(i) for i in range(s8021x.get_num_eap_methods())]
            # Several methods at once is unusual and cannot be represented
            # by one combo; keep such a list exactly as it is.
            current_eap = methods[0] if len(methods) == 1 else ",".join(methods)
        self.original_eap = current_eap

        self.eap_group = Adw.PreferencesGroup(title="Authentication")
        self.eap_row = ChoiceRow("Method", EAP_CHOICES, current_eap or "peap")
        self.eap_row.connect("notify::selected", lambda *_: self._on_eap_changed())
        self.eap_group.add(self.eap_row)

        self.identity_row = Adw.EntryRow(title="Identity (username)")
        self.identity_row.set_text((s8021x.get_identity() if s8021x else None) or "")
        self.eap_group.add(self.identity_row)

        self.anon_row = Adw.EntryRow(title="Anonymous identity (optional)")
        self.anon_row.set_text((s8021x.get_anonymous_identity() if s8021x else None) or "")
        self.eap_group.add(self.anon_row)

        self.original_inner = None
        if s8021x is not None:
            if s8021x.get_phase2_auth():
                self.original_inner = f"auth:{s8021x.get_phase2_auth()}"
            elif s8021x.get_phase2_autheap():
                self.original_inner = f"autheap:{s8021x.get_phase2_autheap()}"
        self.inner_row = ChoiceRow("Inner authentication", PEAP_INNER_CHOICES)
        self.eap_group.add(self.inner_row)

        self.eap_password_row = Adw.PasswordEntryRow(title="Password")
        self.eap_group.add(self.eap_password_row)

        # TLS client certificate and key.
        self.client_cert_row = FileRow("Client certificate", _validate_client_cert)
        self.client_cert_row.error_handler = self._show_error
        self.private_key_row = FileRow("Private key")
        self.key_password_row = Adw.PasswordEntryRow(title="Private key password")
        for row in (self.client_cert_row, self.private_key_row, self.key_password_row):
            self.eap_group.add(row)
        if s8021x is not None:
            self.client_cert_row.load(s8021x.get_client_cert_scheme(),
                                      s8021x.get_client_cert_path, s8021x.get_client_cert_uri)
            self.private_key_row.load(s8021x.get_private_key_scheme(),
                                      s8021x.get_private_key_path, s8021x.get_private_key_uri)
            self._describe_secret_storage(self.eap_group, s8021x.get_password_flags())
        page.add(self.eap_group)

        # Profiles using an EAP method we do not edit (FAST, PWD, LEAP or a
        # list of methods) keep their whole 802.1X setting untouched.
        self.eap_kept_group = Adw.PreferencesGroup(title="Authentication")
        self.eap_kept_group.add(info_row(
            "802.1X settings", f"Uses {current_eap}; kept unchanged by this editor"))
        page.add(self.eap_kept_group)

        # --- Enterprise: server certificate validation ---
        self.ca_group = Adw.PreferencesGroup(
            title="Server certificate",
            description="Verifying the server certificate protects your password "
                        "from fake access points.")
        self.system_ca_row = Adw.SwitchRow(
            title="Use system CA certificates",
            subtitle="Trust certificates from the system store (/etc/ssl/certs)")
        self.system_ca_row.set_active(bool(s8021x and s8021x.get_system_ca_certs()))
        self.system_ca_row.connect("notify::active", lambda *_: self._update_security_visibility())
        self.ca_group.add(self.system_ca_row)

        self.ca_row = FileRow("CA certificate", _validate_ca_file,
                              on_changed=self._on_ca_file_changed)
        self.ca_row.error_handler = self._show_error
        if s8021x is not None:
            self.ca_row.load(s8021x.get_ca_cert_scheme(), s8021x.get_ca_cert_path,
                             s8021x.get_ca_cert_uri)
        self.ca_group.add(self.ca_row)

        self.ca_path = s8021x.get_ca_path() if s8021x else None
        if self.ca_path:
            # A CA directory set by another tool; shown and kept.
            self.ca_group.add(info_row("CA directory (kept)", self.ca_path))

        self.domain_row = Adw.EntryRow(title="Domain (e.g. radius.school.dk)")
        self.domain_row.set_text((s8021x.get_domain_suffix_match() if s8021x else None) or "")
        self.ca_group.add(self.domain_row)

        self.no_ca_row = Adw.SwitchRow(
            title="No CA certificate is required",
            subtitle="Connect without verifying the server (not recommended)")
        has_ca_source = bool(self.ca_row.has_value() or self.system_ca_row.get_active()
                             or self.ca_path)
        # For existing profiles this mirrors what is stored. A new profile
        # starts with it off so the user has to make a deliberate choice.
        self.no_ca_row.set_active(s8021x is not None and self.original_mode == "enterprise"
                                  and not has_ca_source)
        self.no_ca_row.connect("notify::active", lambda *_: self._update_security_visibility())
        self.ca_group.add(self.no_ca_row)

        self.ca_warning_row = Adw.ActionRow(
            title="Server certificate is not verified",
            subtitle="Anyone imitating this network could capture your password.")
        self.ca_warning_row.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic"))
        self.ca_warning_row.add_css_class("security-warning")
        self.ca_group.add(self.ca_warning_row)

        self.domain_hint_row = Adw.ActionRow(
            title="Tip: set the domain",
            subtitle="With system CA certificates, also enter the server's domain so "
                     "only your network's certificate is accepted.")
        self.domain_hint_row.add_prefix(Gtk.Image(icon_name="dialog-information-symbolic"))
        self.ca_group.add(self.domain_hint_row)
        self.domain_row.connect("changed", lambda *_: self._update_security_visibility())
        page.add(self.ca_group)

        # --- Unsupported existing security (WEP, ...) ---
        self.kept_group = Adw.PreferencesGroup(title="Security settings")
        self.kept_group.add(info_row(
            "Kept unchanged",
            "This security type cannot be edited here. Choose another type above "
            "to replace it, or keep the existing settings."))
        page.add(self.kept_group)

        self._on_eap_changed(initial=True)
        self.stack.add_titled_with_icon(page, "security", "Security", "changes-prevent-symbolic")

    @staticmethod
    def _describe_secret_storage(group: Adw.PreferencesGroup,
                                 flags: NM.SettingSecretFlags) -> None:
        """Tell the user where an existing password is kept (no secrets shown)."""
        if flags & NM.SettingSecretFlags.NOT_SAVED:
            group.set_description("This password is not saved; NetworkManager asks "
                                  "for it on every connection. Leave empty to keep that.")
        elif flags & NM.SettingSecretFlags.AGENT_OWNED:
            group.set_description("This password is stored in your keyring "
                                  "(via nm-applet). Leave empty to keep it.")

    def _on_eap_changed(self, initial: bool = False) -> None:
        method = self.eap_row.get_value()
        choices = PEAP_INNER_CHOICES if method == "peap" else TTLS_INNER_CHOICES
        # Keep the profile's inner method if the outer method is unchanged.
        current = self.original_inner if method == self.original_eap else None
        self.inner_row.set_choices(choices, current)
        self._update_security_visibility()

    def _on_ca_file_changed(self) -> None:
        # Choosing a CA certificate is a clear decision to verify the server.
        if self.ca_row.has_value():
            self.no_ca_row.set_active(False)
        self._update_security_visibility()

    def _update_security_visibility(self) -> None:
        mode = self.security_row.get_value()
        known_modes = [value for value, _label in WIFI_SECURITY_CHOICES]
        enterprise = mode == "enterprise"
        method = self.eap_row.get_value()
        eap_editable = method in ("peap", "ttls", "tls")
        password_eap = method in ("peap", "ttls")

        self.personal_group.set_visible(mode in ("wpa-psk", "sae"))
        self.kept_group.set_visible(mode not in known_modes)

        self.eap_group.set_visible(enterprise and eap_editable)
        self.eap_kept_group.set_visible(enterprise and not eap_editable)
        self.anon_row.set_visible(password_eap)
        self.inner_row.set_visible(password_eap)
        self.eap_password_row.set_visible(password_eap)
        for row in (self.client_cert_row, self.private_key_row, self.key_password_row):
            row.set_visible(method == "tls")

        self.ca_group.set_visible(enterprise and eap_editable)
        no_ca = self.no_ca_row.get_active()
        self.system_ca_row.set_sensitive(not no_ca)
        self.ca_row.set_sensitive(not no_ca and not self.system_ca_row.get_active())
        self.domain_row.set_sensitive(not no_ca)
        self.ca_warning_row.set_visible(no_ca)
        self.domain_hint_row.set_visible(
            not no_ca and self.system_ca_row.get_active()
            and not self.domain_row.get_text().strip())

    # -------------------------------------------------------------------------
    # Writing the form into a connection
    # -------------------------------------------------------------------------

    def _secrets_entered(self) -> bool:
        """True if the user typed any new secret (drives fetch_secrets)."""
        if not self.is_wifi:
            return False
        mode = self.security_row.get_value()
        if mode in ("wpa-psk", "sae") and self.psk_row.get_text():
            return True
        if mode == "enterprise" and (self.eap_password_row.get_text()
                                     or self.key_password_row.get_text()):
            return True
        return False

    def _new_skeleton(self) -> NM.Connection:
        """Minimal new profile; the form fills in the rest."""
        connection = NM.SimpleConnection.new()
        s_con = NM.SettingConnection.new()
        s_con.set_property("uuid", NM.utils_uuid_generate())
        s_con.set_property("type", self.type_name)
        connection.add_setting(s_con)
        if self.is_wifi:
            s_wifi = NM.SettingWireless.new()
            s_wifi.set_property("mode", "infrastructure")
            connection.add_setting(s_wifi)
        elif self.is_ethernet:
            connection.add_setting(NM.SettingWired.new())
        return connection

    def _apply_general(self, connection: NM.Connection) -> None:
        name = self.name_row.get_text().strip()
        if not name:
            raise EditorError("Enter a name for the connection.")
        s_con = connection.get_setting_connection()
        s_con.set_property("id", name)
        s_con.set_property("autoconnect", self.autoconnect_row.get_active())
        s_con.set_property("autoconnect-priority", int(self.priority_row.get_value()))

        if self.interface_row is not None:
            interface = self.interface_row.get_text().strip()
            s_con.set_property("interface-name", interface or None)

        if self.is_wifi:
            s_wifi = connection.get_setting_wireless()
            ssid_text = self.ssid_row.get_text()
            if ssid_text != self.original_ssid_text or self.original_ssid_bytes is None:
                # Only rewrite the SSID when it was edited, so SSIDs that are
                # not valid UTF-8 survive a save unchanged.
                raw = ssid_text.encode("utf-8")
                if not raw:
                    raise EditorError("Enter the network name (SSID).")
                if len(raw) > 32:
                    raise EditorError("The network name (SSID) can be at most 32 bytes.")
                s_wifi.set_property("ssid", GLib.Bytes.new(raw))
            s_wifi.set_property("hidden", self.hidden_row.get_active())
            s_wifi.set_property("mtu", int(self.mtu_row.get_value()))
        elif self.is_ethernet:
            s_wired = connection.get_setting_wired()
            if s_wired is None:
                s_wired = NM.SettingWired.new()
                connection.add_setting(s_wired)
            s_wired.set_property("mtu", int(self.mtu_row.get_value()))

    def _apply_security(self, connection: NM.Connection) -> None:
        mode = self.security_row.get_value()
        known_modes = [value for value, _label in WIFI_SECURITY_CHOICES]
        if mode not in known_modes:
            return  # "Keep existing" (WEP etc.): leave everything as stored

        mode_changed = mode != self.original_mode
        wsec_type = NM.SettingWirelessSecurity.__gtype__
        s8021x_type = NM.Setting8021x.__gtype__

        if mode == "open":
            connection.remove_setting(wsec_type)
            connection.remove_setting(s8021x_type)
            return

        wsec = connection.get_setting_wireless_security()
        if wsec is None or mode_changed:
            # A new security type starts from a clean setting so stale
            # options (e.g. WEP keys, pairwise ciphers) do not linger.
            # Same type: keep the existing setting with all its options.
            if mode_changed and mode in ("wpa-psk", "sae") and self.original_mode in ("wpa-psk", "sae") and wsec is not None:
                pass  # PSK <-> SAE switch keeps flags and the stored password
            else:
                connection.remove_setting(wsec_type)
                wsec = NM.SettingWirelessSecurity.new()
                connection.add_setting(wsec)

        if mode in ("wpa-psk", "sae"):
            wsec.set_property("key-mgmt", mode)
            password = self.psk_row.get_text()
            needs_new = self.remote is None or self.original_mode not in ("wpa-psk", "sae")
            if password:
                if mode == "wpa-psk" and not NM.utils_wpa_psk_valid(password):
                    raise EditorError("WPA passwords must be 8–63 characters "
                                      "(or 64 hexadecimal digits).")
                wsec.set_property("psk", password)
            elif needs_new:
                raise EditorError("Enter the network password.")
            connection.remove_setting(s8021x_type)
        elif mode == "owe":
            wsec.set_property("key-mgmt", "owe")
            connection.remove_setting(s8021x_type)
        elif mode == "enterprise":
            wsec.set_property("key-mgmt", "wpa-eap")
            self._apply_8021x(connection)

    def _apply_8021x(self, connection: NM.Connection) -> None:
        method = self.eap_row.get_value()
        if method not in ("peap", "ttls", "tls"):
            # Unsupported/multi-method EAP: the existing setting stays as-is.
            if connection.get_setting_802_1x() is None:
                raise EditorError("Choose an authentication method.")
            return

        s8021x = connection.get_setting_802_1x()
        if s8021x is None:
            s8021x = NM.Setting8021x.new()
            connection.add_setting(s8021x)

        s8021x.clear_eap_methods()
        s8021x.add_eap_method(method)

        identity = self.identity_row.get_text().strip()
        if not identity:
            raise EditorError("Enter your identity (username).")
        s8021x.set_property("identity", identity)

        if method in ("peap", "ttls"):
            s8021x.set_property("anonymous-identity", self.anon_row.get_text().strip() or None)
            kind, _, inner = (self.inner_row.get_value() or "auth:mschapv2").partition(":")
            if kind == "autheap":
                s8021x.set_property("phase2-auth", None)
                s8021x.set_property("phase2-autheap", inner)
            else:
                s8021x.set_property("phase2-autheap", None)
                s8021x.set_property("phase2-auth", inner)
            password = self.eap_password_row.get_text()
            if password:
                s8021x.set_property("password", password)
        else:  # TLS
            s8021x.set_property("phase2-auth", None)
            s8021x.set_property("phase2-autheap", None)
            if self.client_cert_row.changed:
                if self.client_cert_row.path:
                    s8021x.set_client_cert(self.client_cert_row.path,
                                           NM.Setting8021xCKScheme.PATH,
                                           NM.Setting8021xCKFormat.UNKNOWN)
                else:
                    s8021x.set_property("client-cert", None)
            if not self.client_cert_row.has_value():
                raise EditorError("TLS needs a client certificate.")
            key_password = self.key_password_row.get_text() or None
            if self.private_key_row.changed:
                if self.private_key_row.path:
                    try:
                        s8021x.set_private_key(self.private_key_row.path, key_password,
                                               NM.Setting8021xCKScheme.PATH,
                                               NM.Setting8021xCKFormat.UNKNOWN)
                    except GLib.Error as error:
                        raise EditorError(f"Private key: {friendly_error(error)}")
                else:
                    s8021x.set_property("private-key", None)
            elif key_password:
                s8021x.set_property("private-key-password", key_password)
            if not self.private_key_row.has_value():
                raise EditorError("TLS needs a private key.")

        # --- Server certificate validation ---
        no_ca = self.no_ca_row.get_active()
        if no_ca:
            # Explicit user choice; the warning row was shown while selected.
            # Every CA source is cleared so the profile really matches what
            # the switch says. The domain is left as typed.
            s8021x.set_property("system-ca-certs", False)
            s8021x.set_property("ca-cert", None)
            s8021x.set_property("ca-path", None)
            return

        s8021x.set_property("system-ca-certs", self.system_ca_row.get_active())
        if self.ca_row.changed:
            if self.ca_row.path:
                s8021x.set_ca_cert(self.ca_row.path, NM.Setting8021xCKScheme.PATH,
                                   NM.Setting8021xCKFormat.UNKNOWN)
            else:
                s8021x.set_property("ca-cert", None)
        s8021x.set_property("domain-suffix-match", self.domain_row.get_text().strip() or None)

        if not (self.system_ca_row.get_active() or self.ca_row.has_value() or self.ca_path):
            raise EditorError("Choose a CA certificate, use the system CA certificates, "
                              "or explicitly select “No CA certificate is required”.")

    def _apply(self, connection: NM.Connection) -> None:
        self._apply_general(connection)
        if self.is_wifi:
            self._apply_security(connection)
        if self.has_ip_pages:
            self.ipv4_page.apply(connection)
            self.ipv6_page.apply(connection)

    # -------------------------------------------------------------------------
    # Saving
    # -------------------------------------------------------------------------

    def _on_save(self, _button) -> None:
        self._set_busy(True)
        if self.remote is None:
            self._finish_save(self._new_skeleton(), None)
            return

        # Start from the profile as it is stored *now* (it may have changed
        # since the dialog opened), keeping every property we do not edit.
        work = NM.SimpleConnection.new_clone(self.remote)
        if self._secrets_entered():
            self.profiles.fetch_secrets(self.remote, work,
                                        lambda error: self._finish_save(work, error))
        else:
            self._finish_save(work, None)

    def _finish_save(self, work: NM.Connection, error: Optional[str]) -> None:
        if error:
            self._set_busy(False)
            self._show_error(error)
            return
        try:
            self._apply(work)
            # Let libnm fill defaults and run the same checks the daemon does.
            work.normalize()
        except EditorError as exc:
            self._set_busy(False)
            self._show_error(str(exc))
            return
        except GLib.Error as exc:
            self._set_busy(False)
            self._show_error(friendly_error(exc))
            return

        if self.remote is not None:
            self.profiles.update(self.remote, work,
                                 lambda _result, err: self._after_save(self.remote, err))
        else:
            self.profiles.add(work, lambda remote, err: self._after_save(remote, err))

    def _after_save(self, remote: Optional[NM.RemoteConnection], error: Optional[str]) -> None:
        self._set_busy(False)
        if error:
            self._show_error(error)
            return
        name = remote.get_id() if remote else self.name_row.get_text()
        if self.connect_after_save and remote is not None:
            self.backend.activate(remote, self.device, None, name,
                                  lambda ok, msg: self.on_finished(msg, ok) if self.on_finished else None)
            message = f"Saved {name}, connecting…"
        else:
            message = f"Saved {name}"
        if self.on_finished:
            self.on_finished(message, True)
        self.close()
