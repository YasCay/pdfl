"""Session D-Bus daemon for Power Display for Linux (pdfl)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib

from pdfl import BUS_NAME, IFACE_NAME, OBJ_PATH, __version__
from pdfl.backend import PowerDisplayBackend
from pdfl.models import AppSettings, Profile

INTROSPECTION_XML = f"""
<node>
  <interface name="{IFACE_NAME}">
    <method name="Ping">
      <arg type="s" name="version" direction="out"/>
    </method>
    <method name="GetState">
      <arg type="s" name="state_json" direction="out"/>
    </method>
    <method name="Rescan">
      <arg type="s" name="state_json" direction="out"/>
    </method>
    <method name="SetFeature">
      <arg type="s" name="monitor_id" direction="in"/>
      <arg type="s" name="feature" direction="in"/>
      <arg type="i" name="value" direction="in"/>
      <arg type="b" name="confirmed" direction="in"/>
      <arg type="b" name="debounce" direction="in"/>
      <arg type="s" name="result_json" direction="out"/>
    </method>
    <method name="SetLinkedMode">
      <arg type="b" name="enabled" direction="in"/>
      <arg type="s" name="state_json" direction="out"/>
    </method>
    <method name="SetLinkedBrightness">
      <arg type="i" name="percent" direction="in"/>
      <arg type="b" name="debounce" direction="in"/>
      <arg type="s" name="result_json" direction="out"/>
    </method>
    <method name="StepBrightness">
      <arg type="i" name="delta" direction="in"/>
      <arg type="s" name="mode_override" direction="in"/>
      <arg type="s" name="result_json" direction="out"/>
    </method>
    <method name="RequestRotation">
      <arg type="s" name="monitor_id" direction="in"/>
      <arg type="i" name="degrees" direction="in"/>
      <arg type="s" name="result_json" direction="out"/>
    </method>
    <method name="ConfirmRotation">
      <arg type="s" name="token" direction="in"/>
      <arg type="s" name="result_json" direction="out"/>
    </method>
    <method name="RevertRotation">
      <arg type="s" name="token" direction="in"/>
      <arg type="s" name="result_json" direction="out"/>
    </method>
    <method name="ApplyProfile">
      <arg type="s" name="profile_id_or_name" direction="in"/>
      <arg type="s" name="result_json" direction="out"/>
    </method>
    <method name="GetSettings">
      <arg type="s" name="settings_json" direction="out"/>
    </method>
    <method name="SaveSettings">
      <arg type="s" name="settings_json" direction="in"/>
      <arg type="s" name="saved_json" direction="out"/>
    </method>
    <method name="GetProfiles">
      <arg type="s" name="profiles_json" direction="out"/>
    </method>
    <method name="SaveProfile">
      <arg type="s" name="profile_json" direction="in"/>
      <arg type="s" name="saved_json" direction="out"/>
    </method>
    <method name="DeleteProfile">
      <arg type="i" name="profile_id" direction="in"/>
      <arg type="b" name="deleted" direction="out"/>
    </method>
    <method name="ReorderProfile">
      <arg type="i" name="profile_id" direction="in"/>
      <arg type="i" name="delta" direction="in"/>
      <arg type="s" name="profiles_json" direction="out"/>
    </method>
    <method name="OpenSettings">
      <arg type="s" name="page" direction="in"/>
      <arg type="b" name="launched" direction="out"/>
    </method>

    <signal name="StateChanged">
      <arg type="s" name="state_json"/>
    </signal>
    <signal name="ErrorOccurred">
      <arg type="s" name="monitor_id"/>
      <arg type="s" name="message"/>
    </signal>
    <signal name="RotationPending">
      <arg type="s" name="pending_json"/>
    </signal>
  </interface>
