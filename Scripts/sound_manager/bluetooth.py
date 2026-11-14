# =============================================================================
# bluetooth.py - BlueZ state model and commands (via D-Bus)
# =============================================================================
# Same idea as backend.py, but for Bluetooth:
#
#   reading   org.bluez ObjectManager.GetManagedObjects gives the adapter and
#             every known device with all properties in one call.
#   watching  any org.bluez signal (PropertiesChanged, InterfacesAdded/Removed)
#             schedules a debounced reload, then 'changed' is emitted.
#   writing   Properties.Set (power), StartDiscovery, Device1.Connect/Pair ...
#
# No new Python dependency: Gio's D-Bus client is part of PyGObject.
#
# SECURITY: pairing needs a BlueZ "agent". We register a NoInputNoOutput
# agent that auto-accepts ("Just Works") ONLY while a pairing the user started
# is in progress, and unregister it again afterwards. An agent that was always
# registered would silently accept pairing/service requests from any device
# nearby while the window is open. Devices that need a PIN/passkey (keyboards,
# some cars) are therefore not supported here - use bluetoothctl for those.
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from gi.repository import Gio, GLib, GObject

BLUEZ = "org.bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
AGENT_MANAGER_IFACE = "org.bluez.AgentManager1"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
AGENT_PATH = "/dk/mrfaws/SoundManager/agent"

RELOAD_DELAY_MS = 80

AGENT_XML = """
<node>
  <interface name="org.bluez.Agent1">
    <method name="Release"/>
    <method name="Cancel"/>
    <method name="RequestAuthorization"><arg type="o" direction="in"/></method>
    <method name="AuthorizeService">
      <arg type="o" direction="in"/><arg type="s" direction="in"/>
    </method>
  </interface>
</node>
"""

# BlueZ "Icon" property (freedesktop names) -> icon theme name.
_ICONS = {
    "audio-card": "audio-speakers-symbolic",
    "audio-headset": "audio-headset-symbolic",
    "audio-headphones": "audio-headphones-symbolic",
    "input-keyboard": "input-keyboard-symbolic",
    "input-mouse": "input-mouse-symbolic",
    "input-gaming": "input-gaming-symbolic",
    "phone": "phone-symbolic",
    "computer": "computer-symbolic",
}


@dataclass
class BtDevice:
    path: str
    name: str
    address: str
    icon: str
    paired: bool
    connected: bool
    trusted: bool
    nearby: bool        # seen by the current scan (BlueZ reports an RSSI)


def _clean_error(error: GLib.Error) -> str:
    return Gio.DBusError.strip_remote_error(error) or error.message or str(error)


