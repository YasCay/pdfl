"""Adwaita dialogs for profiles, VCP diagnostics, custom mappings, confirmations, and rotation revert."""

from __future__ import annotations

from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk


def show_confirm_dialog(
    parent: Gtk.Widget,
    heading: str,
    body: str,
    confirm_label: str = "Confirm",
    destructive: bool = False,
    on_result: Callable[[bool], None] | None = None,
) -> None:
    """Present an Adw.AlertDialog and invoke on_result(True/False)."""
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("confirm", confirm_label)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")
    if destructive:
        dialog.set_response_appearance("confirm", Adw.ResponseAppearance.DESTRUCTIVE)
    else:
        dialog.set_response_appearance("confirm", Adw.ResponseAppearance.SUGGESTED)

    def _on_choose(dlg: Adw.AlertDialog, res: Any) -> None:
        try:
            choice = dlg.choose_finish(res)
        except Exception:
            choice = "cancel"
        if on_result is not None:
            on_result(choice == "confirm")

    dialog.choose(parent, None, _on_choose)


class RotationRevertDialog:
    """Countdown Adw.AlertDialog to confirm or revert a temporary Wayland rotation."""

    def __init__(
        self,
        parent: Gtk.Widget,
        pending: dict[str, Any],
        on_confirm: Callable[[str], None],
        on_revert: Callable[[str], None],
    ) -> None:
        self._token = str(pending["token"])
        self._remaining = max(1, round(float(pending.get("timeout_seconds", 15))))
        self._on_confirm = on_confirm
        self._on_revert = on_revert
        self._handled = False
        self._timer_id = 0

        deg = pending.get("target_degrees", 0)
        name = pending.get("display_name", "Monitor")
        self._dialog = Adw.AlertDialog(
            heading=f"Keep rotation for {name} ({deg}°)?",
            body=self._body_text(),
        )
        self._dialog.add_response("revert", "Revert Now")
        self._dialog.add_response("keep", "Keep Changes")
        self._dialog.set_default_response("revert")
        self._dialog.set_close_response("revert")
        self._dialog.set_response_appearance("keep", Adw.ResponseAppearance.SUGGESTED)

    def _body_text(self) -> str:
        return (
            f"If the display looks right, confirm this change.\n"
            f"Reverting automatically in {self._remaining} seconds."
        )

    def present(self, parent: Gtk.Widget) -> None:
        self._timer_id = GLib.timeout_add_seconds(1, self._tick)
        self._dialog.choose(parent, None, self._on_response)

    def _tick(self) -> bool:
        if self._handled:
            self._timer_id = 0
            return False
        self._remaining -= 1
        if self._remaining <= 0:
            self._timer_id = 0
            self._handled = True
            try:
                self._dialog.force_close()
            except Exception:
                pass
            return False
        self._dialog.set_body(self._body_text())
        return True

    def _on_response(self, dlg: Adw.AlertDialog, res: Any) -> None:
        if self._timer_id:
            GLib.source_remove(self._timer_id)
            self._timer_id = 0
        if self._handled:
            return
        self._handled = True
        try:
            choice = dlg.choose_finish(res)
        except Exception:
            choice = "revert"
        if choice == "keep":
            self._on_confirm(self._token)
        else:
            self._on_revert(self._token)


