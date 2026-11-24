"""GNOME Mutter Wayland DisplayConfig D-Bus client (read state, backlight, safe rotation)."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from pdfl.models import OperationResult
from pdfl.planners import (
    DEGREES_TO_MUTTER_TRANSFORM,
    MUTTER_TRANSFORM_TO_DEGREES,
    recompute_logical_layout_for_rotation,
)

SOFTWARE_MIN_BRIGHTNESS_SCALE: float = 0.35
SOFTWARE_MIN_CONTRAST_EXPONENT: float = 0.65
SOFTWARE_MAX_CONTRAST_EXPONENT: float = 1.45
SOFTWARE_MIN_TINT_RATIO: float = 0.35
SOFTWARE_LUT_MATCH_TOLERANCE: int = 64
SOFTWARE_RECOVERY_MIN_PEAK: int = int(round(0.30 * 65535))


def software_percent_to_scale(
    percent: int, min_scale: float = SOFTWARE_MIN_BRIGHTNESS_SCALE
) -> float:
    """Map brightness percentage (0..100) linearly to [min_scale, 1.0] with hard floor."""
    clamped = max(0, min(100, int(percent)))
    if clamped == 100:
        return 1.0
    floor = max(0.30, min(0.95, float(min_scale)))
    return floor + (clamped / 100.0) * (1.0 - floor)


def build_ctm_diagonal_s31_32(scale: float) -> tuple[int, ...]:
    """Build a 3x3 diagonal CTM matrix in Linux DRM S31.32 fixed-point format."""
    clamped = max(0.0, min(1.0, float(scale)))
    return build_ctm_rgb_s31_32(clamped, clamped, clamped)


def build_ctm_rgb_s31_32(
    r_scale: float, g_scale: float, b_scale: float
) -> tuple[int, ...]:
    """Build a 3x3 RGB diagonal CTM matrix in Linux DRM S31.32 fixed-point format."""
    rv = int(round(max(0.0, min(1.0, float(r_scale))) * (1 << 32)))
    gv = int(round(max(0.0, min(1.0, float(g_scale))) * (1 << 32)))
    bv = int(round(max(0.0, min(1.0, float(b_scale))) * (1 << 32)))
    return (rv, 0, 0, 0, gv, 0, 0, 0, bv)


SOFTWARE_COLOR_TEMP_RGB: dict[int, tuple[float, float, float]] = {
    0x04: (1.00, 0.89, 0.76),  # 5000K (Warm)
    0x05: (1.00, 1.00, 1.00),  # 6500K (Neutral)
    0x06: (0.94, 0.97, 1.00),  # 7500K (Cool)
    0x08: (0.85, 0.92, 1.00),  # 9300K (Very cool)
}
SOFTWARE_COLOR_PRESET_VALUES: tuple[int, ...] = (0x04, 0x05, 0x06, 0x08)


def software_contrast_to_slope(contrast: int) -> float:
    """Map contrast percentage (0..100) to slope [0.5, 1.5] (50 -> 1.0)."""
    clamped = max(0, min(100, int(contrast)))
    if clamped == 50:
        return 1.0
    return 0.5 + (clamped / 100.0)


def software_contrast_to_exponent(contrast: int) -> float:
    """Map contrast percentage (0..100) to non-crushing power S-curve exponent (50 -> 1.0)."""
    clamped = max(0, min(100, int(contrast)))
    if clamped == 50:
        return 1.0
    if clamped < 50:
        return SOFTWARE_MIN_CONTRAST_EXPONENT + (clamped / 50.0) * (
            1.0 - SOFTWARE_MIN_CONTRAST_EXPONENT
        )
    return 1.0 + ((clamped - 50) / 50.0) * (SOFTWARE_MAX_CONTRAST_EXPONENT - 1.0)


def build_identity_gamma_lut(length: int) -> tuple[list[int], list[int], list[int]]:
    """Build a clean linear 16-bit RGB identity gamma LUT of `length` entries."""
    n = max(2, int(length))
    ramp = [int(round((i * 65535) / (n - 1))) for i in range(n)]
    return (list(ramp), list(ramp), list(ramp))


def luts_within_tolerance(
    a: tuple[list[int], list[int], list[int]],
    b: tuple[list[int], list[int], list[int]],
    tol: int = SOFTWARE_LUT_MATCH_TOLERANCE,
) -> bool:
    """Return True if two 16-bit RGB LUTs match within hardware quantization tolerance."""
    if len(a) != 3 or len(b) != 3:
        return False
    for ch_a, ch_b in zip(a, b):
        if len(ch_a) != len(ch_b):
            return False
        for x, y in zip(ch_a, ch_b):
            if abs(int(x) - int(y)) > tol:
                return False
    return True


def extract_safe_gamma_baseline(
    cur_lut: tuple[list[int], list[int], list[int]]
) -> tuple[list[int], list[int], list[int]]:
    """Extract a full-scale linear baseline LUT preserving normalized white-point tint only.

    Prevents compounding dimming across daemon restarts, quantized readbacks, or crushed LUTs.
    """
    if len(cur_lut) != 3:
        return build_identity_gamma_lut(256)
    r, g, b = cur_lut
    n = len(r)
    if n < 2 or len(g) != n or len(b) != n:
        return build_identity_gamma_lut(max(2, n))
    peak = max(int(r[-1]), int(g[-1]), int(b[-1]))
    if peak <= 0:
        return build_identity_gamma_lut(n)

    tr = max(SOFTWARE_MIN_TINT_RATIO, min(1.0, int(r[-1]) / peak))
    tg = max(SOFTWARE_MIN_TINT_RATIO, min(1.0, int(g[-1]) / peak))
    tb = max(SOFTWARE_MIN_TINT_RATIO, min(1.0, int(b[-1]) / peak))
    tmax = max(tr, tg, tb)
    if tmax > 0.0:
        tr /= tmax
        tg /= tmax
        tb /= tmax

    id_r, _id_g, _id_b = build_identity_gamma_lut(n)
    return (
        [max(0, min(65535, int(round(v * tr)))) for v in id_r],
        [max(0, min(65535, int(round(v * tg)))) for v in id_r],
        [max(0, min(65535, int(round(v * tb)))) for v in id_r],
    )


@dataclass
class SoftwareDisplayState:
    """Combined software display parameters applied via Mutter CTM or CRTC gamma."""

    brightness: int = 100
    contrast: int = 50
    color_temperature: int = 0x05


def scale_gamma_lut(
    lut: tuple[list[int], list[int], list[int]], scale: float
) -> tuple[list[int], list[int], list[int]]:
    """Scale 16-bit RGB gamma LUT channels by `scale`, keeping 1.0 exact."""
    r, g, b = lut
    if scale >= 0.9999:
        return (list(r), list(g), list(b))
    factor = max(0.0, min(1.0, float(scale)))
    return (
        [max(0, min(65535, int(round(v * factor)))) for v in r],
        [max(0, min(65535, int(round(v * factor)))) for v in g],
        [max(0, min(65535, int(round(v * factor)))) for v in b],
    )


def apply_software_gamma_lut(
    lut: tuple[list[int], list[int], list[int]],
    brightness: int = 100,
    contrast: int = 50,
    color_temperature: int = 0x05,
) -> tuple[list[int], list[int], list[int]]:
    """Apply combined brightness, non-crushing S-curve contrast, and white point to a 16-bit LUT."""
    r, g, b = lut
    b_clamped = max(0, min(100, int(brightness)))
    c_clamped = max(0, min(100, int(contrast)))
    ct_val = int(color_temperature)
    if b_clamped == 100 and c_clamped == 50 and ct_val == 0x05:
        return (list(r), list(g), list(b))

    b_scale = software_percent_to_scale(b_clamped)
    exponent = software_contrast_to_exponent(c_clamped)
    wr, wg, wb = SOFTWARE_COLOR_TEMP_RGB.get(ct_val, (1.0, 1.0, 1.0))

    def _xf(ch: list[int], wp: float) -> list[int]:
        out: list[int] = []
        for raw in ch:
            norm = max(0.0, min(1.0, raw / 65535.0))
            if exponent != 1.0:
                if norm <= 0.0:
                    norm = 0.0
                elif norm >= 1.0:
                    norm = 1.0
                elif norm < 0.5:
                    norm = 0.5 * ((2.0 * norm) ** exponent)
                else:
                    norm = 1.0 - 0.5 * ((2.0 * (1.0 - norm)) ** exponent)
            scaled = max(0.0, min(1.0, norm * b_scale * wp))
            out.append(max(0, min(65535, int(round(scaled * 65535.0)))))
        return out

    nr, ng, nb = _xf(r, wr), _xf(g, wg), _xf(b, wb)

    # Hard safety invariant: display must stay clearly readable in any combination.
    min_peak = int(round(SOFTWARE_MIN_BRIGHTNESS_SCALE * 65535))
    if nr and ng and nb:
        out_peak = max(nr[-1], ng[-1], nb[-1])
        if out_peak < min_peak:
            if out_peak > 0:
                boost = min_peak / out_peak
                nr = [max(0, min(65535, int(round(v * boost)))) for v in nr]
                ng = [max(0, min(65535, int(round(v * boost)))) for v in ng]
                nb = [max(0, min(65535, int(round(v * boost)))) for v in nb]
            else:
                nr, ng, nb = scale_gamma_lut(
                    build_identity_gamma_lut(len(r)), SOFTWARE_MIN_BRIGHTNESS_SCALE
                )

        if len(nr) >= 3:
            mid = len(nr) // 2
            min_mid = int(round(0.12 * 65535))
            if max(nr[mid], ng[mid], nb[mid]) < min_mid:
                nr, ng, nb = scale_gamma_lut(
                    build_identity_gamma_lut(len(nr)), SOFTWARE_MIN_BRIGHTNESS_SCALE
                )

    return (nr, ng, nb)


@dataclass
class MutterMonitorEntry:
    connector: str
    vendor: str
    product: str
    serial: str
    display_name: str
    is_builtin: bool
    current_mode_id: str = ""
    current_width: int = 0
    current_height: int = 0


@dataclass
class MutterBacklightEntry:
    connector: str
    active: bool
    min_value: int
    max_value: int
    value: int


@dataclass
class MutterDisplayState:
    serial: int
    monitors: dict[str, MutterMonitorEntry] = field(default_factory=dict)
    logical_monitors: list[dict[str, Any]] = field(default_factory=list)
    layout_mode: int = 1
    apply_allowed: bool = True
    backlight_serial: int = 0
    backlights: dict[str, MutterBacklightEntry] = field(default_factory=dict)
    available: bool = True
    error: str = ""


class GioMutterAdapter:
    """Real Gio session D-Bus adapter for org.gnome.Mutter.DisplayConfig."""

    DEST = "org.gnome.Mutter.DisplayConfig"
    PATH = "/org/gnome/Mutter/DisplayConfig"
    IFACE = "org.gnome.Mutter.DisplayConfig"

    def _bus(self):
        from gi.repository import Gio

        return Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def get_current_state(self):
        from gi.repository import Gio

        res = self._bus().call_sync(
            self.DEST,
            self.PATH,
            self.IFACE,
            "GetCurrentState",
            None,
            None,
            Gio.DBusCallFlags.NONE,
            3000,
            None,
        )
        return res.unpack()

    def get_backlight_property(self):
        from gi.repository import Gio, GLib

        res = self._bus().call_sync(
            self.DEST,
            self.PATH,
            "org.freedesktop.DBus.Properties",
            "Get",
            GLib.Variant("(ss)", (self.IFACE, "Backlight")),
            None,
            Gio.DBusCallFlags.NONE,
            2000,
            None,
        )
        val = res.unpack()[0]
        return val

    def set_backlight(self, serial: int, connector: str, value: int) -> None:
        from gi.repository import Gio, GLib

        self._bus().call_sync(
            self.DEST,
            self.PATH,
            self.IFACE,
            "SetBacklight",
            GLib.Variant("(usi)", (serial, connector, int(value))),
            None,
            Gio.DBusCallFlags.NONE,
            2000,
            None,
        )

    def apply_monitors_config(
        self,
        serial: int,
        method: int,
        logical_monitors: list[dict[str, Any]],
        active_modes: dict[str, tuple[int, int, str]],
    ) -> None:
        from gi.repository import Gio, GLib

        payload = []
        for lm in logical_monitors:
            mons = []
            for conn in lm["connectors"]:
                _w, _h, mode_id = active_modes[conn]
                mons.append((conn, mode_id, {}))
            payload.append(
                (
                    int(lm["x"]),
                    int(lm["y"]),
                    float(lm["scale"]),
                    int(lm["transform"]),
                    bool(lm["is_primary"]),
                    mons,
                )
            )
        variant = GLib.Variant(
            "(uua(iiduba(ssa{sv}))a{sv})",
            (int(serial), int(method), payload, {}),
        )
        self._bus().call_sync(
            self.DEST,
            self.PATH,
            self.IFACE,
            "ApplyMonitorsConfig",
            variant,
            None,
            Gio.DBusCallFlags.NONE,
            4000,
            None,
        )

    def get_resources(self):
        from gi.repository import Gio

        res = self._bus().call_sync(
            self.DEST,
            self.PATH,
            self.IFACE,
            "GetResources",
            None,
            None,
            Gio.DBusCallFlags.NONE,
            3000,
            None,
        )
        return res.unpack()

    def set_output_ctm(
        self, serial: int, output_id: int, matrix: tuple[int, ...]
    ) -> None:
        from gi.repository import Gio, GLib

        if len(matrix) != 9:
            raise ValueError("CTM matrix must have 9 S31.32 entries")
        variant = GLib.Variant(
            "(uu(ttttttttt))",
            (int(serial), int(output_id), tuple(int(v) for v in matrix)),
        )
        self._bus().call_sync(
            self.DEST,
            self.PATH,
            self.IFACE,
            "SetOutputCTM",
            variant,
            None,
            Gio.DBusCallFlags.NONE,
            2000,
            None,
        )

    def get_crtc_gamma(self, serial: int, crtc_id: int):
        from gi.repository import Gio, GLib

        res = self._bus().call_sync(
            self.DEST,
            self.PATH,
            self.IFACE,
            "GetCrtcGamma",
            GLib.Variant("(uu)", (int(serial), int(crtc_id))),
            None,
            Gio.DBusCallFlags.NONE,
            2000,
            None,
        )
        return res.unpack()

    def set_crtc_gamma(
        self,
        serial: int,
        crtc_id: int,
        red: list[int],
        green: list[int],
        blue: list[int],
    ) -> None:
        from gi.repository import Gio, GLib

        variant = GLib.Variant(
            "(uuaqaqaq)",
            (
                int(serial),
                int(crtc_id),
                [int(v) for v in red],
                [int(v) for v in green],
                [int(v) for v in blue],
            ),
        )
        self._bus().call_sync(
            self.DEST,
            self.PATH,
            self.IFACE,
            "SetCrtcGamma",
            variant,
            None,
            Gio.DBusCallFlags.NONE,
            2000,
            None,
        )


class MutterDisplayClient:
    """High-level wrapper over Mutter DisplayConfig supporting dry injection."""

    def __init__(self, adapter: Any | None = None) -> None:
        self._adapter = adapter
        self._lock = threading.RLock()
        self._gamma_baselines: dict[str, tuple[list[int], list[int], list[int]]] = {}
        self._gamma_last_written: dict[str, tuple[list[int], list[int], list[int]]] = {}
        self._software_states: dict[str, SoftwareDisplayState] = {}

    @staticmethod
    def _norm_connector(connector: str) -> str:
        conn = (connector or "").strip()
        if conn.startswith("card") and "-" in conn:
            conn = conn.split("-", 1)[1]
        return conn.replace("HDMI-A-", "HDMI-")

    def _find_output_resource(
        self, connector: str
    ) -> tuple[int, int, int, bool] | None:
        target = self._norm_connector(connector)
        if not target:
            return None
        res = self._get_adapter().get_resources()
        serial = int(res[0])
        outputs = res[2]
        for out in outputs:
            out_id = int(out[0])
            crtc_id = int(out[2])
            out_name = str(out[4])
            props = dict(out[7] or {})
            if out_name == connector or self._norm_connector(out_name) == target:
                supports_ctm = bool(props.get("supports-color-transform", False))
                return serial, out_id, crtc_id, supports_ctm
        return None

    def probe_software_dimming_mode(self, connector: str) -> str | None:
        """Detect whether Mutter CTM or CRTC gamma is usable for `connector`."""
        with self._lock:
            try:
                found = self._find_output_resource(connector)
            except Exception:
                return None
            if found is None:
                return None
            serial, _out_id, crtc_id, supports_ctm = found
            if crtc_id < 0:
                return None
            if supports_ctm:
                return "software_ctm"
            try:
                adapter = self._get_adapter()
                r, g, b = adapter.get_crtc_gamma(serial, crtc_id)
            except Exception:
                return None
            if len(r) >= 2 and len(r) == len(g) == len(b):
                key = self._norm_connector(connector)
                if (
                    key not in self._software_states
                    and max(int(r[-1]), int(g[-1]), int(b[-1])) < SOFTWARE_RECOVERY_MIN_PEAK
                ):
                    try:
                        ir, ig, ib = build_identity_gamma_lut(len(r))
                        adapter.set_crtc_gamma(serial, crtc_id, ir, ig, ib)
                        self._gamma_baselines[key] = (list(ir), list(ig), list(ib))
                        self._gamma_last_written[key] = (list(ir), list(ig), list(ib))
                    except Exception:
                        pass
                return "software_gamma"
            return None

    def get_software_display_state(self, connector: str) -> SoftwareDisplayState:
        with self._lock:
            key = self._norm_connector(connector)
            cur = self._software_states.get(key)
            if cur is None:
                return SoftwareDisplayState()
            return SoftwareDisplayState(
                brightness=cur.brightness,
                contrast=cur.contrast,
                color_temperature=cur.color_temperature,
            )

    def get_software_brightness_percent(self, connector: str) -> int:
        return self.get_software_display_state(connector).brightness

    def set_software_display_feature(
        self, connector: str, feature: str, value: int
    ) -> OperationResult:
        with self._lock:
            if feature not in ("brightness", "contrast", "color_temperature"):
                return OperationResult(
                    ok=False,
                    error_code="unsupported_feature",
                    message=f"Software mode does not support '{feature}'.",
                )

            key = self._norm_connector(connector)
            next_state = self.get_software_display_state(connector)
            if feature == "brightness":
                next_state.brightness = max(0, min(100, int(value)))
            elif feature == "contrast":
                next_state.contrast = max(0, min(100, int(value)))
            else:
                ct = int(value)
                if ct not in SOFTWARE_COLOR_TEMP_RGB:
                    return OperationResult(
                        ok=False,
                        error_code="invalid_value",
                        message=f"Color preset 0x{ct:02X} is not supported in software mode.",
                    )
                next_state.color_temperature = ct

            try:
                found = self._find_output_resource(connector)
            except Exception as exc:
                return OperationResult(
                    ok=False,
                    error_code="mutter_unavailable",
                    message=f"Mutter GetResources failed: {exc}",
                )
            if found is None:
                return OperationResult(
                    ok=False,
                    error_code="mutter_output_missing",
                    message=f"Mutter output {connector} not found.",
                )
            serial, out_id, crtc_id, supports_ctm = found
            if crtc_id < 0:
                return OperationResult(
                    ok=False,
                    error_code="mutter_output_inactive",
                    message=f"Mutter output {connector} has no active CRTC.",
                )

            adapter = self._get_adapter()
            b_scale = software_percent_to_scale(next_state.brightness)
            wr, wg, wb = SOFTWARE_COLOR_TEMP_RGB.get(
                next_state.color_temperature, (1.0, 1.0, 1.0)
            )

            if supports_ctm:
                try:
                    rs, gs, bs = b_scale * wr, b_scale * wg, b_scale * wb
                    peak_s = max(rs, gs, bs)
                    if peak_s < SOFTWARE_MIN_BRIGHTNESS_SCALE:
                        if peak_s > 0.0:
                            boost = SOFTWARE_MIN_BRIGHTNESS_SCALE / peak_s
                            rs = min(1.0, rs * boost)
                            gs = min(1.0, gs * boost)
                            bs = min(1.0, bs * boost)
                        else:
                            rs = gs = bs = SOFTWARE_MIN_BRIGHTNESS_SCALE
                    matrix = build_ctm_rgb_s31_32(rs, gs, bs)
                    adapter.set_output_ctm(serial, out_id, matrix)
                    self._software_states[key] = next_state
                    return OperationResult(
                        ok=True,
                        message=f"Software mode (CTM) for {connector}: updated {feature}.",
                        data={
                            "mode": "software_ctm",
                            "brightness": next_state.brightness,
                            "contrast": next_state.contrast,
                            "color_temperature": next_state.color_temperature,
                            "scale": b_scale,
                        },
                    )
                except Exception as exc:
                    return OperationResult(
                        ok=False,
                        error_code="mutter_ctm_failed",
                        message=f"Mutter SetOutputCTM failed: {exc}",
                    )

            try:
                r, g, b = adapter.get_crtc_gamma(serial, crtc_id)
                cur_lut = ([int(v) for v in r], [int(v) for v in g], [int(v) for v in b])
                if not cur_lut[0]:
                    return OperationResult(
                        ok=False,
                        error_code="mutter_gamma_empty",
                        message=f"Mutter returned an empty gamma LUT for {connector}.",
                    )
                last_written = self._gamma_last_written.get(key)
                if key not in self._gamma_baselines or (
                    last_written is not None
                    and not luts_within_tolerance(cur_lut, last_written)
                ):
                    self._gamma_baselines[key] = extract_safe_gamma_baseline(cur_lut)
                baseline = self._gamma_baselines[key]
                nr, ng, nb = apply_software_gamma_lut(
                    baseline,
                    brightness=next_state.brightness,
                    contrast=next_state.contrast,
                    color_temperature=next_state.color_temperature,
                )
                adapter.set_crtc_gamma(serial, crtc_id, nr, ng, nb)
                self._gamma_last_written[key] = (nr, ng, nb)
                self._software_states[key] = next_state
                return OperationResult(
                    ok=True,
                    message=f"Software mode (Gamma) for {connector}: updated {feature}.",
                    data={
                        "mode": "software_gamma",
                        "brightness": next_state.brightness,
                        "contrast": next_state.contrast,
                        "color_temperature": next_state.color_temperature,
                        "scale": b_scale,
                    },
                )
            except Exception as exc:
                return OperationResult(
                    ok=False,
                    error_code="mutter_gamma_failed",
                    message=f"Mutter SetCrtcGamma failed: {exc}",
                )

    def set_software_brightness_percent(
        self, connector: str, percent: int
    ) -> OperationResult:
        return self.set_software_display_feature(connector, "brightness", percent)

    def reset_software_brightness(self, connector: str) -> OperationResult:
        with self._lock:
            key = self._norm_connector(connector)
            cur = self._software_states.get(key)
            has_state = (
                key in self._gamma_baselines
                or key in self._gamma_last_written
                or (
                    cur is not None
                    and (
                        cur.brightness != 100
                        or cur.contrast != 50
                        or cur.color_temperature != 0x05
                    )
                )
            )
            res = OperationResult(ok=True, message="No active software dimming.")
            if has_state:
                self._software_states[key] = SoftwareDisplayState(
                    brightness=100, contrast=50, color_temperature=0x05
                )
                res = self.set_software_display_feature(connector, "brightness", 100)
            self._gamma_baselines.pop(key, None)
            self._gamma_last_written.pop(key, None)
            self._software_states.pop(key, None)
            return res

    def _get_adapter(self) -> Any:
        if self._adapter is None:
            self._adapter = GioMutterAdapter()
        return self._adapter

    def get_current_state(self) -> MutterDisplayState:
        try:
            serial, raw_monitors, raw_logical, props = self._get_adapter().get_current_state()
        except Exception as exc:
            return MutterDisplayState(serial=0, available=False, error=str(exc))

        monitors: dict[str, MutterMonitorEntry] = {}
        for spec, modes, mprops in raw_monitors:
            connector, vendor, product, mon_serial = spec
            cur_mode_id = ""
            cur_w = 0
            cur_h = 0
            for mode in modes:
                m_id, w, h, _hz, _pref_s, _scales, mode_props = mode
                if mode_props.get("is-current"):
                    cur_mode_id = str(m_id)
                    cur_w = int(w)
                    cur_h = int(h)
                    break
            monitors[str(connector)] = MutterMonitorEntry(
                connector=str(connector),
                vendor=str(vendor),
                product=str(product),
                serial=str(mon_serial),
                display_name=str(mprops.get("display-name") or product or connector),
                is_builtin=bool(mprops.get("is-builtin", False)),
                current_mode_id=cur_mode_id,
                current_width=cur_w,
                current_height=cur_h,
            )

        logical_monitors: list[dict[str, Any]] = []
        for lm in raw_logical:
            x, y, scale, transform, is_primary, lm_mons, _lm_props = lm
            conns = [str(item[0]) for item in lm_mons]
            logical_monitors.append(
                {
                    "x": int(x),
                    "y": int(y),
                    "scale": float(scale),
                    "transform": int(transform),
                    "is_primary": bool(is_primary),
                    "connectors": conns,
                }
            )

        bl_serial = 0
        backlights: dict[str, MutterBacklightEntry] = {}
        try:
            bl_raw = self._get_adapter().get_backlight_property()
            if isinstance(bl_raw, (tuple, list)) and len(bl_raw) >= 2:
                bl_serial = int(bl_raw[0])
                for item in bl_raw[1]:
                    conn = str(item.get("connector", ""))
                    if conn:
                        backlights[conn] = MutterBacklightEntry(
                            connector=conn,
                            active=bool(item.get("active", False)),
                            min_value=int(item.get("min", 0)),
                            max_value=int(item.get("max", 100)),
                            value=int(item.get("value", 0)),
                        )
        except Exception:
            pass

        return MutterDisplayState(
            serial=int(serial),
            monitors=monitors,
            logical_monitors=logical_monitors,
            layout_mode=int(props.get("layout-mode", 1)),
            apply_allowed=bool(props.get("apply-monitors-config-allowed", True)),
            backlight_serial=bl_serial,
            backlights=backlights,
            available=True,
        )

    def set_backlight_percent(self, connector: str, percent: int) -> OperationResult:
        state = self.get_current_state()
        if not state.available:
            return OperationResult(
                ok=False,
                error_code="mutter_unavailable",
                message=f"Mutter DisplayConfig unavailable: {state.error}",
            )
        bl = state.backlights.get(connector)
        if bl is None:
            return OperationResult(
                ok=False,
                error_code="backlight_missing",
                message=f"Mutter reports no backlight entry for {connector}.",
            )
        if not bl.active:
            return OperationResult(
                ok=False,
                error_code="backlight_inactive",
                message=f"Backlight for {connector} is currently inactive according to Mutter.",
            )
        clamped = max(0, min(100, int(percent)))
        span = max(1, bl.max_value - bl.min_value)
        raw_val = bl.min_value + round((clamped / 100.0) * span)
        raw_val = max(bl.min_value, min(bl.max_value, raw_val))
        try:
            self._get_adapter().set_backlight(state.backlight_serial, connector, raw_val)
            return OperationResult(
                ok=True,
                message=f"Brightness for {connector} set to {clamped}%.",
                data={"percent": clamped, "raw": raw_val},
            )
        except Exception as exc:
            return OperationResult(
                ok=False,
                error_code="mutter_backlight_failed",
                message=f"Mutter SetBacklight failed: {exc}",
            )

    def get_connector_rotation_degrees(
        self, state: MutterDisplayState, connector: str
    ) -> tuple[bool, int]:
        for lm in state.logical_monitors:
            if connector in lm["connectors"]:
                t = int(lm["transform"])
                return True, MUTTER_TRANSFORM_TO_DEGREES.get(t, 0)
        return False, 0

    def apply_rotation(
        self, connector: str, degrees: int, method: int = 1
    ) -> OperationResult:
        if degrees not in DEGREES_TO_MUTTER_TRANSFORM:
            return OperationResult(
                ok=False,
                error_code="invalid_rotation",
                message=f"Invalid rotation: {degrees}° (allowed: 0, 90, 180, 270).",
            )
        state = self.get_current_state()
        if not state.available:
            return OperationResult(
                ok=False,
                error_code="mutter_unavailable",
                message=f"Mutter DisplayConfig unavailable: {state.error}",
            )
        if not state.apply_allowed:
            return OperationResult(
                ok=False,
                error_code="rotation_not_allowed",
                message="Mutter does not allow ApplyMonitorsConfig in the current session.",
            )
        active_modes: dict[str, tuple[int, int, str]] = {}
        for lm in state.logical_monitors:
            for conn in lm["connectors"]:
                mon = state.monitors.get(conn)
                if not mon or not mon.current_mode_id:
                    return OperationResult(
                        ok=False,
                        error_code="mode_missing",
                        message=f"Active display mode missing for {conn}.",
                    )
                active_modes[conn] = (
                    mon.current_width,
                    mon.current_height,
                    mon.current_mode_id,
                )
        if connector not in active_modes:
            return OperationResult(
                ok=False,
                error_code="connector_inactive",
                message=f"Monitor {connector} is not an active logical display.",
            )

        previous_snapshot = [
            {
                "x": lm["x"],
                "y": lm["y"],
                "scale": lm["scale"],
                "transform": lm["transform"],
                "is_primary": lm["is_primary"],
                "connectors": list(lm["connectors"]),
            }
            for lm in state.logical_monitors
        ]
        new_transform = DEGREES_TO_MUTTER_TRANSFORM[degrees]
        updated_logical = recompute_logical_layout_for_rotation(
            state.logical_monitors,
            active_modes,
            connector,
            new_transform,
            state.layout_mode,
        )
        adapter = self._get_adapter()
        try:
            # Verify layout (method=0) before applying!
            adapter.apply_monitors_config(
                state.serial, 0, updated_logical, active_modes
            )
            adapter.apply_monitors_config(
                state.serial, method, updated_logical, active_modes
            )
        except Exception as exc:
            return OperationResult(
                ok=False,
                error_code="mutter_apply_failed",
                message=f"Mutter rejected the rotation: {exc}",
            )
        return OperationResult(
            ok=True,
            message=f"Rotation for {connector} set to {degrees}°.",
            data={
                "connector": connector,
                "degrees": degrees,
                "previous_snapshot": previous_snapshot,
                "active_modes": active_modes,
            },
        )

    def restore_logical_snapshot(
        self,
        snapshot: list[dict[str, Any]],
        active_modes: dict[str, tuple[int, int, str]],
        method: int = 1,
    ) -> OperationResult:
        state = self.get_current_state()
        if not state.available:
            return OperationResult(
                ok=False,
                error_code="mutter_unavailable",
                message=f"Mutter DisplayConfig unavailable: {state.error}",
            )
        try:
            self._get_adapter().apply_monitors_config(
                state.serial, method, snapshot, active_modes
            )
            return OperationResult(ok=True, message="Previous display orientation restored.")
        except Exception as exc:
            return OperationResult(
                ok=False,
                error_code="mutter_restore_failed",
                message=f"Failed to restore orientation: {exc}",
            )
