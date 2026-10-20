# =============================================================================
# wifi.py - Wi-Fi scanning, network grouping and quick-connect
# =============================================================================
# NetworkManager reports individual access points (APs). One network name
# (SSID) is often served by several APs (mesh nodes, 2.4 + 5 GHz radios), and
# occasionally two *different* networks share an SSID with different
# security (e.g. an open guest network and a WPA network both called "Cafe").
#
# V1 merged everything by SSID text, which hid the second case. V2 groups APs
# by (raw SSID bytes, security type): identical networks collapse into one
# row showing the strongest signal, while same-name networks with different
# security stay separate and are labelled by their security type.
#
# Connecting:
#   * If a saved profile matches the AP (checked with NM's own
#     AccessPoint.connection_valid, so SSID/BSSID/security must all fit),
#     that profile is activated by its UUID-backed RemoteConnection.
#   * Otherwise a new profile is created with AddAndActivateConnection.
#     A password typed by the user travels to NetworkManager inside the
#     D-Bus message, never on a command line, and is never logged.
#   * Enterprise (802.1X) networks need more information than a password,
#     so the UI opens the full connection editor for them instead.
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

import gi

gi.require_version("NM", "1.0")
from gi.repository import GLib, NM

from .backend import NetworkBackend, boottime_ms, call_async, ssid_to_text


# -----------------------------------------------------------------------------
# Security classification
# -----------------------------------------------------------------------------

class Security(Enum):
    """Security types as far as the UI needs to distinguish them."""
    OPEN = "open"
    OWE = "owe"                      # "Enhanced Open": encrypted, no password
    WEP = "wep"                      # obsolete, see quick-connect note below
    WPA_PERSONAL = "wpa-psk"         # WPA/WPA2 with a passphrase
    WPA2_WPA3_PERSONAL = "wpa2-wpa3" # transition mode: both PSK and SAE offered
    WPA3_PERSONAL = "sae"            # WPA3 only
    ENTERPRISE = "enterprise"        # 802.1X / EAP (school & work networks)

    @property
    def label(self) -> str:
        return SECURITY_LABELS[self]

    @property
    def needs_password(self) -> bool:
        return self in (Security.WPA_PERSONAL, Security.WPA2_WPA3_PERSONAL,
                        Security.WPA3_PERSONAL)

    @property
    def is_secure(self) -> bool:
        return self is not Security.OPEN


SECURITY_LABELS = {
    Security.OPEN: "Open",
    Security.OWE: "Enhanced Open",
    Security.WEP: "WEP (insecure)",
    Security.WPA_PERSONAL: "WPA/WPA2 Personal",
    Security.WPA2_WPA3_PERSONAL: "WPA2/WPA3 Personal",
    Security.WPA3_PERSONAL: "WPA3 Personal",
    Security.ENTERPRISE: "Enterprise (802.1X)",
}

# Short aliases for the AP flag enums (their real names start with a digit,
# which Python cannot spell as attribute access: NM.80211ApSecurityFlags).
_ApFlags = getattr(NM, "80211ApFlags")
_ApSec = getattr(NM, "80211ApSecurityFlags")


def classify_access_point(ap: NM.AccessPoint) -> Security:
    """Derive the security type from an AP's beacon flags.

    WPA1 flags (get_wpa_flags) and RSN/WPA2+ flags (get_rsn_flags) both list
    the offered key-management suites. The order of checks matters: an AP
    offering 802.1X is enterprise even if it also lists PSK, and an AP
    offering both PSK and SAE is in WPA2/WPA3 transition mode.
    """
    wpa = ap.get_wpa_flags()
    rsn = ap.get_rsn_flags()
    both = wpa | rsn

    if both & (_ApSec.KEY_MGMT_802_1X | _ApSec.KEY_MGMT_EAP_SUITE_B_192):
        return Security.ENTERPRISE
    if rsn & _ApSec.KEY_MGMT_SAE:
        if both & _ApSec.KEY_MGMT_PSK:
            return Security.WPA2_WPA3_PERSONAL
        return Security.WPA3_PERSONAL
    if both & _ApSec.KEY_MGMT_PSK:
        return Security.WPA_PERSONAL
    if rsn & (_ApSec.KEY_MGMT_OWE | _ApSec.KEY_MGMT_OWE_TM):
        return Security.OWE
    if ap.get_flags() & _ApFlags.PRIVACY:
        # Privacy bit without WPA/RSN information means static WEP.
        return Security.WEP
    return Security.OPEN


def band_label(frequency_mhz: int) -> str:
    if 2400 <= frequency_mhz < 2500:
        return "2.4 GHz"
    if 4900 <= frequency_mhz < 5925:
        return "5 GHz"
    if 5925 <= frequency_mhz <= 7125:
        return "6 GHz"
    return f"{frequency_mhz} MHz"


def signal_level(strength: int) -> str:
    """Bucket a 0-100 signal into the names used by the Adwaita icons."""
    if strength >= 80:
        return "excellent"
    if strength >= 55:
        return "good"
    if strength >= 30:
        return "ok"
    if strength > 5:
        return "weak"
    return "none"


