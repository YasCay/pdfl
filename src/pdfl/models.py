"""Core data models for Power Display for Linux (pdfl)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class VcpFeatureSpec:
    """Metadata and supported discrete values for a single MCCS VCP code."""

    code: int
    name: str
    supported_values: list[int] = field(default_factory=list)
    raw_max: int = 100
    current_raw: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "supported_values": list(self.supported_values),
            "raw_max": self.raw_max,
            "current_raw": self.current_raw,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VcpFeatureSpec:
        return cls(
            code=int(data["code"]),
            name=str(data.get("name", "")),
            supported_values=[int(v) for v in data.get("supported_values", [])],
            raw_max=int(data.get("raw_max", 100)),
            current_raw=(
                int(data["current_raw"])
                if data.get("current_raw") is not None
                else None
            ),
        )


@dataclass
class MonitorCapabilities:
    """Verified capabilities reported by the monitor or display stack."""

    brightness: bool = False
    contrast: bool = False
    volume: bool = False
    input_source: bool = False
    color_temperature: bool = False
    power_state: bool = False
    rotation: bool = False
    brightness_writable: bool = False
    raw_capabilities: str = ""
    mccs_version: str = ""
    vcp_features: dict[int, VcpFeatureSpec] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "brightness": self.brightness,
            "contrast": self.contrast,
            "volume": self.volume,
            "input_source": self.input_source,
            "color_temperature": self.color_temperature,
            "power_state": self.power_state,
            "rotation": self.rotation,
            "brightness_writable": self.brightness_writable,
            "raw_capabilities": self.raw_capabilities,
            "mccs_version": self.mccs_version,
            "vcp_features": {
                str(code): spec.to_dict()
                for code, spec in sorted(self.vcp_features.items())
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MonitorCapabilities:
        features_raw = data.get("vcp_features", {}) or {}
        vcp_features: dict[int, VcpFeatureSpec] = {}
        for key, spec_dict in features_raw.items():
            code = int(key, 0) if isinstance(key, str) else int(key)
            vcp_features[code] = VcpFeatureSpec.from_dict(spec_dict)
        return cls(
            brightness=bool(data.get("brightness", False)),
            contrast=bool(data.get("contrast", False)),
            volume=bool(data.get("volume", False)),
            input_source=bool(data.get("input_source", False)),
            color_temperature=bool(data.get("color_temperature", False)),
            power_state=bool(data.get("power_state", False)),
            rotation=bool(data.get("rotation", False)),
            brightness_writable=bool(data.get("brightness_writable", False)),
            raw_capabilities=str(data.get("raw_capabilities", "")),
            mccs_version=str(data.get("mccs_version", "")),
            vcp_features=vcp_features,
        )


@dataclass
class MonitorRuntimeHandle:
    """Ephemeral runtime hardware handle; never persisted in profiles/settings."""

    driver_kind: str  # "ddcutil" | "backlight" | "demo"
    bus_number: int | None = None
    ddc_display_num: int | None = None
    connector: str = ""
    sysfs_path: str = ""
    edid_hash: str = ""


@dataclass
class MonitorInfo:
    """Unified runtime representation of a detected display."""

    monitor_id: str
    display_name: str
    manufacturer: str = ""
    model: str = ""
    serial: str = ""
    connector: str = ""
    monitor_number: int = 0
    is_builtin: bool = False
    is_primary: bool = False
    driver: str = ""
    capabilities: MonitorCapabilities = field(default_factory=MonitorCapabilities)
    brightness: int | None = None
    contrast: int | None = None
    volume: int | None = None
    input_source: int | None = None
    color_temperature: int | None = None
    power_state: int | None = None
    rotation: int = 0
    available_inputs: list[dict[str, Any]] = field(default_factory=list)
    available_color_presets: list[dict[str, Any]] = field(default_factory=list)
    available_power_states: list[dict[str, Any]] = field(default_factory=list)
    last_error: str | None = None
    hidden: bool = False
    connection_status: str = "connected"
    ddc_status: str = "not_applicable"
    i2c_bus: int | None = None
    drm_connector: str = ""
    brightness_mode: str = "hardware"  # "hardware" | "software_ctm" | "software_gamma" | "none"
    status_level: str = "none"  # "none" | "info" | "error"
    status_notice: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "monitor_id": self.monitor_id,
            "display_name": self.display_name,
            "manufacturer": self.manufacturer,
            "model": self.model,
            "serial": self.serial,
            "connector": self.connector,
            "monitor_number": self.monitor_number,
            "is_builtin": self.is_builtin,
            "is_primary": self.is_primary,
            "driver": self.driver,
            "capabilities": self.capabilities.to_dict(),
            "brightness": self.brightness,
            "contrast": self.contrast,
            "volume": self.volume,
            "input_source": self.input_source,
            "color_temperature": self.color_temperature,
            "power_state": self.power_state,
            "rotation": self.rotation,
            "available_inputs": list(self.available_inputs),
            "available_color_presets": list(self.available_color_presets),
            "available_power_states": list(self.available_power_states),
            "last_error": self.last_error,
            "hidden": self.hidden,
            "connection_status": self.connection_status,
            "ddc_status": self.ddc_status,
            "i2c_bus": self.i2c_bus,
            "drm_connector": self.drm_connector,
            "brightness_mode": self.brightness_mode,
            "status_level": self.status_level,
            "status_notice": self.status_notice,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MonitorInfo:
        return cls(
            monitor_id=str(data["monitor_id"]),
            display_name=str(data.get("display_name", data["monitor_id"])),
            manufacturer=str(data.get("manufacturer", "")),
            model=str(data.get("model", "")),
            serial=str(data.get("serial", "")),
            connector=str(data.get("connector", "")),
            monitor_number=int(data.get("monitor_number", 0)),
            is_builtin=bool(data.get("is_builtin", False)),
            is_primary=bool(data.get("is_primary", False)),
            driver=str(data.get("driver", "")),
            capabilities=MonitorCapabilities.from_dict(data.get("capabilities", {}) or {}),
            brightness=data.get("brightness"),
            contrast=data.get("contrast"),
            volume=data.get("volume"),
            input_source=data.get("input_source"),
            color_temperature=data.get("color_temperature"),
            power_state=data.get("power_state"),
            rotation=int(data.get("rotation", 0)),
            available_inputs=list(data.get("available_inputs", [])),
            available_color_presets=list(data.get("available_color_presets", [])),
            available_power_states=list(data.get("available_power_states", [])),
            last_error=data.get("last_error"),
            hidden=bool(data.get("hidden", False)),
            connection_status=str(data.get("connection_status", "connected")),
            ddc_status=str(data.get("ddc_status", "not_applicable")),
            i2c_bus=(int(data["i2c_bus"]) if data.get("i2c_bus") is not None else None),
            drm_connector=str(data.get("drm_connector", "")),
            brightness_mode=str(data.get("brightness_mode", "hardware")),
            status_level=str(data.get("status_level", "none")),
            status_notice=data.get("status_notice"),
        )


@dataclass
class MonitorPreference:
    """Per-monitor visibility and opt-in feature preferences."""

    hidden: bool = False
    show_contrast: bool = True
    show_volume: bool = True
    show_input_source: bool = True
    show_color_temperature: bool = False
    show_power_state: bool = False
    show_rotation: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "hidden": self.hidden,
            "show_contrast": self.show_contrast,
            "show_volume": self.show_volume,
            "show_input_source": self.show_input_source,
            "show_color_temperature": self.show_color_temperature,
            "show_power_state": self.show_power_state,
            "show_rotation": self.show_rotation,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MonitorPreference:
        return cls(
            hidden=bool(data.get("hidden", False)),
            show_contrast=bool(data.get("show_contrast", True)),
            show_volume=bool(data.get("show_volume", True)),
            show_input_source=bool(data.get("show_input_source", True)),
            show_color_temperature=bool(data.get("show_color_temperature", False)),
            show_power_state=bool(data.get("show_power_state", False)),
            show_rotation=bool(data.get("show_rotation", True)),
        )


@dataclass
class CustomVcpMapping:
    """User-defined display label for a VCP discrete value."""

    vcp_code: int
    value: int
    custom_name: str
    apply_to_all: bool = True
    target_monitor_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "vcp_code": self.vcp_code,
            "value": self.value,
            "custom_name": self.custom_name,
            "apply_to_all": self.apply_to_all,
            "target_monitor_id": self.target_monitor_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CustomVcpMapping:
        return cls(
            vcp_code=int(data["vcp_code"]),
            value=int(data["value"]),
            custom_name=str(data.get("custom_name", "")),
            apply_to_all=bool(data.get("apply_to_all", True)),
            target_monitor_id=str(data.get("target_monitor_id", "")),
        )


@dataclass
class ProfileMonitorSetting:
    """Saved per-monitor settings inside a profile (safe controls only)."""

    monitor_id: str
    brightness: int | None = None
    contrast: int | None = None
    volume: int | None = None
    color_temperature: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "monitor_id": self.monitor_id,
            "brightness": self.brightness,
            "contrast": self.contrast,
            "volume": self.volume,
            "color_temperature": self.color_temperature,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProfileMonitorSetting:
        return cls(
            monitor_id=str(data["monitor_id"]),
            brightness=data.get("brightness"),
            contrast=data.get("contrast"),
            volume=data.get("volume"),
            color_temperature=data.get("color_temperature"),
        )


@dataclass
class Profile:
    """Named multi-monitor display profile with stable ID."""

    id: int
    name: str
    monitor_settings: list[ProfileMonitorSetting] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "monitor_settings": [item.to_dict() for item in self.monitor_settings],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Profile:
        return cls(
            id=int(data["id"]),
            name=str(data["name"]),
            monitor_settings=[
                ProfileMonitorSetting.from_dict(item)
                for item in data.get("monitor_settings", [])
            ],
        )


VALID_WHEEL_MODES = ("disabled", "primary", "all")


@dataclass
class AppSettings:
    """Persistent user configuration for pdfl."""

    refresh_delay_seconds: float = 1.5
    restore_on_launch: bool = False
    show_profile_switcher: bool = True
    linked_brightness: bool = False
    mouse_wheel_mode: str = "disabled"
    mouse_wheel_step: int = 5
    theme_profile_switching_enabled: bool = False
    light_mode_profile_id: int | None = None
    dark_mode_profile_id: int | None = None
    software_dimming_fallback: bool = False
    monitor_prefs: dict[str, MonitorPreference] = field(default_factory=dict)
    custom_vcp_mappings: list[CustomVcpMapping] = field(default_factory=list)

    def normalized(self) -> AppSettings:
        mode = self.mouse_wheel_mode if self.mouse_wheel_mode in VALID_WHEEL_MODES else "disabled"
        delay = max(0.5, min(10.0, float(self.refresh_delay_seconds)))
        step = max(1, min(25, int(self.mouse_wheel_step)))
        return AppSettings(
            refresh_delay_seconds=delay,
            restore_on_launch=bool(self.restore_on_launch),
            show_profile_switcher=bool(self.show_profile_switcher),
            linked_brightness=bool(self.linked_brightness),
            mouse_wheel_mode=mode,
            mouse_wheel_step=step,
            theme_profile_switching_enabled=bool(self.theme_profile_switching_enabled),
            light_mode_profile_id=(
                int(self.light_mode_profile_id)
                if self.light_mode_profile_id is not None
                else None
            ),
            dark_mode_profile_id=(
                int(self.dark_mode_profile_id)
                if self.dark_mode_profile_id is not None
                else None
            ),
            software_dimming_fallback=bool(self.software_dimming_fallback),
            monitor_prefs=dict(self.monitor_prefs),
            custom_vcp_mappings=list(self.custom_vcp_mappings),
        )

    def to_dict(self) -> dict[str, Any]:
        s = self.normalized()
        return {
            "version": 1,
            "refresh_delay_seconds": s.refresh_delay_seconds,
            "restore_on_launch": s.restore_on_launch,
            "show_profile_switcher": s.show_profile_switcher,
            "linked_brightness": s.linked_brightness,
            "mouse_wheel_mode": s.mouse_wheel_mode,
            "mouse_wheel_step": s.mouse_wheel_step,
            "theme_profile_switching_enabled": s.theme_profile_switching_enabled,
            "light_mode_profile_id": s.light_mode_profile_id,
            "dark_mode_profile_id": s.dark_mode_profile_id,
            "software_dimming_fallback": s.software_dimming_fallback,
            "monitor_prefs": {
                k: v.to_dict() for k, v in sorted(s.monitor_prefs.items())
            },
            "custom_vcp_mappings": [m.to_dict() for m in s.custom_vcp_mappings],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppSettings:
        prefs_raw = data.get("monitor_prefs", {}) or {}
        monitor_prefs = {
            str(k): MonitorPreference.from_dict(v)
            for k, v in prefs_raw.items()
            if isinstance(v, dict)
        }
        mappings = [
            CustomVcpMapping.from_dict(m)
            for m in (data.get("custom_vcp_mappings", []) or [])
            if isinstance(m, dict)
        ]
        return cls(
            refresh_delay_seconds=float(data.get("refresh_delay_seconds", 1.5)),
            restore_on_launch=bool(data.get("restore_on_launch", False)),
            show_profile_switcher=bool(data.get("show_profile_switcher", True)),
            linked_brightness=bool(data.get("linked_brightness", False)),
            mouse_wheel_mode=str(data.get("mouse_wheel_mode", "disabled")),
            mouse_wheel_step=int(data.get("mouse_wheel_step", 5)),
            theme_profile_switching_enabled=bool(
                data.get("theme_profile_switching_enabled", False)
            ),
            light_mode_profile_id=data.get("light_mode_profile_id"),
            dark_mode_profile_id=data.get("dark_mode_profile_id"),
            software_dimming_fallback=bool(data.get("software_dimming_fallback", False)),
            monitor_prefs=monitor_prefs,
            custom_vcp_mappings=mappings,
        ).normalized()


@dataclass
class OperationResult:
    """Structured result returned by drivers and backend operations."""

    ok: bool
    message: str = ""
    error_code: str = ""
    requires_confirmation: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "error_code": self.error_code,
            "requires_confirmation": self.requires_confirmation,
            "data": dict(self.data),
        }
