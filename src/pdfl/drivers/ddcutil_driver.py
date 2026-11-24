"""External DDC/CI monitor driver backed by DRM/EDID sysfs probing and ddcutil 2.2.5."""

from __future__ import annotations

import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from pdfl.ddc_parser import (
    DetectedDdcMonitor,
    ParsedVcpValue,
    classify_ddcutil_bus_error,
    classify_ddcutil_environment_error,
    parse_ddcutil_detect,
    parse_ddcutil_getvcp_terse,
    scale_percent_to_raw,
    scale_raw_to_percent,
)
from pdfl.drivers.base import DiscoveredMonitor
from pdfl.drivers.drm_probe import (
    DrmConnectorInfo,
    find_locked_i2c_buses,
    inspect_i2c_bus,
    scan_drm_connectors,
)
from pdfl.drivers.mutter_display import MutterDisplayClient, MutterDisplayState
from pdfl.mccs_parser import parse_ddcutil_capabilities_output
from pdfl.models import (
    MonitorCapabilities,
    MonitorInfo,
    MonitorRuntimeHandle,
    OperationResult,
    VcpFeatureSpec,
)
from pdfl.planners import build_stable_ddc_id
from pdfl.vcp_names import (
    FEATURE_TO_VCP,
    VCP_BRIGHTNESS,
    VCP_COLOR_TEMP,
    VCP_CONTRAST,
    VCP_INPUT_SOURCE,
    VCP_POWER_MODE,
    VCP_VOLUME,
    get_value_name,
)

_STATUS_PRIORITY: dict[str, int] = {
    "bus_locked": 1,
    "permission_denied": 2,
    "ddc_unresponsive": 3,
    "timeout": 4,
    "error": 5,
    "missing_device": 6,
    "no_monitor_on_bus": 7,
    "no_i2c_bus": 8,
}


def _connector_key(connector: str) -> str:
    conn = (connector or "").strip()
    if conn.startswith("card") and "-" in conn:
        conn = conn.split("-", 1)[1]
    return conn.replace("HDMI-A-", "HDMI-")


def _format_ddc_failure_message(
    status: str,
    connector: str,
    bus_number: int | None,
    lock_pid: int | None = None,
    lock_process: str = "",
    detail: str = "",
) -> str:
    bus_label = f"/dev/i2c-{bus_number}" if bus_number is not None else "/dev/i2c-*"
    if status == "bus_locked":
        if lock_process and lock_pid is not None:
            holder = f"{lock_process}, PID {lock_pid}"
        elif lock_pid is not None:
            holder = f"PID {lock_pid}"
        else:
            holder = "another process"
        return (
            f"I2C bus {bus_label} is locked by another process ({holder}). "
            "Please close the other DDC/CI application and rescan."
        )
    if status == "permission_denied":
        return f"No read/write access to {bus_label} (check user permissions for I2C, e.g. i2c group)."
    if status == "ddc_unresponsive":
        return (
            f"DRM monitor detected, but DDC/CI on {bus_label} (address 0x37) did not respond "
            "(EREMOTEIO / no reply). Please enable DDC/CI in the monitor OSD and check your "
            "USB-C hub/dock or cable."
        )
    if status == "timeout":
        return f"DDC/CI timed out on {bus_label}; monitor remains visible via DRM/Mutter."
    if status in ("no_i2c_bus", "missing_device", "no_monitor_on_bus"):
        return f"No accessible /dev/i2c bus found for DRM connector {connector or '-'} (check i2c-dev)."
    return detail or f"DDC/CI error on {bus_label}."


