"""Shared backend orchestrating monitors, profiles, rotation revert, and settings."""

from __future__ import annotations

import threading
import uuid
from copy import deepcopy
from typing import Any, Callable

from pdfl.drivers.backlight_driver import BacklightDriver
from pdfl.drivers.ddcutil_driver import DdcUtilDriver
from pdfl.drivers.demo_driver import DemoDriver
from pdfl.drivers.mutter_display import (
    SOFTWARE_COLOR_PRESET_VALUES,
    MutterDisplayClient,
)
from pdfl.models import (
    AppSettings,
    MonitorInfo,
    MonitorPreference,
    MonitorRuntimeHandle,
    OperationResult,
    Profile,
    ProfileMonitorSetting,
)
from pdfl.planners import (
    DEGREES_TO_MUTTER_TRANSFORM,
    DebouncedCoalescer,
    LinkedBrightnessPlanner,
    LinkTarget,
    WheelAdjustmentPlanner,
    WheelTarget,
    clamp_percent,
)
from pdfl.storage import MonitorStateStore, ProfileStore, SettingsStore
from pdfl.vcp_names import (
    VCP_COLOR_TEMP,
    VCP_CONTRAST,
    VCP_INPUT_SOURCE,
    VCP_POWER_MODE,
    VCP_VOLUME,
    get_value_name,
)


