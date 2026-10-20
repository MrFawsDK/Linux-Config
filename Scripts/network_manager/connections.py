# =============================================================================
# connections.py - Saved NetworkManager connection profiles
# =============================================================================
# A "profile" (NM.RemoteConnection) is a saved connection definition owned by
# NetworkManager, stored by NM in /etc/NetworkManager/system-connections.
# This module never touches those files; it only uses NM's D-Bus API, so NM
# validates every change and other tools (nmcli, nm-applet,
# nm-connection-editor) see updates immediately.
#
# Identification is always by UUID. Names are not unique and can be renamed
# at any time, which is what broke V1's "nmcli connection up id <ssid>".
#
# Saving rules (important for preserving e.g. school Wi-Fi profiles):
#   * Edits start from a full clone of the existing profile, so every
#     property the editor does not show is kept as it was.
#   * libnm does not hand secrets to clients unless asked, so a clone has no
#     passwords. When an update arrives *without any secrets*, NM keeps the
#     stored ones. When it contains *some* secrets, NM replaces them all, so
#     before sending a new password we first fetch the existing secrets
#     (fetch_secrets) to avoid wiping unrelated ones (e.g. a private-key
#     password next to an EAP password).
#   * Nothing here runs without an explicit user action in the UI.
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

import gi

gi.require_version("NM", "1.0")
from gi.repository import NM

from .backend import (NetworkBackend, ResultCallback, call_async,
                      connection_type_label, ssid_to_text)


# -----------------------------------------------------------------------------
# Profile categories
# -----------------------------------------------------------------------------

CATEGORY_WIFI = "wifi"
CATEGORY_ETHERNET = "ethernet"
CATEGORY_VPN = "vpn"
CATEGORY_OTHER = "other"

CATEGORY_TITLES = {
    CATEGORY_WIFI: "Wi-Fi",
    CATEGORY_ETHERNET: "Ethernet",
    CATEGORY_VPN: "VPN",
    CATEGORY_OTHER: "Other",
}

CATEGORY_ICONS = {
    CATEGORY_WIFI: "network-wireless-symbolic",
    CATEGORY_ETHERNET: "network-wired-symbolic",
    CATEGORY_VPN: "network-vpn-symbolic",
    CATEGORY_OTHER: "network-workgroup-symbolic",
}


def profile_category(type_name: str) -> str:
    if type_name == NM.SETTING_WIRELESS_SETTING_NAME:
        return CATEGORY_WIFI
    if type_name == NM.SETTING_WIRED_SETTING_NAME:
        return CATEGORY_ETHERNET
    if type_name in (NM.SETTING_VPN_SETTING_NAME, NM.SETTING_WIREGUARD_SETTING_NAME):
        return CATEGORY_VPN
    return CATEGORY_OTHER


# Human-readable names for the EAP methods NM supports.
EAP_LABELS = {
    "peap": "PEAP", "ttls": "TTLS", "tls": "TLS", "pwd": "PWD",
    "fast": "FAST", "leap": "LEAP", "md5": "MD5",
}


def wifi_security_label(connection: NM.Connection) -> str:
    """Describe the security configured in a saved Wi-Fi profile."""
    wsec = connection.get_setting_wireless_security()
    if wsec is None:
        return "Open"
    key_mgmt = wsec.get_key_mgmt()
    if key_mgmt == "wpa-psk":
        return "WPA/WPA2 Personal"
    if key_mgmt == "sae":
        return "WPA3 Personal"
    if key_mgmt == "owe":
        return "Enhanced Open"
    if key_mgmt in ("wpa-eap", "wpa-eap-suite-b-192"):
        s8021x = connection.get_setting_802_1x()
        methods = []
        if s8021x is not None:
            methods = [EAP_LABELS.get(s8021x.get_eap_method(i), s8021x.get_eap_method(i).upper())
                       for i in range(s8021x.get_num_eap_methods())]
        suffix = f" ({', '.join(methods)})" if methods else ""
        return ("WPA3 Enterprise 192-bit" if key_mgmt == "wpa-eap-suite-b-192"
                else "Enterprise") + suffix
    if key_mgmt == "none":
        return "WEP"
    if key_mgmt == "ieee8021x":
        return "Dynamic WEP (802.1X)"
    return key_mgmt or "Unknown"


# -----------------------------------------------------------------------------
# VPN plugin detection
# -----------------------------------------------------------------------------

