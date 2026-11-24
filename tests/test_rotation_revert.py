"""Tests for Wayland Mutter DisplayConfig rotation verification and auto-revert logic."""

import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from pdfl.backend import PowerDisplayBackend
from pdfl.drivers.backlight_driver import BacklightDriver
from pdfl.drivers.ddcutil_driver import DdcUtilDriver
from pdfl.drivers.mutter_display import MutterDisplayClient
from pdfl.storage import MonitorStateStore, ProfileStore, SettingsStore


class _FakeMutterRotationAdapter:
    def __init__(self, fail_verify: bool = False) -> None:
        self.fail_verify = fail_verify
        self.calls: list[tuple[int, int, list]] = []
        self.current_transform = 0

    def get_current_state(self):
        return (
            42,
            [
                (
                    ("eDP-1", "LEN", "0x9052", "0"),
                    [("1920x1080@60", 1920, 1080, 60.0, 1.0, [1.0], {"is-current": True})],
                    {"is-builtin": True, "display-name": "Laptop Panel"},
                )
            ],
            [
                (0, 0, 1.0, self.current_transform, True, [("eDP-1", "LEN", "0x9052", "0")], {})
            ],
            {"layout-mode": 1, "apply-monitors-config-allowed": True},
        )

    def get_backlight_property(self):
        return (1, [{"connector": "eDP-1", "active": True, "min": 0, "max": 100, "value": 50}])

    def apply_monitors_config(self, serial, method, logical_monitors, _active_modes):
        self.calls.append((serial, method, logical_monitors))
        if method == 0 and self.fail_verify:
            raise RuntimeError("Invalid config rejected by Mutter")
        if method in (1, 2):
            self.current_transform = logical_monitors[0]["transform"]


class RotationRevertTests(unittest.TestCase):
    def _make_backend(self, tmp: str, adapter: _FakeMutterRotationAdapter, revert_s: float = 0.25):
        root = pathlib.Path(tmp)
        sysfs = root / "backlight" / "amdgpu_bl1"
        sysfs.mkdir(parents=True)
        (sysfs / "brightness").write_text("50\n", encoding="utf-8")
        (sysfs / "max_brightness").write_text("100\n", encoding="utf-8")

        mutter = MutterDisplayClient(adapter=adapter)
        return PowerDisplayBackend(
            demo=False,
            settings_store=SettingsStore(root / "settings.json"),
            profile_store=ProfileStore(root / "profiles.json"),
            state_store=MonitorStateStore(root / "state.json"),
            mutter_client=mutter,
            backlight_driver=BacklightDriver(mutter_client=mutter, sysfs_root=root / "backlight", which_fn=lambda _x: None),
            ddc_driver=DdcUtilDriver(mutter_client=mutter, which_fn=lambda _x: None),
            rotation_revert_seconds=revert_s,
            debounce_seconds=0.0,
        )

    def test_verify_failure_aborts_without_applying(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter = _FakeMutterRotationAdapter(fail_verify=True)
            backend = self._make_backend(tmp, adapter)
            st = backend.initialize()
            mid = st["monitors"][0]["monitor_id"]

            res = backend.request_rotation(mid, 90)
            self.assertFalse(res.ok)
            self.assertEqual(adapter.current_transform, 0)
            # Only verify call (method=0) happened
            self.assertEqual([c[1] for c in adapter.calls], [0])
            backend.shutdown()

    def test_auto_revert_restores_previous_orientation_on_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter = _FakeMutterRotationAdapter()
            backend = self._make_backend(tmp, adapter, revert_s=0.2)
            st = backend.initialize()
            mid = st["monitors"][0]["monitor_id"]

            res = backend.request_rotation(mid, 90)
            self.assertTrue(res.ok)
            self.assertEqual(adapter.current_transform, 1)
            self.assertEqual([c[1] for c in adapter.calls], [0, 1])

            # Wait for auto-revert timer
            time.sleep(0.45)
            self.assertEqual(adapter.current_transform, 0)
            self.assertIsNone(backend.get_state()["pending_rotation"])
            backend.shutdown()

    def test_confirm_rotation_persists_with_method_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter = _FakeMutterRotationAdapter()
            backend = self._make_backend(tmp, adapter, revert_s=0.5)
            st = backend.initialize()
            mid = st["monitors"][0]["monitor_id"]

            res = backend.request_rotation(mid, 270)
            self.assertTrue(res.ok)
            token = res.data["token"]
            conf = backend.confirm_rotation(token)
            self.assertTrue(conf.ok)
            self.assertEqual(adapter.current_transform, 3)
            # Calls: verify(0), temp(1), verify(0), persistent(2)
            self.assertEqual([c[1] for c in adapter.calls], [0, 1, 0, 2])
            time.sleep(0.6)
            self.assertEqual(adapter.current_transform, 3)
            backend.shutdown()


if __name__ == "__main__":
    unittest.main()
