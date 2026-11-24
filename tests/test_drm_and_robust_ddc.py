"""Tests for DRM/EDID scanning, non-blocking flock detection, and resilient DDC discovery."""

import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from pdfl.backend import PowerDisplayBackend
from pdfl.drivers.backlight_driver import BacklightDriver
from pdfl.drivers.ddcutil_driver import DdcUtilDriver
from pdfl.drivers.drm_probe import (
    find_flock_holder,
    inspect_i2c_bus,
    parse_edid_bytes,
    scan_drm_connectors,
)
from pdfl.drivers.mutter_display import (
    SOFTWARE_MIN_BRIGHTNESS_SCALE,
    MutterDisplayClient,
    apply_software_gamma_lut,
    build_ctm_diagonal_s31_32,
    build_identity_gamma_lut,
    scale_gamma_lut,
    software_percent_to_scale,
)
from pdfl.models import AppSettings
from pdfl.storage import MonitorStateStore, ProfileStore, SettingsStore

# Synthetic 128-byte base block matching test metadata + 128-byte padded extension block
_BENQ_EDID_BASE_HEX = (
    "00ffffffffffff0009d1507939300000"
    "151e0103804627782e00000000000000"
    "00000000000000000000000000000000"
    "000000000000000000fd00184c1e871e"
    "000a202020202020000000ff0044454d"
    "4f303030313233340a20000000fc0042"
    "656e5120455733323730550a000000fe"
    "0053796e7468657469632045444901a5"
)
_BENQ_EDID_BYTES = bytes.fromhex(_BENQ_EDID_BASE_HEX) + (b"\x02" + b"\x00" * 127)


class _FakeBenqMutterAdapter:
    def get_current_state(self):
        return (
            1,
            [
                (
                    ("DP-1", "BNQ", "BenQ EW3270U", "DEMO0001234"),
                    [("3840x2160@30.000", 3840, 2160, 30.0, 1.25, [1.0, 1.25], {"is-current": True})],
                    {"is-builtin": False, "display-name": 'BenQ Corporation 32"'},
                ),
                (
                    ("eDP-1", "LEN", "0x9052", "0x00000000"),
                    [],
                    {"is-builtin": True, "display-name": "Built-in Display"},
                ),
            ],
            [
                (0, 0, 1.25, 0, True, [("DP-1", "BNQ", "BenQ EW3270U", "DEMO0001234")], {}),
            ],
            {"layout-mode": 1, "apply-monitors-config-allowed": True},
        )

    def get_backlight_property(self):
        return (1, [{"connector": "eDP-1", "active": False, "min": 655, "max": 65535, "value": 35911}])


def _setup_synthetic_drm(root: pathlib.Path, include_second_dp: bool = False) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    drm = root / "sys_drm"
    i2c_sysfs = root / "sys_i2c"
    dev = root / "dev"
    drm.mkdir(parents=True)
    i2c_sysfs.mkdir(parents=True)
    dev.mkdir(parents=True)

    # Create mock i2c adapters in sysfs
    for num, name in ((5, "AMDGPU DM i2c hw bus 0"), (7, "AMDGPU DM i2c hw bus 2"), (8, "AMDGPU DM aux hw bus 0"), (9, "AMDGPU DM aux hw bus 2")):
        d = i2c_sysfs / f"i2c-{num}"
        d.mkdir(parents=True)
        (d / "name").write_text(f"{name}\n", encoding="utf-8")
        (dev / f"i2c-{num}").write_bytes(b"")

    # card1-DP-1 with DP-AUX i2c-9 and legacy ddc -> i2c-7
    dp1 = drm / "card1-DP-1"
    dp1.mkdir(parents=True)
    (dp1 / "status").write_text("connected\n", encoding="utf-8")
    (dp1 / "enabled").write_text("enabled\n", encoding="utf-8")
    (dp1 / "dpms").write_text("On\n", encoding="utf-8")
    (dp1 / "edid").write_bytes(_BENQ_EDID_BYTES)
    (dp1 / "i2c-9").mkdir()
    (dp1 / "i2c-9" / "name").write_text("AMDGPU DM aux hw bus 2\n", encoding="utf-8")
    os.symlink(str(i2c_sysfs / "i2c-7"), dp1 / "ddc")

    # card1-eDP-1 builtin
    edp = drm / "card1-eDP-1"
    edp.mkdir(parents=True)
    (edp / "status").write_text("connected\n", encoding="utf-8")
    (edp / "enabled").write_text("disabled\n", encoding="utf-8")
    (edp / "dpms").write_text("Off\n", encoding="utf-8")
    (edp / "i2c-8").mkdir()

    # card1-HDMI-A-1 disconnected
    hdmi = drm / "card1-HDMI-A-1"
    hdmi.mkdir(parents=True)
    (hdmi / "status").write_text("disconnected\n", encoding="utf-8")
    (hdmi / "enabled").write_text("disabled\n", encoding="utf-8")

    if include_second_dp:
        d10 = i2c_sysfs / "i2c-10"
        d10.mkdir(parents=True)
        (d10 / "name").write_text("AMDGPU DM aux hw bus 3\n", encoding="utf-8")
        (dev / "i2c-10").write_bytes(b"")

        dp2 = drm / "card1-DP-2"
        dp2.mkdir(parents=True)
        (dp2 / "status").write_text("connected\n", encoding="utf-8")
        (dp2 / "enabled").write_text("enabled\n", encoding="utf-8")
        (dp2 / "dpms").write_text("On\n", encoding="utf-8")
        (dp2 / "i2c-10").mkdir()
        (dp2 / "i2c-10" / "name").write_text("AMDGPU DM aux hw bus 3\n", encoding="utf-8")

    return drm, i2c_sysfs, dev


