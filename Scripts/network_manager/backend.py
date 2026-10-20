# =============================================================================
# backend.py - NetworkManager client wrapper
# =============================================================================
# Everything that talks to NetworkManager goes through libnm (gi NM 1.0),
# NetworkManager's official client library. libnm keeps a cached mirror of
# the daemon's D-Bus objects (devices, access points, profiles, active
# connections) that updates itself on the GLib main loop.
#
# Design notes:
#   * All calls that change state use the *_async variants and complete on
#     the GTK main loop. No worker threads are needed and the UI never
#     blocks. (NM.Client is not thread-safe, which is one reason V1's
#     thread-based nmcli approach was replaced.)
#   * Authorisation is handled by NetworkManager itself through PolicyKit.
#     If an action needs authentication, NM asks the session's polkit agent.
#     Secrets for existing profiles come from NM or the session's secret
#     agent (nm-applet), so this app never has to store passwords itself.
#   * The activity log only ever contains messages composed by this app plus
#     NetworkManager's error text. Passwords are never logged.
# =============================================================================

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import gi

gi.require_version("NM", "1.0")
from gi.repository import Gio, GLib, GObject, NM


# -----------------------------------------------------------------------------
# Error handling helpers
# -----------------------------------------------------------------------------

# Errors coming back over D-Bus look like
#   "GDBus.Error:org.freedesktop.NetworkManager.Device.NotAllowed: Scanning not allowed..."
# The D-Bus error name means little to a user, so strip it off.
_DBUS_ERROR_PREFIX = re.compile(r"^GDBus\.Error:[\w.\-]+:\s*")


def friendly_error(error: object) -> str:
    """Turn a GLib.Error (or any exception) into a short, readable message."""
    message = getattr(error, "message", None) or str(error)
    message = _DBUS_ERROR_PREFIX.sub("", message).strip()
    if not message:
        return "Unknown error"
    return message[0].upper() + message[1:]


# Callback type used throughout the backend: callback(result, error_message).
# Exactly one of the two is meaningful: error_message is None on success.
ResultCallback = Callable[[object, Optional[str]], None]


def call_async(start: Callable[[Callable], None], finish_name: str,
               callback: Optional[ResultCallback]) -> None:
    """Run a libnm async/finish pair and report the result to callback.

    `start` receives the GAsyncReadyCallback and must start the operation,
    e.g. `lambda cb: client.activate_connection_async(c, d, None, None, cb)`.
    `finish_name` is the name of the matching *_finish method on the source
    object. This wrapper keeps the try/except for GLib.Error in one place.
    """
    def on_ready(source, result, *_unused):
        try:
            value = getattr(source, finish_name)(result)
        except GLib.Error as error:
            if callback:
                callback(None, friendly_error(error))
            return
        if callback:
            callback(value, None)

    start(on_ready)


# -----------------------------------------------------------------------------
# Human-readable labels for NetworkManager enums
# -----------------------------------------------------------------------------

CONNECTION_TYPE_LABELS = {
    NM.SETTING_WIRELESS_SETTING_NAME: "Wi-Fi",
    NM.SETTING_WIRED_SETTING_NAME: "Ethernet",
    NM.SETTING_VPN_SETTING_NAME: "VPN",
    NM.SETTING_WIREGUARD_SETTING_NAME: "WireGuard",
    NM.SETTING_LOOPBACK_SETTING_NAME: "Loopback",
    "bridge": "Bridge",
    "bond": "Bond",
    "vlan": "VLAN",
    "gsm": "Mobile broadband",
    "cdma": "Mobile broadband",
    "bluetooth": "Bluetooth",
    "tun": "TUN/TAP",
}

DEVICE_TYPE_LABELS = {
    NM.DeviceType.ETHERNET: "Ethernet",
    NM.DeviceType.WIFI: "Wi-Fi",
    NM.DeviceType.WIFI_P2P: "Wi-Fi P2P",
    NM.DeviceType.LOOPBACK: "Loopback",
    NM.DeviceType.BRIDGE: "Bridge",
    NM.DeviceType.BOND: "Bond",
    NM.DeviceType.VLAN: "VLAN",
    NM.DeviceType.TUN: "TUN/TAP",
    NM.DeviceType.WIREGUARD: "WireGuard",
    NM.DeviceType.MODEM: "Modem",
    NM.DeviceType.BT: "Bluetooth",
}

