"""Main Adwaita window for Power Display for Linux (Displays, Profiles, Settings)."""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from pdfl import APP_ID, __version__
from pdfl.backend import PowerDisplayBackend
from pdfl.dbus_client import PdflDbusClient
from pdfl.gui.dialogs import (
    CustomVcpMappingDialog,
    ProfileEditorDialog,
    RotationRevertDialog,
    VcpCapabilitiesDialog,
    show_confirm_dialog,
)
from pdfl.models import AppSettings, Profile

AUTOSTART_FILENAME = f"{APP_ID}-daemon.desktop"


def _autostart_path() -> Path:
    cfg = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return cfg / "autostart" / AUTOSTART_FILENAME


def _resolve_daemon_exec() -> str:
    found = shutil.which("pdfl-daemon")
    if found:
        return found
    repo_bin = Path(__file__).resolve().parent.parent.parent.parent / "bin" / "pdfl-daemon"
    if repo_bin.exists():
        return str(repo_bin)
    return "pdfl-daemon"


class _BackendBridge:
    """Uniform adapter over either D-Bus PdflDbusClient or in-process PowerDisplayBackend."""

    def __init__(self, demo: bool = False, direct: bool = False) -> None:
        self.demo = demo
        self.direct = direct
        self.dbus: PdflDbusClient | None = None
        self.backend: PowerDisplayBackend | None = None

        if not direct:
            client = PdflDbusClient()
            if client.is_available():
                self.dbus = client
                return

        self.backend = PowerDisplayBackend(demo=demo)
        self.backend.initialize()

    @property
    def mode_label(self) -> str:
        return "D-Bus Service (pdfl-daemon)" if self.dbus else "Direct Local Backend"

    def get_state(self) -> dict[str, Any]:
        return self.dbus.get_state() if self.dbus else self.backend.get_state()

    def rescan(self) -> dict[str, Any]:
        return self.dbus.rescan() if self.dbus else self.backend.rescan(False)

    def set_feature(
        self, monitor_id: str, feature: str, value: int, confirmed: bool = False, debounce: bool = False
    ) -> dict[str, Any]:
        if self.dbus:
            return self.dbus.set_feature(monitor_id, feature, value, confirmed, debounce)
        return self.backend.set_feature(monitor_id, feature, value, confirmed, debounce).to_dict()

    def set_linked_mode(self, enabled: bool) -> dict[str, Any]:
        return self.dbus.set_linked_mode(enabled) if self.dbus else self.backend.set_linked_mode(enabled)

    def set_linked_brightness(self, percent: int, debounce: bool = False) -> dict[str, Any]:
        if self.dbus:
            return self.dbus.set_linked_brightness(percent, debounce)
        return self.backend.set_linked_brightness(percent, debounce).to_dict()

    def request_rotation(self, monitor_id: str, degrees: int) -> dict[str, Any]:
        if self.dbus:
            return self.dbus.request_rotation(monitor_id, degrees)
        return self.backend.request_rotation(monitor_id, degrees).to_dict()

    def confirm_rotation(self, token: str) -> dict[str, Any]:
        if self.dbus:
            return self.dbus.confirm_rotation(token)
        return self.backend.confirm_rotation(token).to_dict()

    def revert_rotation(self, token: str) -> dict[str, Any]:
        if self.dbus:
            return self.dbus.revert_rotation(token)
        return self.backend.revert_rotation(token).to_dict()

    def save_settings(self, settings_dict: dict[str, Any]) -> dict[str, Any]:
        if self.dbus:
            return self.dbus.save_settings(settings_dict)
        return self.backend.save_settings(AppSettings.from_dict(settings_dict)).to_dict()

    def save_profile(self, profile_dict: dict[str, Any]) -> dict[str, Any]:
        if self.dbus:
            return self.dbus.save_profile(profile_dict)
        return self.backend.save_profile(Profile.from_dict(profile_dict)).to_dict()

    def apply_profile(self, profile_id: int) -> dict[str, Any]:
        if self.dbus:
            return self.dbus.apply_profile(str(profile_id))
        return self.backend.apply_profile(profile_id).to_dict()

    def delete_profile(self, profile_id: int) -> bool:
        if self.dbus:
            return self.dbus.delete_profile(profile_id)
        return self.backend.delete_profile(profile_id)

    def reorder_profile(self, profile_id: int, delta: int) -> list[dict[str, Any]]:
        if self.dbus:
            return self.dbus.reorder_profile(profile_id, delta)
        return [p.to_dict() for p in self.backend.reorder_profile(profile_id, delta)]

    def shutdown(self) -> None:
        if self.backend:
            self.backend.shutdown()