class DrmAndRobustDdcTests(unittest.TestCase):
    def test_parse_benq_edid_bytes_and_drm_scan_priority(self) -> None:
        info = parse_edid_bytes(_BENQ_EDID_BYTES)
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.mfg_id, "BNQ")
        self.assertEqual(info.model, "BenQ EW3270U")
        self.assertEqual(info.serial, "DEMO0001234")
        self.assertEqual(info.binary_serial, "12345")
        self.assertEqual(info.product_code, "0x7950")
        self.assertEqual(info.edid_hex, _BENQ_EDID_BASE_HEX)

        with tempfile.TemporaryDirectory() as tmp:
            drm, i2c_sysfs, _dev = _setup_synthetic_drm(pathlib.Path(tmp))
            conns = scan_drm_connectors(drm, i2c_sysfs)
            self.assertEqual([c.connector for c in conns], ["DP-1", "eDP-1"])
            dp1 = conns[0]
            self.assertFalse(dp1.is_builtin)
            self.assertEqual(dp1.candidate_buses, [9, 7])
            self.assertIn(9, dp1.dp_aux_buses)
            self.assertNotIn(7, dp1.dp_aux_buses)
            self.assertEqual(dp1.bus_names[9], "AMDGPU DM aux hw bus 2")
            self.assertEqual(dp1.bus_names[7], "AMDGPU DM i2c hw bus 2")
            self.assertTrue(conns[1].is_builtin)

    def test_flock_holder_matches_exact_dev_and_inode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            drm, i2c_sysfs, dev = _setup_synthetic_drm(root)
            dev9 = dev / "i2c-9"
            st = os.stat(dev9)
            exact_key = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
            wrong_dev_key = f"{os.major(st.st_dev) + 1:02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"

            proc_root = root / "proc"
            (proc_root / "99999").mkdir(parents=True)
            (proc_root / "99999" / "comm").write_text("wrongproc\n", encoding="utf-8")
            (proc_root / "88888").mkdir(parents=True)
            (proc_root / "88888" / "comm").write_text("ddcui\n", encoding="utf-8")

            locks_file = root / "proc_locks"
            locks_file.write_text(
                f"1: FLOCK  ADVISORY  WRITE 99999 {wrong_dev_key} 0 EOF\n"
                f"2: FLOCK  ADVISORY  WRITE 88888 {exact_key} 0 EOF\n",
                encoding="utf-8",
            )

            pid, name = find_flock_holder(st.st_dev, st.st_ino, locks_file, proc_root)
            self.assertEqual(pid, 88888)
            self.assertEqual(name, "ddcui")

            # Verify wrong major:minor does NOT match even with identical inode
            locks_file.write_text(
                f"1: FLOCK  ADVISORY  WRITE 99999 {wrong_dev_key} 0 EOF\n",
                encoding="utf-8",
            )
            pid2, name2 = find_flock_holder(st.st_dev, st.st_ino, locks_file, proc_root)
            self.assertIsNone(pid2)
            self.assertEqual(name2, "")

            # Hold a real child flock on dev/i2c-9 and test inspect_i2c_bus
            locks_file.write_text(
                f"2: FLOCK  ADVISORY  WRITE 88888 {exact_key} 0 EOF\n",
                encoding="utf-8",
            )
            holder = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import fcntl, os, sys, time; fd = os.open(sys.argv[1], os.O_RDWR); fcntl.flock(fd, fcntl.LOCK_EX); print('LOCKED', flush=True); time.sleep(10)",
                    str(dev9),
                ],
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                assert holder.stdout is not None
                self.assertEqual(holder.stdout.readline().strip(), "LOCKED")
                probe = inspect_i2c_bus(
                    9,
                    dev_root=dev,
                    proc_locks_path=locks_file,
                    proc_root=proc_root,
                    i2c_sysfs_root=i2c_sysfs,
                    lock_wait_seconds=0.05,
                    is_dp_aux=True,
                )
                self.assertEqual(probe.status, "bus_locked")
                self.assertEqual(probe.lock_pid, 88888)
                self.assertEqual(probe.lock_process, "ddcui")
                self.assertIn("/dev/i2c-9", probe.detail)
            finally:
                if holder.stdout is not None:
                    holder.stdout.close()
                holder.terminate()
                holder.wait(timeout=5)

    def test_locked_bus_keeps_benq_visible_without_calling_ddcutil_on_locked_bus(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            drm, i2c_sysfs, dev = _setup_synthetic_drm(root)
            dev9 = dev / "i2c-9"
            st = os.stat(dev9)
            exact_key = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"

            proc_root = root / "proc"
            (proc_root / "79404").mkdir(parents=True)
            (proc_root / "79404" / "comm").write_text("ddcui\n", encoding="utf-8")
            locks_file = root / "proc_locks"
            locks_file.write_text(
                f"122: FLOCK  ADVISORY  WRITE 79404 {exact_key} 0 EOF\n",
                encoding="utf-8",
            )

            calls: list[list[str]] = []

            def fake_runner(cmd, **_kwargs):
                calls.append(cmd)
                if "--bus" in cmd:
                    bus = cmd[cmd.index("--bus") + 1]
                    if bus == "9":
                        raise AssertionError("ddcutil must not be invoked on locked bus 9!")
                    if bus == "7":
                        return subprocess.CompletedProcess(
                            cmd, 1, stdout="", stderr="No monitor detected on bus /dev/i2c-7\n"
                        )
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

            holder = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import fcntl, os, sys, time; fd = os.open(sys.argv[1], os.O_RDWR); fcntl.flock(fd, fcntl.LOCK_EX); print('LOCKED', flush=True); time.sleep(10)",
                    str(dev9),
                ],
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                assert holder.stdout is not None
                self.assertEqual(holder.stdout.readline().strip(), "LOCKED")

                mutter = MutterDisplayClient(adapter=_FakeBenqMutterAdapter())
                ddc = DdcUtilDriver(
                    mutter_client=mutter,
                    command_runner=fake_runner,
                    which_fn=lambda _n: "/usr/bin/ddcutil",
                    drm_root=drm,
                    dev_root=dev,
                    proc_locks_path=locks_file,
                    proc_root=proc_root,
                    i2c_sysfs_root=i2c_sysfs,
                    lock_wait_seconds=0.05,
                )
                bl = BacklightDriver(
                    mutter_client=mutter,
                    sysfs_root=root / "no_bl",
                    which_fn=lambda _n: None,
                )
                backend = PowerDisplayBackend(
                    demo=False,
                    settings_store=SettingsStore(root / "settings.json"),
                    profile_store=ProfileStore(root / "profiles.json"),
                    state_store=MonitorStateStore(root / "state.json"),
                    mutter_client=mutter,
                    backlight_driver=bl,
                    ddc_driver=ddc,
                    debounce_seconds=0.0,
                )
                state = backend.initialize()
                self.assertEqual(len(state["monitors"]), 1)
                benq = state["monitors"][0]
                self.assertEqual(benq["monitor_id"], "ddc:BNQ:BenQ_EW3270U:DEMO0001234")
                self.assertEqual(benq["display_name"], "BenQ EW3270U")
                self.assertEqual(benq["connector"], "DP-1")
                self.assertEqual(benq["drm_connector"], "card1-DP-1")
                self.assertTrue(benq["is_primary"])
                self.assertEqual(benq["ddc_status"], "bus_locked")
                self.assertEqual(benq["i2c_bus"], 9)
                self.assertFalse(benq["capabilities"]["brightness"])
                self.assertFalse(benq["capabilities"]["brightness_writable"])
                self.assertTrue(benq["capabilities"]["rotation"])
                self.assertIn("ddcui", benq["last_error"])
                self.assertIn("79404", benq["last_error"])
                self.assertIn("/dev/i2c-9", benq["last_error"])

                # Attempting brightness change cleanly reports ddc_bus_locked without touching bus 9
                res = backend.set_feature(benq["monitor_id"], "brightness", 50)
                self.assertFalse(res.ok)
                self.assertEqual(res.error_code, "ddc_bus_locked")
                self.assertFalse(any("--bus" in c and c[c.index("--bus") + 1] == "9" for c in calls))
                backend.shutdown()
            finally:
                if holder.stdout is not None:
                    holder.stdout.close()
                holder.terminate()
                holder.wait(timeout=5)

    def test_unresponsive_ddc_surfaces_actionable_error_while_keeping_benq_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            drm, i2c_sysfs, dev = _setup_synthetic_drm(root)

            def fake_runner(cmd, **_kwargs):
                bus = cmd[cmd.index("--bus") + 1]
                if bus == "9":
                    return subprocess.CompletedProcess(
                        cmd,
                        1,
                        stdout="VCP 10 ERR\n",
                        stderr="Error detecting VCP version using VCP feature xDF: Error_Info[DDCRC_RETRIES in ddc_write_read_with_retry, causes: EREMOTEIO(10)]\n",
                    )
                return subprocess.CompletedProcess(
                    cmd, 1, stdout="", stderr="No monitor detected on bus /dev/i2c-7\n"
                )

            mutter = MutterDisplayClient(adapter=_FakeBenqMutterAdapter())
            ddc = DdcUtilDriver(
                mutter_client=mutter,
                command_runner=fake_runner,
                which_fn=lambda _n: "/usr/bin/ddcutil",
                drm_root=drm,
                dev_root=dev,
                i2c_sysfs_root=i2c_sysfs,
                lock_wait_seconds=0.02,
            )
            items, errs = ddc.discover(mutter.get_current_state())
            self.assertEqual(errs, [])
            self.assertEqual(len(items), 1)
            info = items[0].info
            self.assertEqual(info.monitor_id, "ddc:BNQ:BenQ_EW3270U:DEMO0001234")
            self.assertEqual(info.ddc_status, "ddc_unresponsive")
            self.assertEqual(info.i2c_bus, 9)
            self.assertFalse(info.capabilities.brightness)
            self.assertTrue(info.capabilities.rotation)
            self.assertIn("0x37", info.last_error or "")
            self.assertIn("OSD", info.last_error or "")

    def test_hung_bus_isolates_timeout_and_allows_other_monitor_to_succeed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            drm, i2c_sysfs, dev = _setup_synthetic_drm(root, include_second_dp=True)

            def fake_runner(cmd, **_kwargs):
                bus = cmd[cmd.index("--bus") + 1]
                if bus == "10":
                    raise subprocess.TimeoutExpired(cmd=cmd, timeout=4.0)
                if bus == "9":
                    if "getvcp" in cmd:
                        return subprocess.CompletedProcess(
                            cmd, 0, stdout="VCP 10 C 40 100\n", stderr=""
                        )
                    if "capabilities" in cmd:
                        return subprocess.CompletedProcess(
                            cmd,
                            0,
                            stdout="(prot(monitor)type(lcd)model(BenQ EW3270U)vcp(10 12 60(0F 11))mccs_ver(2.2))",
                            stderr="",
                        )
                return subprocess.CompletedProcess(
                    cmd, 1, stdout="", stderr="No monitor detected on bus /dev/i2c-7\n"
                )

            mutter = MutterDisplayClient(adapter=_FakeBenqMutterAdapter())
            ddc = DdcUtilDriver(
                mutter_client=mutter,
                command_runner=fake_runner,
                which_fn=lambda _n: "/usr/bin/ddcutil",
                drm_root=drm,
                dev_root=dev,
                i2c_sysfs_root=i2c_sysfs,
                lock_wait_seconds=0.02,
                max_workers=2,
            )
            items, errs = ddc.discover(mutter.get_current_state())
            self.assertEqual(errs, [])
            self.assertEqual(len(items), 2)

            by_conn = {item.info.connector: item for item in items}
            benq = by_conn["DP-1"]
            self.assertEqual(benq.info.ddc_status, "available")
            self.assertEqual(benq.info.i2c_bus, 9)
            self.assertEqual(benq.info.brightness, 40)
            self.assertTrue(benq.info.capabilities.brightness_writable)
            self.assertEqual(benq.handle.bus_number, 9)

            dp2 = by_conn["DP-2"]
            self.assertEqual(dp2.info.ddc_status, "timeout")
            self.assertEqual(dp2.info.i2c_bus, 10)
            self.assertFalse(dp2.info.capabilities.brightness)

    def test_working_ddc_bus_reads_and_writes_brightness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            drm, i2c_sysfs, dev = _setup_synthetic_drm(root)
            written: list[list[str]] = []

            def fake_runner(cmd, **_kwargs):
                bus = cmd[cmd.index("--bus") + 1]
                if bus == "9":
                    if "setvcp" in cmd:
                        written.append(cmd)
                        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
                    if "capabilities" in cmd:
                        return subprocess.CompletedProcess(
                            cmd,
                            0,
                            stdout="(prot(monitor)type(lcd)model(BenQ EW3270U)vcp(10 12 60(0F 11))mccs_ver(2.2))",
                            stderr="",
                        )
                    if "getvcp" in cmd:
                        if "10" in cmd and "12" not in cmd:
                            return subprocess.CompletedProcess(
                                cmd, 0, stdout="VCP 10 C 40 100\n", stderr=""
                            )
                        return subprocess.CompletedProcess(
                            cmd, 0, stdout="VCP 12 C 50 100\nVCP 60 SNC x0f\n", stderr=""
                        )
                return subprocess.CompletedProcess(
                    cmd, 1, stdout="", stderr="No monitor detected on bus /dev/i2c-7\n"
                )

            mutter = MutterDisplayClient(adapter=_FakeBenqMutterAdapter())
            ddc = DdcUtilDriver(
                mutter_client=mutter,
                command_runner=fake_runner,
                which_fn=lambda _n: "/usr/bin/ddcutil",
                drm_root=drm,
                dev_root=dev,
                i2c_sysfs_root=i2c_sysfs,
                lock_wait_seconds=0.02,
            )
            bl = BacklightDriver(
                mutter_client=mutter,
                sysfs_root=root / "no_bl",
                which_fn=lambda _n: None,
            )
            backend = PowerDisplayBackend(
                demo=False,
                settings_store=SettingsStore(root / "settings.json"),
                profile_store=ProfileStore(root / "profiles.json"),
                state_store=MonitorStateStore(root / "state.json"),
                mutter_client=mutter,
                backlight_driver=bl,
                ddc_driver=ddc,
                debounce_seconds=0.0,
            )
            state = backend.initialize()
            benq = state["monitors"][0]
            self.assertEqual(benq["ddc_status"], "available")
            self.assertEqual(benq["brightness"], 40)
            self.assertEqual(benq["contrast"], 50)
            self.assertEqual(benq["input_source"], 0x0F)
            self.assertTrue(benq["capabilities"]["brightness_writable"])

            res = backend.set_feature(benq["monitor_id"], "brightness", 41)
            self.assertTrue(res.ok, res.message)
            self.assertEqual(len(written), 1)
            self.assertIn("--bus", written[0])
            self.assertEqual(written[0][written[0].index("--bus") + 1], "9")
            self.assertEqual(written[0][-3:], ["setvcp", "10", "41"])
            backend.shutdown()

    def test_software_dimming_math_and_mutter_ctm_vs_gamma_paths(self) -> None:
        self.assertAlmostEqual(software_percent_to_scale(100), 1.0)
        self.assertAlmostEqual(software_percent_to_scale(0), SOFTWARE_MIN_BRIGHTNESS_SCALE)
        self.assertAlmostEqual(software_percent_to_scale(0), 0.35)
        self.assertAlmostEqual(software_percent_to_scale(50), 0.675)

        ctm_full = build_ctm_diagonal_s31_32(1.0)
        self.assertEqual(ctm_full, (1 << 32, 0, 0, 0, 1 << 32, 0, 0, 0, 1 << 32))
        scaled_lut = scale_gamma_lut(([0, 1000, 65535], [0, 1000, 65535], [0, 1000, 65535]), 0.5)
        self.assertEqual(scaled_lut[0], [0, 500, 32768])

        # 1. CTM path when supports-color-transform=True
        class FakeCtmAdapter(_FakeBenqMutterAdapter):
            def __init__(self):
                self.ctm_calls = []

            def get_resources(self):
                return (
                    42,
                    [(1, 92, 0, 0, 3840, 2160, 121, 0, [0], {})],
                    [(7, 4294967413, 1, [0], "DP-1", [], [], {"supports-color-transform": True})],
                    [],
                    3840,
                    2160,
                )

            def set_output_ctm(self, serial, output_id, matrix):
                self.ctm_calls.append((serial, output_id, matrix))

        ctm_adapter = FakeCtmAdapter()
        ctm_client = MutterDisplayClient(adapter=ctm_adapter)
        self.assertEqual(ctm_client.probe_software_dimming_mode("DP-1"), "software_ctm")
        res_ctm = ctm_client.set_software_brightness_percent("DP-1", 50)
        self.assertTrue(res_ctm.ok, res_ctm.message)
        self.assertEqual(res_ctm.data["mode"], "software_ctm")
        self.assertEqual(ctm_adapter.ctm_calls[0][0], 42)
        self.assertEqual(ctm_adapter.ctm_calls[0][1], 7)
        self.assertEqual(
            ctm_adapter.ctm_calls[0][2],
            build_ctm_diagonal_s31_32(software_percent_to_scale(50)),
        )
        ctm_client.reset_software_brightness("DP-1")
        self.assertEqual(ctm_adapter.ctm_calls[-1][2], build_ctm_diagonal_s31_32(1.0))

        # 2. Gamma LUT path when supports-color-transform=False (preserves external Night Light changes)
        class FakeGammaAdapter(_FakeBenqMutterAdapter):
            def __init__(self):
                self.lut = ([0, 32768, 65535], [0, 30000, 60000], [0, 20000, 40000])
                self.gamma_writes = []

            def get_resources(self):
                return (
                    9,
                    [(1, 92, 0, 0, 3840, 2160, 121, 0, [0], {})],
                    [(0, 4294967413, 1, [0], "DP-1", [], [], {"supports-color-transform": False})],
                    [],
                    3840,
                    2160,
                )

            def get_crtc_gamma(self, _serial, _crtc_id):
                return (list(self.lut[0]), list(self.lut[1]), list(self.lut[2]))

            def set_crtc_gamma(self, serial, crtc_id, r, g, b):
                self.lut = (list(r), list(g), list(b))
                self.gamma_writes.append((serial, crtc_id, list(r), list(g), list(b)))

        g_adapter = FakeGammaAdapter()
        g_client = MutterDisplayClient(adapter=g_adapter)
        self.assertEqual(g_client.probe_software_dimming_mode("DP-1"), "software_gamma")
        r1 = g_client.set_software_brightness_percent("DP-1", 60)
        self.assertTrue(r1.ok, r1.message)
        self.assertEqual(r1.data["mode"], "software_gamma")
        self.assertEqual(g_adapter.lut[0][-1], round(65535 * software_percent_to_scale(60)))
        self.assertEqual(g_adapter.lut[2][-1], round(40000 * software_percent_to_scale(60)))

        # Simulate external Night Light changing blue channel baseline before next slider move
        g_adapter.lut = ([0, 32768, 65535], [0, 28000, 56000], [0, 15000, 30000])
        r2 = g_client.set_software_brightness_percent("DP-1", 50)
        self.assertTrue(r2.ok)
        self.assertEqual(g_adapter.lut[2][-1], round(30000 * software_percent_to_scale(50)))

        g_client.reset_software_brightness("DP-1")
        self.assertEqual(g_adapter.lut, ([0, 32768, 65535], [0, 28000, 56000], [0, 15000, 30000]))

    def test_backend_opt_in_software_dimming_fallback_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            drm, i2c_sysfs, dev = _setup_synthetic_drm(root, include_second_dp=True)

            class FakeGammaAdapter(_FakeBenqMutterAdapter):
                def __init__(self):
                    self.lut = ([0, 32768, 65535], [0, 32768, 65535], [0, 32768, 65535])

                def get_current_state(self):
                    base = super().get_current_state()
                    mons = list(base[1]) + [
                        (
                            ("DP-2", "DEL", "Dell U2723QE", "XYZ123"),
                            [("2560x1440@60.000", 2560, 1440, 60.0, 1.0, [1.0], {"is-current": True})],
                            {"is-builtin": False, "display-name": "Dell U2723QE"},
                        )
                    ]
                    lms = list(base[2]) + [
                        (3840, 0, 1.0, 0, False, [("DP-2", "DEL", "Dell U2723QE", "XYZ123")], {}),
                    ]
                    return (base[0], mons, lms, base[3])

                def get_resources(self):
                    return (
                        11,
                        [(1, 92, 0, 0, 3840, 2160, 121, 0, [0], {}), (2, 96, 3840, 0, 2560, 1440, 122, 0, [0], {})],
                        [
                            (0, 4294967413, 1, [0], "DP-1", [], [], {"supports-color-transform": False}),
                            (1, 4294967414, 2, [0], "DP-2", [], [], {"supports-color-transform": False}),
                        ],
                        [],
                        6400,
                        2160,
                    )

                def get_crtc_gamma(self, _s, _c):
                    return (list(self.lut[0]), list(self.lut[1]), list(self.lut[2]))

                def set_crtc_gamma(self, _s, _c, r, g, b):
                    self.lut = (list(r), list(g), list(b))

            ddc_calls = []

            def fake_runner(cmd, **_kwargs):
                bus = cmd[cmd.index("--bus") + 1]
                if bus == "9":
                    return subprocess.CompletedProcess(
                        cmd,
                        1,
                        stdout="VCP 10 ERR\n",
                        stderr="Error detecting VCP version: EREMOTEIO(10)\n",
                    )
                if bus == "10":
                    if "setvcp" in cmd:
                        ddc_calls.append(cmd)
                        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
                    if "capabilities" in cmd:
                        return subprocess.CompletedProcess(
                            cmd, 0, stdout="(prot(monitor)vcp(10)mccs_ver(2.2))", stderr=""
                        )
                    if "getvcp" in cmd:
                        return subprocess.CompletedProcess(cmd, 0, stdout="VCP 10 C 45 100\n", stderr="")
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="No monitor detected\n")

            adapter = FakeGammaAdapter()
            mutter = MutterDisplayClient(adapter=adapter)
            ddc = DdcUtilDriver(
                mutter_client=mutter,
                command_runner=fake_runner,
                which_fn=lambda _n: "/usr/bin/ddcutil",
                drm_root=drm,
                dev_root=dev,
                i2c_sysfs_root=i2c_sysfs,
                lock_wait_seconds=0.02,
            )
            bl = BacklightDriver(
                mutter_client=mutter,
                sysfs_root=root / "no_bl",
                which_fn=lambda _n: None,
            )
            backend = PowerDisplayBackend(
                demo=False,
                settings_store=SettingsStore(root / "settings.json"),
                profile_store=ProfileStore(root / "profiles.json"),
                state_store=MonitorStateStore(root / "state.json"),
                mutter_client=mutter,
                backlight_driver=bl,
                ddc_driver=ddc,
                debounce_seconds=0.0,
            )
            try:
                # Default: software_dimming_fallback=False -> BenQ stays non-writable
                s0 = backend.initialize()
                by_conn0 = {m["connector"]: m for m in s0["monitors"]}
                self.assertEqual(by_conn0["DP-1"]["brightness_mode"], "none")
                self.assertFalse(by_conn0["DP-1"]["capabilities"]["brightness_writable"])
                self.assertEqual(by_conn0["DP-2"]["brightness_mode"], "hardware")
                self.assertTrue(by_conn0["DP-2"]["capabilities"]["brightness_writable"])

                # Opt-in: enable software_dimming_fallback -> BenQ uses software_gamma, Dell stays hardware
                backend.save_settings(AppSettings(software_dimming_fallback=True))
                s1 = backend.get_state()
                by_conn1 = {m["connector"]: m for m in s1["monitors"]}
                benq = by_conn1["DP-1"]
                dell = by_conn1["DP-2"]
                self.assertEqual(benq["brightness_mode"], "software_gamma")
                self.assertEqual(benq["status_level"], "info")
                self.assertIn("software mode", benq["status_notice"] or "")
                self.assertTrue(benq["capabilities"]["brightness"])
                self.assertTrue(benq["capabilities"]["brightness_writable"])
                self.assertTrue(benq["capabilities"]["contrast"])
                self.assertTrue(benq["capabilities"]["color_temperature"])
                self.assertEqual(benq["brightness"], 100)
                self.assertEqual(benq["contrast"], 50)
                self.assertEqual(benq["color_temperature"], 0x05)
                self.assertIn("0x37", benq["last_error"] or "")
                self.assertEqual(dell["brightness_mode"], "hardware")

                res = backend.set_feature(benq["monitor_id"], "brightness", 70)
                self.assertTrue(res.ok, res.message)
                self.assertEqual(adapter.lut[0][-1], round(65535 * software_percent_to_scale(70)))
                self.assertEqual(backend.get_state()["monitors"][0]["brightness"], 70)

                res_c = backend.set_feature(benq["monitor_id"], "contrast", 80)
                self.assertTrue(res_c.ok, res_c.message)
                res_ct = backend.set_feature(benq["monitor_id"], "color_temperature", 0x04)
                self.assertTrue(res_ct.ok, res_ct.message)
                expected_lut = apply_software_gamma_lut(
                    ([0, 32768, 65535], [0, 32768, 65535], [0, 32768, 65535]),
                    brightness=70,
                    contrast=80,
                    color_temperature=0x04,
                )
                self.assertEqual(adapter.lut, expected_lut)
                # Warm 5000K shifts blue channel below red channel
                self.assertLess(adapter.lut[2][-1], adapter.lut[0][-1])
                # DDC hardware write was never called for BenQ
                self.assertEqual(ddc_calls, [])

                # Disable fallback -> resets BenQ LUT to 100% and reverts to non-writable
                backend.save_settings(AppSettings(software_dimming_fallback=False))
                self.assertEqual(adapter.lut[0][-1], 65535)
                self.assertEqual(adapter.lut[2][-1], 65535)
                benq_after = {m["connector"]: m for m in backend.get_state()["monitors"]}["DP-1"]
                self.assertEqual(benq_after["brightness_mode"], "none")
                self.assertEqual(benq_after["status_level"], "error")
                self.assertFalse(benq_after["capabilities"]["brightness_writable"])
                self.assertFalse(benq_after["capabilities"]["contrast"])
                self.assertFalse(benq_after["capabilities"]["color_temperature"])
                self.assertIsNone(benq_after["brightness"])
            finally:
                backend.shutdown()

    def test_inactive_builtin_display_reports_calm_info_without_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            sysfs = root / "backlight" / "amdgpu_bl0"
            sysfs.mkdir(parents=True)
            (sysfs / "brightness").write_text("55\n", encoding="utf-8")
            (sysfs / "actual_brightness").write_text("55\n", encoding="utf-8")
            (sysfs / "max_brightness").write_text("100\n", encoding="utf-8")
            os.chmod(sysfs / "brightness", 0o444)

            class _ClosedLidAdapter(_FakeBenqMutterAdapter):
                def get_backlight_property(self):
                    return (
                        2,
                        [{"connector": "eDP-1", "active": False, "min": 0, "max": 100, "value": 55}],
                    )

            mutter = MutterDisplayClient(adapter=_ClosedLidAdapter())
            bl = BacklightDriver(
                mutter_client=mutter,
                sysfs_root=root / "backlight",
                which_fn=lambda _n: None,
            )
            items, warns = bl.discover(mutter.get_current_state())
            self.assertEqual(warns, [])
            self.assertEqual(len(items), 1)
            info = items[0].info
            self.assertEqual(info.connection_status, "inactive")
            self.assertEqual(info.status_level, "info")
            self.assertIn("lid closed", info.status_notice or "")
            self.assertIsNone(info.last_error)

    def test_software_gamma_never_compounds_across_quantization_or_restart(self) -> None:
        class QuantizingGammaAdapter(_FakeBenqMutterAdapter):
            def __init__(self):
                self.lut = build_identity_gamma_lut(256)

            def get_resources(self):
                return (
                    19,
                    [(1, 92, 0, 0, 3840, 2160, 121, 0, [0], {})],
                    [(0, 4294967413, 1, [0], "DP-1", [], [], {"supports-color-transform": False})],
                    [],
                    3840,
                    2160,
                )

            def get_crtc_gamma(self, _serial, _crtc_id):
                return (list(self.lut[0]), list(self.lut[1]), list(self.lut[2]))

            def set_crtc_gamma(self, _serial, _crtc_id, r, g, b):
                # Simulate GPU quantization rounding every LUT entry to multiples of 16
                self.lut = (
                    [min(65535, (int(v) // 16) * 16) for v in r],
                    [min(65535, (int(v) // 16) * 16) for v in g],
                    [min(65535, (int(v) // 16) * 16) for v in b],
                )

        adapter = QuantizingGammaAdapter()
        client = MutterDisplayClient(adapter=adapter)
        expected_peak = (round(65535 * software_percent_to_scale(50)) // 16) * 16

        peaks: list[int] = []
        for _ in range(20):
            res = client.set_software_display_feature("DP-1", "brightness", 50)
            self.assertTrue(res.ok, res.message)
            peaks.append(adapter.lut[0][-1])

        self.assertEqual(set(peaks), {expected_peak})

        # Simulate daemon restart while display CRTC gamma is ALREADY dimmed to 30%
        scale_30 = 0.30
        adapter.lut = scale_gamma_lut(build_identity_gamma_lut(256), scale_30)
        restarted_client = MutterDisplayClient(adapter=adapter)
        res_after_restart = restarted_client.set_software_display_feature(
            "DP-1", "brightness", 50
        )
        self.assertTrue(res_after_restart.ok, res_after_restart.message)
        # Must NOT compound 0.30 * 0.675; peak must sit cleanly at 50% scale!
        self.assertEqual(adapter.lut[0][-1], expected_peak)

    def test_software_gamma_worst_case_no_black_crush_and_safe_floor(self) -> None:
        base_lut = build_identity_gamma_lut(256)
        nr, ng, nb = apply_software_gamma_lut(
            base_lut,
            brightness=0,
            contrast=100,
            color_temperature=0x04,
        )
        min_peak = int(round(SOFTWARE_MIN_BRIGHTNESS_SCALE * 65535))
        self.assertGreaterEqual(max(nr[-1], ng[-1], nb[-1]), min_peak)

        # Dark-mode gray (~15% normalized) must remain strictly non-zero and monotonic
        dark_idx = round(0.15 * 255)
        self.assertGreater(nr[dark_idx], 0)
        self.assertGreater(ng[dark_idx], 0)
        self.assertGreater(nb[dark_idx], 0)
        for ch in (nr, ng, nb):
            for i in range(len(ch) - 1):
                self.assertLessEqual(ch[i], ch[i + 1])

        # Midtone safety check
        self.assertGreaterEqual(max(nr[128], ng[128], nb[128]), int(round(0.12 * 65535)))

    def test_probe_auto_recovers_dangerously_dark_lut(self) -> None:
        class DarkLutAdapter(_FakeBenqMutterAdapter):
            def __init__(self):
                self.lut = scale_gamma_lut(build_identity_gamma_lut(256), 5000.0 / 65535.0)
                self.writes = 0

            def get_resources(self):
                return (
                    21,
                    [(1, 92, 0, 0, 3840, 2160, 121, 0, [0], {})],
                    [(0, 4294967413, 1, [0], "DP-1", [], [], {"supports-color-transform": False})],
                    [],
                    3840,
                    2160,
                )

            def get_crtc_gamma(self, _serial, _crtc_id):
                return (list(self.lut[0]), list(self.lut[1]), list(self.lut[2]))

            def set_crtc_gamma(self, _serial, _crtc_id, r, g, b):
                self.writes += 1
                self.lut = (list(r), list(g), list(b))

        adapter = DarkLutAdapter()
        client = MutterDisplayClient(adapter=adapter)
        mode = client.probe_software_dimming_mode("DP-1")
        self.assertEqual(mode, "software_gamma")
        self.assertEqual(adapter.writes, 1)
        self.assertEqual(adapter.lut[0][-1], 65535)
        self.assertEqual(adapter.lut[1][-1], 65535)
        self.assertEqual(adapter.lut[2][-1], 65535)


if __name__ == "__main__":
    unittest.main()
