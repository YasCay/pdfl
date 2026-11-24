"""In-memory Demo monitor driver — zero hardware dependencies, zero real display writes."""

from __future__ import annotations

from copy import deepcopy

from pdfl.drivers.base import DiscoveredMonitor
from pdfl.mccs_parser import MccsCapabilitiesParser
from pdfl.models import (
    MonitorCapabilities,
    MonitorInfo,
    MonitorRuntimeHandle,
    OperationResult,
)
from pdfl.vcp_names import (
    VCP_COLOR_TEMP,
    VCP_INPUT_SOURCE,
    VCP_POWER_MODE,
    get_value_name,
)

_DELL_RAW_CAPS = (
    "(prot(monitor)type(lcd)model(Dell U2723QE)"
    "cmds(01 02 03 07 0C E3 F3)"
    "vcp(02 04 05 08 10 12 14(01 04 05 06 0B) 60(0F 11 1B) 62 D6(01 02 04) DF)"
    "mccs_ver(2.2))"
)

_LG_RAW_CAPS = (
    "(prot(monitor)type(lcd)model(LG 34WN80C)"
    "cmds(01 02 03 07)"
    "vcp(02 10 12 14(01 05 08) 60(11 12 0F) DF)"
    "mccs_ver(2.1))"
)


class DemoDriver:
    """Simulated multi-monitor environment for safe UI, CLI, D-Bus, and unit testing."""

    name = "demo"

    def __init__(self) -> None:
        self._monitors: list[MonitorInfo] = self._build_initial_monitors()
        self.inject_error: str | None = None

    def _build_initial_monitors(self) -> list[MonitorInfo]:
        dell_caps = MccsCapabilitiesParser.parse(_DELL_RAW_CAPS).capabilities
        lg_caps = MccsCapabilitiesParser.parse(_LG_RAW_CAPS).capabilities

        builtin = MonitorInfo(
            monitor_id="demo:internal:eDP-1",
            display_name="Built-in Display (Demo)",
            manufacturer="LEN",
            model="Built-in OLED",
            serial="",
            connector="eDP-1",
            monitor_number=1,
            is_builtin=True,
            is_primary=False,
            driver="demo",
            capabilities=MonitorCapabilities(
                brightness=True,
                brightness_writable=True,
                rotation=True,
            ),
            brightness=65,
            rotation=0,
            ddc_status="not_applicable",
        )

        dell = MonitorInfo(
            monitor_id="demo:ddc:DEL:U2723QE:CN04K921",
            display_name="Dell U2723QE (Demo)",
            manufacturer="DEL",
            model="Dell U2723QE",
            serial="CN04K921",
            connector="DP-1",
            monitor_number=2,
            is_builtin=False,
            is_primary=True,
            driver="demo",
            capabilities=MonitorCapabilities(
                brightness=True,
                contrast=True,
                volume=True,
                input_source=True,
                color_temperature=True,
                power_state=True,
                rotation=True,
                brightness_writable=True,
                raw_capabilities=_DELL_RAW_CAPS,
                mccs_version="2.2",
                vcp_features=dell_caps.vcp_features,
            ),
            brightness=75,
            contrast=70,
            volume=45,
            input_source=0x0F,
            color_temperature=0x05,
            power_state=0x01,
            rotation=0,
            ddc_status="available",
            available_inputs=[
                {"value": v, "name": get_value_name(VCP_INPUT_SOURCE, v)}
                for v in (0x0F, 0x11, 0x1B)
            ],
            available_color_presets=[
                {"value": v, "name": get_value_name(VCP_COLOR_TEMP, v)}
                for v in (0x01, 0x04, 0x05, 0x06, 0x0B)
            ],
            available_power_states=[
                {"value": v, "name": get_value_name(VCP_POWER_MODE, v)}
                for v in (0x01, 0x02, 0x04)
            ],
        )

        # Notice LG intentionally has NO volume and NO power_state -> tests capability gating!
        lg = MonitorInfo(
            monitor_id="demo:ddc:GSM:34WN80C:204NTBK881",
            display_name="LG 34WN80C (Demo)",
            manufacturer="GSM",
            model="LG 34WN80C",
            serial="204NTBK881",
            connector="HDMI-1",
            monitor_number=3,
            is_builtin=False,
            is_primary=False,
            driver="demo",
            capabilities=MonitorCapabilities(
                brightness=True,
                contrast=True,
                volume=False,
                input_source=True,
                color_temperature=True,
                power_state=False,
                rotation=True,
                brightness_writable=True,
                raw_capabilities=_LG_RAW_CAPS,
                mccs_version="2.1",
                vcp_features=lg_caps.vcp_features,
            ),
            brightness=60,
            contrast=65,
            input_source=0x11,
            color_temperature=0x01,
            rotation=0,
            ddc_status="available",
            available_inputs=[
                {"value": v, "name": get_value_name(VCP_INPUT_SOURCE, v)}
                for v in (0x11, 0x12, 0x0F)
            ],
            available_color_presets=[
                {"value": v, "name": get_value_name(VCP_COLOR_TEMP, v)}
                for v in (0x01, 0x05, 0x08)
            ],
        )

        return [builtin, dell, lg]

    def discover(self) -> tuple[list[DiscoveredMonitor], list[str]]:
        items = []
        for m in self._monitors:
            handle = MonitorRuntimeHandle(
                driver_kind="demo",
                connector=m.connector,
            )
            items.append(DiscoveredMonitor(info=deepcopy(m), handle=handle))
        return items, []

    def set_feature(
        self, handle: MonitorRuntimeHandle, feature: str, value: int
    ) -> OperationResult:
        if self.inject_error:
            return OperationResult(
                ok=False, error_code="demo_injected_error", message=self.inject_error
            )
        for m in self._monitors:
            if m.connector == handle.connector:
                if feature == "brightness":
                    m.brightness = value
                elif feature == "contrast":
                    m.contrast = value
                elif feature == "volume":
                    m.volume = value
                elif feature == "color_temperature":
                    m.color_temperature = value
                elif feature == "input_source":
                    m.input_source = value
                elif feature == "power_state":
                    m.power_state = value
                else:
                    return OperationResult(
                        ok=False,
                        error_code="unsupported_feature",
                        message=f"Unknown feature: {feature}",
                    )
                return OperationResult(
                    ok=True, message=f"Demo: {m.display_name} {feature} -> {value}"
                )
        return OperationResult(
            ok=False, error_code="monitor_not_found", message="Demo monitor not found."
        )

    def set_rotation(self, monitor_id: str, degrees: int) -> OperationResult:
        for m in self._monitors:
            if m.monitor_id == monitor_id:
                prev = m.rotation
                m.rotation = degrees
                return OperationResult(
                    ok=True,
                    message=f"Demo: {m.display_name} rotation -> {degrees}°",
                    data={"previous_degrees": prev},
                )
        return OperationResult(
            ok=False, error_code="monitor_not_found", message="Demo monitor not found."
        )