def signal_icon_name(strength: int) -> str:
    return f"network-wireless-signal-{signal_level(strength)}-symbolic"


def validate_wifi_password(security: Security, password: str) -> Optional[str]:
    """Return an error message if the password cannot work, else None.

    Checking locally gives a clear message instead of NM's generic
    "802-11-wireless-security.psk: property is invalid".
    """
    if security in (Security.WPA_PERSONAL, Security.WPA2_WPA3_PERSONAL):
        # NM.utils_wpa_psk_valid: 8-63 printable characters or 64 hex digits.
        if not NM.utils_wpa_psk_valid(password):
            return "WPA passwords must be 8–63 characters (or 64 hexadecimal digits)."
    elif security is Security.WPA3_PERSONAL:
        if not password:
            return "Enter the network password."
    return None


# -----------------------------------------------------------------------------
# Scan results
# -----------------------------------------------------------------------------

@dataclass
class WifiNetwork:
    """One row on the Wi-Fi page: all APs sharing an SSID + security type."""
    ssid_bytes: bytes
    ssid: str
    security: Security
    best_ap: NM.AccessPoint
    strength: int
    bands: set[str] = field(default_factory=set)
    ap_count: int = 0
    active: bool = False
    # Saved profiles that NM considers valid for this network, best first.
    saved: list[NM.RemoteConnection] = field(default_factory=list)

    @property
    def key(self) -> tuple[bytes, Security]:
        return (self.ssid_bytes, self.security)

    @property
    def is_saved(self) -> bool:
        return bool(self.saved)

    @property
    def preferred_profile(self) -> Optional[NM.RemoteConnection]:
        return self.saved[0] if self.saved else None


def _profile_rank(connection: NM.RemoteConnection) -> tuple[int, int]:
    """Sort key so the profile NM itself would prefer comes first:
    highest autoconnect priority, then most recently used."""
    setting = connection.get_setting_connection()
    if setting is None:
        return (0, 0)
    return (-setting.get_autoconnect_priority(), -setting.get_timestamp())


@dataclass
class ScanResult:
    networks: list[WifiNetwork]
    hidden_ap_count: int        # APs that broadcast no SSID
    last_scan_age_s: Optional[int]