class VcpCapabilitiesDialog(Adw.Dialog):
    """Diagnostics dialog showing monitor identity, raw MCCS string, and VCP codes."""

    def __init__(self, monitor: dict[str, Any]) -> None:
        super().__init__(
            title=f"VCP Diagnostics — {monitor.get('display_name', '')}",
            content_width=640,
            content_height=540,
        )
        caps = monitor.get("capabilities", {}) or {}

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())

        scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        page = Adw.PreferencesPage()

        id_group = Adw.PreferencesGroup(title="Monitor Identification")
        bus_num = monitor.get("i2c_bus")
        bus_display = f"/dev/i2c-{bus_num}" if bus_num is not None else "—"
        rows = [
            ("Stable Monitor ID", monitor.get("monitor_id", "")),
            ("Connector", monitor.get("connector") or "—"),
            ("DRM Connector", monitor.get("drm_connector") or "—"),
            ("I2C Bus", bus_display),
            ("DDC/CI Status", monitor.get("ddc_status") or "—"),
            ("Vendor / Model", f"{monitor.get('manufacturer') or '—'} / {monitor.get('model') or '—'}"),
            ("Serial Number", monitor.get("serial") or "—"),
            ("Driver", monitor.get("driver") or "—"),
            ("MCCS Version", caps.get("mccs_version") or "—"),
        ]
        for title, val in rows:
            row = Adw.ActionRow(title=title, subtitle=str(val), subtitle_selectable=True)
            id_group.add(row)
        page.add(id_group)

        raw_str = caps.get("raw_capabilities") or "(No MCCS capabilities string reported)"
        raw_group = Adw.PreferencesGroup(title="MCCS Capabilities String")
        raw_view = Gtk.TextView(
            editable=False,
            cursor_visible=False,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            top_margin=8,
            bottom_margin=8,
            left_margin=10,
            right_margin=10,
        )
        raw_view.get_buffer().set_text(raw_str)
        raw_frame = Gtk.Frame(child=raw_view)
        raw_group.add(raw_frame)
        page.add(raw_group)

        vcp_map = caps.get("vcp_features", {}) or {}
        vcp_group = Adw.PreferencesGroup(
            title=f"Detected VCP Codes ({len(vcp_map)})"
        )
        if not vcp_map:
            vcp_group.add(
                Adw.ActionRow(
                    title="No VCP table available",
                    subtitle="Built-in display or monitor without MCCS capabilities.",
                )
            )
        else:
            for code_key in sorted(vcp_map.keys(), key=lambda k: int(k)):
                spec = vcp_map[code_key]
                code_int = int(spec["code"])
                vals = spec.get("supported_values") or []
                vals_txt = (
                    ", ".join(f"0x{int(v):02X}" for v in vals)
                    if vals
                    else f"Continuous (max={spec.get('raw_max', 100)}, current={spec.get('current_raw')})"
                )
                vcp_group.add(
                    Adw.ActionRow(
                        title=f"0x{code_int:02X} — {spec.get('name', '')}",
                        subtitle=vals_txt,
                        subtitle_selectable=True,
                    )
                )
        page.add(vcp_group)

        scroll.set_child(page)
        toolbar.set_content(scroll)
        self.set_child(toolbar)


class CustomVcpMappingDialog(Adw.Dialog):
    """Dialog to create a custom name mapping for a discrete VCP value (0x14 / 0x60)."""

    def __init__(
        self,
        monitors: list[dict[str, Any]],
        on_save: Callable[[dict[str, Any]], None],
    ) -> None:
        super().__init__(
            title="Custom VCP Label",
            content_width=440,
            content_height=380,
        )
        self._monitors = monitors
        self._on_save = on_save

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda *_: self.close())
        save_btn = Gtk.Button(label="Save", css_classes=["suggested-action"])
        save_btn.connect("clicked", self._handle_save)
        header.pack_start(cancel_btn)
        header.pack_end(save_btn)
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup()

        self._code_choices = [0x60, 0x14, 0xD6]
        self._code_row = Adw.ComboRow(
            title="VCP Code",
            model=Gtk.StringList.new(
                [
                    "0x60 — Input Source",
                    "0x14 — Color Preset",
                    "0xD6 — Power Mode",
                ]
            ),
        )
        group.add(self._code_row)

        self._value_row = Adw.EntryRow(title="VCP Value (e.g. 0x11 or 17)")
        self._value_row.set_text("0x11")
        group.add(self._value_row)

        self._name_row = Adw.EntryRow(title="Display Label (e.g. Work Laptop)")
        group.add(self._name_row)

        self._scope_ids = [""] + [m["monitor_id"] for m in monitors]
        scope_labels = ["All Monitors (Global)"] + [
            m["display_name"] for m in monitors
        ]
        self._scope_row = Adw.ComboRow(
            title="Scope",
            model=Gtk.StringList.new(scope_labels),
        )
        group.add(self._scope_row)

        self._error_label = Gtk.Label(
            label="",
            css_classes=["error"],
            visible=False,
            margin_top=6,
        )
        page.add(group)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(page)
        box.append(self._error_label)
        toolbar.set_content(box)
        self.set_child(toolbar)

    def _handle_save(self, *_args: Any) -> None:
        raw_val = self._value_row.get_text().strip()
        name = self._name_row.get_text().strip()
        try:
            parsed_val = int(raw_val, 0)
            if parsed_val < 0 or parsed_val > 0xFFFF:
                raise ValueError
        except ValueError:
            self._error_label.set_text("Invalid VCP value (decimal or 0xHH expected).")
            self._error_label.set_visible(True)
            return
        if not name:
            self._error_label.set_text("Please enter a display label.")
            self._error_label.set_visible(True)
            return

        code = self._code_choices[self._code_row.get_selected()]
        scope_idx = self._scope_row.get_selected()
        target_id = self._scope_ids[scope_idx] if scope_idx < len(self._scope_ids) else ""

        self._on_save(
            {
                "vcp_code": code,
                "value": parsed_val,
                "custom_name": name,
                "apply_to_all": target_id == "",
                "target_monitor_id": target_id,
            }
        )
        self.close()