class PowerDisplayBackend:
    """Reusable core backend shared by D-Bus daemon, GTK4 GUI, and CLI."""

    def __init__(
        self,
        demo: bool = False,
        settings_store: SettingsStore | None = None,
        profile_store: ProfileStore | None = None,
        state_store: MonitorStateStore | None = None,
        mutter_client: MutterDisplayClient | None = None,
        backlight_driver: BacklightDriver | None = None,
        ddc_driver: DdcUtilDriver | None = None,
        demo_driver: DemoDriver | None = None,
        rotation_revert_seconds: float = 15.0,
        debounce_seconds: float = 0.2,
    ) -> None:
        self.demo = demo
        self._lock = threading.RLock()
        self._settings_store = settings_store or SettingsStore()
        self._profile_store = profile_store or ProfileStore()
        self._state_store = state_store or MonitorStateStore()

        self._mutter = mutter_client or MutterDisplayClient()
        self._demo_driver = demo_driver or (DemoDriver() if demo else None)
        self._backlight_driver = backlight_driver or BacklightDriver(
            mutter_client=self._mutter
        )
        self._ddc_driver = ddc_driver or DdcUtilDriver(mutter_client=self._mutter)

        self._rotation_revert_seconds = max(0.05, float(rotation_revert_seconds))
        self._coalescer = DebouncedCoalescer(delay_seconds=debounce_seconds)

        self._settings: AppSettings = self._settings_store.load()
        self._monitors: dict[str, MonitorInfo] = {}
        self._monitor_order: list[str] = []
        self._handles: dict[str, MonitorRuntimeHandle] = {}
        self._warnings: list[str] = []
        self._linked_master_brightness: int | None = None

        self._pending_rotation: dict[str, Any] | None = None
        self._rotation_timer: threading.Timer | None = None

        self._state_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._error_listeners: list[Callable[[str, str], None]] = []
        self._rotation_listeners: list[Callable[[dict[str, Any]], None]] = []

    def add_state_listener(self, cb: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            self._state_listeners.append(cb)

    def add_error_listener(self, cb: Callable[[str, str], None]) -> None:
        with self._lock:
            self._error_listeners.append(cb)

    def add_rotation_listener(self, cb: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            self._rotation_listeners.append(cb)

    def _emit_state(self) -> None:
        with self._lock:
            snapshot = self.get_state()
            listeners = list(self._state_listeners)
        for cb in listeners:
            try:
                cb(snapshot)
            except Exception:
                pass

    def _emit_error(self, monitor_id: str, message: str) -> None:
        with self._lock:
            listeners = list(self._error_listeners)
        for cb in listeners:
            try:
                cb(monitor_id, message)
            except Exception:
                pass

    def _emit_rotation_pending(self, pending: dict[str, Any]) -> None:
        with self._lock:
            listeners = list(self._rotation_listeners)
        for cb in listeners:
            try:
                cb(pending)
            except Exception:
                pass

    def initialize(self) -> dict[str, Any]:
        """Discover monitors and optionally apply saved state only if restore_on_launch=True."""
        return self.rescan(apply_startup_restore=True)

    def rescan(self, apply_startup_restore: bool = False) -> dict[str, Any]:
        with self._lock:
            self._settings = self._settings_store.load()
            warnings: list[str] = []
            discovered = []

            if self.demo:
                assert self._demo_driver is not None
                items, w = self._demo_driver.discover()
                discovered.extend(items)
                warnings.extend(w)
            else:
                mutter_state = self._mutter.get_current_state()
                if not mutter_state.available and mutter_state.error:
                    warnings.append(
                        f"Mutter DisplayConfig unavailable: {mutter_state.error}"
                    )
                bl_items, bl_w = self._backlight_driver.discover(mutter_state)
                ddc_items, ddc_w = self._ddc_driver.discover(mutter_state)
                discovered.extend(bl_items)
                discovered.extend(ddc_items)
                warnings.extend(bl_w)
                warnings.extend(ddc_w)

            self._monitors.clear()
            self._monitor_order.clear()
            self._handles.clear()

            for idx, item in enumerate(discovered, start=1):
                info = deepcopy(item.info)
                info.monitor_number = idx
                self._enrich_monitor(info)
                self._monitors[info.monitor_id] = info
                self._monitor_order.append(info.monitor_id)
                self._handles[info.monitor_id] = item.handle

            self._warnings = warnings
            self._reseed_linked_brightness_locked()

        if apply_startup_restore and self._settings.restore_on_launch:
            self._restore_saved_state()

        self._emit_state()
        return self.get_state()

    def _enrich_monitor(self, info: MonitorInfo) -> None:
        prefs = self._settings.monitor_prefs.get(info.monitor_id, MonitorPreference())
        info.hidden = prefs.hidden
        mappings = self._settings.custom_vcp_mappings

        if not self.demo and not info.is_builtin:
            if info.ddc_status not in ("available", "not_applicable"):
                if self._settings.software_dimming_fallback:
                    if not info.capabilities.brightness_writable or info.brightness_mode in (
                        "software_ctm",
                        "software_gamma",
                    ):
                        mode = self._mutter.probe_software_dimming_mode(info.connector)
                        if mode:
                            sw = self._mutter.get_software_display_state(info.connector)
                            info.capabilities.brightness = True
                            info.capabilities.brightness_writable = True
                            info.brightness_mode = mode
                            info.brightness = sw.brightness
                            info.capabilities.color_temperature = True
                            info.color_temperature = sw.color_temperature
                            info.available_color_presets = [
                                {
                                    "value": v,
                                    "name": get_value_name(
                                        VCP_COLOR_TEMP, v, mappings, info.monitor_id
                                    ),
                                }
                                for v in SOFTWARE_COLOR_PRESET_VALUES
                            ]
                            if mode == "software_gamma":
                                info.capabilities.contrast = True
                                info.contrast = sw.contrast
                                info.status_notice = (
                                    "Controlled in software mode (Mutter Gamma) — brightness, "
                                    "contrast, and color temperature active; DDC/CI unavailable "
                                    "on this port."
                                )
                            else:
                                info.status_notice = (
                                    "Controlled in software mode (Mutter CTM) — brightness and "
                                    "color temperature active; DDC/CI unavailable on this port."
                                )
                            info.status_level = "info"
                        else:
                            info.brightness_mode = "none"
                            info.status_level = "error" if info.last_error else "none"
                            info.status_notice = None
                else:
                    if info.brightness_mode in ("software_ctm", "software_gamma"):
                        self._mutter.reset_software_brightness(info.connector)
                        info.capabilities.brightness = False
                        info.capabilities.brightness_writable = False
                        info.capabilities.contrast = False
                        info.capabilities.color_temperature = False
                        info.brightness = None
                        info.contrast = None
                        info.color_temperature = None
                        info.available_color_presets = []
                    info.brightness_mode = "none"
                    info.status_level = "error" if info.last_error else "none"
                    info.status_notice = None
            else:
                info.brightness_mode = "hardware"
                info.status_level = "error" if info.last_error else "none"
                info.status_notice = None
        else:
            info.brightness_mode = "hardware"
            if info.status_level not in ("info", "error"):
                info.status_level = "error" if info.last_error else "none"

        if info.capabilities.input_source:
            vals = [x["value"] for x in info.available_inputs]
            if not vals and VCP_INPUT_SOURCE in info.capabilities.vcp_features:
                vals = info.capabilities.vcp_features[VCP_INPUT_SOURCE].supported_values
            info.available_inputs = [
                {
                    "value": int(v),
                    "name": get_value_name(
                        VCP_INPUT_SOURCE, int(v), mappings, info.monitor_id
                    ),
                }
                for v in vals
            ]
        if info.capabilities.color_temperature:
            vals = [x["value"] for x in info.available_color_presets]
            if not vals and VCP_COLOR_TEMP in info.capabilities.vcp_features:
                vals = info.capabilities.vcp_features[VCP_COLOR_TEMP].supported_values
            info.available_color_presets = [
                {
                    "value": int(v),
                    "name": get_value_name(
                        VCP_COLOR_TEMP, int(v), mappings, info.monitor_id
                    ),
                }
                for v in vals
            ]
        if info.capabilities.power_state:
            vals = [x["value"] for x in info.available_power_states]
            if not vals:
                vals = [0x01, 0x02, 0x04]
            info.available_power_states = [
                {
                    "value": int(v),
                    "name": get_value_name(
                        VCP_POWER_MODE, int(v), mappings, info.monitor_id
                    ),
                }
                for v in vals
            ]

    def _reseed_linked_brightness_locked(self) -> None:
        if not self._settings.linked_brightness:
            self._linked_master_brightness = None
            return
        targets = [
            LinkTarget(
                monitor_id=m.monitor_id,
                monitor_number=m.monitor_number,
                brightness=m.brightness,
            )
            for m in self._ordered_monitors_locked()
            if not m.hidden
            and m.capabilities.brightness
            and m.capabilities.brightness_writable
            and m.brightness is not None
        ]
        self._linked_master_brightness = LinkedBrightnessPlanner.seed(targets)

    def _ordered_monitors_locked(self) -> list[MonitorInfo]:
        return [self._monitors[mid] for mid in self._monitor_order if mid in self._monitors]

    def _restore_saved_state(self) -> None:
        saved = self._state_store.load_all()
        for mid, values in saved.items():
            mon = self._monitors.get(mid)
            if mon is None:
                continue
            for feat in ("brightness", "contrast", "volume", "color_temperature"):
                if feat in values:
                    self.set_feature(mid, feat, int(values[feat]), confirmed=True)

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            monitors = [m.to_dict() for m in self._ordered_monitors_locked()]
            profiles = [p.to_dict() for p in self._profile_store.list_profiles()]
            pending = dict(self._pending_rotation) if self._pending_rotation else None
            if pending is not None:
                pending.pop("previous_snapshot", None)
                pending.pop("active_modes", None)
            return {
                "demo": self.demo,
                "monitors": monitors,
                "warnings": list(self._warnings),
                "settings": self._settings.to_dict(),
                "profiles": profiles,
                "linked_brightness_value": self._linked_master_brightness,
                "pending_rotation": pending,
            }

    def get_settings(self) -> AppSettings:
        with self._lock:
            return deepcopy(self._settings)

    def save_settings(self, settings: AppSettings) -> AppSettings:
        with self._lock:
            old_linked = self._settings.linked_brightness
            old_sw = self._settings.software_dimming_fallback
            self._settings = self._settings_store.save(settings)
            for mon in self._monitors.values():
                self._enrich_monitor(mon)
            if (
                self._settings.linked_brightness != old_linked
                or self._settings.software_dimming_fallback != old_sw
            ):
                self._reseed_linked_brightness_locked()
        self._emit_state()
        return self.get_settings()

    def get_profiles(self) -> list[Profile]:
        return self._profile_store.list_profiles()

    def save_profile(self, profile: Profile) -> Profile:
        saved = self._profile_store.add_or_update(profile)
        self._emit_state()
        return saved

    def delete_profile(self, profile_id: int) -> bool:
        deleted = self._profile_store.delete(profile_id)
        if deleted:
            self._emit_state()
        return deleted

    def reorder_profile(self, profile_id: int, delta: int) -> list[Profile]:
        ordered = self._profile_store.reorder(profile_id, delta)
        self._emit_state()
        return ordered

    def set_linked_mode(self, enabled: bool) -> dict[str, Any]:
        with self._lock:
            self._settings.linked_brightness = bool(enabled)
            self._settings = self._settings_store.save(self._settings)
            self._reseed_linked_brightness_locked()
        self._emit_state()
        return self.get_state()

    def set_linked_brightness(
        self, percent: int, debounce: bool = False
    ) -> OperationResult:
        clamped = clamp_percent(percent)
        with self._lock:
            self._linked_master_brightness = clamped
            targets = [
                m.monitor_id
                for m in self._ordered_monitors_locked()
                if not m.hidden
                and m.capabilities.brightness
                and m.capabilities.brightness_writable
            ]
        if not targets:
            return OperationResult(
                ok=False,
                error_code="no_writable_targets",
                message="No writable displays available for linked brightness.",
            )
        errors: list[str] = []
        for mid in targets:
            res = self.set_feature(mid, "brightness", clamped, debounce=debounce)
            if not res.ok:
                errors.append(f"{mid}: {res.message}")
        if errors:
            return OperationResult(
                ok=False,
                error_code="partial_failure",
                message="; ".join(errors),
            )
        return OperationResult(
            ok=True,
            message=f"Linked brightness set to {clamped}%.",
            data={"brightness": clamped, "targets": targets},
        )

    def step_brightness(
        self, delta: int, mode_override: str | None = None
    ) -> OperationResult:
        with self._lock:
            mode = mode_override or self._settings.mouse_wheel_mode
            if mode == "disabled":
                return OperationResult(
                    ok=False,
                    error_code="wheel_disabled",
                    message="Panel icon mouse-wheel control is disabled.",
                )
            if self._settings.linked_brightness and self._linked_master_brightness is not None:
                next_master = clamp_percent(self._linked_master_brightness + int(delta))
                return self.set_linked_brightness(next_master, debounce=False)

            targets = [
                WheelTarget(
                    monitor_id=m.monitor_id,
                    is_primary=m.is_primary,
                    supports_brightness=m.capabilities.brightness,
                    brightness_writable=m.capabilities.brightness_writable,
                    hidden=m.hidden,
                    current_brightness=m.brightness,
                )
                for m in self._ordered_monitors_locked()
            ]
        plan = WheelAdjustmentPlanner.plan(mode, targets, delta)
        if not plan:
            return OperationResult(
                ok=False,
                error_code="no_eligible_targets",
                message="No eligible target monitor found for step adjustment.",
            )
        errors: list[str] = []
        for adj in plan:
            res = self.set_feature(adj.monitor_id, "brightness", adj.brightness)
            if not res.ok:
                errors.append(f"{adj.monitor_id}: {res.message}")
        if errors:
            return OperationResult(
                ok=False, error_code="step_failed", message="; ".join(errors)
            )
        return OperationResult(
            ok=True,
            message="Brightness adjusted.",
            data={"adjustments": [{"monitor_id": a.monitor_id, "brightness": a.brightness} for a in plan]},
        )

    def set_feature(
        self,
        monitor_id: str,
        feature: str,
        value: int,
        confirmed: bool = False,
        debounce: bool = False,
    ) -> OperationResult:
        with self._lock:
            mon = self._monitors.get(monitor_id)
            handle = self._handles.get(monitor_id)
            if mon is None or handle is None:
                return OperationResult(
                    ok=False,
                    error_code="monitor_not_found",
                    message=f"Unknown monitor: {monitor_id}",
                )

            caps = mon.capabilities
            if (
                feature
                in (
                    "brightness",
                    "contrast",
                    "volume",
                    "color_temperature",
                    "input_source",
                    "power_state",
                )
                and not mon.is_builtin
                and mon.ddc_status not in ("available", "not_applicable")
                and not getattr(caps, feature, False)
            ):
                err_code = (
                    mon.ddc_status
                    if mon.ddc_status.startswith("ddc_")
                    else f"ddc_{mon.ddc_status}"
                )
                return OperationResult(
                    ok=False,
                    error_code=err_code,
                    message=(
                        f"DDC/CI unavailable for {mon.display_name}: "
                        f"{mon.last_error or mon.ddc_status}"
                    ),
                )
            if feature == "brightness":
                if not caps.brightness:
                    return OperationResult(
                        ok=False,
                        error_code="unsupported_feature",
                        message=f"{mon.display_name} does not support brightness control.",
                    )
                if not caps.brightness_writable:
                    msg = mon.last_error or "Brightness is not writable on this system."
                    return OperationResult(
                        ok=False, error_code="not_writable", message=msg
                    )
                target_val = clamp_percent(value)
                raw_max = 100
                if 0x10 in caps.vcp_features:
                    raw_max = caps.vcp_features[0x10].raw_max
            elif feature == "contrast":
                if not caps.contrast:
                    return OperationResult(
                        ok=False,
                        error_code="unsupported_feature",
                        message=f"{mon.display_name} does not support contrast control.",
                    )
                target_val = clamp_percent(value)
                raw_max = caps.vcp_features.get(VCP_CONTRAST).raw_max if VCP_CONTRAST in caps.vcp_features else 100
            elif feature == "volume":
                if not caps.volume:
                    return OperationResult(
                        ok=False,
                        error_code="unsupported_feature",
                        message=f"{mon.display_name} does not support monitor speaker volume.",
                    )
                target_val = clamp_percent(value)
                raw_max = caps.vcp_features.get(VCP_VOLUME).raw_max if VCP_VOLUME in caps.vcp_features else 100
            elif feature == "color_temperature":
                if not caps.color_temperature:
                    return OperationResult(
                        ok=False,
                        error_code="unsupported_feature",
                        message=f"{mon.display_name} does not support color temperature presets.",
                    )
                allowed = {int(item["value"]) for item in mon.available_color_presets}
                if allowed and int(value) not in allowed:
                    return OperationResult(
                        ok=False,
                        error_code="invalid_value",
                        message=f"Color preset 0x{int(value):02X} is not supported.",
                    )
                target_val = int(value)
                raw_max = 100
            elif feature == "input_source":
                if not caps.input_source:
                    return OperationResult(
                        ok=False,
                        error_code="unsupported_feature",
                        message=f"{mon.display_name} does not support input switching.",
                    )
                allowed = {int(item["value"]) for item in mon.available_inputs}
                if allowed and int(value) not in allowed:
                    return OperationResult(
                        ok=False,
                        error_code="invalid_value",
                        message=f"Input source 0x{int(value):02X} is not supported.",
                    )
                target_val = int(value)
                if not confirmed:
                    label = get_value_name(
                        VCP_INPUT_SOURCE,
                        target_val,
                        self._settings.custom_vcp_mappings,
                        monitor_id,
                    )
                    return OperationResult(
                        ok=False,
                        requires_confirmation=True,
                        error_code="confirmation_required",
                        message=f"Really switch input on {mon.display_name} to {label}?",
                        data={"feature": feature, "value": target_val, "label": label},
                    )
                raw_max = 100
            elif feature == "power_state":
                if not caps.power_state:
                    return OperationResult(
                        ok=False,
                        error_code="unsupported_feature",
                        message=f"{mon.display_name} does not support power state control.",
                    )
                allowed = {int(item["value"]) for item in mon.available_power_states}
                if allowed and int(value) not in allowed:
                    return OperationResult(
                        ok=False,
                        error_code="invalid_value",
                        message=f"Power mode 0x{int(value):02X} is not supported.",
                    )
                target_val = int(value)
                if target_val != 0x01 and not confirmed:
                    label = get_value_name(
                        VCP_POWER_MODE,
                        target_val,
                        self._settings.custom_vcp_mappings,
                        monitor_id,
                    )
                    return OperationResult(
                        ok=False,
                        requires_confirmation=True,
                        error_code="confirmation_required",
                        message=f"Really set {mon.display_name} to '{label}' mode?",
                        data={"feature": feature, "value": target_val, "label": label},
                    )
                raw_max = 100
            else:
                return OperationResult(
                    ok=False,
                    error_code="unknown_feature",
                    message=f"Unknown feature: {feature}",
                )

        if debounce and feature in ("brightness", "contrast", "volume"):
            # Update cached UI state immediately for smooth response, coalesce hardware write
            self._apply_cached_feature(monitor_id, feature, target_val)
            self._coalescer.schedule(
                f"{monitor_id}:{feature}",
                lambda: self._execute_hardware_write(
                    monitor_id, handle, feature, target_val, raw_max
                ),
            )
            return OperationResult(
                ok=True,
                message="Update scheduled.",
                data={"value": target_val, "debounced": True},
            )

        return self._execute_hardware_write(
            monitor_id, handle, feature, target_val, raw_max
        )

    def _apply_cached_feature(self, monitor_id: str, feature: str, value: int) -> None:
        with self._lock:
            mon = self._monitors.get(monitor_id)
            if mon is None:
                return
            setattr(mon, feature, value)

    def _execute_hardware_write(
        self,
        monitor_id: str,
        handle: MonitorRuntimeHandle,
        feature: str,
        target_val: int,
        raw_max: int,
    ) -> OperationResult:
        with self._lock:
            cur_mon = self._monitors.get(monitor_id)
            brightness_mode = cur_mon.brightness_mode if cur_mon else "hardware"

        if (
            feature in ("brightness", "contrast", "color_temperature")
            and brightness_mode in ("software_ctm", "software_gamma")
        ):
            res = self._mutter.set_software_display_feature(
                handle.connector, feature, target_val
            )
        elif handle.driver_kind == "demo":
            assert self._demo_driver is not None
            res = self._demo_driver.set_feature(handle, feature, target_val)
        elif handle.driver_kind == "backlight":
            res = self._backlight_driver.set_feature(handle, feature, target_val)
        elif handle.driver_kind == "ddcutil":
            res = self._ddc_driver.set_feature(
                handle, feature, target_val, raw_max=raw_max
            )
        else:
            res = OperationResult(
                ok=False, error_code="unknown_driver", message="Unknown display driver."
            )

        with self._lock:
            mon = self._monitors.get(monitor_id)
            if mon is not None:
                if res.ok:
                    setattr(mon, feature, target_val)
                    if mon.brightness_mode in ("software_ctm", "software_gamma"):
                        mon.status_level = "info"
                    else:
                        mon.last_error = None
                        mon.status_level = "none"
                        mon.status_notice = None
                else:
                    mon.last_error = res.message
                    mon.status_level = "error"

        if res.ok:
            if feature in ("brightness", "contrast", "volume", "color_temperature"):
                self._state_store.update_monitor(monitor_id, {feature: target_val})
            self._emit_state()
        else:
            self._emit_error(monitor_id, res.message)
            self._emit_state()

        return res

    def request_rotation(self, monitor_id: str, degrees: int) -> OperationResult:
        if degrees not in DEGREES_TO_MUTTER_TRANSFORM:
            return OperationResult(
                ok=False,
                error_code="invalid_rotation",
                message=f"Invalid rotation: {degrees}° (allowed: 0, 90, 180, 270).",
            )
        with self._lock:
            mon = self._monitors.get(monitor_id)
            if mon is None:
                return OperationResult(
                    ok=False,
                    error_code="monitor_not_found",
                    message=f"Unknown monitor: {monitor_id}",
                )
            if not mon.capabilities.rotation:
                return OperationResult(
                    ok=False,
                    error_code="unsupported_feature",
                    message=f"{mon.display_name} does not support rotation.",
                )
            previous_degrees = mon.rotation
            if self._rotation_timer is not None:
                self._rotation_timer.cancel()
                self._rotation_timer = None

            if self.demo:
                assert self._demo_driver is not None
                apply_res = self._demo_driver.set_rotation(monitor_id, degrees)
            else:
                apply_res = self._mutter.apply_rotation(mon.connector, degrees, method=1)

            if not apply_res.ok:
                mon.last_error = apply_res.message
                self._emit_error(monitor_id, apply_res.message)
                return apply_res

            mon.rotation = degrees
            token = uuid.uuid4().hex[:12]
            pending: dict[str, Any] = {
                "token": token,
                "monitor_id": monitor_id,
                "display_name": mon.display_name,
                "connector": mon.connector,
                "previous_degrees": previous_degrees,
                "target_degrees": degrees,
                "timeout_seconds": self._rotation_revert_seconds,
                "previous_snapshot": apply_res.data.get("previous_snapshot"),
                "active_modes": apply_res.data.get("active_modes"),
            }
            self._pending_rotation = pending
            timer = threading.Timer(
                self._rotation_revert_seconds,
                self._auto_revert_rotation,
                args=(token,),
            )
            timer.daemon = True
            self._rotation_timer = timer
            timer.start()

            public_pending = {
                k: v
                for k, v in pending.items()
                if k not in ("previous_snapshot", "active_modes")
            }

        self._emit_rotation_pending(public_pending)
        self._emit_state()
        return OperationResult(
            ok=True,
            message=(
                f"Rotation set to {degrees}° temporarily. "
                f"Please confirm within {self._rotation_revert_seconds:.0f}s."
            ),
            data=public_pending,
        )

    def _auto_revert_rotation(self, token: str) -> None:
        self.revert_rotation(token)

    def confirm_rotation(self, token: str) -> OperationResult:
        with self._lock:
            pending = self._pending_rotation
            if pending is None or pending["token"] != token:
                return OperationResult(
                    ok=False,
                    error_code="no_pending_rotation",
                    message="No matching pending rotation to confirm.",
                )
            if self._rotation_timer is not None:
                self._rotation_timer.cancel()
                self._rotation_timer = None
            connector = pending["connector"]
            target_degrees = int(pending["target_degrees"])
            self._pending_rotation = None

        if not self.demo:
            res = self._mutter.apply_rotation(connector, target_degrees, method=2)
            if not res.ok:
                self._emit_error(pending["monitor_id"], res.message)
                self._emit_state()
                return res

        self._emit_state()
        return OperationResult(
            ok=True, message=f"Rotation ({target_degrees}°) confirmed permanently."
        )

    def revert_rotation(self, token: str) -> OperationResult:
        with self._lock:
            pending = self._pending_rotation
            if pending is None or pending["token"] != token:
                return OperationResult(
                    ok=False,
                    error_code="no_pending_rotation",
                    message="No matching pending rotation to revert.",
                )
            if self._rotation_timer is not None:
                self._rotation_timer.cancel()
                self._rotation_timer = None
            self._pending_rotation = None
            monitor_id = pending["monitor_id"]
            prev_degrees = int(pending["previous_degrees"])
            snap = pending.get("previous_snapshot")
            modes = pending.get("active_modes")

        if self.demo:
            assert self._demo_driver is not None
            res = self._demo_driver.set_rotation(monitor_id, prev_degrees)
        elif snap and modes:
            res = self._mutter.restore_logical_snapshot(snap, modes, method=1)
        else:
            res = self._mutter.apply_rotation(pending["connector"], prev_degrees, method=1)

        with self._lock:
            mon = self._monitors.get(monitor_id)
            if mon is not None and res.ok:
                mon.rotation = prev_degrees

        self._emit_state()
        return res

    def apply_profile(self, profile_id_or_name: int | str) -> OperationResult:
        profile = self._profile_store.find_by_name_or_id(str(profile_id_or_name))
        if profile is None:
            return OperationResult(
                ok=False,
                error_code="profile_not_found",
                message=f"Profile '{profile_id_or_name}' was not found.",
            )

        applied_monitors: list[str] = []
        skipped: list[str] = []
        errors: list[str] = []

        for entry in profile.monitor_settings:
            with self._lock:
                mon = self._monitors.get(entry.monitor_id)
            if mon is None:
                skipped.append(f"{entry.monitor_id} (not connected)")
                continue

            did_any = False
            for feat in ("brightness", "contrast", "volume", "color_temperature"):
                val = getattr(entry, feat)
                if val is None:
                    continue
                if not getattr(mon.capabilities, feat, False):
                    skipped.append(f"{mon.display_name}:{feat} (unsupported)")
                    continue
                res = self.set_feature(mon.monitor_id, feat, int(val), confirmed=True)
                if res.ok:
                    did_any = True
                else:
                    errors.append(f"{mon.display_name}:{feat}: {res.message}")
            if did_any:
                applied_monitors.append(mon.display_name)

        if errors:
            return OperationResult(
                ok=False,
                error_code="profile_partial_error",
                message="; ".join(errors),
                data={
                    "profile_id": profile.id,
                    "profile_name": profile.name,
                    "applied": applied_monitors,
                    "skipped": skipped,
                },
            )
        return OperationResult(
            ok=True,
            message=f"Applied profile '{profile.name}'.",
            data={
                "profile_id": profile.id,
                "profile_name": profile.name,
                "applied": applied_monitors,
                "skipped": skipped,
            },
        )

    def on_system_theme_changed(self, is_dark: bool) -> OperationResult | None:
        """Apply configured light/dark profile only when user opted in."""
        with self._lock:
            settings = self._settings
            if not settings.theme_profile_switching_enabled:
                return None
            target_id = (
                settings.dark_mode_profile_id
                if is_dark
                else settings.light_mode_profile_id
            )
        if target_id is None:
            return None
        return self.apply_profile(target_id)

    def shutdown(self) -> None:
        self._coalescer.cancel_all()
        with self._lock:
            if self._rotation_timer is not None:
                self._rotation_timer.cancel()
                self._rotation_timer = None