def vpn_support(connection: NM.Connection) -> tuple[bool, str]:
    """Return (can_activate, description) for a VPN/WireGuard profile.

    Classic NM VPNs (OpenVPN, WireGuard via plugin, OpenConnect, ...) need a
    separate plugin package (networkmanager-openvpn etc.). If the plugin for
    the profile's service type is missing, NM can only fail, so the UI
    disables the button and explains why instead.
    """
    type_name = connection.get_connection_type()
    if type_name == NM.SETTING_WIREGUARD_SETTING_NAME:
        return True, "WireGuard (built into NetworkManager)"
    s_vpn = connection.get_setting_vpn()
    if s_vpn is None:
        return False, "Profile has no VPN settings"
    service = s_vpn.get_service_type() or ""
    short_name = service.rsplit(".", 1)[-1] if service else "unknown"
    plugins = NM.VpnPluginInfo.list_load()
    if NM.VpnPluginInfo.list_find_by_service(plugins, service) is None:
        return False, (f"The NetworkManager '{short_name}' VPN plugin is not installed "
                       f"(service {service}). Install e.g. networkmanager-{short_name}.")
    return True, f"{short_name.capitalize()} VPN"


def installed_vpn_plugins() -> list[str]:
    """Names of installed NM VPN plugins, for the Ethernet & VPN page."""
    return sorted(p.get_name() for p in NM.VpnPluginInfo.list_load())


# -----------------------------------------------------------------------------
# Profile summary
# -----------------------------------------------------------------------------

@dataclass
class SavedProfile:
    """Display data for one saved profile. `remote` is the live NM object."""
    remote: NM.RemoteConnection
    uuid: str
    name: str
    type_name: str
    type_label: str
    category: str
    detail: str              # SSID for Wi-Fi, VPN type for VPN, interface...
    security: str            # Wi-Fi security label, empty otherwise
    autoconnect: bool
    priority: int
    last_used: int           # Unix timestamp, 0 = never
    active_state: Optional[NM.ActiveConnectionState]
    editable: bool           # False for NM-generated/external profiles
    can_activate: bool
    note: str                # Explanation when can_activate is False

    @property
    def is_active(self) -> bool:
        return self.active_state == NM.ActiveConnectionState.ACTIVATED

    @property
    def is_busy(self) -> bool:
        return self.active_state in (NM.ActiveConnectionState.ACTIVATING,
                                     NM.ActiveConnectionState.DEACTIVATING)

    @property
    def last_used_text(self) -> str:
        if not self.last_used:
            return "Never used"
        return "Last used " + datetime.fromtimestamp(self.last_used).strftime("%Y-%m-%d %H:%M")


def summarize_profile(remote: NM.RemoteConnection,
                      backend: NetworkBackend) -> SavedProfile:
    s_con = remote.get_setting_connection()
    type_name = remote.get_connection_type() or ""
    category = profile_category(type_name)

    detail = ""
    security = ""
    can_activate = True
    note = ""
    if category == CATEGORY_WIFI:
        s_wifi = remote.get_setting_wireless()
        detail = ssid_to_text(s_wifi.get_ssid()) if s_wifi else ""
        security = wifi_security_label(remote)
    elif category == CATEGORY_VPN:
        can_activate, detail = vpn_support(remote)
        if not can_activate:
            note = detail
            detail = "Plugin missing"
    # OLD IMPLEMENTATION - disabled in V2 development
    # Reason: showing the interface restriction made profile subtitles too
    # long in the list; it is visible and editable in the connection editor.
    #
    # if s_con and s_con.get_interface_name():
    #     detail = (detail + " · " if detail else "") + f"Interface {s_con.get_interface_name()}"

    # NM-generated (e.g. "lo") and external profiles are created by NM to
    # describe state it did not configure; editing or deleting them makes
    # no sense and NM may recreate them.
    flags = remote.get_flags()
    generated = bool(flags & (NM.SettingsConnectionFlags.NM_GENERATED
                              | NM.SettingsConnectionFlags.EXTERNAL))
    if type_name == NM.SETTING_LOOPBACK_SETTING_NAME:
        can_activate = False
        note = "The loopback interface is managed automatically."

    active = backend.active_for_uuid(remote.get_uuid())
    return SavedProfile(
        remote=remote,
        uuid=remote.get_uuid(),
        name=remote.get_id() or "(unnamed)",
        type_name=type_name,
        type_label=connection_type_label(type_name),
        category=category,
        detail=detail,
        security=security,
        autoconnect=s_con.get_autoconnect() if s_con else False,
        priority=s_con.get_autoconnect_priority() if s_con else 0,
        last_used=s_con.get_timestamp() if s_con else 0,
        active_state=active.get_state() if active else None,
        editable=not generated,
        can_activate=can_activate,
        note=note,
    )


# -----------------------------------------------------------------------------
# Profile operations
# -----------------------------------------------------------------------------