class DdcUtilDriver:
    """DDC/CI monitor discovery and control via DRM/EDID mapping & ddcutil."""

    name = "ddcutil"

    def __init__(
        self,
        mutter_client: MutterDisplayClient | None = None,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        which_fn: Callable[[str], str | None] | None = None,
        detect_timeout: float = 8.0,
        caps_timeout: float = 8.0,
        vcp_timeout: float = 5.0,
        probe_timeout: float = 4.0,
        max_workers: int = 2,
        drm_root: Path | None = None,
        dev_root: Path | None = None,
        proc_locks_path: Path | None = None,
        proc_root: Path | None = None,
        i2c_sysfs_root: Path | None = None,
        lock_wait_seconds: float = 0.25,
    ) -> None:
        self._mutter = mutter_client or MutterDisplayClient()
        self._run = command_runner or subprocess.run
        self._which = which_fn or shutil.which
        self._detect_timeout = detect_timeout
        self._caps_timeout = caps_timeout
        self._vcp_timeout = vcp_timeout
        self._probe_timeout = probe_timeout
        self._max_workers = max(1, max_workers)

        is_live = command_runner is None
        self._drm_root = drm_root if drm_root is not None else (Path("/sys/class/drm") if is_live else None)
        self._dev_root = dev_root if dev_root is not None else (Path("/dev") if is_live else None)
        self._proc_locks_path = proc_locks_path or Path("/proc/locks")
        self._proc_root = proc_root or Path("/proc")
        self._i2c_sysfs_root = (
            i2c_sysfs_root
            if i2c_sysfs_root is not None
            else (Path("/sys/bus/i2c/devices") if is_live else None)
        )
        self._lock_wait_seconds = max(0.0, float(lock_wait_seconds))

        self._bus_locks_guard = threading.Lock()
        self._bus_locks: dict[int, threading.Lock] = {}

    def _bus_lock(self, bus_number: int) -> threading.Lock:
        with self._bus_locks_guard:
            lock = self._bus_locks.get(bus_number)
            if lock is None:
                lock = threading.Lock()
                self._bus_locks[bus_number] = lock
            return lock

    def _exec_ddcutil(
        self, args: list[str], timeout: float
    ) -> tuple[bool, str, str, str]:
        exe = self._which("ddcutil")
        if not exe:
            return False, "", "", "ddcutil executable was not found in PATH."
        cmd = [exe, *args]
        try:
            proc = self._run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            return proc.returncode == 0, proc.stdout or "", proc.stderr or "", ""
        except subprocess.TimeoutExpired:
            return (
                False,
                "",
                "",
                f"DDC/CI timed out after {timeout:.1f}s ({' '.join(args)}).",
            )
        except OSError as exc:
            return False, "", "", f"Failed to execute ddcutil: {exc}"

    def _resolve_mutter_connector(
        self, state: MutterDisplayState, connector: str
    ) -> str:
        if not state.available or not connector:
            return connector
        if connector in state.monitors:
            return connector
        alt = connector.replace("HDMI-A-", "HDMI-")
        if alt in state.monitors:
            return alt
        return connector

    def _mutter_metadata(
        self, state: MutterDisplayState, connector: str
    ) -> tuple[str, object | None, bool, tuple[bool, int]]:
        mutter_conn = self._resolve_mutter_connector(state, connector)
        mutter_mon = (
            state.monitors.get(mutter_conn) if state.available and mutter_conn else None
        )
        rot_supported, rot_degrees = (
            self._mutter.get_connector_rotation_degrees(state, mutter_conn)
            if state.available and mutter_conn
            else (False, 0)
        )
        is_primary = False
        if state.available and mutter_conn:
            is_primary = any(
                lm["is_primary"] and mutter_conn in lm["connectors"]
                for lm in state.logical_monitors
            )
        return mutter_conn, mutter_mon, is_primary, (rot_supported, rot_degrees)

    def _build_drm_only_monitor(
        self,
        drm_conn: DrmConnectorInfo,
        state: MutterDisplayState,
        ddc_status: str,
        bus_number: int | None,
        last_error: str,
        display_index: int = 0,
    ) -> DiscoveredMonitor:
        mutter_conn, mutter_mon, is_primary, (rot_supported, rot_degrees) = (
            self._mutter_metadata(state, drm_conn.connector)
        )
        edid = drm_conn.edid
        mfg_id = (edid.mfg_id if edid else "") or (mutter_mon.vendor if mutter_mon else "")
        model = (edid.model if edid else "") or (mutter_mon.product if mutter_mon else "")
        serial = edid.serial if edid else ""
        if not serial and mutter_mon and mutter_mon.serial not in ("", "0x00000000", "unspecified"):
            serial = mutter_mon.serial
        binary_serial = edid.binary_serial if edid else ""
        edid_hex = edid.edid_hex if edid else ""

        stable_id = build_stable_ddc_id(
            mfg_id=mfg_id,
            model=model,
            serial=serial or binary_serial,
            edid_hex=edid_hex,
            connector=mutter_conn or drm_conn.connector,
            fallback_index=display_index,
        )
        display_name = (
            model
            or (mutter_mon.display_name if mutter_mon else "")
            or mutter_conn
            or drm_conn.connector
            or "External Display"
        )
        caps = MonitorCapabilities(rotation=rot_supported)
        info = MonitorInfo(
            monitor_id=stable_id,
            display_name=display_name,
            manufacturer=mfg_id,
            model=model,
            serial=serial or binary_serial,
            connector=mutter_conn or drm_conn.connector,
            monitor_number=display_index,
            is_builtin=False,
            is_primary=is_primary,
            driver=self.name,
            capabilities=caps,
            rotation=rot_degrees,
            last_error=last_error or None,
            connection_status=drm_conn.status or "connected",
            ddc_status=ddc_status,
            i2c_bus=bus_number,
            drm_connector=drm_conn.drm_name,
        )
        handle = MonitorRuntimeHandle(
            driver_kind=self.name,
            bus_number=bus_number,
            ddc_display_num=None,
            connector=mutter_conn or drm_conn.connector,
        )
        return DiscoveredMonitor(info=info, handle=handle)

    def _discover_drm_connector(
        self, drm_conn: DrmConnectorInfo, state: MutterDisplayState
    ) -> DiscoveredMonitor:
        if not drm_conn.candidate_buses:
            msg = _format_ddc_failure_message("no_i2c_bus", drm_conn.connector, None)
            return self._build_drm_only_monitor(
                drm_conn, state, ddc_status="no_i2c_bus", bus_number=None, last_error=msg
            )

        if not self._which("ddcutil"):
            return self._build_drm_only_monitor(
                drm_conn,
                state,
                ddc_status="error",
                bus_number=drm_conn.candidate_buses[0],
                last_error="ddcutil executable was not found in PATH.",
            )

        edid = drm_conn.edid
        _, mutter_mon, _, _ = self._mutter_metadata(state, drm_conn.connector)
        mfg_id = (edid.mfg_id if edid else "") or (mutter_mon.vendor if mutter_mon else "")
        model = (edid.model if edid else "") or (mutter_mon.product if mutter_mon else "")
        serial = edid.serial if edid else ""
        if not serial and mutter_mon and mutter_mon.serial not in ("", "0x00000000", "unspecified"):
            serial = mutter_mon.serial
        binary_serial = edid.binary_serial if edid else ""
        edid_hex = edid.edid_hex if edid else ""
        product_code = edid.product_code if edid else ""

        outcomes: list[tuple[int, str, int, int | None, str, str]] = []

        for bus_num in drm_conn.candidate_buses:
            if self._dev_root is not None:
                probe = inspect_i2c_bus(
                    bus_num,
                    dev_root=self._dev_root,
                    proc_locks_path=self._proc_locks_path,
                    proc_root=self._proc_root,
                    i2c_sysfs_root=self._i2c_sysfs_root,
                    lock_wait_seconds=self._lock_wait_seconds,
                    is_dp_aux=(bus_num in drm_conn.dp_aux_buses),
                )
                if probe.status != "ready":
                    msg = _format_ddc_failure_message(
                        probe.status,
                        drm_conn.connector,
                        bus_num,
                        lock_pid=probe.lock_pid,
                        lock_process=probe.lock_process,
                        detail=probe.detail,
                    )
                    outcomes.append(
                        (
                            _STATUS_PRIORITY.get(probe.status, 9),
                            probe.status,
                            bus_num,
                            probe.lock_pid,
                            probe.lock_process,
                            msg,
                        )
                    )
                    continue

            lock = self._bus_lock(bus_num)
            with lock:
                get_ok, get_out, get_err, get_exec_err = self._exec_ddcutil(
                    ["--bus", str(bus_num), "--terse", "getvcp", f"{VCP_BRIGHTNESS:02x}"],
                    self._probe_timeout,
                )

            parsed_vcp = parse_ddcutil_getvcp_terse(get_out)
            b_val = parsed_vcp.get(VCP_BRIGHTNESS)
            if (
                not get_exec_err
                and b_val is not None
                and b_val.kind == "C"
                and b_val.current is not None
            ):
                mon = DetectedDdcMonitor(
                    display_number=0,
                    bus_number=bus_num,
                    connector=drm_conn.connector,
                    mfg_id=mfg_id,
                    model=model,
                    product_code=product_code,
                    serial=serial,
                    binary_serial=binary_serial,
                    edid_hex=edid_hex,
                    valid=True,
                )
                return self._inspect_monitor(
                    mon,
                    state,
                    drm_connector=drm_conn.drm_name,
                    pre_vcp_values={VCP_BRIGHTNESS: b_val},
                )

            status_code, raw_detail = classify_ddcutil_bus_error(
                get_out, get_err, get_exec_err
            )

            if status_code == "unsupported_feature":
                with self._bus_lock(bus_num):
                    caps_res = self._exec_ddcutil(
                        ["--bus", str(bus_num), "capabilities"], self._caps_timeout
                    )
                parsed_caps = parse_ddcutil_capabilities_output(caps_res[1])
                if parsed_caps.capabilities.vcp_features:
                    mon = DetectedDdcMonitor(
                        display_number=0,
                        bus_number=bus_num,
                        connector=drm_conn.connector,
                        mfg_id=mfg_id,
                        model=model,
                        product_code=product_code,
                        serial=serial,
                        binary_serial=binary_serial,
                        edid_hex=edid_hex,
                        valid=True,
                    )
                    return self._inspect_monitor(
                        mon,
                        state,
                        drm_connector=drm_conn.drm_name,
                        pre_vcp_values={
                            VCP_BRIGHTNESS: ParsedVcpValue(
                                code=VCP_BRIGHTNESS, kind="ERR", raw_line=raw_detail
                            )
                        },
                        pre_caps=caps_res,
                    )
                status_code = "error"

            if get_ok and b_val is not None and b_val.kind != "ERR":
                # Non-continuous or partial VCP 0x10 response; still inspect full capabilities
                mon = DetectedDdcMonitor(
                    display_number=0,
                    bus_number=bus_num,
                    connector=drm_conn.connector,
                    mfg_id=mfg_id,
                    model=model,
                    product_code=product_code,
                    serial=serial,
                    binary_serial=binary_serial,
                    edid_hex=edid_hex,
                    valid=True,
                )
                return self._inspect_monitor(
                    mon,
                    state,
                    drm_connector=drm_conn.drm_name,
                    pre_vcp_values={VCP_BRIGHTNESS: b_val},
                )

            msg = _format_ddc_failure_message(
                status_code, drm_conn.connector, bus_num, detail=raw_detail
            )
            outcomes.append(
                (
                    _STATUS_PRIORITY.get(status_code, 9),
                    status_code,
                    bus_num,
                    None,
                    "",
                    msg,
                )
            )

        if outcomes:
            outcomes.sort(key=lambda item: item[0])
            _, best_status, best_bus, _, _, best_msg = outcomes[0]
            if best_status in ("no_monitor_on_bus", "missing_device"):
                best_status = "no_i2c_bus"
                best_msg = _format_ddc_failure_message(
                    "no_i2c_bus", drm_conn.connector, best_bus
                )
            return self._build_drm_only_monitor(
                drm_conn,
                state,
                ddc_status=best_status,
                bus_number=best_bus,
                last_error=best_msg,
            )

        msg = _format_ddc_failure_message("no_i2c_bus", drm_conn.connector, None)
        return self._build_drm_only_monitor(
            drm_conn, state, ddc_status="no_i2c_bus", bus_number=None, last_error=msg
        )

    def discover(
        self, mutter_state: MutterDisplayState | None = None
    ) -> tuple[list[DiscoveredMonitor], list[str]]:
        state = mutter_state or self._mutter.get_current_state()
        discovered_by_conn: dict[str, DiscoveredMonitor] = {}
        ordered_keys: list[str] = []
        errors: list[str] = []

        drm_externals: list[DrmConnectorInfo] = []
        if self._drm_root is not None and self._drm_root.exists():
            drm_externals = [
                c
                for c in scan_drm_connectors(self._drm_root, self._i2c_sysfs_root)
                if not c.is_builtin
            ]

        # Phase 1: Targeted per-bus probing for DRM connectors with mapped I2C buses
        with_buses = [c for c in drm_externals if c.candidate_buses]
        without_buses = [c for c in drm_externals if not c.candidate_buses]

        if with_buses:
            if len(with_buses) == 1 or self._max_workers == 1:
                phase1_results = [
                    self._discover_drm_connector(c, state) for c in with_buses
                ]
            else:
                with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
                    phase1_results = list(
                        pool.map(lambda c: self._discover_drm_connector(c, state), with_buses)
                    )
            for item in phase1_results:
                key = _connector_key(item.info.connector)
                discovered_by_conn[key] = item
                if key not in ordered_keys:
                    ordered_keys.append(key)

        # Phase 2: Fallback `ddcutil detect --verbose` if DRM sysfs unavailable or incomplete
        need_detect_fallback = (
            self._drm_root is None
            or not drm_externals
            or bool(without_buses)
        )
        detect_timed_out = False

        if need_detect_fallback:
            ignore_args: list[str] = []
            if self._dev_root is not None:
                locked = find_locked_i2c_buses(
                    dev_root=self._dev_root,
                    proc_locks_path=self._proc_locks_path,
                    proc_root=self._proc_root,
                    i2c_sysfs_root=self._i2c_sysfs_root,
                    lock_wait_seconds=min(0.05, self._lock_wait_seconds),
                )
                for bus_num in sorted(locked):
                    ignore_args.extend(["--ignore-bus", str(bus_num)])

            ok, stdout, stderr, exec_err = self._exec_ddcutil(
                [*ignore_args, "detect", "--verbose"], self._detect_timeout
            )
            if exec_err:
                detect_timed_out = "timed out" in exec_err.lower() or "timeout" in exec_err.lower()
                if not discovered_by_conn:
                    errors.append(exec_err)
            else:
                detected, detect_errors = parse_ddcutil_detect(stdout, stderr)
                if not detected:
                    if not ok and not detect_errors:
                        env_err = classify_ddcutil_environment_error(f"{stdout}\n{stderr}")
                        if env_err:
                            detect_errors.append(env_err[1])
                        elif stderr.strip():
                            detect_errors.append(stderr.strip())
                    if not discovered_by_conn:
                        errors.extend(detect_errors)
                else:
                    errors.extend(detect_errors)
                    to_inspect: list[DetectedDdcMonitor] = []
                    for mon in detected:
                        key = _connector_key(mon.connector)
                        existing = key and discovered_by_conn.get(key)
                        if existing and existing.info.ddc_status == "available":
                            continue
                        to_inspect.append(mon)

                    if len(to_inspect) <= 1 or self._max_workers == 1:
                        inspected = [self._inspect_monitor(m, state) for m in to_inspect]
                    else:
                        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
                            inspected = list(
                                pool.map(lambda m: self._inspect_monitor(m, state), to_inspect)
                            )
                    for item in inspected:
                        key = _connector_key(item.info.connector) or f"bus-{item.handle.bus_number}"
                        discovered_by_conn[key] = item
                        if key not in ordered_keys:
                            ordered_keys.append(key)

        # Emit DRM-visible monitors for connectors without mapped I2C buses if detect didn't find them
        for idx, drm_conn in enumerate(without_buses, start=1):
            key = _connector_key(drm_conn.connector)
            if key in discovered_by_conn:
                continue
            status = "timeout" if detect_timed_out else "no_i2c_bus"
            msg = _format_ddc_failure_message(status, drm_conn.connector, None)
            discovered_by_conn[key] = self._build_drm_only_monitor(
                drm_conn,
                state,
                ddc_status=status,
                bus_number=None,
                last_error=msg,
                display_index=idx,
            )
            ordered_keys.append(key)

        # Phase 3: Mutter external fallback for active external displays not yet in discovered
        if state.available:
            for conn, mutter_mon in state.monitors.items():
                if mutter_mon.is_builtin:
                    continue
                key = _connector_key(conn)
                if key in discovered_by_conn:
                    continue
                status = "timeout" if detect_timed_out else "no_i2c_bus"
                msg = (
                    f"DDC/CI timed out; monitor {conn} remains visible via Mutter."
                    if detect_timed_out
                    else f"No DDC/CI I2C bus found for {conn}; monitor visible via Mutter."
                )
                synth = DrmConnectorInfo(
                    drm_name="",
                    connector=conn,
                    status="connected",
                    enabled=bool(mutter_mon.current_mode_id),
                    dpms="On",
                    is_builtin=False,
                    edid=None,
                )
                discovered_by_conn[key] = self._build_drm_only_monitor(
                    synth,
                    state,
                    ddc_status=status,
                    bus_number=None,
                    last_error=msg,
                )
                ordered_keys.append(key)

        discovered = [discovered_by_conn[k] for k in ordered_keys if k in discovered_by_conn]
        return discovered, errors

    def _inspect_monitor(
        self,
        mon: DetectedDdcMonitor,
        state: MutterDisplayState,
        drm_connector: str = "",
        pre_vcp_values: dict[int, ParsedVcpValue] | None = None,
        pre_caps: tuple[bool, str, str, str] | None = None,
    ) -> DiscoveredMonitor:
        lock = self._bus_lock(mon.bus_number)
        warnings: list[str] = []

        with lock:
            if pre_caps is not None:
                caps_ok, caps_out, caps_err, caps_exec_err = pre_caps
            else:
                caps_ok, caps_out, caps_err, caps_exec_err = self._exec_ddcutil(
                    ["--bus", str(mon.bus_number), "capabilities"], self._caps_timeout
                )
            parsed_caps = parse_ddcutil_capabilities_output(caps_out)
            if caps_exec_err:
                warnings.append(caps_exec_err)
            elif not caps_ok and not parsed_caps.capabilities.vcp_features:
                msg = caps_err.strip() or "Failed to query monitor capabilities."
                warnings.append(msg)

            vcp_features = dict(parsed_caps.capabilities.vcp_features)
            vcp_values: dict[int, ParsedVcpValue] = dict(pre_vcp_values or {})

            codes_to_probe: list[int] = []
            for candidate in (
                VCP_BRIGHTNESS,
                VCP_CONTRAST,
                VCP_VOLUME,
                VCP_COLOR_TEMP,
                VCP_INPUT_SOURCE,
                VCP_POWER_MODE,
            ):
                if candidate in vcp_values:
                    continue
                if candidate == VCP_BRIGHTNESS or candidate in vcp_features:
                    codes_to_probe.append(candidate)

            if codes_to_probe:
                hex_codes = [f"{c:02x}" for c in codes_to_probe]
                get_ok, get_out, get_err, get_exec_err = self._exec_ddcutil(
                    ["--bus", str(mon.bus_number), "--terse", "getvcp", *hex_codes],
                    self._vcp_timeout,
                )
                if get_exec_err:
                    warnings.append(get_exec_err)
                elif not get_ok and not get_out.strip() and get_err.strip():
                    warnings.append(get_err.strip())
                vcp_values.update(parse_ddcutil_getvcp_terse(get_out))

        # Evaluate capabilities strictly based on evidence
        b_val = vcp_values.get(VCP_BRIGHTNESS)
        supports_brightness = (
            VCP_BRIGHTNESS in vcp_features
            or (b_val is not None and b_val.kind == "C" and b_val.current is not None)
        ) and not (b_val is not None and b_val.kind == "ERR")

        c_val = vcp_values.get(VCP_CONTRAST)
        supports_contrast = (
            VCP_CONTRAST in vcp_features
            and c_val is not None
            and c_val.kind != "ERR"
            and c_val.current is not None
        )

        v_val = vcp_values.get(VCP_VOLUME)
        supports_volume = (
            VCP_VOLUME in vcp_features
            and v_val is not None
            and v_val.kind != "ERR"
            and v_val.current is not None
        )

        ct_val = vcp_values.get(VCP_COLOR_TEMP)
        supports_color_temp = VCP_COLOR_TEMP in vcp_features and not (
            ct_val is not None and ct_val.kind == "ERR"
        )

        in_val = vcp_values.get(VCP_INPUT_SOURCE)
        supports_input = VCP_INPUT_SOURCE in vcp_features and not (
            in_val is not None and in_val.kind == "ERR"
        )

        pw_val = vcp_values.get(VCP_POWER_MODE)
        supports_power = VCP_POWER_MODE in vcp_features and not (
            pw_val is not None and pw_val.kind == "ERR"
        )

        # Update raw_max / current_raw in vcp_features
        for code, parsed in vcp_values.items():
            if parsed.kind == "ERR":
                continue
            spec = vcp_features.get(code)
            if spec is None:
                spec = VcpFeatureSpec(code=code, name=f"VCP 0x{code:02X}")
                vcp_features[code] = spec
            if parsed.maximum is not None and parsed.maximum > 0:
                spec.raw_max = parsed.maximum
            spec.current_raw = parsed.current

        brightness_pct = (
            scale_raw_to_percent(b_val.current, b_val.maximum or 100)
            if supports_brightness and b_val is not None and b_val.current is not None
            else None
        )
        contrast_pct = (
            scale_raw_to_percent(c_val.current, c_val.maximum or 100)
            if supports_contrast and c_val is not None and c_val.current is not None
            else None
        )
        volume_pct = (
            scale_raw_to_percent(v_val.current, v_val.maximum or 100)
            if supports_volume and v_val is not None and v_val.current is not None
            else None
        )

        def discrete_options(code: int, fallback_vals: list[int] | None = None) -> list[dict[str, object]]:
            spec = vcp_features.get(code)
            vals = list(spec.supported_values) if spec and spec.supported_values else list(fallback_vals or [])
            return [{"value": val, "name": get_value_name(code, val)} for val in vals]

        stable_id = build_stable_ddc_id(
            mfg_id=mon.mfg_id,
            model=mon.model or parsed_caps.capabilities.model,
            serial=mon.serial or mon.binary_serial,
            edid_hex=mon.edid_hex,
            connector=mon.connector,
            fallback_index=mon.display_number,
        )

        mutter_conn, mutter_mon, is_primary, (rot_supported, rot_degrees) = (
            self._mutter_metadata(state, mon.connector)
        )
        display_name = (
            mon.model
            or parsed_caps.capabilities.model
            or (mutter_mon.display_name if mutter_mon else "")
            or f"Monitor {mon.display_number}"
        )

        caps = MonitorCapabilities(
            brightness=supports_brightness,
            contrast=supports_contrast,
            volume=supports_volume,
            input_source=supports_input,
            color_temperature=supports_color_temp,
            power_state=supports_power,
            rotation=rot_supported,
            brightness_writable=supports_brightness,
            raw_capabilities=parsed_caps.capabilities.raw,
            mccs_version=parsed_caps.capabilities.mccs_version or mon.vcp_version,
            vcp_features=vcp_features,
        )

        info = MonitorInfo(
            monitor_id=stable_id,
            display_name=display_name,
            manufacturer=mon.mfg_id,
            model=mon.model or parsed_caps.capabilities.model,
            serial=mon.serial or mon.binary_serial,
            connector=mutter_conn or mon.connector,
            monitor_number=mon.display_number,
            is_builtin=False,
            is_primary=is_primary,
            driver=self.name,
            capabilities=caps,
            brightness=brightness_pct,
            contrast=contrast_pct,
            volume=volume_pct,
            input_source=in_val.current if in_val and in_val.kind != "ERR" else None,
            color_temperature=ct_val.current if ct_val and ct_val.kind != "ERR" else None,
            power_state=pw_val.current if pw_val and pw_val.kind != "ERR" else None,
            rotation=rot_degrees,
            available_inputs=discrete_options(VCP_INPUT_SOURCE) if supports_input else [],
            available_color_presets=(
                discrete_options(VCP_COLOR_TEMP) if supports_color_temp else []
            ),
            available_power_states=(
                discrete_options(VCP_POWER_MODE, [0x01, 0x02, 0x04])
                if supports_power
                else []
            ),
            last_error="; ".join(warnings) if warnings else None,
            connection_status="connected",
            ddc_status="available",
            i2c_bus=mon.bus_number,
            drm_connector=drm_connector,
        )
        handle = MonitorRuntimeHandle(
            driver_kind=self.name,
            bus_number=mon.bus_number,
            ddc_display_num=mon.display_number,
            connector=mutter_conn or mon.connector,
        )
        return DiscoveredMonitor(info=info, handle=handle)

    def set_feature(
        self,
        handle: MonitorRuntimeHandle,
        feature: str,
        value: int,
        raw_max: int = 100,
    ) -> OperationResult:
        if handle.bus_number is None:
            return OperationResult(
                ok=False,
                error_code="missing_bus",
                message="No valid I2C bus known for this DDC/CI monitor.",
            )
        code = FEATURE_TO_VCP.get(feature)
        if code is None:
            return OperationResult(
                ok=False,
                error_code="unsupported_feature",
                message=f"Unknown DDC/CI feature: {feature}",
            )

        if self._dev_root is not None:
            probe = inspect_i2c_bus(
                handle.bus_number,
                dev_root=self._dev_root,
                proc_locks_path=self._proc_locks_path,
                proc_root=self._proc_root,
                i2c_sysfs_root=self._i2c_sysfs_root,
                lock_wait_seconds=self._lock_wait_seconds,
            )
            if probe.status == "bus_locked":
                return OperationResult(
                    ok=False,
                    error_code="ddc_bus_locked",
                    message=_format_ddc_failure_message(
                        "bus_locked",
                        handle.connector,
                        handle.bus_number,
                        lock_pid=probe.lock_pid,
                        lock_process=probe.lock_process,
                        detail=probe.detail,
                    ),
                )
            if probe.status == "permission_denied":
                return OperationResult(
                    ok=False,
                    error_code="ddc_permission_denied",
                    message=_format_ddc_failure_message(
                        "permission_denied", handle.connector, handle.bus_number
                    ),
                )
            if probe.status == "missing_device":
                return OperationResult(
                    ok=False,
                    error_code="ddc_missing_device",
                    message=_format_ddc_failure_message(
                        "missing_device", handle.connector, handle.bus_number
                    ),
                )

        if feature in ("brightness", "contrast", "volume"):
            target_val = scale_percent_to_raw(value, raw_max)
            val_arg = str(target_val)
        else:
            target_val = int(value)
            val_arg = f"0x{target_val:02x}"

        lock = self._bus_lock(handle.bus_number)
        with lock:
            ok, stdout, stderr, exec_err = self._exec_ddcutil(
                [
                    "--bus",
                    str(handle.bus_number),
                    "setvcp",
                    f"{code:02x}",
                    val_arg,
                ],
                self._vcp_timeout,
            )

        if exec_err:
            return OperationResult(
                ok=False, error_code="ddc_timeout_or_exec", message=exec_err
            )
        if not ok:
            err_msg = stderr.strip() or stdout.strip() or f"ddcutil setvcp 0x{code:02X} failed."
            return OperationResult(
                ok=False, error_code="ddc_set_failed", message=err_msg
            )
        return OperationResult(
            ok=True,
            message=f"VCP 0x{code:02X} set to {val_arg}.",
            data={"raw": target_val},
        )
