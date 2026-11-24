"""Thin Gio D-Bus client used by CLI and GTK4 GUI to talk to pdfl-daemon."""

from __future__ import annotations

import json
from typing import Any

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib

from pdfl import BUS_NAME, IFACE_NAME, OBJ_PATH


class PdflDbusClient:
    """Synchronous/callback-capable session D-Bus client for CLI and GTK4."""

    def __init__(self, timeout_ms: int = 12000) -> None:
        self._timeout_ms = timeout_ms
        self._proxy: Gio.DBusProxy | None = None

    def _ensure_proxy(self) -> Gio.DBusProxy:
        if self._proxy is None:
            self._proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION,
                Gio.DBusProxyFlags.NONE,
                None,
                BUS_NAME,
                OBJ_PATH,
                IFACE_NAME,
                None,
            )
        return self._proxy

    def is_available(self) -> bool:
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            has_owner = bus.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "NameHasOwner",
                GLib.Variant("(s)", (BUS_NAME,)),
                None,
                Gio.DBusCallFlags.NONE,
                1500,
                None,
            ).unpack()[0]
            if not has_owner:
                self._proxy = None
                return False
            proxy = self._ensure_proxy()
            if proxy.get_name_owner() is None:
                self._proxy = None
                proxy = self._ensure_proxy()
            self._call_raw("Ping", None)
            return True
        except Exception:
            self._proxy = None
            return False

    def _call_raw(self, method: str, params: GLib.Variant | None) -> Any:
        proxy = self._ensure_proxy()
        res = proxy.call_sync(
            method,
            params,
            Gio.DBusCallFlags.NONE,
            self._timeout_ms,
            None,
        )
        unpacked = res.unpack()
        return unpacked[0] if len(unpacked) == 1 else unpacked

    def get_state(self) -> dict[str, Any]:
        return json.loads(self._call_raw("GetState", None))

    def rescan(self) -> dict[str, Any]:
        return json.loads(self._call_raw("Rescan", None))

    def set_feature(
        self,
        monitor_id: str,
        feature: str,
        value: int,
        confirmed: bool = False,
        debounce: bool = False,
    ) -> dict[str, Any]:
        raw = self._call_raw(
            "SetFeature",
            GLib.Variant(
                "(ssibb)",
                (monitor_id, feature, int(value), bool(confirmed), bool(debounce)),
            ),
        )
        return json.loads(raw)

    def set_linked_mode(self, enabled: bool) -> dict[str, Any]:
        return json.loads(
            self._call_raw("SetLinkedMode", GLib.Variant("(b)", (bool(enabled),)))
        )

    def set_linked_brightness(self, percent: int, debounce: bool = False) -> dict[str, Any]:
        return json.loads(
            self._call_raw(
                "SetLinkedBrightness",
                GLib.Variant("(ib)", (int(percent), bool(debounce))),
            )
        )

    def step_brightness(self, delta: int, mode_override: str = "") -> dict[str, Any]:
        return json.loads(
            self._call_raw(
                "StepBrightness", GLib.Variant("(is)", (int(delta), mode_override))
            )
        )

    def request_rotation(self, monitor_id: str, degrees: int) -> dict[str, Any]:
        return json.loads(
            self._call_raw(
                "RequestRotation", GLib.Variant("(si)", (monitor_id, int(degrees)))
            )
        )

    def confirm_rotation(self, token: str) -> dict[str, Any]:
        return json.loads(
            self._call_raw("ConfirmRotation", GLib.Variant("(s)", (token,)))
        )

    def revert_rotation(self, token: str) -> dict[str, Any]:
        return json.loads(
            self._call_raw("RevertRotation", GLib.Variant("(s)", (token,)))
        )

    def apply_profile(self, profile_id_or_name: str) -> dict[str, Any]:
        return json.loads(
            self._call_raw(
                "ApplyProfile", GLib.Variant("(s)", (str(profile_id_or_name),))
            )
        )

    def get_settings(self) -> dict[str, Any]:
        return json.loads(self._call_raw("GetSettings", None))

    def save_settings(self, settings_dict: dict[str, Any]) -> dict[str, Any]:
        return json.loads(
            self._call_raw(
                "SaveSettings",
                GLib.Variant("(s)", (json.dumps(settings_dict, ensure_ascii=False),)),
            )
        )

    def get_profiles(self) -> list[dict[str, Any]]:
        return json.loads(self._call_raw("GetProfiles", None))

    def save_profile(self, profile_dict: dict[str, Any]) -> dict[str, Any]:
        return json.loads(
            self._call_raw(
                "SaveProfile",
                GLib.Variant("(s)", (json.dumps(profile_dict, ensure_ascii=False),)),
            )
        )

    def delete_profile(self, profile_id: int) -> bool:
        return bool(
            self._call_raw("DeleteProfile", GLib.Variant("(i)", (int(profile_id),)))
        )

    def reorder_profile(self, profile_id: int, delta: int) -> list[dict[str, Any]]:
        return json.loads(
            self._call_raw(
                "ReorderProfile",
                GLib.Variant("(ii)", (int(profile_id), int(delta))),
            )
        )

    def connect_signal(self, signal_name: str, callback) -> int:
        proxy = self._ensure_proxy()
        return proxy.connect(
            "g-signal",
            lambda _p, _sender, sig, params: callback(*params.unpack())
            if sig == signal_name
            else None,
        )
