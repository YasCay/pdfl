"""Internal laptop backlight driver using /sys/class/backlight + Mutter DisplayConfig / brightnessctl."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from pdfl.ddc_parser import scale_percent_to_raw, scale_raw_to_percent
from pdfl.drivers.base import DiscoveredMonitor
from pdfl.drivers.mutter_display import MutterDisplayClient, MutterDisplayState
from pdfl.models import (
    MonitorCapabilities,
    MonitorInfo,
    MonitorRuntimeHandle,
    OperationResult,
)
from pdfl.planners import build_stable_internal_id


class BacklightDriver:
    """Controls internal display brightness without software dimming fallbacks."""

    name = "backlight"

    def __init__(
        self,
        mutter_client: MutterDisplayClient | None = None,
        sysfs_root: Path | None = None,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        which_fn: Callable[[str], str | None] | None = None,
    ) -> None:
        self._mutter = mutter_client or MutterDisplayClient()
        self._sysfs_root = sysfs_root or Path("/sys/class/backlight")
        self._run = command_runner or subprocess.run
        self._which = which_fn or shutil.which

    def _read_int(self, path: Path) -> int | None:
        try:
            return int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def discover(
        self, mutter_state: MutterDisplayState | None = None
    ) -> tuple[list[DiscoveredMonitor], list[str]]:
        state = mutter_state or self._mutter.get_current_state()
        warnings: list[str] = []
        discovered: list[DiscoveredMonitor] = []

        if not self._sysfs_root.exists():
            return discovered, warnings

        try:
            entries = sorted(self._sysfs_root.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            warnings.append(f"Could not read backlight sysfs: {exc}")
            return discovered, warnings

        has_brightnessctl = self._which("brightnessctl") is not None

        for idx, entry in enumerate(entries, start=1):
            max_b = self._read_int(entry / "max_brightness")
            cur_b = self._read_int(entry / "actual_brightness")
            if cur_b is None:
                cur_b = self._read_int(entry / "brightness")
            if max_b is None or max_b <= 0 or cur_b is None:
                continue

            connector, drm_connector = self._infer_connector(entry, state)
            mutter_mon = state.monitors.get(connector) if state.available else None
            mutter_bl = state.backlights.get(connector) if state.available else None

            display_name = (
                mutter_mon.display_name
                if mutter_mon and mutter_mon.display_name
                else "Built-in Display"
            )
            vendor = mutter_mon.vendor if mutter_mon else ""
            product = mutter_mon.product if mutter_mon else ""
            monitor_id = build_stable_internal_id(
                connector=connector or "eDP-1",
                vendor=vendor,
                product=product,
                sysfs_name=entry.name,
            )

            can_mutter_write = bool(mutter_bl is not None and mutter_bl.active)
            can_sysfs_write = os.access(entry / "brightness", os.W_OK)
            brightness_writable = can_mutter_write or has_brightnessctl or can_sysfs_write

            is_inactive_builtin = bool(
                state.available
                and (
                    (mutter_bl is not None and not mutter_bl.active)
                    or (mutter_mon is not None and not mutter_mon.current_mode_id)
                )
            )
            conn_status = "inactive" if is_inactive_builtin else "connected"
            status_level = "none"
            status_notice: str | None = None
            if not brightness_writable:
                status_level = "info"
                if is_inactive_builtin:
                    status_notice = (
                        "Built-in display is currently inactive (e.g. laptop lid closed) — "
                        "brightness is read-only."
                    )
                else:
                    status_notice = (
                        "Built-in brightness is read-only (no writable backlight interface "
                        "available; software dimming is not used for internal panels)."
                    )
            elif is_inactive_builtin:
                status_level = "info"
                status_notice = "Built-in display is currently inactive (e.g. laptop lid closed)."

            rot_supported, rot_degrees = (
                self._mutter.get_connector_rotation_degrees(state, connector)
                if state.available and connector
                else (False, 0)
            )
            is_primary = False
            if state.available and connector:
                is_primary = any(
                    lm["is_primary"] and connector in lm["connectors"]
                    for lm in state.logical_monitors
                )

            if can_mutter_write and mutter_bl is not None:
                span = max(1, mutter_bl.max_value - mutter_bl.min_value)
                pct = max(
                    0,
                    min(100, round(((mutter_bl.value - mutter_bl.min_value) / span) * 100)),
                )
            else:
                pct = scale_raw_to_percent(cur_b, max_b)

            caps = MonitorCapabilities(
                brightness=True,
                brightness_writable=brightness_writable,
                rotation=rot_supported,
            )
            info = MonitorInfo(
                monitor_id=monitor_id,
                display_name=display_name,
                manufacturer=vendor,
                model=product or entry.name,
                serial="",
                connector=connector,
                monitor_number=idx,
                is_builtin=True,
                is_primary=is_primary,
                driver=self.name,
                capabilities=caps,
                brightness=pct,
                rotation=rot_degrees,
                last_error=None,
                hidden=False,
                connection_status=conn_status,
                ddc_status="not_applicable",
                drm_connector=drm_connector,
                status_level=status_level,
                status_notice=status_notice,
            )
            handle = MonitorRuntimeHandle(
                driver_kind=self.name,
                connector=connector,
                sysfs_path=str(entry),
            )
            discovered.append(DiscoveredMonitor(info=info, handle=handle))

        return discovered, warnings

    def _infer_connector(self, entry: Path, state: MutterDisplayState) -> tuple[str, str]:
        try:
            dev_link = os.readlink(entry / "device")
            base = Path(dev_link).name  # e.g. card1-eDP-1
            if "-" in base and base.startswith("card"):
                return base.split("-", 1)[1], base
        except OSError:
            pass
        if state.available:
            for conn, mon in state.monitors.items():
                if mon.is_builtin:
                    return conn, ""
        return "eDP-1", ""

    def set_feature(
        self, handle: MonitorRuntimeHandle, feature: str, value: int
    ) -> OperationResult:
        if feature != "brightness":
            return OperationResult(
                ok=False,
                error_code="unsupported_feature",
                message=f"Built-in display does not support '{feature}'.",
            )
        clamped = max(0, min(100, int(value)))

        # 1. Prefer Mutter DisplayConfig SetBacklight if active
        if handle.connector:
            mutter_res = self._mutter.set_backlight_percent(handle.connector, clamped)
            if mutter_res.ok:
                return mutter_res

        # 2. Try brightnessctl if installed
        bctl = self._which("brightnessctl")
        if bctl and handle.sysfs_path:
            dev_name = Path(handle.sysfs_path).name
            try:
                proc = self._run(
                    [bctl, "-d", dev_name, "set", f"{clamped}%"],
                    capture_output=True,
                    text=True,
                    timeout=3.0,
                    check=False,
                )
                if proc.returncode == 0:
                    return OperationResult(
                        ok=True,
                        message=f"Brightness set to {clamped}% via brightnessctl.",
                        data={"percent": clamped},
                    )
            except Exception as exc:
                return OperationResult(
                    ok=False,
                    error_code="brightnessctl_failed",
                    message=f"brightnessctl failed: {exc}",
                )

        # 3. Direct sysfs write only if writable without sudo
        if handle.sysfs_path:
            b_path = Path(handle.sysfs_path) / "brightness"
            max_path = Path(handle.sysfs_path) / "max_brightness"
            if os.access(b_path, os.W_OK):
                max_b = self._read_int(max_path) or 100
                raw_val = scale_percent_to_raw(clamped, max_b)
                try:
                    b_path.write_text(f"{raw_val}\n", encoding="utf-8")
                    return OperationResult(
                        ok=True,
                        message=f"Brightness set to {clamped}% via sysfs.",
                        data={"percent": clamped, "raw": raw_val},
                    )
                except OSError as exc:
                    return OperationResult(
                        ok=False,
                        error_code="sysfs_write_failed",
                        message=f"Sysfs write error: {exc}",
                    )

        return OperationResult(
            ok=False,
            error_code="backlight_not_writable",
            message=(
                "No write method available for internal display (Mutter backlight inactive, "
                "brightnessctl missing, sysfs not writable)."
            ),
        )