DEVICE_STATE_LABELS = {
    NM.DeviceState.UNKNOWN: "Unknown",
    NM.DeviceState.UNMANAGED: "Unmanaged",
    NM.DeviceState.UNAVAILABLE: "Unavailable",
    NM.DeviceState.DISCONNECTED: "Disconnected",
    NM.DeviceState.PREPARE: "Preparing",
    NM.DeviceState.CONFIG: "Configuring",
    NM.DeviceState.NEED_AUTH: "Waiting for authentication",
    NM.DeviceState.IP_CONFIG: "Getting IP address",
    NM.DeviceState.IP_CHECK: "Checking connectivity",
    NM.DeviceState.SECONDARIES: "Starting secondary connections",
    NM.DeviceState.ACTIVATED: "Connected",
    NM.DeviceState.DEACTIVATING: "Disconnecting",
    NM.DeviceState.FAILED: "Failed",
}

NM_STATE_LABELS = {
    NM.State.UNKNOWN: "Unknown",
    NM.State.ASLEEP: "Asleep (networking disabled)",
    NM.State.DISCONNECTED: "Disconnected",
    NM.State.DISCONNECTING: "Disconnecting",
    NM.State.CONNECTING: "Connecting",
    NM.State.CONNECTED_LOCAL: "Connected (local only)",
    NM.State.CONNECTED_SITE: "Connected (site only)",
    NM.State.CONNECTED_GLOBAL: "Connected",
}

CONNECTIVITY_LABELS = {
    NM.ConnectivityState.UNKNOWN: "Unknown",
    NM.ConnectivityState.NONE: "No network",
    NM.ConnectivityState.PORTAL: "Captive portal (login required)",
    NM.ConnectivityState.LIMITED: "Limited (no internet)",
    NM.ConnectivityState.FULL: "Full internet access",
}

ACTIVE_STATE_LABELS = {
    NM.ActiveConnectionState.UNKNOWN: "Unknown",
    NM.ActiveConnectionState.ACTIVATING: "Connecting",
    NM.ActiveConnectionState.ACTIVATED: "Connected",
    NM.ActiveConnectionState.DEACTIVATING: "Disconnecting",
    NM.ActiveConnectionState.DEACTIVATED: "Disconnected",
}


def connection_type_label(type_name: Optional[str]) -> str:
    """'802-11-wireless' -> 'Wi-Fi', unknown types are shown as-is."""
    if not type_name:
        return "Unknown"
    return CONNECTION_TYPE_LABELS.get(type_name, type_name)


def device_type_label(device: NM.Device) -> str:
    device_type = device.get_device_type()
    return DEVICE_TYPE_LABELS.get(device_type, device_type.value_nick)


def device_state_label(device: NM.Device) -> str:
    return DEVICE_STATE_LABELS.get(device.get_state(), "Unknown")


def enum_reason_text(enum_value) -> str:
    """Make an NM reason enum readable: 'no-secrets' -> 'No secrets'."""
    nick = getattr(enum_value, "value_nick", str(enum_value))
    text = nick.replace("-", " ").strip()
    return text[:1].upper() + text[1:]


def ssid_to_text(ssid: Optional[GLib.Bytes]) -> str:
    """Convert an SSID (raw bytes, not necessarily UTF-8) to display text.

    NM.utils_ssid_to_utf8 tries the user's locale encodings and falls back
    to a safe representation, which is what nm-applet uses too.
    """
    if ssid is None:
        return ""
    data = ssid.get_data()
    if not data:
        return ""
    return NM.utils_ssid_to_utf8(data)


def boottime_ms() -> int:
    """Milliseconds on CLOCK_BOOTTIME, the clock NM uses for 'last-scan'."""
    return int(time.clock_gettime(time.CLOCK_BOOTTIME) * 1000)


# -----------------------------------------------------------------------------
# IP configuration snapshot
# -----------------------------------------------------------------------------

@dataclass
class IPDetails:
    """Addresses, gateway and DNS for one IP family of an active connection."""
    addresses: list[str] = field(default_factory=list)   # "10.0.0.5/24"
    gateway: str = ""
    dns: list[str] = field(default_factory=list)