class ProfileEditorDialog(Adw.Dialog):
    """Create or edit a multi-monitor display profile."""

    def __init__(
        self,
        monitors: list[dict[str, Any]],
        existing: dict[str, Any] | None,
        on_save: Callable[[dict[str, Any]], None],
    ) -> None:
        is_edit = existing is not None
        super().__init__(
            title="Edit Profile" if is_edit else "New Profile",
            content_width=560,
            content_height=580,
        )
        self._monitors = monitors
        self._existing = existing
        self._on_save = on_save
        self._widgets: dict[str, dict[str, Any]] = {}

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda *_: self.close())
        save_btn = Gtk.Button(label="Save", css_classes=["suggested-action"])
        save_btn.connect("clicked", self._handle_save)
        header.pack_start(cancel_btn)
        header.pack_end(save_btn)
        toolbar.add_top_bar(header)

        scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        page = Adw.PreferencesPage()

        name_group = Adw.PreferencesGroup()
        self._name_row = Adw.EntryRow(title="Profile Name")
        if existing:
            self._name_row.set_text(existing.get("name", ""))
        name_group.add(self._name_row)

        capture_row = Adw.ActionRow(
            title="Capture Current Monitor Values",
            subtitle="Copies current slider values from all detected displays.",
        )
        capture_btn = Gtk.Button(label="Capture", valign=Gtk.Align.CENTER)
        capture_btn.connect("clicked", self._capture_current)
        capture_row.add_suffix(capture_btn)
        name_group.add(capture_row)
        page.add(name_group)

        existing_map: dict[str, dict[str, Any]] = {}
        if existing:
            for item in existing.get("monitor_settings", []):
                existing_map[item["monitor_id"]] = item

        for mon in monitors:
            mid = mon["monitor_id"]
            caps = mon.get("capabilities", {}) or {}
            saved_entry = existing_map.get(mid)

            m_group = Adw.PreferencesGroup(title=mon["display_name"])
            include_row = Adw.SwitchRow(
                title="Include in profile",
                active=saved_entry is not None or existing is None,
            )
            m_group.add(include_row)

            w_dict: dict[str, Any] = {"include": include_row, "monitor": mon}

            if caps.get("brightness"):
                b_val = (
                    saved_entry.get("brightness")
                    if saved_entry and saved_entry.get("brightness") is not None
                    else (mon.get("brightness") if mon.get("brightness") is not None else 70)
                )
                b_row = Adw.SpinRow.new_with_range(0, 100, 1)
                b_row.set_title("Brightness (%)")
                b_row.set_value(float(b_val))
                m_group.add(b_row)
                w_dict["brightness"] = b_row

            if caps.get("contrast"):
                c_val = (
                    saved_entry.get("contrast")
                    if saved_entry and saved_entry.get("contrast") is not None
                    else (mon.get("contrast") if mon.get("contrast") is not None else 50)
                )
                c_row = Adw.SpinRow.new_with_range(0, 100, 1)
                c_row.set_title("Contrast (%)")
                c_row.set_value(float(c_val))
                m_group.add(c_row)
                w_dict["contrast"] = c_row

            if caps.get("volume"):
                v_val = (
                    saved_entry.get("volume")
                    if saved_entry and saved_entry.get("volume") is not None
                    else (mon.get("volume") if mon.get("volume") is not None else 30)
                )
                v_row = Adw.SpinRow.new_with_range(0, 100, 1)
                v_row.set_title("Volume (%)")
                v_row.set_value(float(v_val))
                m_group.add(v_row)
                w_dict["volume"] = v_row

            if caps.get("color_temperature") and mon.get("available_color_presets"):
                presets = mon["available_color_presets"]
                labels = ["Unchanged"] + [p["name"] for p in presets]
                preset_vals = [None] + [int(p["value"]) for p in presets]
                ct_row = Adw.ComboRow(
                    title="Color Temperature",
                    model=Gtk.StringList.new(labels),
                )
                target_ct = saved_entry.get("color_temperature") if saved_entry else None
                if target_ct in preset_vals:
                    ct_row.set_selected(preset_vals.index(target_ct))
                m_group.add(ct_row)
                w_dict["color_temperature"] = (ct_row, preset_vals)

            self._widgets[mid] = w_dict
            page.add(m_group)

        self._err_label = Gtk.Label(label="", css_classes=["error"], visible=False)
        scroll.set_child(page)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(scroll)
        box.append(self._err_label)
        toolbar.set_content(box)
        self.set_child(toolbar)

    def _capture_current(self, *_args: Any) -> None:
        for w_dict in self._widgets.values():
            mon = w_dict["monitor"]
            if "brightness" in w_dict and mon.get("brightness") is not None:
                w_dict["brightness"].set_value(float(mon["brightness"]))
            if "contrast" in w_dict and mon.get("contrast") is not None:
                w_dict["contrast"].set_value(float(mon["contrast"]))
            if "volume" in w_dict and mon.get("volume") is not None:
                w_dict["volume"].set_value(float(mon["volume"]))
            if "color_temperature" in w_dict and mon.get("color_temperature") is not None:
                ct_row, preset_vals = w_dict["color_temperature"]
                cur = int(mon["color_temperature"])
                if cur in preset_vals:
                    ct_row.set_selected(preset_vals.index(cur))

    def _handle_save(self, *_args: Any) -> None:
        name = self._name_row.get_text().strip()
        if not name:
            self._err_label.set_text("Please enter a profile name.")
            self._err_label.set_visible(True)
            return

        entries: list[dict[str, Any]] = []
        for mid, w_dict in self._widgets.items():
            if not w_dict["include"].get_active():
                continue
            entry: dict[str, Any] = {
                "monitor_id": mid,
                "brightness": int(w_dict["brightness"].get_value())
                if "brightness" in w_dict
                else None,
                "contrast": int(w_dict["contrast"].get_value())
                if "contrast" in w_dict
                else None,
                "volume": int(w_dict["volume"].get_value())
                if "volume" in w_dict
                else None,
                "color_temperature": None,
            }
            if "color_temperature" in w_dict:
                ct_row, preset_vals = w_dict["color_temperature"]
                idx = ct_row.get_selected()
                if 0 <= idx < len(preset_vals):
                    entry["color_temperature"] = preset_vals[idx]
            entries.append(entry)

        if not entries:
            self._err_label.set_text("Please include at least one monitor.")
            self._err_label.set_visible(True)
            return

        payload = {
            "id": int(self._existing["id"]) if self._existing else 0,
            "name": name,
            "monitor_settings": entries,
        }
        self._on_save(payload)
        self.close()
