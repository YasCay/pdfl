"""Tests for atomic XDG JSON storage, defaults, and profile ordering."""

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from pdfl.models import AppSettings, MonitorPreference, Profile, ProfileMonitorSetting
from pdfl.storage import MonitorStateStore, ProfileStore, SettingsStore


class StorageAndProfilesTests(unittest.TestCase):
    def test_safe_defaults_and_settings_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SettingsStore(pathlib.Path(tmp) / "settings.json")
            defaults = store.load()
            self.assertFalse(defaults.restore_on_launch)
            self.assertEqual(defaults.mouse_wheel_mode, "disabled")
            self.assertFalse(defaults.theme_profile_switching_enabled)

            pref_default = MonitorPreference()
            self.assertFalse(pref_default.show_color_temperature)
            self.assertFalse(pref_default.show_power_state)

            defaults.mouse_wheel_mode = "invalid_mode"
            defaults.mouse_wheel_step = 999
            defaults.refresh_delay_seconds = 0.01
            saved = store.save(defaults)
            self.assertEqual(saved.mouse_wheel_mode, "disabled")
            self.assertEqual(saved.mouse_wheel_step, 25)
            self.assertEqual(saved.refresh_delay_seconds, 0.5)

    def test_corrupt_json_falls_back_gracefully(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "profiles.json"
            p.write_text("{corrupt json!!!", encoding="utf-8")
            store = ProfileStore(p)
            self.assertEqual(store.list_profiles(), [])

    def test_profile_crud_and_display_order_preservation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(pathlib.Path(tmp) / "profiles.json")
            p1 = store.add_or_update(
                Profile(0, "Work", [ProfileMonitorSetting("mon-1", brightness=80)])
            )
            p2 = store.add_or_update(
                Profile(0, "Night", [ProfileMonitorSetting("mon-1", brightness=25)])
            )
            p3 = store.add_or_update(
                Profile(0, "Gaming", [ProfileMonitorSetting("mon-1", brightness=100)])
            )
            self.assertEqual([p.id for p in store.list_profiles()], [1, 2, 3])

            # Reorder Night (id=2) to top; IDs stay immutable!
            reordered = store.reorder(p2.id, -1)
            self.assertEqual([(p.id, p.name) for p in reordered], [(2, "Night"), (1, "Work"), (3, "Gaming")])

            # Update Work preserves position
            store.add_or_update(
                Profile(p1.id, "Work Bright", [ProfileMonitorSetting("mon-1", brightness=85)])
            )
            self.assertEqual(
                [(p.id, p.name) for p in store.list_profiles()],
                [(2, "Night"), (1, "Work Bright"), (3, "Gaming")],
            )

            self.assertTrue(store.delete(p3.id))
            self.assertFalse(store.delete(999))
            self.assertEqual(len(store.list_profiles()), 2)

            # Next created profile gets id=4 (monotonic next_id)
            p4 = store.add_or_update(Profile(0, "Presentation", []))
            self.assertEqual(p4.id, 4)

    def test_monitor_state_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            st = MonitorStateStore(pathlib.Path(tmp) / "monitor_state.json")
            st.update_monitor("ddc:DEL:U2723QE:CN04K921", {"brightness": 70, "contrast": 65})
            loaded = st.load_all()
            self.assertEqual(loaded["ddc:DEL:U2723QE:CN04K921"]["brightness"], 70)


if __name__ == "__main__":
    unittest.main()