</node>
"""


class PdflDaemon:
    """Async session D-Bus service wrapping PowerDisplayBackend."""

    def __init__(
        self,
        backend: PowerDisplayBackend,
        enable_watchers: bool = True,
    ) -> None:
        self.backend = backend
        self._enable_watchers = enable_watchers
        self._loop = GLib.MainLoop()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pdfl-rpc")
        self._connection: Gio.DBusConnection | None = None
        self._reg_id: int = 0
        self._own_id: int = 0
        self._session_subs: list[int] = []
        self._system_bus: Gio.DBusConnection | None = None
        self._system_subs: list[int] = []
        self._rescan_source_id: int = 0

        self._node_info = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION_XML)
        self._iface_info = self._node_info.lookup_interface(IFACE_NAME)

        self.backend.add_state_listener(self._on_backend_state)
        self.backend.add_error_listener(self._on_backend_error)
        self.backend.add_rotation_listener(self._on_backend_rotation)

    def _on_backend_state(self, state: dict[str, Any]) -> None:
        payload = json.dumps(state, ensure_ascii=False)
        GLib.idle_add(self._emit_signal, "StateChanged", GLib.Variant("(s)", (payload,)))

    def _on_backend_error(self, monitor_id: str, message: str) -> None:
        GLib.idle_add(
            self._emit_signal,
            "ErrorOccurred",
            GLib.Variant("(ss)", (monitor_id, message)),
        )

    def _on_backend_rotation(self, pending: dict[str, Any]) -> None:
        payload = json.dumps(pending, ensure_ascii=False)
        GLib.idle_add(
            self._emit_signal, "RotationPending", GLib.Variant("(s)", (payload,))
        )

    def _emit_signal(self, name: str, params: GLib.Variant) -> bool:
        if self._connection is not None:
            try:
                self._connection.emit_signal(
                    None, OBJ_PATH, IFACE_NAME, name, params
                )
            except Exception:
                pass
        return False

    def _on_bus_acquired(self, connection: Gio.DBusConnection, _name: str) -> None:
        self._connection = connection
        self._reg_id = connection.register_object(
            OBJ_PATH,
            self._iface_info,
            self._handle_method_call,
            None,
            None,
        )
        if self._enable_watchers:
            self._setup_watchers(connection)

    def _on_name_acquired(self, _connection: Gio.DBusConnection, _name: str) -> None:
        # Run initial scan off the main loop once D-Bus name is ready
        self._executor.submit(self.backend.initialize)

    def _on_name_lost(self, _connection: Gio.DBusConnection, _name: str) -> None:
        self.quit()

    def _setup_watchers(self, session_bus: Gio.DBusConnection) -> None:
        # 1. Wayland/Mutter DisplayConfig MonitorsChanged -> debounced rescan
        try:
            sub_disp = session_bus.signal_subscribe(
                "org.gnome.Mutter.DisplayConfig",
                "org.gnome.Mutter.DisplayConfig",
                "MonitorsChanged",
                "/org/gnome/Mutter/DisplayConfig",
                None,
                Gio.DBusSignalFlags.NONE,
                self._on_monitors_changed,
            )
            self._session_subs.append(sub_disp)
        except Exception:
            pass

        # 2. XDG Desktop Portal color-scheme changes -> opt-in theme profile switching
        try:
            sub_theme = session_bus.signal_subscribe(
                "org.freedesktop.portal.Desktop",
                "org.freedesktop.portal.Settings",
                "SettingChanged",
                "/org/freedesktop/portal/desktop",
                "org.freedesktop.appearance",
                Gio.DBusSignalFlags.NONE,
                self._on_portal_setting_changed,
            )
            self._session_subs.append(sub_theme)
        except Exception:
            pass

        # 3. System bus login1 PrepareForSleep -> rescan on wake
        try:
            self._system_bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            sub_sleep = self._system_bus.signal_subscribe(
                "org.freedesktop.login1",
                "org.freedesktop.login1.Manager",
                "PrepareForSleep",
                "/org/freedesktop/login1",
                None,
                Gio.DBusSignalFlags.NONE,
                self._on_prepare_for_sleep,
            )
            self._system_subs.append(sub_sleep)
        except Exception:
            self._system_bus = None

    def _schedule_rescan(self) -> None:
        if self._rescan_source_id:
            GLib.source_remove(self._rescan_source_id)
            self._rescan_source_id = 0
        delay_ms = max(250, int(self.backend.get_settings().refresh_delay_seconds * 1000))

        def _trigger() -> bool:
            self._rescan_source_id = 0
            self._executor.submit(self.backend.rescan, False)
            return False

        self._rescan_source_id = GLib.timeout_add(delay_ms, _trigger)

    def _on_monitors_changed(self, *_args: Any) -> None:
        self._schedule_rescan()

    def _on_prepare_for_sleep(
        self,
        _conn: Gio.DBusConnection,
        _sender: str,
        _path: str,
        _iface: str,
        _signal: str,
        params: GLib.Variant,
    ) -> None:
        starting_sleep = bool(params.unpack()[0])
        if not starting_sleep:
            self._schedule_rescan()

    def _on_portal_setting_changed(
        self,
        _conn: Gio.DBusConnection,
        _sender: str,
        _path: str,
        _iface: str,
        _signal: str,
        params: GLib.Variant,
    ) -> None:
        ns, key, val = params.unpack()
        if ns == "org.freedesktop.appearance" and key == "color-scheme":
            # Freedesktop color-scheme: 1 = prefer-dark, 0/2 = default/light
            is_dark = int(val) == 1
            self._executor.submit(self.backend.on_system_theme_changed, is_dark)

    def _run_async_rpc(
        self,
        invocation: Gio.DBusMethodInvocation,
        fn: Callable[[], Any],
        variant_fmt: str,
        wrap_tuple: bool = True,
    ) -> None:
        def worker() -> None:
            try:
                result = fn()

                def finish_ok() -> bool:
                    val = (result,) if wrap_tuple else result
                    invocation.return_value(GLib.Variant(variant_fmt, val))
                    return False

                GLib.idle_add(finish_ok)
            except Exception as exc:
                msg = str(exc)

                def finish_err() -> bool:
                    invocation.return_dbus_error(
                        f"{IFACE_NAME}.Error", msg
                    )
                    return False

                GLib.idle_add(finish_err)

        self._executor.submit(worker)

    def _handle_method_call(
        self,
        _connection: Gio.DBusConnection,
        _sender: str,
        _object_path: str,
        _interface_name: str,
        method_name: str,
        parameters: GLib.Variant,
        invocation: Gio.DBusMethodInvocation,
    ) -> None:
        args = parameters.unpack()

        if method_name == "Ping":
            invocation.return_value(GLib.Variant("(s)", (__version__,)))
            return
        if method_name == "GetState":
            self._run_async_rpc(
                invocation,
                lambda: json.dumps(self.backend.get_state(), ensure_ascii=False),
                "(s)",
            )
            return
        if method_name == "Rescan":
            self._run_async_rpc(
                invocation,
                lambda: json.dumps(self.backend.rescan(False), ensure_ascii=False),
                "(s)",
            )
            return
        if method_name == "SetFeature":
            mid, feat, val, confirmed, debounce = args
            self._run_async_rpc(
                invocation,
                lambda: json.dumps(
                    self.backend.set_feature(
                        mid, feat, int(val), confirmed=bool(confirmed), debounce=bool(debounce)
                    ).to_dict(),
                    ensure_ascii=False,
                ),
                "(s)",
            )
            return
        if method_name == "SetLinkedMode":
            (enabled,) = args
            self._run_async_rpc(
                invocation,
                lambda: json.dumps(
                    self.backend.set_linked_mode(bool(enabled)), ensure_ascii=False
                ),
                "(s)",
            )
            return
        if method_name == "SetLinkedBrightness":
            percent, debounce = args
            self._run_async_rpc(
                invocation,
                lambda: json.dumps(
                    self.backend.set_linked_brightness(
                        int(percent), debounce=bool(debounce)
                    ).to_dict(),
                    ensure_ascii=False,
                ),
                "(s)",
            )
            return
        if method_name == "StepBrightness":
            delta, mode_override = args
            override = mode_override if mode_override else None
            self._run_async_rpc(
                invocation,
                lambda: json.dumps(
                    self.backend.step_brightness(int(delta), mode_override=override).to_dict(),
                    ensure_ascii=False,
                ),
                "(s)",
            )
            return
        if method_name == "RequestRotation":
            mid, degrees = args
            self._run_async_rpc(
                invocation,
                lambda: json.dumps(
                    self.backend.request_rotation(mid, int(degrees)).to_dict(),
                    ensure_ascii=False,
                ),
                "(s)",
            )
            return
        if method_name == "ConfirmRotation":
            (token,) = args
            self._run_async_rpc(
                invocation,
                lambda: json.dumps(
                    self.backend.confirm_rotation(token).to_dict(), ensure_ascii=False
                ),
                "(s)",
            )
            return
        if method_name == "RevertRotation":
            (token,) = args
            self._run_async_rpc(
                invocation,
                lambda: json.dumps(
                    self.backend.revert_rotation(token).to_dict(), ensure_ascii=False
                ),
                "(s)",
            )
            return
        if method_name == "ApplyProfile":
            (prof_key,) = args
            self._run_async_rpc(
                invocation,
                lambda: json.dumps(
                    self.backend.apply_profile(prof_key).to_dict(), ensure_ascii=False
                ),
                "(s)",
            )
            return
        if method_name == "GetSettings":
            self._run_async_rpc(
                invocation,
                lambda: json.dumps(
                    self.backend.get_settings().to_dict(), ensure_ascii=False
                ),
                "(s)",
            )
            return
        if method_name == "SaveSettings":
            (raw_json,) = args
            parsed = json.loads(raw_json)
            new_settings = AppSettings.from_dict(parsed)
            self._run_async_rpc(
                invocation,
                lambda: json.dumps(
                    self.backend.save_settings(new_settings).to_dict(),
                    ensure_ascii=False,
                ),
                "(s)",
            )
            return
        if method_name == "GetProfiles":
            self._run_async_rpc(
                invocation,
                lambda: json.dumps(
                    [p.to_dict() for p in self.backend.get_profiles()],
                    ensure_ascii=False,
                ),
                "(s)",
            )
            return
        if method_name == "SaveProfile":
            (raw_json,) = args
            parsed = json.loads(raw_json)
            prof = Profile.from_dict(parsed)
            self._run_async_rpc(
                invocation,
                lambda: json.dumps(
                    self.backend.save_profile(prof).to_dict(), ensure_ascii=False
                ),
                "(s)",
            )
            return
        if method_name == "DeleteProfile":
            (pid,) = args
            self._run_async_rpc(
                invocation, lambda: self.backend.delete_profile(int(pid)), "(b)"
            )
            return
        if method_name == "ReorderProfile":
            pid, delta = args
            self._run_async_rpc(
                invocation,
                lambda: json.dumps(
                    [p.to_dict() for p in self.backend.reorder_profile(int(pid), int(delta))],
                    ensure_ascii=False,
                ),
                "(s)",
            )
            return
        if method_name == "OpenSettings":
            (page,) = args
            launched = self._launch_settings_window(page)
            invocation.return_value(GLib.Variant("(b)", (launched,)))
            return

        invocation.return_dbus_error(
            f"{IFACE_NAME}.UnknownMethod", f"Unknown method: {method_name}"
        )

    def _launch_settings_window(self, page: str) -> bool:
        safe_page = page if page in ("displays", "profiles", "settings") else "displays"
        cmd: list[str]
        local_bin = Path(__file__).resolve().parent.parent.parent / "bin" / "pdfl-settings"
        if local_bin.exists():
            cmd = [str(local_bin)]
        elif shutil.which("pdfl-settings"):
            cmd = [shutil.which("pdfl-settings") or "pdfl-settings"]
        else:
            cmd = [sys.executable, "-m", "pdfl.gui.app"]
        if self.backend.demo:
            cmd.append("--demo")
        cmd.extend(["--page", safe_page])
        env = dict(os.environ)
        src_dir = str(Path(__file__).resolve().parent.parent)
        if src_dir not in env.get("PYTHONPATH", ""):
            env["PYTHONPATH"] = f"{src_dir}:{env['PYTHONPATH']}" if env.get("PYTHONPATH") else src_dir
        try:
            subprocess.Popen(cmd, env=env, start_new_session=True)
            return True
        except OSError:
            return False

    def run(self) -> int:
        self._own_id = Gio.bus_own_name(
            Gio.BusType.SESSION,
            BUS_NAME,
            Gio.BusNameOwnerFlags.NONE,
            self._on_bus_acquired,
            self._on_name_acquired,
            self._on_name_lost,
        )
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self._on_unix_sig)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self._on_unix_sig)
        try:
            self._loop.run()
        finally:
            self.cleanup()
        return 0

    def _on_unix_sig(self) -> bool:
        self.quit()
        return False

    def quit(self) -> None:
        if self._loop.is_running():
            self._loop.quit()

    def cleanup(self) -> None:
        if self._rescan_source_id:
            GLib.source_remove(self._rescan_source_id)
            self._rescan_source_id = 0
        if self._connection is not None:
            for sid in self._session_subs:
                try:
                    self._connection.signal_unsubscribe(sid)
                except Exception:
                    pass
            if self._reg_id:
                try:
                    self._connection.unregister_object(self._reg_id)
                except Exception:
                    pass
                self._reg_id = 0
        self._session_subs.clear()
        if self._system_bus is not None:
            for sid in self._system_subs:
                try:
                    self._system_bus.signal_unsubscribe(sid)
                except Exception:
                    pass
        self._system_subs.clear()
        if self._own_id:
            Gio.bus_unown_name(self._own_id)
            self._own_id = 0
        self.backend.shutdown()
        self._executor.shutdown(wait=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pdfl-daemon",
        description="Power Display for Linux D-Bus session service",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run in safe demo mode without hardware access",
    )
    parser.add_argument(
        "--no-watchers",
        action="store_true",
        help="Disable D-Bus system/session watchers (useful for tests)",
    )
    args = parser.parse_args(argv)
    backend = PowerDisplayBackend(demo=args.demo)
    daemon = PdflDaemon(backend=backend, enable_watchers=not args.no_watchers)
    return daemon.run()


if __name__ == "__main__":
    raise SystemExit(main())
