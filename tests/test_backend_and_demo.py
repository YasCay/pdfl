"""Tests for PowerDisplayBackend capability gating, confirmation, clamping, mock DDC, and backlight."""

import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from pdfl.backend import PowerDisplayBackend
from pdfl.drivers.backlight_driver import BacklightDriver
from pdfl.drivers.ddcutil_driver import DdcUtilDriver
from pdfl.drivers.mutter_display import MutterDisplayClient
from pdfl.models import AppSettings, Profile, ProfileMonitorSetting
from pdfl.storage import MonitorStateStore, ProfileStore, SettingsStore

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


class _FakeMutterAdapter:
    def __init__(self, backlight_active: bool = False) -> None:
        self.backlight_active = backlight_active
        self.backlight_writes: list[tuple[int, str, int]] = []

    def get_current_state(self):
        return (
            1,
            [
                (
                    ("eDP-1", "LEN", "0x9052", "0"),
                    [("1920x1200@60", 1920, 1200, 60.0, 1.0, [1.0], {"is-current": True})],
                    {"is-builtin": True, "display-name": "Built-in Panel"},
                ),
                (
                    ("DP-1", "DEL", "Dell U2723QE", "CN04K921"),
                    [("3840x2160@60", 3840, 2160, 60.0, 1.25, [1.0, 1.25], {"is-current": True})],
                    {"is-builtin": False, "display-name": "Dell U2723QE"},
                ),
            ],
            [
                (0, 0, 1.0, 0, False, [("eDP-1", "LEN", "0x9052", "0")], {}),
                (1920, 0, 1.25, 0, True, [("DP-1", "DEL", "Dell U2723QE", "CN04K921")], {}),
            ],
            {"layout-mode": 1, "apply-monitors-config-allowed": True},
        )

    def get_backlight_property(self):
        return (
            7,
            [
                {
                    "connector": "eDP-1",
                    "active": self.backlight_active,
                    "min": 0,
                    "max": 1000,
                    "value": 600,
                }
            ],
        )

    def set_backlight(self, serial: int, connector: str, value: int) -> None:
        self.backlight_writes.append((serial, connector, value))


