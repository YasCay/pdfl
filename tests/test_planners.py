"""Tests for stable monitor IDs, linked brightness seed, wheel steps, and Wayland rotation layout."""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from pdfl.planners import (
    LinkedBrightnessPlanner,
    LinkTarget,
    WheelAdjustment,
    WheelAdjustmentPlanner,
    WheelTarget,
    build_stable_ddc_id,
    clamp_percent,
    recompute_logical_layout_for_rotation,
)


class PlannersTests(unittest.TestCase):
    def test_stable_id_ignores_bus_changes(self) -> None:
        id1 = build_stable_ddc_id("DEL", "Dell U2723QE", "CN04K921", "00ff", "DP-1", 1)
        id2 = build_stable_ddc_id("DEL", "Dell U2723QE", "CN04K921", "00ff", "DP-2", 4)
        self.assertEqual(id1, id2)
        self.assertNotIn("i2c", id1)

        edid_id_a = build_stable_ddc_id("GSM", "LG 34WN80C", "", "00ffaa11", "HDMI-1", 1)
        edid_id_b = build_stable_ddc_id("GSM", "LG 34WN80C", "", "00ffaa11", "HDMI-2", 2)
        self.assertEqual(edid_id_a, edid_id_b)

    def test_linked_brightness_seed_prefers_lowest_monitor_number(self) -> None:
        targets = [
            LinkTarget("mon-3", 3, 90),
            LinkTarget("mon-1", 1, 40),
            LinkTarget("mon-2", 2, 75),
        ]
        self.assertEqual(LinkedBrightnessPlanner.seed(targets), 40)
        self.assertIsNone(LinkedBrightnessPlanner.seed([]))

    def test_wheel_planner_modes_and_clamping(self) -> None:
        targets = [
            WheelTarget("mon-1", is_primary=False, supports_brightness=True, brightness_writable=True, hidden=False, current_brightness=95),
            WheelTarget("mon-2", is_primary=True, supports_brightness=True, brightness_writable=True, hidden=False, current_brightness=98),
            WheelTarget("mon-3", is_primary=False, supports_brightness=True, brightness_writable=True, hidden=True, current_brightness=20),
            WheelTarget("mon-4", is_primary=False, supports_brightness=True, brightness_writable=False, hidden=False, current_brightness=50),
        ]
        self.assertEqual(WheelAdjustmentPlanner.plan("disabled", targets, 5), [])
        self.assertEqual(
            WheelAdjustmentPlanner.plan("primary", targets, 5),
            [WheelAdjustment("mon-2", 100)],
        )
        self.assertEqual(
            WheelAdjustmentPlanner.plan("all", targets, -10),
            [WheelAdjustment("mon-1", 85), WheelAdjustment("mon-2", 88)],
        )

    def test_rotation_layout_reflows_multi_monitor_without_overlap(self) -> None:
        logical = [
            {"x": 0, "y": 0, "scale": 1.0, "transform": 0, "is_primary": True, "connectors": ["DP-1"]},
            {"x": 1920, "y": 0, "scale": 1.0, "transform": 0, "is_primary": False, "connectors": ["HDMI-1"]},
        ]
        modes = {
            "DP-1": (1920, 1080, "1920x1080@60"),
            "HDMI-1": (2560, 1440, "2560x1440@60"),
        }
        # Rotating DP-1 to 90 deg makes its width 1080 -> HDMI-1 must shift left to x=1080!
        updated = recompute_logical_layout_for_rotation(logical, modes, "DP-1", 1, layout_mode=1)
        self.assertEqual(updated[0]["transform"], 1)
        self.assertEqual(updated[0]["x"], 0)
        self.assertEqual(updated[1]["x"], 1080)
        self.assertEqual(updated[1]["y"], 0)
        self.assertEqual(clamp_percent(140), 100)
        self.assertEqual(clamp_percent(-10), 0)


if __name__ == "__main__":
    unittest.main()