def collect_networks(device: NM.DeviceWifi,
                     saved_connections: list[NM.RemoteConnection]) -> ScanResult:
    """Group the device's current AP list into WifiNetwork rows."""
    wifi_profiles = [c for c in saved_connections
                     if c.get_connection_type() == NM.SETTING_WIRELESS_SETTING_NAME]
    active_ap = device.get_active_access_point()
    active_path = active_ap.get_path() if active_ap else None

    groups: dict[tuple[bytes, Security], WifiNetwork] = {}
    hidden = 0

    for ap in device.get_access_points():
        ssid = ap.get_ssid()
        raw = bytes(ssid.get_data()) if ssid is not None else b""
        if not raw or raw.strip(b"\x00") == b"":
            # Hidden networks broadcast an empty (or all-zero) SSID. They
            # cannot be shown by name; the UI offers "Connect to Hidden Network".
            hidden += 1
            continue

        security = classify_access_point(ap)
        key = (raw, security)
        strength = ap.get_strength()
        network = groups.get(key)
        if network is None:
            network = WifiNetwork(ssid_bytes=raw, ssid=ssid_to_text(ssid),
                                  security=security, best_ap=ap, strength=strength)
            groups[key] = network
        elif strength > network.strength:
            network.best_ap = ap
            network.strength = strength

        network.ap_count += 1
        network.bands.add(band_label(ap.get_frequency()))
        if ap.get_path() == active_path:
            network.active = True
            # Show the AP we are actually associated with, not merely the
            # strongest one, so the signal shown matches the connection.
            network.best_ap = ap
            network.strength = strength

    # Match saved profiles once per network using NM's own validation logic.
    for network in groups.values():
        network.saved = sorted(
            (c for c in wifi_profiles if network.best_ap.connection_valid(c)),
            key=_profile_rank)

    networks = sorted(groups.values(),
                      key=lambda n: (not n.active, -n.strength, n.ssid.lower()))

    last_scan = device.get_last_scan()
    age = None
    if last_scan and last_scan > 0:
        age = max(0, (boottime_ms() - last_scan) // 1000)
    return ScanResult(networks, hidden, age)


# -----------------------------------------------------------------------------
# Wi-Fi operations
# -----------------------------------------------------------------------------

class WifiManager:
    """Scan and quick-connect operations for one backend."""

    # How long to wait for NM to publish new scan results after a scan
    # request was accepted. Scans normally take 2-5 seconds.
    SCAN_TIMEOUT_S = 15

    def __init__(self, backend: NetworkBackend) -> None:
        self.backend = backend
        self.scanning = False

    @property
    def client(self) -> NM.Client:
        return self.backend.client

    # -------------------------------------------------------------------------
    # Wi-Fi scanning
    # -------------------------------------------------------------------------

    def request_scan(self, device: NM.DeviceWifi,
                     on_done: Callable[[Optional[str]], None]) -> None:
        """Ask NM for a fresh scan and call on_done(error) when it finished.

        RequestScan returns as soon as the scan has *started*. The results
        are published later by updating the device's 'last-scan' property,
        so we wait for that (with a timeout) before reporting completion.
        NM refuses scans that come too quickly after the previous one; that
        error is passed on as a message rather than treated as fatal.
        """
        if self.scanning:
            return
        self.scanning = True
        previous_scan = device.get_last_scan()
        handler_id = 0
        timeout_id = 0

        def finish(error: Optional[str]) -> None:
            nonlocal handler_id, timeout_id
            if not self.scanning:
                return
            self.scanning = False
            if handler_id:
                # handler_disconnect, NOT device.disconnect(): NM.Device has
                # its own disconnect() method that disconnects the network
                # device itself, shadowing GObject's signal disconnect.
                device.handler_disconnect(handler_id)
                handler_id = 0
            if timeout_id:
                GLib.source_remove(timeout_id)
                timeout_id = 0
            if error:
                self.backend.add_log(f"Wi-Fi scan: {error}", "error")
            on_done(error)

        def on_last_scan(_device, _pspec) -> None:
            if device.get_last_scan() != previous_scan:
                finish(None)

        def on_timeout() -> bool:
            nonlocal timeout_id
            timeout_id = 0
            # Not an error: NM may simply have found nothing new.
            finish(None)
            return GLib.SOURCE_REMOVE

        def started(_result, error) -> None:
            nonlocal handler_id, timeout_id
            if error:
                finish(error)
                return
            handler_id = device.connect("notify::last-scan", on_last_scan)
            timeout_id = GLib.timeout_add_seconds(self.SCAN_TIMEOUT_S, on_timeout)

        call_async(lambda cb: device.request_scan_async(None, cb),
                   "request_scan_finish", started)

    # -------------------------------------------------------------------------
    # Connecting
    # -------------------------------------------------------------------------

    def connect_saved(self, network: WifiNetwork, device: NM.DeviceWifi,
                      on_result: Optional[Callable[[bool, str], None]] = None) -> None:
        """Activate the best matching saved profile for a visible network.

        Passing the AP path as 'specific object' makes NM associate with that
        exact AP. If the profile's secrets are missing or wrong, NM asks the
        session's secret agent (nm-applet) for them.
        """
        profile = network.preferred_profile
        if profile is None:
            return
        self.backend.activate(profile, device, network.best_ap.get_path(),
                              profile.get_id() or network.ssid, on_result)

    def connect_new(self, network: WifiNetwork, device: NM.DeviceWifi,
                    password: Optional[str],
                    on_result: Optional[Callable[[bool, str, Optional[str]], None]] = None) -> None:
        """Create and activate a new profile for an unsaved network.

        on_result(ok, message, new_profile_uuid) - the UUID is passed so the
        UI can offer to forget the new profile if the connection failed
        (NM keeps profiles created by AddAndActivate even on failure).
        """
        security = network.security
        partial = NM.SimpleConnection.new()

        if security in (Security.WPA_PERSONAL, Security.WPA2_WPA3_PERSONAL,
                        Security.WPA3_PERSONAL):
            wsec = NM.SettingWirelessSecurity.new()
            # Transition networks accept classic WPA2-PSK, which works with
            # every driver; WPA3-only networks require SAE.
            key_mgmt = "sae" if security is Security.WPA3_PERSONAL else "wpa-psk"
            wsec.set_property(NM.SETTING_WIRELESS_SECURITY_KEY_MGMT, key_mgmt)
            # The secret lives only inside this NM.Setting object and the
            # D-Bus message built from it.
            wsec.set_property(NM.SETTING_WIRELESS_SECURITY_PSK, password or "")
            partial.add_setting(wsec)
        elif security is Security.OWE:
            wsec = NM.SettingWirelessSecurity.new()
            wsec.set_property(NM.SETTING_WIRELESS_SECURITY_KEY_MGMT, "owe")
            partial.add_setting(wsec)
        elif security is not Security.OPEN:
            # WEP and enterprise are handled elsewhere (see module notes).
            raise ValueError(f"Quick-connect does not support {security.label}")

        # NM fills in SSID, mode, IP settings and a connection name from the
        # AP when it completes the partial profile.
        name = network.ssid
        self.backend.add_log(f"Creating new profile for {name}…")

        def started(result, error):
            if error:
                message = f"Could not connect to {name}: {error}"
                self.backend.add_log(message, "error")
                if on_result:
                    on_result(False, message, None)
                return
            active = result
            new_uuid = active.get_uuid()
            self.backend.watch_activation(
                active, name,
                lambda ok, message: on_result(ok, message, new_uuid) if on_result else None)

        call_async(
            lambda cb: self.client.add_and_activate_connection_async(
                partial if partial.get_settings() else None,
                device, network.best_ap.get_path(), None, cb),
            "add_and_activate_connection_finish", started)

    def disconnect(self, device: NM.DeviceWifi,
                   callback=None) -> None:
        """Disconnect the device's current Wi-Fi connection."""
        active = device.get_active_connection()
        if active is not None:
            self.backend.deactivate(active, callback)