class PdflMainWindow(Adw.ApplicationWindow):
    """Main GNOME/Adwaita window for Power Display for Linux."""

    def __init__(
        self,
        app: Adw.Application,
        demo: bool = False,
        direct: bool = False,
        initial_page: str = "displays",
    ) -> None:
        super().__init__(
            application=app,
            title="Power Display for Linux",
            default_width=760,
            default_height=700,
        )
        self._bridge = _BackendBridge(demo=demo, direct=direct)
        self._state: dict[str, Any] = self._bridge.get_state()
        self._syncing = False
        self._debounce_timers: dict[str, int] = {}

        self._toast_overlay = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()

        self._stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self._stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)

        rescan_btn = Gtk.Button(
            icon_name="view-refresh-symbolic",
            tooltip_text="Rescan monitors",
        )
        rescan_btn.connect("clicked", self._on_rescan_clicked)
        header.pack_end(rescan_btn)

        toolbar.add_top_bar(header)

        self._banner = Adw.Banner(revealed=False)
        toolbar.add_top_bar(self._banner)

        self._displays_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._profiles_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._settings_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        self._stack.add_titled_with_icon(
            self._wrap_scroll(self._displays_container),
            "displays",
            "Displays",
            "video-display-symbolic",
        )
        self._stack.add_titled_with_icon(
            self._wrap_scroll(self._profiles_container),
            "profiles",
            "Profiles",
            "starred-symbolic",
        )
        self._stack.add_titled_with_icon(
            self._wrap_scroll(self._settings_container),
            "settings",
            "Settings",
            "emblem-system-symbolic",
        )

        if initial_page in ("displays", "profiles", "settings"):
            self._stack.set_visible_child_name(initial_page)

        toolbar.set_content(self._stack)
        self._toast_overlay.set_child(toolbar)
        self.set_content(self._toast_overlay)

        if self._bridge.dbus is not None:
            self._bridge.dbus.connect_signal("StateChanged", self._on_dbus_state_changed)
            self._bridge.dbus.connect_signal("ErrorOccurred", self._on_dbus_error)

        self.connect("close-request", self._on_close_request)
        self._render_all()

    def switch_page(self, page_name: str) -> None:
        self._stack.set_visible_child_name(page_name)

    def _wrap_scroll(self, child: Gtk.Widget) -> Gtk.ScrolledWindow:
        scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        clamp = Adw.Clamp(maximum_size=680, margin_top=16, margin_bottom=24, margin_start=12, margin_end=12)
        clamp.set_child(child)
        scroll.set_child(clamp)
        return scroll

    def _toast(self, text: str) -> None:
        self._toast_overlay.add_toast(Adw.Toast(title=text, timeout=3))

    def _on_close_request(self, *_args: Any) -> bool:
        for tid in list(self._debounce_timers.values()):
            GLib.source_remove(tid)
        self._debounce_timers.clear()
        self._bridge.shutdown()
        return False

    def _on_dbus_state_changed(self, state_json: str) -> None:
        import json

        try:
            self._state = json.loads(state_json)
            GLib.idle_add(self._render_all)
        except Exception:
            pass

    def _on_dbus_error(self, _monitor_id: str, message: str) -> None:
        GLib.idle_add(self._toast, message)

    def _run_bg(self, fn, on_done=None) -> None:
        def worker() -> None:
            res = fn()
            if on_done is not None:
                GLib.idle_add(on_done, res)

        threading.Thread(target=worker, daemon=True).start()

    def _on_rescan_clicked(self, *_args: Any) -> None:
        def _done(new_state: dict[str, Any]) -> bool:
            self._state = new_state
            self._render_all()
            self._toast("Monitor list updated.")
            return False

        self._run_bg(self._bridge.rescan, _done)

    def _clear_box(self, box: Gtk.Box) -> None:
        child = box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt

    def _render_all(self) -> bool:
        self._syncing = True
        try:
            self._update_banner()
            self._render_displays_tab()
            self._render_profiles_tab()
            self._render_settings_tab()
        finally:
            self._syncing = False
        return False

    def _update_banner(self) -> None:
        warnings = self._state.get("warnings") or []
        if self._state.get("demo"):
            self._banner.set_title("Demo mode active — changes do not affect physical monitors.")
            self._banner.set_revealed(True)
        elif warnings:
            self._banner.set_title(warnings[0])
            self._banner.set_revealed(True)
        else:
            self._banner.set_revealed(False)

    def _schedule_slider(self, key: str, callback) -> None:
        old = self._debounce_timers.pop(key, None)
        if old:
            GLib.source_remove(old)

        def _fire() -> bool:
            self._debounce_timers.pop(key, None)
            self._run_bg(callback)
            return False

        self._debounce_timers[key] = GLib.timeout_add(120, _fire)

    def _render_displays_tab(self) -> None:
        self._clear_box(self._displays_container)
        settings = self._state.get("settings", {}) or {}
        monitors = self._state.get("monitors", []) or []
        prefs_map = settings.get("monitor_prefs", {}) or {}

        # Linked Brightness Section
        linked_group = Adw.PreferencesGroup(
            title="Unified Control",
            margin_bottom=16,
        )
        linked_row = Adw.SwitchRow(
            title="Linked Brightness",
            subtitle="Controls all visible writable displays together.",
            active=bool(settings.get("linked_brightness", False)),
            tooltip_text="Synchronizes brightness changes across all active monitors.",
        )
        linked_row.connect("notify::active", self._on_linked_toggled)
        linked_group.add(linked_row)

        if settings.get("linked_brightness"):
            master_val = self._state.get("linked_brightness_value")
            if master_val is None:
                master_val = 50
            master_row = self._make_slider_row(
                "Master Brightness",
                int(master_val),
                True,
                lambda val: self._schedule_slider(
                    "linked:master",
                    lambda v=val: self._bridge.set_linked_brightness(v, debounce=False),
                ),
            )
            linked_group.add(master_row)
        self._displays_container.append(linked_group)

        if not monitors:
            empty = Adw.StatusPage(
                icon_name="video-display-symbolic",
                title="No Supported Monitors Detected",
                description=(
                    "Check I2C/DDC-CI permissions (ddcutil detect) or "
                    "try the interface safely with 'pdfl-settings --demo'."
                ),
            )
            self._displays_container.append(empty)
            return

        for mon in monitors:
            mid = mon["monitor_id"]
            caps = mon.get("capabilities", {}) or {}
            prefs = prefs_map.get(mid, {}) or {}
            kind = "Built-in" if mon.get("is_builtin") else f"External ({mon.get('connector') or 'DDC/CI'})"
            is_sw_dim = str(mon.get("brightness_mode", "")).startswith("software_")
            ddc_tag = ""
            if not mon.get("is_builtin"):
                ddc_st = mon.get("ddc_status", "not_applicable")
                if ddc_st == "available":
                    ddc_tag = " · DDC/CI active"
                elif is_sw_dim:
                    ddc_tag = " · Software dimming (no DDC/CI)"
                elif ddc_st != "not_applicable":
                    ddc_tag = " · DRM only · no DDC/CI"
            prim = " · Primary" if mon.get("is_primary") else ""
            hidden_tag = " · Hidden in flyout" if mon.get("hidden") else ""

            m_group = Adw.PreferencesGroup(
                title=mon["display_name"],
                description=f"{kind}{ddc_tag}{prim}{hidden_tag}",
                margin_bottom=18,
            )

            if mon.get("status_level") == "info" and mon.get("status_notice"):
                info_row = Adw.ActionRow(
                    title="Status Notice",
                    subtitle=mon["status_notice"],
                    tooltip_text=mon.get("last_error") or mon["status_notice"],
                )
                m_group.add(info_row)
            elif mon.get("last_error"):
                err_row = Adw.ActionRow(
                    title="Notice / Hardware Status",
                    subtitle=mon["last_error"],
                )
                err_row.add_css_class("warning")
                m_group.add(err_row)

            # Brightness
            if caps.get("brightness") and mon.get("brightness") is not None:
                writable = bool(caps.get("brightness_writable", False))
                b_row = self._make_slider_row(
                    "Brightness (Software Mode)" if is_sw_dim else "Brightness",
                    int(mon["brightness"]),
                    writable,
                    lambda val, m_id=mid: self._schedule_slider(
                        f"{m_id}:brightness",
                        lambda: self._bridge.set_feature(m_id, "brightness", val, True, False),
                    ),
                    tooltip=(
                        "Adjusts display brightness via GPU Gamma/CTM (minimum 35% safety floor)."
                        if is_sw_dim
                        else ("Adjust monitor brightness." if writable else "Currently read-only.")
                    ),
                )
                m_group.add(b_row)

            # Contrast
            if caps.get("contrast") and mon.get("contrast") is not None:
                c_row = self._make_slider_row(
                    "Contrast (Software Mode)" if is_sw_dim else "Contrast",
                    int(mon["contrast"]),
                    True,
                    lambda val, m_id=mid: self._schedule_slider(
                        f"{m_id}:contrast",
                        lambda: self._bridge.set_feature(m_id, "contrast", val, True, False),
                    ),
                    tooltip=(
                        "Adjusts contrast curve via GPU Gamma LUT (50% = neutral, no black crush)."
                        if is_sw_dim
                        else "Adjust monitor contrast (VCP 0x12)."
                    ),
                )
                m_group.add(c_row)

            # Volume
            if caps.get("volume") and mon.get("volume") is not None:
                v_row = self._make_slider_row(
                    "Monitor Volume",
                    int(mon["volume"]),
                    True,
                    lambda val, m_id=mid: self._schedule_slider(
                        f"{m_id}:volume",
                        lambda: self._bridge.set_feature(m_id, "volume", val, True, False),
                    ),
                    tooltip="Adjust built-in monitor speaker volume (VCP 0x62).",
                )
                m_group.add(v_row)

            # Color Temperature
            if caps.get("color_temperature") and mon.get("available_color_presets"):
                presets = mon["available_color_presets"]
                ct_row = Adw.ComboRow(
                    title="Color Temperature (Software Mode)" if is_sw_dim else "Color Temperature (VCP 0x14)",
                    model=Gtk.StringList.new([p["name"] for p in presets]),
                    tooltip_text=(
                        "Select white point via GPU Gamma LUT (6500K = neutral)."
                        if is_sw_dim
                        else "Select hardware color temperature preset."
                    ),
                )
                cur_ct = mon.get("color_temperature")
                for i, p in enumerate(presets):
                    if p["value"] == cur_ct:
                        ct_row.set_selected(i)
                        break
                ct_row.connect(
                    "notify::selected",
                    self._make_discrete_handler(mid, "color_temperature", presets, needs_confirm=False),
                )
                m_group.add(ct_row)

            if is_sw_dim:
                sw_reset_row = Adw.ActionRow(
                    title="Restore Default Picture Values",
                    subtitle="Brightness 100%, Contrast 50%, Color Temperature 6500K",
                    tooltip_text="Immediately resets brightness (100%), contrast (50%), and color temperature (6500K).",
                )
                sw_reset_btn = Gtk.Button(
                    label="Reset",
                    valign=Gtk.Align.CENTER,
                    tooltip_text="Reset software display values to defaults (100% / 50% / 6500K)",
                )

                def _on_sw_reset(
                    *_args: Any,
                    m_id: str = mid,
                    has_c: bool = bool(caps.get("contrast")),
                    has_ct: bool = bool(caps.get("color_temperature")),
                ) -> None:
                    def _work() -> dict[str, Any]:
                        res = self._bridge.set_feature(m_id, "brightness", 100, True, False)
                        if not res.get("ok"):
                            return res
                        if has_c:
                            res = self._bridge.set_feature(m_id, "contrast", 50, True, False)
                            if not res.get("ok"):
                                return res
                        if has_ct:
                            res = self._bridge.set_feature(
                                m_id, "color_temperature", 0x05, True, False
                            )
                        return res

                    def _done(res: dict[str, Any]) -> bool:
                        if res.get("ok"):
                            self._toast("Software picture settings restored to defaults.")
                        return self._on_op_done(res)

                    self._run_bg(_work, _done)

                sw_reset_btn.connect("clicked", _on_sw_reset)
                sw_reset_row.add_suffix(sw_reset_btn)
                sw_reset_row.activatable_widget = sw_reset_btn
                m_group.add(sw_reset_row)

            # Input Source (confirmed)
            if caps.get("input_source") and mon.get("available_inputs"):
                inputs = mon["available_inputs"]
                in_row = Adw.ComboRow(
                    title="Input Source (requires confirmation)",
                    model=Gtk.StringList.new([p["name"] for p in inputs]),
                )
                cur_in = mon.get("input_source")
                for i, p in enumerate(inputs):
                    if p["value"] == cur_in:
                        in_row.set_selected(i)
                        break
                in_row.connect(
                    "notify::selected",
                    self._make_discrete_handler(mid, "input_source", inputs, needs_confirm=True, mon_name=mon["display_name"]),
                )
                m_group.add(in_row)

            # Power State (confirmed)
            if caps.get("power_state") and mon.get("available_power_states"):
                pws = mon["available_power_states"]
                pw_row = Adw.ComboRow(
                    title="Power Mode (requires confirmation)",
                    model=Gtk.StringList.new([p["name"] for p in pws]),
                )
                cur_pw = mon.get("power_state")
                for i, p in enumerate(pws):
                    if p["value"] == cur_pw:
                        pw_row.set_selected(i)
                        break
                pw_row.connect(
                    "notify::selected",
                    self._make_discrete_handler(mid, "power_state", pws, needs_confirm=True, mon_name=mon["display_name"]),
                )
                m_group.add(pw_row)

            # Rotation (Wayland Mutter with 15s revert)
            if caps.get("rotation"):
                degs = [0, 90, 180, 270]
                rot_row = Adw.ComboRow(
                    title="Display Orientation",
                    model=Gtk.StringList.new(["0° (Normal)", "90°", "180°", "270°"]),
                )
                cur_rot = int(mon.get("rotation", 0))
                if cur_rot in degs:
                    rot_row.set_selected(degs.index(cur_rot))
                rot_row.connect(
                    "notify::selected",
                    lambda row, _pspec, m_id=mid: self._on_rotation_selected(
                        m_id, degs[row.get_selected()]
                    ),
                )
                m_group.add(rot_row)

            # Expander for Flyout Visibility + Diagnostics
            exp = Adw.ExpanderRow(
                title="Flyout Visibility &amp; VCP Diagnostics",
                subtitle="Choose which controls appear in the panel flyout.",
            )
            self._add_monitor_pref_switch(exp, mid, prefs, "hidden", "Hide monitor in flyout", False)
            if caps.get("contrast"):
                self._add_monitor_pref_switch(exp, mid, prefs, "show_contrast", "Show contrast in flyout", True)
            if caps.get("volume"):
                self._add_monitor_pref_switch(exp, mid, prefs, "show_volume", "Show volume in flyout", True)
            if caps.get("input_source"):
                self._add_monitor_pref_switch(exp, mid, prefs, "show_input_source", "Show input switch in flyout", True)
            if caps.get("color_temperature"):
                self._add_monitor_pref_switch(
                    exp,
                    mid,
                    prefs,
                    "show_color_temperature",
                    "Show color temperature in flyout",
                    False,
                    warn_title="Show Color Temperature in Flyout?",
                    warn_body=(
                        "Changing VCP 0x14 (Color Preset) may cause unexpected "
                        "color shifts on some monitors."
                    ),
                )
            if caps.get("power_state"):
                self._add_monitor_pref_switch(
                    exp,
                    mid,
                    prefs,
                    "show_power_state",
                    "Show monitor power state in flyout",
                    False,
                    warn_title="Enable Monitor Power Control in Flyout?",
                    warn_body=(
                        "Standby/Off (VCP 0xD6) turns off the display; not every monitor "
                        "wakes back up via DDC/CI."
                    ),
                )
            if caps.get("rotation"):
                self._add_monitor_pref_switch(exp, mid, prefs, "show_rotation", "Show rotation in flyout", True)

            diag_row = Adw.ActionRow(title="Inspect MCCS &amp; VCP Capabilities")
            diag_btn = Gtk.Button(label="Details", valign=Gtk.Align.CENTER)
            diag_btn.connect(
                "clicked",
                lambda *_a, m=mon: VcpCapabilitiesDialog(m).present(self),
            )
            diag_row.add_suffix(diag_btn)
            diag_row.activatable_widget = diag_btn
            exp.add_row(diag_row)
            m_group.add(exp)

            self._displays_container.append(m_group)

    def _make_slider_row(
        self,
        title: str,
        value: int,
        sensitive: bool,
        on_change,
        tooltip: str | None = None,
    ) -> Adw.ActionRow:
        row = Adw.ActionRow(title=title)
        if tooltip:
            row.set_tooltip_text(tooltip)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, valign=Gtk.Align.CENTER)
        adj = Gtk.Adjustment(value=float(value), lower=0.0, upper=100.0, step_increment=1.0, page_increment=10.0)
        scale = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL,
            adjustment=adj,
            draw_value=False,
            width_request=210,
            sensitive=sensitive,
        )
        if tooltip:
            scale.set_tooltip_text(tooltip)
        val_label = Gtk.Label(label=f"{value}%", width_chars=4, xalign=1.0)

        def _changed(sc: Gtk.Scale) -> None:
            v = round(sc.get_value())
            val_label.set_text(f"{v}%")
            if self._syncing:
                return
            on_change(v)

        scale.connect("value-changed", _changed)
        box.append(scale)
        box.append(val_label)
        row.add_suffix(box)
        return row

    def _make_discrete_handler(
        self,
        monitor_id: str,
        feature: str,
        options: list[dict[str, Any]],
        needs_confirm: bool,
        mon_name: str = "",
    ):
        def _handler(row: Adw.ComboRow, _pspec: Any) -> None:
            if self._syncing:
                return
            idx = row.get_selected()
            if idx < 0 or idx >= len(options):
                return
            opt = options[idx]
            val = int(opt["value"])

            if not needs_confirm or (feature == "power_state" and val == 1):
                self._run_bg(
                    lambda: self._bridge.set_feature(monitor_id, feature, val, True, False),
                    lambda res: self._on_op_done(res),
                )
                return

            label = opt["name"]
            heading = (
                f"Switch input on {mon_name}?"
                if feature == "input_source"
                else f"Set {mon_name} to '{label}'?"
            )
            body = (
                f"Video input will switch to {label}. This desktop display may disappear from screen."
                if feature == "input_source"
                else f"VCP 0xD6 will be set to '{label}'."
            )

            def _after_confirm(ok: bool) -> None:
                if not ok:
                    self._render_all()
                    return
                self._run_bg(
                    lambda: self._bridge.set_feature(monitor_id, feature, val, True, False),
                    lambda res: self._on_op_done(res),
                )

            show_confirm_dialog(
                self, heading=heading, body=body, confirm_label="Execute", destructive=True, on_result=_after_confirm
            )

        return _handler

    def _on_op_done(self, res: dict[str, Any]) -> bool:
        if not res.get("ok"):
            self._toast(res.get("message") or "Operation failed.")
        self._state = self._bridge.get_state()
        self._render_all()
        return False

    def _on_rotation_selected(self, monitor_id: str, degrees: int) -> None:
        if self._syncing:
            return

        def _done(res: dict[str, Any]) -> bool:
            if not res.get("ok"):
                self._toast(res.get("message") or "Rotation failed.")
                self._render_all()
                return False
            pending = res.get("data") or {}
            dlg = RotationRevertDialog(
                self,
                pending,
                on_confirm=lambda tok: self._run_bg(
                    lambda: self._bridge.confirm_rotation(tok),
                    lambda r: self._on_op_done(r),
                ),
                on_revert=lambda tok: self._run_bg(
                    lambda: self._bridge.revert_rotation(tok),
                    lambda r: self._on_op_done(r),
                ),
            )
            dlg.present(self)
            return False

        self._run_bg(lambda: self._bridge.request_rotation(monitor_id, degrees), _done)

    def _add_monitor_pref_switch(
        self,
        expander: Adw.ExpanderRow,
        monitor_id: str,
        prefs: dict[str, Any],
        key: str,
        title: str,
        default_val: bool,
        warn_title: str | None = None,
        warn_body: str | None = None,
    ) -> None:
        sw = Adw.SwitchRow(title=title, active=bool(prefs.get(key, default_val)))

        def _toggled(row: Adw.SwitchRow, _pspec: Any) -> None:
            if self._syncing:
                return
            active = row.get_active()
            if active and warn_title and warn_body:
                def _confirmed(ok: bool) -> None:
                    if not ok:
                        self._syncing = True
                        row.set_active(False)
                        self._syncing = False
                        return
                    self._save_monitor_pref(monitor_id, key, True)

                show_confirm_dialog(
                    self,
                    heading=warn_title,
                    body=warn_body,
                    confirm_label="Enable",
                    destructive=False,
                    on_result=_confirmed,
                )
            else:
                self._save_monitor_pref(monitor_id, key, active)

        sw.connect("notify::active", _toggled)
        expander.add_row(sw)

    def _save_monitor_pref(self, monitor_id: str, key: str, value: bool) -> None:
        settings = dict(self._state.get("settings", {}) or {})
        prefs_map = dict(settings.get("monitor_prefs", {}) or {})
        entry = dict(prefs_map.get(monitor_id, {}) or {})
        entry[key] = value
        prefs_map[monitor_id] = entry
        settings["monitor_prefs"] = prefs_map
        self._bridge.save_settings(settings)
        self._state = self._bridge.get_state()
        self._render_all()

    def _on_linked_toggled(self, row: Adw.SwitchRow, _pspec: Any) -> None:
        if self._syncing:
            return
        self._state = self._bridge.set_linked_mode(row.get_active())
        self._render_all()

    def _render_profiles_tab(self) -> None:
        self._clear_box(self._profiles_container)
        profiles = self._state.get("profiles", []) or []
        monitors = self._state.get("monitors", []) or []

        header_group = Adw.PreferencesGroup(
            title="Display Profiles",
            description="Saved combinations of brightness, contrast, volume, and color temperature.",
            margin_bottom=14,
        )
        new_row = Adw.ActionRow(title="Create New Profile")
        new_btn = Gtk.Button(label="New", valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
        new_btn.connect(
            "clicked",
            lambda *_: ProfileEditorDialog(
                monitors, None, self._on_profile_saved
            ).present(self),
        )
        new_row.add_suffix(new_btn)
        new_row.activatable_widget = new_btn
        header_group.add(new_row)
        self._profiles_container.append(header_group)

        if not profiles:
            self._profiles_container.append(
                Adw.StatusPage(
                    icon_name="starred-symbolic",
                    title="No Profiles Yet",
                    description="Create a profile to switch between work, gaming, or night setups in one click.",
                )
            )
            return

        list_group = Adw.PreferencesGroup(title="Saved Profiles (Order = Flyout Order)")
        for idx, prof in enumerate(profiles):
            pid = int(prof["id"])
            count = len(prof.get("monitor_settings", []))
            row = Adw.ActionRow(
                title=prof["name"],
                subtitle=f"ID {pid} · {count} monitor(s)",
            )
            btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, valign=Gtk.Align.CENTER)

            apply_btn = Gtk.Button(label="Apply", css_classes=["suggested-action"])
            apply_btn.connect(
                "clicked",
                lambda *_, p_id=pid: self._run_bg(
                    lambda: self._bridge.apply_profile(p_id),
                    self._on_profile_applied,
                ),
            )
            up_btn = Gtk.Button(icon_name="go-up-symbolic", tooltip_text="Move up", sensitive=idx > 0)
            up_btn.connect("clicked", lambda *_, p_id=pid: self._reorder_profile(p_id, -1))
            down_btn = Gtk.Button(icon_name="go-down-symbolic", tooltip_text="Move down", sensitive=idx < len(profiles) - 1)
            down_btn.connect("clicked", lambda *_, p_id=pid: self._reorder_profile(p_id, 1))
            edit_btn = Gtk.Button(icon_name="document-edit-symbolic", tooltip_text="Edit")
            edit_btn.connect(
                "clicked",
                lambda *_, p=prof: ProfileEditorDialog(
                    monitors, p, self._on_profile_saved
                ).present(self),
            )
            del_btn = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Delete")
            del_btn.connect("clicked", lambda *_, p_id=pid: self._delete_profile(p_id))

            for b in (apply_btn, up_btn, down_btn, edit_btn, del_btn):
                btn_box.append(b)
            row.add_suffix(btn_box)
            list_group.add(row)

        self._profiles_container.append(list_group)

    def _on_profile_saved(self, profile_dict: dict[str, Any]) -> None:
        self._bridge.save_profile(profile_dict)
        self._state = self._bridge.get_state()
        self._render_all()
        self._toast(f"Profile '{profile_dict['name']}' saved.")

    def _on_profile_applied(self, res: dict[str, Any]) -> bool:
        self._toast(res.get("message") or "Profile applied.")
        self._state = self._bridge.get_state()
        self._render_all()
        return False

    def _reorder_profile(self, pid: int, delta: int) -> None:
        self._bridge.reorder_profile(pid, delta)
        self._state = self._bridge.get_state()
        self._render_all()

    def _delete_profile(self, pid: int) -> None:
        self._bridge.delete_profile(pid)
        self._state = self._bridge.get_state()
        self._render_all()
        self._toast("Profile deleted.")

    def _render_settings_tab(self) -> None:
        self._clear_box(self._settings_container)
        settings = self._state.get("settings", {}) or {}
        profiles = self._state.get("profiles", []) or []
        monitors = self._state.get("monitors", []) or []

        # 1. General
        gen_group = Adw.PreferencesGroup(title="General", margin_bottom=16)
        delay_row = Adw.SpinRow.new_with_range(0.5, 10.0, 0.5)
        delay_row.set_digits(1)
        delay_row.set_title("Hotplug / Wake Rescan Delay (seconds)")
        delay_row.set_value(float(settings.get("refresh_delay_seconds", 1.5)))
        delay_row.connect(
            "notify::value",
            lambda r, _p: self._update_setting("refresh_delay_seconds", round(r.get_value(), 1)),
        )
        gen_group.add(delay_row)

        restore_row = Adw.SwitchRow(
            title="Restore last values on launch",
            subtitle="Off by default so monitors are not modified unexpectedly.",
            active=bool(settings.get("restore_on_launch", False)),
        )
        restore_row.connect(
            "notify::active",
            lambda r, _p: self._update_setting("restore_on_launch", r.get_active()),
        )
        gen_group.add(restore_row)

        sw_dim_row = Adw.SwitchRow(
            title="Software Dimming Fallback (Opt-in)",
            subtitle="Uses Mutter Gamma/CTM (brightness, contrast & color temp) for external monitors without DDC/CI.",
            active=bool(settings.get("software_dimming_fallback", False)),
            tooltip_text="Enables GPU-side brightness, contrast, and color temperature when DDC/CI is blocked by a hub or cable.",
        )
        sw_dim_row.connect(
            "notify::active",
            lambda r, _p: self._update_setting("software_dimming_fallback", r.get_active()),
        )
        gen_group.add(sw_dim_row)

        if self._state.get("demo"):
            autostart_row = Adw.SwitchRow(
                title="Start background daemon automatically",
                subtitle="Disabled in demo mode (manage via install-user.sh --autostart).",
                active=False,
                sensitive=False,
            )
        else:
            autostart_row = Adw.SwitchRow(
                title="Start background daemon automatically (XDG Autostart)",
                subtitle=f"Manages {AUTOSTART_FILENAME} in your user config directory.",
                active=_autostart_path().exists(),
            )
            autostart_row.connect("notify::active", self._on_autostart_toggled)
        gen_group.add(autostart_row)
        self._settings_container.append(gen_group)

        # 2. Panel & Flyout
        panel_group = Adw.PreferencesGroup(title="Panel Icon &amp; Flyout", margin_bottom=16)
        prof_sw_row = Adw.SwitchRow(
            title="Show profile switcher in flyout",
            active=bool(settings.get("show_profile_switcher", True)),
        )
        prof_sw_row.connect(
            "notify::active",
            lambda r, _p: self._update_setting("show_profile_switcher", r.get_active()),
        )
        panel_group.add(prof_sw_row)

        wheel_modes = ["disabled", "primary", "all"]
        wheel_row = Adw.ComboRow(
            title="Mouse Wheel on Panel Icon",
            subtitle="Optional: scroll over the status icon to step brightness.",
            model=Gtk.StringList.new(["Disabled", "Primary Display Only", "All Displays"]),
        )
        cur_w = settings.get("mouse_wheel_mode", "disabled")
        wheel_row.set_selected(wheel_modes.index(cur_w) if cur_w in wheel_modes else 0)
        wheel_row.connect(
            "notify::selected",
            lambda r, _p: self._update_setting("mouse_wheel_mode", wheel_modes[r.get_selected()]),
        )
        panel_group.add(wheel_row)

        step_row = Adw.SpinRow.new_with_range(1, 25, 1)
        step_row.set_title("Mouse Wheel Step Size (%)")
        step_row.set_value(float(settings.get("mouse_wheel_step", 5)))
        step_row.connect(
            "notify::value",
            lambda r, _p: self._update_setting("mouse_wheel_step", int(r.get_value())),
        )
        panel_group.add(step_row)
        self._settings_container.append(panel_group)

        # 3. GNOME Theme Integration (Opt-in)
        theme_group = Adw.PreferencesGroup(
            title="GNOME Light / Dark Theme Integration (Opt-in)",
            description="Automatically applies a profile when the GNOME color scheme changes.",
            margin_bottom=16,
        )
        theme_row = Adw.SwitchRow(
            title="Apply profile on system theme change",
            active=bool(settings.get("theme_profile_switching_enabled", False)),
        )
        theme_row.connect(
            "notify::active",
            lambda r, _p: self._update_setting(
                "theme_profile_switching_enabled", r.get_active()
            ),
        )
        theme_group.add(theme_row)

        prof_ids = [None] + [int(p["id"]) for p in profiles]
        prof_labels = ["No Profile"] + [p["name"] for p in profiles]

        for key, label in (
            ("light_mode_profile_id", "Light Mode Profile"),
            ("dark_mode_profile_id", "Dark Mode Profile"),
        ):
            p_row = Adw.ComboRow(title=label, model=Gtk.StringList.new(prof_labels))
            cur_pid = settings.get(key)
            if cur_pid in prof_ids:
                p_row.set_selected(prof_ids.index(cur_pid))
            p_row.connect(
                "notify::selected",
                lambda r, _p, k=key: self._update_setting(k, prof_ids[r.get_selected()]),
            )
            theme_group.add(p_row)
        self._settings_container.append(theme_group)

        # 4. Custom VCP Name Mappings
        map_group = Adw.PreferencesGroup(
            title="Custom VCP Labels",
            description="Custom names for video inputs (0x60) or color presets (0x14).",
            margin_bottom=16,
        )
        add_map_row = Adw.ActionRow(title="Add New VCP Label")
        add_map_btn = Gtk.Button(label="Add", valign=Gtk.Align.CENTER)
        add_map_btn.connect(
            "clicked",
            lambda *_: CustomVcpMappingDialog(monitors, self._on_mapping_added).present(self),
        )
        add_map_row.add_suffix(add_map_btn)
        add_map_row.activatable_widget = add_map_btn
        map_group.add(add_map_row)

        mappings = settings.get("custom_vcp_mappings", []) or []
        for idx, m in enumerate(mappings):
            scope = "All Monitors" if m.get("apply_to_all", True) else m.get("target_monitor_id", "")
            m_row = Adw.ActionRow(
                title=f"VCP 0x{int(m['vcp_code']):02X}: 0x{int(m['value']):02X} → {m['custom_name']}",
                subtitle=scope,
            )
            rm_btn = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
            rm_btn.connect("clicked", lambda *_, i=idx: self._remove_mapping(i))
            m_row.add_suffix(rm_btn)
            map_group.add(m_row)
        self._settings_container.append(map_group)

        # 5. System Diagnostics Info
        sys_group = Adw.PreferencesGroup(title="System Information")
        sys_group.add(Adw.ActionRow(title="Power Display for Linux (pdfl)", subtitle=f"Version {__version__}"))
        sys_group.add(Adw.ActionRow(title="Active Backend", subtitle=self._bridge.mode_label))
        self._settings_container.append(sys_group)

    def _update_setting(self, key: str, value: Any) -> None:
        if self._syncing:
            return
        settings = dict(self._state.get("settings", {}) or {})
        settings[key] = value
        self._bridge.save_settings(settings)
        self._state = self._bridge.get_state()

    def _on_mapping_added(self, mapping: dict[str, Any]) -> None:
        settings = dict(self._state.get("settings", {}) or {})
        items = list(settings.get("custom_vcp_mappings", []) or [])
        items.append(mapping)
        settings["custom_vcp_mappings"] = items
        self._bridge.save_settings(settings)
        self._state = self._bridge.get_state()
        self._render_all()
        self._toast("VCP label saved.")

    def _remove_mapping(self, index: int) -> None:
        settings = dict(self._state.get("settings", {}) or {})
        items = list(settings.get("custom_vcp_mappings", []) or [])
        if 0 <= index < len(items):
            items.pop(index)
            settings["custom_vcp_mappings"] = items
            self._bridge.save_settings(settings)
            self._state = self._bridge.get_state()
            self._render_all()

    def _on_autostart_toggled(self, row: Adw.SwitchRow, _pspec: Any) -> None:
        if self._syncing:
            return
        path = _autostart_path()
        if row.get_active():
            path.parent.mkdir(parents=True, exist_ok=True)
            exec_cmd = _resolve_daemon_exec()
            content = (
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Name=Power Display for Linux Daemon\n"
                "Comment=D-Bus background daemon for pdfl\n"
                f"Exec={exec_cmd}\n"
                f"Icon={APP_ID}\n"
                "X-GNOME-Autostart-enabled=true\n"
                "NoDisplay=true\n"
            )
            path.write_text(content, encoding="utf-8")
            self._toast("Autostart enabled.")
        else:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            self._toast("Autostart disabled.")