class BluetoothBackend(GObject.Object):
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self) -> None:
        super().__init__()
        self.bus: Optional[Gio.DBusConnection] = None
        self.loaded = False
        self.installed = True        # False when nothing owns org.bluez
        self.adapter_path: Optional[str] = None
        self.adapter_name = ""
        self.powered = False
        self.discovering = False
        self.devices: list[BtDevice] = []
        self.busy: set[str] = set()  # device paths with a connect/pair running
        self.on_error: Callable[[str], None] = lambda message: None
        self._reload_id = 0
        self._agent_id = 0
        self._want_scan = False

        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error:
            self.installed = False
            self.loaded = True
            return
        # Every org.bluez signal means "something changed, re-read".
        self.bus.signal_subscribe(BLUEZ, None, None, None, None,
                                  Gio.DBusSignalFlags.NONE, self._on_signal)
        # bluetoothd started/stopped (e.g. after `systemctl enable --now`).
        self.bus.signal_subscribe("org.freedesktop.DBus", "org.freedesktop.DBus",
                                  "NameOwnerChanged", "/org/freedesktop/DBus", BLUEZ,
                                  Gio.DBusSignalFlags.NONE, self._on_signal)
        self.reload()

    # -------------------------------------------------------------------------
    # Reading
    # -------------------------------------------------------------------------

    def _on_signal(self, *_args) -> None:
        self.queue_reload()

    def queue_reload(self) -> None:
        if self._reload_id == 0:
            self._reload_id = GLib.timeout_add(RELOAD_DELAY_MS, self._reload_now)

    def _reload_now(self) -> bool:
        self._reload_id = 0
        self.reload()
        return False

    def reload(self) -> None:
        if self.bus is None:
            return
        self.bus.call(BLUEZ, "/", "org.freedesktop.DBus.ObjectManager",
                      "GetManagedObjects", None,
                      GLib.VariantType("(a{oa{sa{sv}}})"),
                      Gio.DBusCallFlags.NONE, 5000, None, self._on_objects)

    def _on_objects(self, bus, result) -> None:
        try:
            objects = bus.call_finish(result).unpack()[0]
        except GLib.Error:
            # ServiceUnknown / NameHasNoOwner: bluez is not installed or the
            # service cannot be started.
            self.installed = False
            self.adapter_path = None
            self.devices = []
            self._finish_load()
            return

        self.installed = True
        self.adapter_path = None
        devices: list[BtDevice] = []
        for path, interfaces in objects.items():
            if ADAPTER_IFACE in interfaces and self.adapter_path is None:
                props = interfaces[ADAPTER_IFACE]
                self.adapter_path = path
                self.adapter_name = props.get("Alias", "")
                self.powered = bool(props.get("Powered", False))
                self.discovering = bool(props.get("Discovering", False))
            if DEVICE_IFACE in interfaces:
                p = interfaces[DEVICE_IFACE]
                address = p.get("Address", "")
                name = p.get("Alias") or p.get("Name") or address
                devices.append(BtDevice(
                    path=path, name=name, address=address,
                    icon=_ICONS.get(p.get("Icon", ""), "bluetooth-symbolic"),
                    paired=bool(p.get("Paired", False)),
                    connected=bool(p.get("Connected", False)),
                    trusted=bool(p.get("Trusted", False)),
                    nearby="RSSI" in p))
        devices.sort(key=lambda d: (not d.connected, d.name.lower()))
        self.devices = devices
        if self.adapter_path is None:
            self.powered = False
            self.discovering = False
        self._finish_load()

    def _finish_load(self) -> None:
        self.loaded = True
        self._sync_scan()
        self.emit("changed")

    # -------------------------------------------------------------------------
    # Writing
    # -------------------------------------------------------------------------

    def _call(self, path: str, iface: str, method: str, params: Optional[GLib.Variant],
              done: Optional[Callable[[Optional[GLib.Error]], None]] = None,
              timeout_ms: int = 30000) -> None:
        def finished(bus, result):
            error = None
            try:
                bus.call_finish(result)
            except GLib.Error as e:
                error = e
            if done is not None:
                done(error)
            elif error is not None:
                self.on_error(_clean_error(error))
            self.queue_reload()

        self.bus.call(BLUEZ, path, iface, method, params, None,
                      Gio.DBusCallFlags.NONE, timeout_ms, None, finished)

    def set_powered(self, on: bool) -> None:
        if self.adapter_path is None:
            return
        if on:
            # Bluetooth is soft-blocked after boot on many laptops; BlueZ
            # refuses to power on while rfkill blocks it.
            try:
                Gio.Subprocess.new(["rfkill", "unblock", "bluetooth"],
                                   Gio.SubprocessFlags.STDERR_SILENCE).wait_async(
                    None, lambda p, r: (p.wait_finish(r), self._set_powered_now(True)))
                return
            except GLib.Error:
                pass
        self._set_powered_now(on)

    def _set_powered_now(self, on: bool) -> None:
        self._call(self.adapter_path, PROPS_IFACE, "Set",
                   GLib.Variant("(ssv)", (ADAPTER_IFACE, "Powered", GLib.Variant("b", on))))

    def set_scanning(self, scan: bool) -> None:
        """Ask for discovery to run while the Bluetooth page is open."""
        self._want_scan = scan
        self._sync_scan()

    def _sync_scan(self) -> None:
        if self.adapter_path is None or self.bus is None:
            return
        want = self._want_scan and self.powered
        if want and not self.discovering:
            self._call(self.adapter_path, ADAPTER_IFACE, "StartDiscovery", None,
                       done=lambda e: None)
        elif not want and self.discovering:
            self._call(self.adapter_path, ADAPTER_IFACE, "StopDiscovery", None,
                       done=lambda e: None)

    def connect_device(self, device: BtDevice) -> None:
        self._run(device, DEVICE_IFACE, "Connect", f"Could not connect to {device.name}")

    def disconnect_device(self, device: BtDevice) -> None:
        self._run(device, DEVICE_IFACE, "Disconnect", f"Could not disconnect {device.name}")

    def forget_device(self, device: BtDevice) -> None:
        if self.adapter_path is None:
            return
        self._call(self.adapter_path, ADAPTER_IFACE, "RemoveDevice",
                   GLib.Variant("(o)", (device.path,)))

    def _run(self, device: BtDevice, iface: str, method: str, failure: str) -> None:
        self.busy.add(device.path)
        self.emit("changed")

        def done(error):
            self.busy.discard(device.path)
            if error is not None:
                self.on_error(f"{failure}: {_clean_error(error)}")
            self.emit("changed")

        self._call(device.path, iface, method, None, done=done)

    # -------------------------------------------------------------------------
    # Pairing (temporary agent, see the SECURITY note at the top)
    # -------------------------------------------------------------------------

    def pair_device(self, device: BtDevice) -> None:
        if self.adapter_path is None or device.path in self.busy:
            return
        self.busy.add(device.path)
        self.emit("changed")

        def fail(message: str) -> None:
            self._unregister_agent()
            self.busy.discard(device.path)
            self.on_error(message)
            self.emit("changed")

        if not self._register_agent_object():
            fail("Could not start the pairing helper")
            return

        def agent_ready(error):
            if error is not None:
                fail(f"Pairing failed: {_clean_error(error)}")
                return

            def paired(error):
                self._unregister_agent()
                self.busy.discard(device.path)
                if error is not None:
                    self.on_error(f"Could not pair with {device.name}: {_clean_error(error)}")
                    self.emit("changed")
                    return
                # Trusted lets the device reconnect by itself later; then
                # connect right away so the user hears something.
                self._call(device.path, PROPS_IFACE, "Set",
                           GLib.Variant("(ssv)", (DEVICE_IFACE, "Trusted",
                                                  GLib.Variant("b", True))),
                           done=lambda e: None)
                self._run(device, DEVICE_IFACE, "Connect",
                          f"Paired, but could not connect to {device.name}")

            self._call(device.path, DEVICE_IFACE, "Pair", None, done=paired,
                       timeout_ms=60000)

        # Register with BlueZ and make us the default agent for this pairing.
        def registered(error):
            if error is not None:
                agent_ready(error)
                return
            self._call("/org/bluez", AGENT_MANAGER_IFACE, "RequestDefaultAgent",
                       GLib.Variant("(o)", (AGENT_PATH,)), done=agent_ready)

        self._call("/org/bluez", AGENT_MANAGER_IFACE, "RegisterAgent",
                   GLib.Variant("(os)", (AGENT_PATH, "NoInputNoOutput")), done=registered)

    def _register_agent_object(self) -> bool:
        if self._agent_id:
            return True
        try:
            info = Gio.DBusNodeInfo.new_for_xml(AGENT_XML).interfaces[0]
            self._agent_id = self.bus.register_object(AGENT_PATH, info, self._agent_call,
                                                      None, None)
        except GLib.Error:
            return False
        return self._agent_id != 0

    def _agent_call(self, _conn, _sender, _path, _iface, method, _params, invocation) -> None:
        # Only reachable during a pairing the user started. All methods return
        # nothing; returning without error means "accepted".
        invocation.return_value(None)

    def _unregister_agent(self) -> None:
        if self._agent_id:
            self.bus.unregister_object(self._agent_id)
            self._agent_id = 0
        self._call("/org/bluez", AGENT_MANAGER_IFACE, "UnregisterAgent",
                   GLib.Variant("(o)", (AGENT_PATH,)), done=lambda e: None)
