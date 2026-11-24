"""DRM connector, EDID, and non-blocking /dev/i2c-* flock inspection helpers."""

from __future__ import annotations

import errno
import fcntl
import os
import re
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path

_CARD_CONN_RE = re.compile(r"^card\d+-(.+)$")
_I2C_DIR_RE = re.compile(r"^i2c-(\d+)$")


@dataclass
class EdidInfo:
    """Decoded basic monitor metadata from raw EDID bytes."""

    mfg_id: str = ""
    model: str = ""
    product_code: str = ""
    serial: str = ""
    binary_serial: str = ""
    edid_hex: str = ""


@dataclass
class DrmConnectorInfo:
    """Connected DRM connector metadata from /sys/class/drm."""

    drm_name: str
    connector: str
    status: str
    enabled: bool
    dpms: str
    is_builtin: bool
    edid: EdidInfo | None = None
    candidate_buses: list[int] = field(default_factory=list)
    bus_names: dict[int, str] = field(default_factory=dict)
    dp_aux_buses: set[int] = field(default_factory=set)


@dataclass
class I2cBusProbe:
    """Non-blocking accessibility and flock status of a /dev/i2c-N device."""

    bus_number: int
    dev_path: str
    adapter_name: str = ""
    is_dp_aux: bool = False
    status: str = "ready"  # "ready" | "missing_device" | "permission_denied" | "bus_locked" | "error"
    lock_pid: int | None = None
    lock_process: str = ""
    detail: str = ""


