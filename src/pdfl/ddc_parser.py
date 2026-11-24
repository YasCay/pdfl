"""Parsers and value scalers for ddcutil 2.2.5 detect and getvcp output."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class DetectedDdcMonitor:
    """Raw monitor metadata parsed from `ddcutil detect --verbose`."""

    display_number: int
    bus_number: int
    connector: str = ""
    mfg_id: str = ""
    model: str = ""
    product_code: str = ""
    serial: str = ""
    binary_serial: str = ""
    edid_hex: str = ""
    vcp_version: str = ""
    valid: bool = True
    invalid_reason: str = ""


@dataclass
class ParsedVcpValue:
    """Parsed value from `ddcutil getvcp --terse`."""

    code: int
    kind: str  # "C" | "SNC" | "NC" | "CNC" | "ERR" | "T"
    current: int | None = None
    maximum: int | None = None
    raw_line: str = ""


_DISPLAY_HEADER_RE = re.compile(r"^Display\s+(\d+)\s*$")
_BUS_RE = re.compile(r"/dev/i2c-(\d+)")
_CONNECTOR_RE = re.compile(r"^(?:card\d+-)?(.+?)\s*$")
_EDID_LINE_RE = re.compile(r"^\s*\+?[0-9A-Fa-f]{4}\s+([0-9A-Fa-f]{2}(?:\s+[0-9A-Fa-f]{2})*)")


def classify_ddcutil_environment_error(text: str) -> tuple[str, str] | None:
    """Detect environment/permission/lock issues in ddcutil stdout/stderr."""
    lower = text.lower()
    if "flock() for /dev/i2c" in lower or ("max wait time" in lower and "flock" in lower):
        return (
            "bus_locked",
            "An I2C bus is locked by another DDC/CI application (flock).",
        )
    if "no /dev/i2c" in lower or "requires module i2c-dev" in lower:
        return (
            "no_i2c_dev",
            "No /dev/i2c devices found (kernel module i2c-dev not loaded or unavailable in sandbox).",
        )
    if "permission denied" in lower or "eacces" in lower or "access denied" in lower:
        return (
            "permission_denied",
            "Permission denied accessing /dev/i2c-* (user needs read/write access, e.g. i2c group).",
        )
    if "command not found" in lower or "no such file" in lower:
        return ("ddcutil_missing", "ddcutil executable was not found.")
    return None


def classify_ddcutil_bus_error(
    stdout: str, stderr: str = "", exec_err: str = ""
) -> tuple[str, str]:
    """Classify a per-bus ddcutil probe failure into a structured status code and message."""
    if exec_err:
        if "timed out" in exec_err.lower() or "timeout" in exec_err.lower():
            return "timeout", exec_err
        return "error", exec_err

    combined = f"{stdout}\n{stderr}".strip()
    lower = combined.lower()

    if "no monitor detected on bus" in lower:
        return "no_monitor_on_bus", combined

    env_err = classify_ddcutil_environment_error(combined)
    if env_err is not None:
        return env_err

    if "unsupported feature" in lower or "ddcrc_reported_unsupported" in lower:
        return "unsupported_feature", combined

    if (
        "eremoteio" in lower
        or "ddcrc_retries" in lower
        or "maximum retries exceeded" in lower
        or "ddc communication failed" in lower
        or "slave address x37 is unresponsive" in lower
        or "does not support ddc/ci" in lower
        or "eio" in lower
        or re.search(r"\bvcp\s+[0-9a-f]{2}\s+err\b", lower) is not None
    ):
        return (
            "ddc_unresponsive",
            combined or "Monitor did not respond on DDC/CI address 0x37.",
        )

    return "error", combined or "Unknown DDC/CI error."


def parse_ddcutil_detect(
    stdout: str, stderr: str = ""
) -> tuple[list[DetectedDdcMonitor], list[str]]:
    """Parse `ddcutil detect --verbose` output into DetectedDdcMonitor objects."""
    errors: list[str] = []
    combined = f"{stdout}\n{stderr}"
    env_err = classify_ddcutil_environment_error(combined)
    if env_err is not None:
        errors.append(env_err[1])

    monitors: list[DetectedDdcMonitor] = []
    current: dict[str, object] | None = None
    in_edid_dump = False
    edid_bytes: list[str] = []

    def finalize_current() -> None:
        nonlocal current, edid_bytes, in_edid_dump
        if current is None:
            return
        bus_num = current.get("bus_number")
        if current.get("valid", True) and isinstance(bus_num, int):
            monitors.append(
                DetectedDdcMonitor(
                    display_number=int(current.get("display_number", 0)),
                    bus_number=bus_num,
                    connector=str(current.get("connector", "")),
                    mfg_id=str(current.get("mfg_id", "")),
                    model=str(current.get("model", "")),
                    product_code=str(current.get("product_code", "")),
                    serial=str(current.get("serial", "")),
                    binary_serial=str(current.get("binary_serial", "")),
                    edid_hex="".join(edid_bytes).lower(),
                    vcp_version=str(current.get("vcp_version", "")),
                    valid=True,
                )
            )
        current = None
        edid_bytes = []
        in_edid_dump = False

    for raw_line in stdout.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue

        m_disp = _DISPLAY_HEADER_RE.match(stripped)
        if m_disp:
            finalize_current()
            current = {
                "display_number": int(m_disp.group(1)),
                "valid": True,
            }
            continue

        if stripped.startswith("Invalid display"):
            finalize_current()
            current = {
                "display_number": 0,
                "valid": False,
            }
            continue

        if current is None:
            continue

        if in_edid_dump:
            m_edid = _EDID_LINE_RE.match(line)
            if m_edid:
                hex_part = m_edid.group(1).replace(" ", "")
                edid_bytes.append(hex_part)
                continue
            if ":" in stripped and not stripped.startswith("+"):
                in_edid_dump = False

        if stripped.startswith("I2C bus:"):
            m_bus = _BUS_RE.search(stripped)
            if m_bus:
                current["bus_number"] = int(m_bus.group(1))
        elif stripped.startswith(("DRM connector:", "DRM_connector:")):
            val = stripped.split(":", 1)[1].strip()
            m_conn = _CONNECTOR_RE.search(val)
            current["connector"] = m_conn.group(1) if m_conn else val
        elif stripped.startswith("Mfg id:"):
            val = stripped.split(":", 1)[1].strip()
            current["mfg_id"] = val.split()[0] if val else ""
        elif stripped.startswith("Model:"):
            current["model"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Product code:"):
            current["product_code"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Serial number:"):
            val = stripped.split(":", 1)[1].strip()
            if val.lower() not in ("unspecified", "none", "(none)"):
                current["serial"] = val
        elif stripped.startswith("Binary serial number:"):
            val = stripped.split(":", 1)[1].strip()
            if val.lower() not in ("unspecified", "none"):
                current["binary_serial"] = val
        elif stripped.startswith("VCP version:"):
            current["vcp_version"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("EDID hex dump:"):
            in_edid_dump = True
        elif stripped.startswith("DDC communication failed") or "Is the monitor laptop" in stripped:
            current["valid"] = False

    finalize_current()
    return monitors, errors


def _parse_hex_or_int(token: str) -> int | None:
    tok = token.strip().rstrip(",")
    if not tok:
        return None
    if tok.lower().startswith("x"):
        try:
            return int(tok[1:], 16)
        except ValueError:
            return None
    if tok.lower().startswith("0x"):
        try:
            return int(tok, 16)
        except ValueError:
            return None
    try:
        return int(tok, 10)
    except ValueError:
        try:
            return int(tok, 16)
        except ValueError:
            return None


def parse_ddcutil_getvcp_terse(stdout: str) -> dict[int, ParsedVcpValue]:
    """Parse one or more lines of `ddcutil getvcp ... --terse` output."""
    results: dict[int, ParsedVcpValue] = {}
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped.startswith("VCP "):
            continue
        parts = stripped.split()
        if len(parts) < 3:
            continue
        try:
            code = int(parts[1], 16)
        except ValueError:
            continue
        kind = parts[2].upper()
        if kind == "C" and len(parts) >= 5:
            cur = _parse_hex_or_int(parts[3])
            mx = _parse_hex_or_int(parts[4])
            results[code] = ParsedVcpValue(
                code=code, kind="C", current=cur, maximum=mx, raw_line=stripped
            )
        elif kind in ("SNC", "NC") and len(parts) >= 4:
            cur = _parse_hex_or_int(parts[3])
            results[code] = ParsedVcpValue(
                code=code, kind=kind, current=cur, raw_line=stripped
            )
        elif kind == "CNC" and len(parts) >= 4:
            # ddcutil CNC outputs bytes mh ml sh sl (last byte is sl)
            cur = _parse_hex_or_int(parts[-1])
            results[code] = ParsedVcpValue(
                code=code, kind="CNC", current=cur, raw_line=stripped
            )
        elif kind == "ERR":
            results[code] = ParsedVcpValue(
                code=code, kind="ERR", current=None, raw_line=stripped
            )
        else:
            results[code] = ParsedVcpValue(
                code=code, kind=kind, current=None, raw_line=stripped
            )
    return results


def scale_raw_to_percent(raw: int, raw_max: int) -> int:
    """Scale raw hardware value [0..raw_max] to percentage [0..100]."""
    if raw_max <= 0:
        return 0
    if raw_max == 100:
        return max(0, min(100, int(raw)))
    pct = round((raw / raw_max) * 100)
    return max(0, min(100, pct))


def scale_percent_to_raw(percent: int, raw_max: int) -> int:
    """Scale percentage [0..100] to hardware value [0..raw_max]."""
    clamped = max(0, min(100, int(percent)))
    if raw_max <= 0:
        return 0
    if raw_max == 100:
        return clamped
    raw = round((clamped / 100) * raw_max)
    return max(0, min(raw_max, raw))