class BackendAndDemoTests(unittest.TestCase):
    def _make_demo_backend(self, tmp: str) -> PowerDisplayBackend:
        root = pathlib.Path(tmp)
        backend = PowerDisplayBackend(
            demo=True,
            settings_store=SettingsStore(root / "settings.json"),
            profile_store=ProfileStore(root / "profiles.json"),
            state_store=MonitorStateStore(root / "state.json"),
            debounce_seconds=0.0,
        )
        backend.initialize()
        return backend

    def test_demo_capability_gating_and_clamping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._make_demo_backend(tmp)
            state = backend.get_state()
            self.assertEqual(len(state["monitors"]), 3)

            # LG demo monitor has no volume or power_state
            res_vol = backend.set_feature("demo:ddc:GSM:34WN80C:204NTBK881", "volume", 50)
            self.assertFalse(res_vol.ok)
            self.assertEqual(res_vol.error_code, "unsupported_feature")

            # Builtin has no contrast
            res_con = backend.set_feature("demo:internal:eDP-1", "contrast", 50)
            self.assertFalse(res_con.ok)

            # Clamping bounds
            res_hi = backend.set_feature("demo:ddc:DEL:U2723QE:CN04K921", "brightness", 180)
            self.assertTrue(res_hi.ok)
            res_lo = backend.set_feature("demo:ddc:DEL:U2723QE:CN04K921", "contrast", -20)
            self.assertTrue(res_lo.ok)
            dell = next(m for m in backend.get_state()["monitors"] if m["monitor_id"].startswith("demo:ddc:DEL"))
            self.assertEqual(dell["brightness"], 100)
            self.assertEqual(dell["contrast"], 0)
            backend.shutdown()

    def test_destructive_actions_require_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._make_demo_backend(tmp)
            mid = "demo:ddc:DEL:U2723QE:CN04K921"

            # Unconfirmed input switch blocked
            r1 = backend.set_feature(mid, "input_source", 0x11, confirmed=False)
            self.assertFalse(r1.ok)
            self.assertTrue(r1.requires_confirmation)

            # Confirmed input switch succeeds
            r2 = backend.set_feature(mid, "input_source", 0x11, confirmed=True)
            self.assertTrue(r2.ok)

            # Unconfirmed standby blocked
            p1 = backend.set_feature(mid, "power_state", 0x02, confirmed=False)
            self.assertFalse(p1.ok)
            self.assertTrue(p1.requires_confirmation)

            p2 = backend.set_feature(mid, "power_state", 0x02, confirmed=True)
            self.assertTrue(p2.ok)
            backend.shutdown()

    def test_linked_brightness_and_profile_application(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = self._make_demo_backend(tmp)
            # Turning linked mode on seeds from monitor #1 (65%) WITHOUT writing others
            st = backend.set_linked_mode(True)
            self.assertEqual(st["linked_brightness_value"], 65)
            dell_before = next(m for m in st["monitors"] if "DEL" in m["monitor_id"])
            self.assertEqual(dell_before["brightness"], 75)

            # Setting linked brightness writes all non-hidden monitors
            res = backend.set_linked_brightness(42)
            self.assertTrue(res.ok)
            for m in backend.get_state()["monitors"]:
                self.assertEqual(m["brightness"], 42)

            # Profile with disconnected monitor skips missing safely
            prof = backend.save_profile(
                Profile(
                    0,
                    "Focus",
                    [
                        ProfileMonitorSetting("demo:ddc:DEL:U2723QE:CN04K921", brightness=88, contrast=72),
                        ProfileMonitorSetting("ddc:MISSING:999", brightness=10),
                    ],
                )
            )
            apply_res = backend.apply_profile(prof.id)
            self.assertTrue(apply_res.ok)
            self.assertTrue(any("not connected" in s for s in apply_res.data["skipped"]))
            dell_after = next(m for m in backend.get_state()["monitors"] if "DEL" in m["monitor_id"])
            self.assertEqual(dell_after["brightness"], 88)
            self.assertEqual(dell_after["contrast"], 72)
            backend.shutdown()

    def test_mock_ddcutil_and_sysfs_backlight_discovery(self) -> None:
        detect_txt = (FIXTURES / "ddcutil_detect_multi.txt").read_text(encoding="utf-8")
        dell_caps = (FIXTURES / "ddcutil_caps_dell.txt").read_text(encoding="utf-8")
        lg_caps = (FIXTURES / "ddcutil_caps_lg.txt").read_text(encoding="utf-8")
        dell_vcp = (FIXTURES / "ddcutil_getvcp_terse_dell.txt").read_text(encoding="utf-8")
        lg_vcp = (FIXTURES / "ddcutil_getvcp_terse_lg.txt").read_text(encoding="utf-8")
        calls: list[list[str]] = []

        def fake_runner(cmd, **_kwargs):
            calls.append(cmd)
            if "detect" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=detect_txt, stderr="")
            if "--bus" in cmd and "capabilities" in cmd:
                bus = cmd[cmd.index("--bus") + 1]
                out = dell_caps if bus == "4" else lg_caps
                return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
            if "--bus" in cmd and "getvcp" in cmd:
                bus = cmd[cmd.index("--bus") + 1]
                out = dell_vcp if bus == "4" else lg_vcp
                return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
            if "setvcp" in cmd:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=5.0)
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unknown")

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            sysfs = root / "backlight" / "amdgpu_bl1"
            sysfs.mkdir(parents=True)
            (sysfs / "brightness").write_text("500\n", encoding="utf-8")
            (sysfs / "actual_brightness").write_text("500\n", encoding="utf-8")
            (sysfs / "max_brightness").write_text("1000\n", encoding="utf-8")

            mutter = MutterDisplayClient(adapter=_FakeMutterAdapter(backlight_active=True))
            bl_driver = BacklightDriver(
                mutter_client=mutter,
                sysfs_root=root / "backlight",
                which_fn=lambda _n: None,
            )
            ddc_driver = DdcUtilDriver(
                mutter_client=mutter,
                command_runner=fake_runner,
                which_fn=lambda _n: "/usr/bin/ddcutil",
            )
            backend = PowerDisplayBackend(
                demo=False,
                settings_store=SettingsStore(root / "settings.json"),
                profile_store=ProfileStore(root / "profiles.json"),
                state_store=MonitorStateStore(root / "state.json"),
                mutter_client=mutter,
                backlight_driver=bl_driver,
                ddc_driver=ddc_driver,
                debounce_seconds=0.0,
            )
            state = backend.initialize()
            self.assertEqual(len(state["monitors"]), 3)
            # Verify bundled getvcp was called only ONCE per DDC monitor!
            getvcp_calls = [c for c in calls if "getvcp" in c]
            self.assertEqual(len(getvcp_calls), 2)

            # Backlight write goes through Mutter SetBacklight (no software dimming!)
            internal_mon = next(m for m in state["monitors"] if m["is_builtin"])
            res_bl = backend.set_feature(internal_mon["monitor_id"], "brightness", 80)
            self.assertTrue(res_bl.ok)
            self.assertEqual(mutter._adapter.backlight_writes, [(7, "eDP-1", 800)])

            # Timeout on DDC write surfaces visible error
            dell_mon = next(m for m in state["monitors"] if "DEL" in m["monitor_id"])
            res_err = backend.set_feature(dell_mon["monitor_id"], "brightness", 55)
            self.assertFalse(res_err.ok)
            self.assertIn("timed out", res_err.message)
            backend.shutdown()


if __name__ == "__main__":
    unittest.main()