def parse_edid_bytes(data: bytes) -> EdidInfo | None:
    """Parse standard 128/256-byte EDID binary payload."""
    if len(data) < 128 or data[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return None

    mfg_raw = struct.unpack(">H", data[8:10])[0]
    mfg_chars = [chr(((mfg_raw >> shift) & 0x1F) + 64) for shift in (10, 5, 0)]
    mfg_id = "".join(mfg_chars) if all("A" <= ch <= "Z" for ch in mfg_chars) else ""

    prod = struct.unpack("<H", data[10:12])[0]
    product_code = f"0x{prod:04x}"

    bin_ser = struct.unpack("<I", data[12:16])[0]
    binary_serial = str(bin_ser) if bin_ser != 0 else ""

    model = ""
    serial = ""
    for off in (54, 72, 90, 108):
        blk = data[off : off + 18]
        if len(blk) < 18 or blk[0:3] != b"\x00\x00\x00":
            continue
        tag = blk[3]
        text = blk[5:18].decode("latin-1", errors="ignore").replace("\x00", "").strip()
        if not text:
            continue
        if tag == 0xFC and not model:
            model = text
        elif tag == 0xFF and not serial:
            serial = text

    return EdidInfo(
        mfg_id=mfg_id,
        model=model,
        product_code=product_code,
        serial=serial,
        binary_serial=binary_serial,
        edid_hex=data[:128].hex().lower(),
    )


def _read_text_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return ""


def _read_bytes_safe(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def _extract_bus_from_link(path: Path) -> int | None:
    if not path.is_symlink() and not path.exists():
        return None
    try:
        target = os.path.realpath(path)
    except OSError:
        try:
            target = os.readlink(path)
        except OSError:
            return None
    m = _I2C_DIR_RE.search(Path(target).name)
    if m:
        return int(m.group(1))
    try:
        raw_link = os.readlink(path)
        m2 = _I2C_DIR_RE.search(Path(raw_link).name)
        if m2:
            return int(m2.group(1))
    except OSError:
        pass
    return None


def scan_drm_connectors(
    drm_root: Path = Path("/sys/class/drm"),
    i2c_sysfs_root: Path | None = Path("/sys/bus/i2c/devices"),
) -> list[DrmConnectorInfo]:
    """Scan connected DRM connectors and map candidate DP-AUX/DDC I2C buses."""
    if not drm_root.exists():
        return []

    try:
        entries = sorted(drm_root.iterdir(), key=lambda p: p.name)
    except OSError:
        return []

    connectors: list[DrmConnectorInfo] = []
    for entry in entries:
        m_conn = _CARD_CONN_RE.match(entry.name)
        if not m_conn:
            continue
        status_file = entry / "status"
        if not status_file.exists():
            continue
        status = _read_text_safe(status_file).lower()
        if status != "connected":
            continue

        connector = m_conn.group(1)
        is_builtin = connector.lower().startswith(("edp-", "lvds-", "dsi-"))
        enabled = _read_text_safe(entry / "enabled").lower() == "enabled"
        dpms = _read_text_safe(entry / "dpms")

        edid_bytes = _read_bytes_safe(entry / "edid")
        edid = parse_edid_bytes(edid_bytes) if edid_bytes else None

        candidate_buses: list[int] = []
        bus_names: dict[int, str] = {}
        dp_aux_buses: set[int] = set()

        # 1. Prioritize DP-AUX I2C-over-AUX child adapters (e.g. card1-DP-1/i2c-9)
        aux_candidates: list[tuple[int, Path]] = []
        try:
            children = list(entry.iterdir())
        except OSError:
            children = []

        for child in children:
            m_bus = _I2C_DIR_RE.match(child.name)
            if m_bus:
                aux_candidates.append((int(m_bus.group(1)), child))
            elif child.name.startswith("drm_dp_aux"):
                try:
                    for sub in child.iterdir():
                        m_sub = _I2C_DIR_RE.match(sub.name)
                        if m_sub:
                            aux_candidates.append((int(m_sub.group(1)), sub))
                except OSError:
                    pass

        for bus_num, bus_dir in sorted(aux_candidates, key=lambda item: item[0]):
            if bus_num not in candidate_buses:
                candidate_buses.append(bus_num)
                dp_aux_buses.add(bus_num)
                name = _read_text_safe(bus_dir / "name")
                if name:
                    bus_names[bus_num] = name

        # 2. Fall back to legacy connector `ddc` symlink (e.g. card1-DP-1/ddc -> i2c-7)
        ddc_bus = _extract_bus_from_link(entry / "ddc")
        if ddc_bus is not None and ddc_bus not in candidate_buses:
            candidate_buses.append(ddc_bus)
            name = _read_text_safe(entry / "ddc" / "name")
            if name:
                bus_names[ddc_bus] = name

        if i2c_sysfs_root is not None:
            for bus_num in candidate_buses:
                if bus_num not in bus_names:
                    name = _read_text_safe(i2c_sysfs_root / f"i2c-{bus_num}" / "name")
                    if name:
                        bus_names[bus_num] = name

        connectors.append(
            DrmConnectorInfo(
                drm_name=entry.name,
                connector=connector,
                status=status,
                enabled=enabled,
                dpms=dpms,
                is_builtin=is_builtin,
                edid=edid,
                candidate_buses=candidate_buses,
                bus_names=bus_names,
                dp_aux_buses=dp_aux_buses,
            )
        )

    return connectors


def find_flock_holder(
    st_dev: int,
    st_ino: int,
    proc_locks_path: Path = Path("/proc/locks"),
    proc_root: Path = Path("/proc"),
) -> tuple[int | None, str]:
    """Find PID and process name holding a FLOCK lock for exact (st_dev, st_ino)."""
    if not proc_locks_path.exists():
        return None, ""
    target_key = f"{os.major(st_dev):02x}:{os.minor(st_dev):02x}:{st_ino}".lower()
    try:
        lines = proc_locks_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return None, ""

    for line in lines:
        parts = line.split()
        if len(parts) < 6:
            continue
        # Ignore blocked waiters (`->` marker at parts[1]); find active holder
        if parts[1] != "FLOCK":
            continue
        for idx in range(5, len(parts)):
            if parts[idx].lower() == target_key:
                pid_tok = parts[idx - 1]
                if pid_tok.isdigit():
                    pid = int(pid_tok)
                    comm = _read_text_safe(proc_root / str(pid) / "comm")
                    return pid, comm
                return None, ""
    return None, ""


def inspect_i2c_bus(
    bus_number: int,
    dev_root: Path = Path("/dev"),
    proc_locks_path: Path = Path("/proc/locks"),
    proc_root: Path = Path("/proc"),
    i2c_sysfs_root: Path | None = Path("/sys/bus/i2c/devices"),
    lock_wait_seconds: float = 0.25,
    is_dp_aux: bool = False,
) -> I2cBusProbe:
    """Check /dev/i2c-N existence, RW permissions, and non-blocking flock availability."""
    dev_path_obj = dev_root / f"i2c-{bus_number}"
    dev_path = str(dev_path_obj)
    adapter_name = ""
    if i2c_sysfs_root is not None:
        adapter_name = _read_text_safe(i2c_sysfs_root / f"i2c-{bus_number}" / "name")

    if not dev_path_obj.exists():
        return I2cBusProbe(
            bus_number=bus_number,
            dev_path=dev_path,
            adapter_name=adapter_name,
            is_dp_aux=is_dp_aux,
            status="missing_device",
            detail=f"I2C device {dev_path} does not exist.",
        )

    try:
        fd = os.open(dev_path, os.O_RDWR)
    except PermissionError as exc:
        return I2cBusProbe(
            bus_number=bus_number,
            dev_path=dev_path,
            adapter_name=adapter_name,
            is_dp_aux=is_dp_aux,
            status="permission_denied",
            detail=f"No read/write permission for {dev_path}: {exc}",
        )
    except FileNotFoundError:
        return I2cBusProbe(
            bus_number=bus_number,
            dev_path=dev_path,
            adapter_name=adapter_name,
            is_dp_aux=is_dp_aux,
            status="missing_device",
            detail=f"I2C device {dev_path} does not exist.",
        )
    except OSError as exc:
        return I2cBusProbe(
            bus_number=bus_number,
            dev_path=dev_path,
            adapter_name=adapter_name,
            is_dp_aux=is_dp_aux,
            status="error",
            detail=f"Failed to open {dev_path}: {exc}",
        )

    try:
        deadline = time.monotonic() + max(0.0, lock_wait_seconds)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                return I2cBusProbe(
                    bus_number=bus_number,
                    dev_path=dev_path,
                    adapter_name=adapter_name,
                    is_dp_aux=is_dp_aux,
                    status="ready",
                )
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    return I2cBusProbe(
                        bus_number=bus_number,
                        dev_path=dev_path,
                        adapter_name=adapter_name,
                        is_dp_aux=is_dp_aux,
                        status="error",
                        detail=f"flock check failed on {dev_path}: {exc}",
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    st = os.fstat(fd)
                    lock_pid, lock_proc = find_flock_holder(
                        st.st_dev,
                        st.st_ino,
                        proc_locks_path=proc_locks_path,
                        proc_root=proc_root,
                    )
                    if lock_pid is not None and lock_proc:
                        holder = f"{lock_proc}, PID {lock_pid}"
                    elif lock_pid is not None:
                        holder = f"PID {lock_pid}"
                    else:
                        holder = "another process"
                    return I2cBusProbe(
                        bus_number=bus_number,
                        dev_path=dev_path,
                        adapter_name=adapter_name,
                        is_dp_aux=is_dp_aux,
                        status="bus_locked",
                        lock_pid=lock_pid,
                        lock_process=lock_proc,
                        detail=(
                            f"I2C bus {dev_path} is locked by another process ({holder})."
                        ),
                    )
                time.sleep(min(0.05, remaining))
    finally:
        os.close(fd)


def find_locked_i2c_buses(
    dev_root: Path = Path("/dev"),
    proc_locks_path: Path = Path("/proc/locks"),
    proc_root: Path = Path("/proc"),
    i2c_sysfs_root: Path | None = Path("/sys/bus/i2c/devices"),
    lock_wait_seconds: float = 0.05,
) -> dict[int, I2cBusProbe]:
    """Identify all accessible /dev/i2c-* buses currently locked by another process."""
    locked: dict[int, I2cBusProbe] = {}
    if not dev_root.exists():
        return locked
    try:
        entries = list(dev_root.iterdir())
    except OSError:
        return locked

    bus_nums: list[int] = []
    for entry in entries:
        m = _I2C_DIR_RE.match(entry.name)
        if m:
            bus_nums.append(int(m.group(1)))

    for bus_num in sorted(bus_nums):
        probe = inspect_i2c_bus(
            bus_num,
            dev_root=dev_root,
            proc_locks_path=proc_locks_path,
            proc_root=proc_root,
            i2c_sysfs_root=i2c_sysfs_root,
            lock_wait_seconds=lock_wait_seconds,
        )
        if probe.status == "bus_locked":
            locked[bus_num] = probe
    return locked