def read_ip_config(config: Optional[NM.IPConfig]) -> IPDetails:
    """Extract the useful fields from an NM.IPConfig (may be None)."""
    details = IPDetails()
    if config is None:
        return details
    details.addresses = [f"{a.get_address()}/{a.get_prefix()}"
                         for a in config.get_addresses()]
    details.gateway = config.get_gateway() or ""
    details.dns = list(config.get_nameservers() or [])
    return details


# -----------------------------------------------------------------------------
# Status snapshots used by the UI
# -----------------------------------------------------------------------------

@dataclass
class ActiveDetails:
    """Everything the Overview page shows about the current connection."""
    active: NM.ActiveConnection
    name: str
    uuid: str
    type_name: str
    type_label: str
    state: NM.ActiveConnectionState
    state_label: str
    device: Optional[NM.Device]
    interface: str
    ssid: str
    signal: Optional[int]
    ipv4: IPDetails
    ipv6: IPDetails


@dataclass
class DeviceDetails:
    """Adapter information for the Ethernet and Diagnostics pages."""
    device: NM.Device
    interface: str
    type_label: str
    state_label: str
    driver: str
    hw_address: str
    mtu: int
    managed: bool
    connection_name: str
    extra: dict[str, str]
    ipv4: IPDetails
    ipv6: IPDetails


@dataclass
class LogEntry:
    """One line in the in-app activity log (never contains secrets)."""
    timestamp: float
    message: str
    kind: str  # "info", "success" or "error"

    @property
    def time_text(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.timestamp))


# -----------------------------------------------------------------------------
# The backend object
# -----------------------------------------------------------------------------