class ProfileManager:
    """List, activate, update, add and delete saved profiles."""

    # Settings that can contain secrets the editor may change.
    SECRET_SETTINGS = (NM.SETTING_WIRELESS_SECURITY_SETTING_NAME,
                       NM.SETTING_802_1X_SETTING_NAME)

    def __init__(self, backend: NetworkBackend) -> None:
        self.backend = backend

    @property
    def client(self) -> NM.Client:
        return self.backend.client

    # -------------------------------------------------------------------------
    # Saved connections: listing
    # -------------------------------------------------------------------------

    def profiles(self) -> list[SavedProfile]:
        """All saved profiles, grouped by category, then name."""
        order = [CATEGORY_WIFI, CATEGORY_ETHERNET, CATEGORY_VPN, CATEGORY_OTHER]
        result = [summarize_profile(r, self.backend) for r in self.client.get_connections()]
        return sorted(result, key=lambda p: (order.index(p.category), p.name.lower()))

    def by_uuid(self, uuid: str) -> Optional[NM.RemoteConnection]:
        return self.client.get_connection_by_uuid(uuid)

    # -------------------------------------------------------------------------
    # Saved connections: activation
    # -------------------------------------------------------------------------

    def activate(self, uuid: str,
                 on_result: Optional[Callable[[bool, str], None]] = None) -> None:
        """Activate a profile by UUID; NM chooses a suitable device."""
        remote = self.by_uuid(uuid)
        if remote is None:
            if on_result:
                on_result(False, "This profile no longer exists.")
            return
        profile = summarize_profile(remote, self.backend)
        if not profile.can_activate:
            self.backend.add_log(f"Cannot connect {profile.name}: {profile.note}", "error")
            if on_result:
                on_result(False, profile.note)
            return
        self.backend.activate(remote, None, None, profile.name, on_result)

    def deactivate(self, uuid: str, callback: Optional[ResultCallback] = None) -> None:
        active = self.backend.active_for_uuid(uuid)
        if active is not None:
            self.backend.deactivate(active, callback)

    # -------------------------------------------------------------------------
    # Saved connections: modification
    # -------------------------------------------------------------------------

    def fetch_secrets(self, remote: NM.RemoteConnection, target: NM.Connection,
                      callback: Callable[[Optional[str]], None]) -> None:
        """Copy the profile's stored secrets into `target` (an editor clone).

        Only needed before an update that changes a password; see the module
        header for why. The secrets stay inside the NM.Connection objects in
        memory and go straight back to NM in the update.
        """
        present = [name for name in self.SECRET_SETTINGS
                   if remote.get_setting_by_name(name) is not None]

        def next_setting(index: int) -> None:
            if index >= len(present):
                callback(None)
                return
            setting_name = present[index]

            def done(secrets, error):
                if error:
                    callback(f"Could not read the saved secrets ({setting_name}): {error}")
                    return
                try:
                    if secrets is not None and secrets.n_children() > 0:
                        target.update_secrets(setting_name, secrets)
                except Exception as exc:  # GLib.Error from update_secrets
                    callback(f"Could not merge saved secrets: {exc}")
                    return
                next_setting(index + 1)

            call_async(lambda cb: remote.get_secrets_async(setting_name, None, cb),
                       "get_secrets_finish", done)

        next_setting(0)

    def update(self, remote: NM.RemoteConnection, connection: NM.Connection,
               callback: ResultCallback) -> None:
        """Replace a saved profile's settings and write it to disk.

        Update2 with TO_DISK is the same call nm-connection-editor uses; NM
        validates the settings and rejects invalid ones with an error.
        """
        name = connection.get_id() or remote.get_id()

        def done(result, error):
            if error:
                self.backend.add_log(f"Could not save {name}: {error}", "error")
            else:
                self.backend.add_log(f"Saved changes to {name}", "success")
            callback(result, error)

        settings = connection.to_dbus(NM.ConnectionSerializationFlags.ALL)
        call_async(lambda cb: remote.update2(settings, NM.SettingsUpdate2Flags.TO_DISK,
                                             None, None, cb),
                   "update2_finish", done)

    def add(self, connection: NM.Connection, callback: ResultCallback) -> None:
        """Save a brand-new profile to disk. Result: the new RemoteConnection."""
        name = connection.get_id()

        def done(result, error):
            if error:
                self.backend.add_log(f"Could not create {name}: {error}", "error")
                callback(None, error)
                return
            # add_connection2_finish returns (remote, out_result).
            remote = result[0] if isinstance(result, tuple) else result
            self.backend.add_log(f"Created profile {name}", "success")
            callback(remote, None)

        settings = connection.to_dbus(NM.ConnectionSerializationFlags.ALL)
        call_async(lambda cb: self.client.add_connection2(
                       settings, NM.SettingsAddConnection2Flags.TO_DISK, None, True, None, cb),
                   "add_connection2_finish", done)

    def delete(self, uuid: str, callback: ResultCallback) -> None:
        """Permanently delete a profile. The UI asks for confirmation first."""
        remote = self.by_uuid(uuid)
        if remote is None:
            callback(None, "This profile no longer exists.")
            return
        name = remote.get_id()

        def done(result, error):
            if error:
                self.backend.add_log(f"Could not delete {name}: {error}", "error")
            else:
                self.backend.add_log(f"Deleted profile {name}", "success")
            callback(result, error)

        call_async(lambda cb: remote.delete_async(None, cb), "delete_finish", done)