class NetworkBackend(GObject.Object):
    """Owns the NM.Client and turns its many signals into one 'changed'.

    Pages subscribe to `changed` and re-read whatever they display. libnm
    can fire dozens of property notifications for a single event (e.g. a
    connection activating touches device state, IP config, active
    connections...), so those are coalesced with a short timeout to avoid
    rebuilding the UI many times per second.
    """

    __gsignals__ = {
        # Something in NetworkManager's state changed; re-read and redraw.
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # A new entry was added to the activity log.
        "log-added": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    # Delay used to coalesce bursts of NM notifications into one 'changed'.
    CHANGE_DEBOUNCE_MS = 150
    # Keep the in-memory activity log bounded.
    MAX_LOG_ENTRIES = 200

    def __init__(self) -> None:
        super().__init__()
        # NM.Client.new() connects to the system bus and loads the initial
        # object cache synchronously. That takes a few milliseconds and only
        # happens once at startup. If NetworkManager is not running, the
        # client is still created and get_nm_running() returns False; it
        # reconnects by itself when the daemon starts.
        self.client: NM.Client = NM.Client.new(None)
        self.log: list[LogEntry] = []
        self._change_source_id = 0
        # Objects we already attached signal handlers to, keyed by D-Bus
        # path, so a device/profile is never connected twice.
        self._watched_paths: set[str] = set()
        self._connect_client_signals()

    # -------------------------------------------------------------------------
    # Change notification plumbing
    # -------------------------------------------------------------------------

    def _connect_client_signals(self) -> None:
        client = self.client
        # Generic property notifications on the client cover state,
        # connectivity, primary-connection, wireless-enabled, nm-running...
        client.connect("notify", self._on_anything_changed)
        for signal_name in ("device-added", "device-removed",
                            "connection-added", "connection-removed",
                            "active-connection-added",
                            "active-connection-removed"):
            client.connect(signal_name, self._on_object_added_or_removed)

        for device in client.get_devices():
            self._watch_device(device)
        for connection in client.get_connections():
            self._watch_connection(connection)
        for active in client.get_active_connections():
            self._watch_active(active)

    def _on_object_added_or_removed(self, _client, obj) -> None:
        # Hook up signals on newly appeared objects, then refresh.
        if isinstance(obj, NM.Device):
            self._watch_device(obj)
        elif isinstance(obj, NM.RemoteConnection):
            self._watch_connection(obj)
        elif isinstance(obj, NM.ActiveConnection):
            self._watch_active(obj)
        self.queue_changed()

    def _remember(self, obj) -> bool:
        """Return True the first time an object path is seen."""
        path = obj.get_path()
        if path in self._watched_paths:
            return False
        self._watched_paths.add(path)
        return True

    def _watch_device(self, device: NM.Device) -> None:
        if not self._remember(device):
            return
        # Device state, IP configs and active connection are all properties.
        device.connect("notify", self._on_anything_changed)
        if isinstance(device, NM.DeviceWifi):
            # The scan list changes through these signals, not via notify.
            device.connect("access-point-added", self._on_anything_changed)
            device.connect("access-point-removed", self._on_anything_changed)

    def _watch_connection(self, connection: NM.RemoteConnection) -> None:
        if not self._remember(connection):
            return
        # Emitted when a saved profile is modified (by us or another tool).
        connection.connect("changed", self._on_anything_changed)

    def _watch_active(self, active: NM.ActiveConnection) -> None:
        if not self._remember(active):
            return
        active.connect("notify", self._on_anything_changed)

    def _on_anything_changed(self, *_args) -> None:
        self.queue_changed()

    def queue_changed(self) -> None:
        """Schedule one 'changed' emission (coalescing repeated requests)."""
        if self._change_source_id:
            return
        self._change_source_id = GLib.timeout_add(
            self.CHANGE_DEBOUNCE_MS, self._emit_changed)

    def _emit_changed(self) -> bool:
        self._change_source_id = 0
        self.emit("changed")
        return GLib.SOURCE_REMOVE

    # -------------------------------------------------------------------------
    # Activity log
    # -------------------------------------------------------------------------

    def add_log(self, message: str, kind: str = "info") -> LogEntry:
        """Record an operation result for the Diagnostics page.

        Callers must only pass messages they composed themselves or NM error
        text. Never pass passwords or other secrets here.
        """
        entry = LogEntry(time.time(), message, kind)
        self.log.append(entry)
        del self.log[:-self.MAX_LOG_ENTRIES]
        self.emit("log-added", entry)
        return entry

    def clear_log(self) -> None:
        self.log.clear()
        self.emit("log-added", None)

    # -------------------------------------------------------------------------
    # Daemon / global state
    # -------------------------------------------------------------------------

    @property
    def nm_running(self) -> bool:
        return self.client.get_nm_running()

    @property
    def nm_version(self) -> str:
        return self.client.get_version() or "unknown"

    def state_label(self) -> str:
        return NM_STATE_LABELS.get(self.client.get_state(), "Unknown")

    def connectivity_label(self) -> str:
        return CONNECTIVITY_LABELS.get(self.client.get_connectivity(), "Unknown")

    def wifi_enabled(self) -> bool:
        """Software radio switch (what the toggle in the UI controls)."""
        return self.client.wireless_get_enabled()

    def wifi_hardware_enabled(self) -> bool:
        """Hardware/rfkill switch; if False the software toggle does nothing."""
        return self.client.wireless_hardware_get_enabled()

    def set_wifi_enabled(self, enabled: bool,
                         callback: Optional[ResultCallback] = None) -> None:
        """Turn the Wi-Fi radio on/off via NM's WirelessEnabled property.

        Writing the D-Bus property is the modern replacement for the
        deprecated NM.Client.wireless_set_enabled(). NM checks the
        'enable-disable-wifi' PolicyKit permission.
        """
        def done(_result, error):
            if error:
                self.add_log(f"Could not turn Wi-Fi {'on' if enabled else 'off'}: {error}", "error")
            else:
                self.add_log(f"Wi-Fi turned {'on' if enabled else 'off'}", "success")
            if callback:
                callback(None, error)

        call_async(
            lambda cb: self.client.dbus_set_property(
                NM.DBUS_PATH, NM.DBUS_INTERFACE, "WirelessEnabled",
                GLib.Variant("b", enabled), -1, None, cb),
            "dbus_set_property_finish", done)

    # -------------------------------------------------------------------------
    # Devices
    # -------------------------------------------------------------------------

    def devices(self, include_internal: bool = True) -> list[NM.Device]:
        """All devices, optionally without loopback and Wi-Fi P2P helpers."""
        result = []
        for device in self.client.get_devices():
            if not include_internal and device.get_device_type() in (
                    NM.DeviceType.LOOPBACK, NM.DeviceType.WIFI_P2P):
                continue
            result.append(device)
        return sorted(result, key=lambda d: d.get_iface() or "")

    def wifi_devices(self) -> list[NM.DeviceWifi]:
        return [d for d in self.devices() if isinstance(d, NM.DeviceWifi)]

    def ethernet_devices(self) -> list[NM.DeviceEthernet]:
        # NM.DeviceVeth subclasses DeviceEthernet; virtual veth pairs are not
        # physical adapters, so only plain Ethernet device types are listed.
        return [d for d in self.devices()
                if d.get_device_type() == NM.DeviceType.ETHERNET]

    def primary_wifi_device(self) -> Optional[NM.DeviceWifi]:
        """The Wi-Fi adapter the Wi-Fi page works with.

        Prefers a managed adapter; most laptops have exactly one.
        """
        devices = self.wifi_devices()
        managed = [d for d in devices if d.get_managed()]
        return (managed or devices or [None])[0]

    def describe_device(self, device: NM.Device) -> DeviceDetails:
        """Collect adapter details for display."""
        extra: dict[str, str] = {}
        if isinstance(device, NM.DeviceEthernet):
            extra["Cable"] = "Plugged in" if device.get_carrier() else "Unplugged"
            speed = device.get_speed()
            if speed:
                extra["Link speed"] = f"{speed} Mb/s"
            permanent = device.get_permanent_hw_address()
            if permanent and permanent != device.get_hw_address():
                extra["Permanent MAC"] = permanent
        elif isinstance(device, NM.DeviceWifi):
            bitrate = device.get_bitrate()  # kbit/s
            if bitrate:
                extra["Bit rate"] = f"{bitrate // 1000} Mb/s"
            ap = device.get_active_access_point()
            if ap is not None:
                extra["Network"] = ssid_to_text(ap.get_ssid())
                extra["Signal"] = f"{ap.get_strength()}%"
                extra["Frequency"] = f"{ap.get_frequency()} MHz"
                extra["BSSID"] = ap.get_bssid() or ""
        vendor = device.get_vendor()
        product = device.get_product()
        if vendor or product:
            extra["Hardware"] = " ".join(p for p in (vendor, product) if p)

        active = device.get_active_connection()
        return DeviceDetails(
            device=device,
            interface=device.get_iface() or "?",
            type_label=device_type_label(device),
            state_label=device_state_label(device),
            driver=device.get_driver() or "",
            hw_address=device.get_hw_address() or "",
            mtu=device.get_mtu(),
            managed=device.get_managed(),
            connection_name=active.get_id() if active else "",
            extra=extra,
            ipv4=read_ip_config(device.get_ip4_config()),
            ipv6=read_ip_config(device.get_ip6_config()),
        )

    # -------------------------------------------------------------------------
    # Active connections
    # -------------------------------------------------------------------------

    def active_connections(self) -> list[NM.ActiveConnection]:
        """Active connections excluding the always-on loopback profile."""
        return [a for a in self.client.get_active_connections()
                if a.get_connection_type() != NM.SETTING_LOOPBACK_SETTING_NAME]

    def active_for_uuid(self, uuid: str) -> Optional[NM.ActiveConnection]:
        for active in self.client.get_active_connections():
            if active.get_uuid() == uuid:
                return active
        return None

    def current_connection(self) -> Optional[NM.ActiveConnection]:
        """The connection to feature on the Overview page.

        NM's 'primary connection' is the one holding the default route. If
        there is none (e.g. still connecting), fall back to any active,
        non-loopback connection so 'Connecting...' is visible.
        """
        primary = self.client.get_primary_connection()
        if primary is not None and primary.get_connection_type() != NM.SETTING_LOOPBACK_SETTING_NAME:
            return primary
        candidates = self.active_connections()
        return candidates[0] if candidates else None

    def describe_active(self, active: NM.ActiveConnection) -> ActiveDetails:
        devices = active.get_devices()
        device = devices[0] if devices else None
        ssid = ""
        signal = None
        if isinstance(device, NM.DeviceWifi):
            ap = device.get_active_access_point()
            if ap is not None:
                ssid = ssid_to_text(ap.get_ssid())
                signal = ap.get_strength()
        type_name = active.get_connection_type() or ""
        state = active.get_state()
        return ActiveDetails(
            active=active,
            name=active.get_id() or "",
            uuid=active.get_uuid() or "",
            type_name=type_name,
            type_label=connection_type_label(type_name),
            state=state,
            state_label=ACTIVE_STATE_LABELS.get(state, "Unknown"),
            device=device,
            interface=device.get_iface() if device else "",
            ssid=ssid,
            signal=signal,
            ipv4=read_ip_config(active.get_ip4_config()),
            ipv6=read_ip_config(active.get_ip6_config()),
        )

    # -------------------------------------------------------------------------
    # Activation / deactivation
    # -------------------------------------------------------------------------

    def watch_activation(self, active: NM.ActiveConnection, name: str,
                         on_result: Optional[Callable[[bool, str], None]] = None,
                         timeout_s: int = 90) -> None:
        """Follow an activation until it succeeds or fails, then report it.

        activate_connection_finish() only tells us NM *accepted* the request.
        Whether it actually connected (or failed because of a wrong password,
        DHCP timeout, ...) is only visible through the active connection's
        state-changed signal, so we listen to it here.
        """
        handler_id = 0
        timeout_id = 0
        finished = False

        def finish(ok: bool, message: str) -> None:
            nonlocal finished
            if finished:
                return
            finished = True
            if handler_id:
                # handler_disconnect() is used for all libnm objects because
                # some (NM.Device) define a disconnect() that drops the link.
                active.handler_disconnect(handler_id)
            if timeout_id:
                GLib.source_remove(timeout_id)
            self.add_log(message, "success" if ok else "error")
            if on_result:
                on_result(ok, message)

        def describe_failure(reason_code: int) -> str:
            # The device state reason is usually more specific than the
            # active-connection reason (e.g. "no secrets" vs "device disconnected").
            try:
                reason = enum_reason_text(NM.ActiveConnectionStateReason(reason_code))
            except ValueError:
                reason = "Unknown reason"
            for device in active.get_devices() or []:
                device_reason = device.get_state_reason()
                if device_reason not in (NM.DeviceStateReason.NONE,
                                         NM.DeviceStateReason.UNKNOWN):
                    reason = f"{reason} ({enum_reason_text(device_reason)})"
                    break
            return f"Could not connect to {name}: {reason}"

        def on_state_changed(_active, state, reason) -> None:
            if state == NM.ActiveConnectionState.ACTIVATED:
                finish(True, f"Connected to {name}")
            elif state == NM.ActiveConnectionState.DEACTIVATED:
                finish(False, describe_failure(reason))

        def on_timeout() -> bool:
            nonlocal timeout_id
            timeout_id = 0
            finish(False, f"Connecting to {name} is taking long; still waiting in the background")
            return GLib.SOURCE_REMOVE

        # The activation may already have completed by the time we get here.
        state = active.get_state()
        if state == NM.ActiveConnectionState.ACTIVATED:
            finish(True, f"Connected to {name}")
            return
        if state == NM.ActiveConnectionState.DEACTIVATED:
            finish(False, describe_failure(int(active.get_state_reason())))
            return
        handler_id = active.connect("state-changed", on_state_changed)
        timeout_id = GLib.timeout_add_seconds(timeout_s, on_timeout)

    def activate(self, connection: Optional[NM.Connection],
                 device: Optional[NM.Device], specific_object: Optional[str],
                 name: str, on_result: Optional[Callable[[bool, str], None]] = None) -> None:
        """Activate a saved profile (or let NM pick one for `device`).

        Passing connection=None with a device asks NM to choose the best
        available saved profile for that device, which is what
        'Connect' on the Overview page does.
        """
        self.add_log(f"Connecting to {name}…")

        def started(active, error):
            if error:
                message = f"Could not connect to {name}: {error}"
                self.add_log(message, "error")
                if on_result:
                    on_result(False, message)
                return
            self.watch_activation(active, name, on_result)

        call_async(
            lambda cb: self.client.activate_connection_async(
                connection, device, specific_object, None, cb),
            "activate_connection_finish", started)

    def deactivate(self, active: NM.ActiveConnection,
                   callback: Optional[ResultCallback] = None) -> None:
        """Disconnect an active connection. The profile itself is untouched."""
        name = active.get_id() or "connection"

        def done(result, error):
            if error:
                self.add_log(f"Could not disconnect {name}: {error}", "error")
            else:
                self.add_log(f"Disconnected {name}", "success")
            if callback:
                callback(result, error)

        call_async(
            lambda cb: self.client.deactivate_connection_async(active, None, cb),
            "deactivate_connection_finish", done)

    def disconnect_device(self, device: NM.Device,
                          callback: Optional[ResultCallback] = None) -> None:
        """Disconnect a whole device.

        Unlike deactivate(), NM also stops auto-connecting on this device
        until the user connects it again, matching nm-applet's behaviour
        for wired adapters.
        """
        name = device.get_iface() or "device"

        def done(result, error):
            if error:
                self.add_log(f"Could not disconnect {name}: {error}", "error")
            else:
                self.add_log(f"Disconnected {name}", "success")
            if callback:
                callback(result, error)

        call_async(lambda cb: device.disconnect_async(None, cb),
                   "disconnect_finish", done)

    # -------------------------------------------------------------------------
    # Diagnostics
    # -------------------------------------------------------------------------

    def check_connectivity(self, callback: ResultCallback) -> None:
        """Ask NM to run its connectivity check now (HTTP probe to the
        URI configured in NetworkManager, ping.archlinux.org on Arch)."""
        def done(state, error):
            if error:
                callback(None, error)
            else:
                callback(CONNECTIVITY_LABELS.get(state, "Unknown"), None)

        call_async(lambda cb: self.client.check_connectivity_async(None, cb),
                   "check_connectivity_finish", done)

    def connectivity_check_available(self) -> bool:
        return self.client.connectivity_check_get_available()

    @staticmethod
    def resolve_host(host: str, callback: ResultCallback) -> None:
        """DNS lookup through the system resolver (no subprocess)."""
        resolver = Gio.Resolver.get_default()

        def done(addresses, error):
            if error:
                callback(None, error)
            else:
                callback([a.to_string() for a in addresses], None)

        call_async(lambda cb: resolver.lookup_by_name_async(host, None, cb),
                   "lookup_by_name_finish", done)

    @staticmethod
    def check_tcp(host: str, port: int, callback: ResultCallback) -> None:
        """Open (and immediately close) a TCP connection to host:port."""
        socket_client = Gio.SocketClient.new()
        socket_client.set_timeout(8)
        started = time.monotonic()

        def done(connection, error):
            if error:
                callback(None, error)
                return
            elapsed_ms = int((time.monotonic() - started) * 1000)
            connection.close(None)
            callback(elapsed_ms, None)

        call_async(lambda cb: socket_client.connect_to_host_async(host, port, None, cb),
                   "connect_to_host_finish", done)

    @staticmethod
    def get_service_status(callback: ResultCallback) -> None:
        """Read NetworkManager.service state from systemd over D-Bus.

        Returns (ActiveState, SubState), e.g. ("active", "running").
        LoadUnit is used instead of GetUnit because GetUnit fails for a unit
        that is installed but not currently loaded.
        """
        def on_bus(_source, result, *_):
            try:
                bus = Gio.bus_get_finish(result)
            except GLib.Error as error:
                callback(None, friendly_error(error))
                return
            bus.call("org.freedesktop.systemd1", "/org/freedesktop/systemd1",
                     "org.freedesktop.systemd1.Manager", "LoadUnit",
                     GLib.Variant("(s)", ("NetworkManager.service",)),
                     GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, 3000,
                     None, lambda src, res, *_: on_unit(bus, src, res))

        def on_unit(bus, source, result):
            try:
                (unit_path,) = source.call_finish(result).unpack()
            except GLib.Error as error:
                callback(None, friendly_error(error))
                return
            bus.call("org.freedesktop.systemd1", unit_path,
                     "org.freedesktop.DBus.Properties", "GetAll",
                     GLib.Variant("(s)", ("org.freedesktop.systemd1.Unit",)),
                     GLib.VariantType("(a{sv})"), Gio.DBusCallFlags.NONE, 3000,
                     None, on_properties)

        def on_properties(source, result, *_):
            try:
                (properties,) = source.call_finish(result).unpack()
            except GLib.Error as error:
                callback(None, friendly_error(error))
                return
            callback((properties.get("ActiveState", "unknown"),
                      properties.get("SubState", "unknown")), None)

        Gio.bus_get(Gio.BusType.SYSTEM, None, on_bus)
