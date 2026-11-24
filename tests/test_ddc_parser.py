"""Tests for ddcutil 2.2.5 detect, getvcp --terse, and scaling helpers."""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from pdfl.ddc_parser import (
    classify_ddcutil_bus_error,
    parse_ddcutil_detect,
    parse_ddcutil_getvcp_terse,
    scale_percent_to_raw,
    scale_raw_to_percent,
)

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


class DdcParserTests(unittest.TestCase):
    def test_parse_detect_multi_monitor_fixture(self) -> None:
        raw = (FIXTURES / "ddcutil_detect_multi.txt").read_text(encoding="utf-8")
        monitors, errors = parse_ddcutil_detect(raw)
        self.assertEqual(errors, [])
        self.assertEqual(len(monitors), 2)

        dell = monitors[0]
        self.assertEqual(dell.display_number, 1)
        self.assertEqual(dell.bus_number, 4)
        self.assertEqual(dell.connector, "DP-1")
        self.assertEqual(dell.mfg_id, "DEL")
        self.assertEqual(dell.model, "Dell U2723QE")
        self.assertEqual(dell.serial, "CN04K921")
        self.assertTrue(len(dell.edid_hex) >= 64)

        lg = monitors[1]
        self.assertEqual(lg.bus_number, 7)
        self.assertEqual(lg.connector, "HDMI-A-1")
        self.assertEqual(lg.serial, "")
        self.assertIn("987654", lg.binary_serial)

    def test_detects_missing_i2c_and_permission_errors(self) -> None:
        mons, errs = parse_ddcutil_detect("No /dev/i2c devices exist.\nddcutil requires module i2c-dev.")
        self.assertEqual(mons, [])
        self.assertTrue(any("i2c-dev" in e for e in errs))

        mons2, errs2 = parse_ddcutil_detect("", "Error opening /dev/i2c-4: Permission denied")
        self.assertEqual(mons2, [])
        self.assertTrue(any("Permission denied" in e for e in errs2))

    def test_parse_getvcp_terse_fixture(self) -> None:
        dell_raw = (FIXTURES / "ddcutil_getvcp_terse_dell.txt").read_text(encoding="utf-8")
        vals = parse_ddcutil_getvcp_terse(dell_raw)
        self.assertEqual(vals[0x10].kind, "C")
        self.assertEqual(vals[0x10].current, 75)
        self.assertEqual(vals[0x10].maximum, 100)
        self.assertEqual(vals[0x14].current, 0x05)
        self.assertEqual(vals[0x60].current, 0x0F)
        self.assertEqual(vals[0xD6].current, 0x01)

        lg_raw = (FIXTURES / "ddcutil_getvcp_terse_lg.txt").read_text(encoding="utf-8")
        vals_lg = parse_ddcutil_getvcp_terse(lg_raw)
        self.assertEqual(vals_lg[0x62].kind, "ERR")
        self.assertIsNone(vals_lg[0x62].current)

    def test_parse_getvcp_cnc_and_scaling(self) -> None:
        vals = parse_ddcutil_getvcp_terse("VCP 60 CNC x00 x00 x00 x1b\nVCP 10 C 128 255\n")
        self.assertEqual(vals[0x60].current, 0x1B)
        self.assertEqual(scale_raw_to_percent(128, 255), 50)
        self.assertEqual(scale_percent_to_raw(50, 255), 128)
        self.assertEqual(scale_raw_to_percent(500, 100), 100)
        self.assertEqual(scale_percent_to_raw(-20, 200), 0)

    def test_parse_detect_drm_connector_underscore_and_bus_errors(self) -> None:
        raw = """Display 1
   I2C bus:  /dev/i2c-9
      DRM_connector:                             card1-DP-1
   EDID synopsis:
      Mfg id:               BNQ - UNK
      Model:                BenQ EW3270U
      Product code:         31056  (0x7950)
      Serial number:        DEMO0001234
   VCP version:         2.2
"""
        monitors, errors = parse_ddcutil_detect(raw)
        self.assertEqual(errors, [])
        self.assertEqual(len(monitors), 1)
        self.assertEqual(monitors[0].connector, "DP-1")
        self.assertEqual(monitors[0].bus_number, 9)
        self.assertEqual(monitors[0].model, "BenQ EW3270U")

        code1, _ = classify_ddcutil_bus_error(
            "VCP 10 ERR",
            "Error detecting VCP version using VCP feature xDF: Error_Info[DDCRC_RETRIES, causes: EREMOTEIO(10)]",
        )
        self.assertEqual(code1, "ddc_unresponsive")

        code2, _ = classify_ddcutil_bus_error(
            "", "Max wait time 3000 milliseconds exceeded after 32 flock() calls\nflock() for /dev/i2c-9 failed"
        )
        self.assertEqual(code2, "bus_locked")

        code3, _ = classify_ddcutil_bus_error("", "No monitor detected on bus /dev/i2c-7")
        self.assertEqual(code3, "no_monitor_on_bus")

        code4, _ = classify_ddcutil_bus_error("", "", "DDC/CI timed out after 4.0s")
        self.assertEqual(code4, "timeout")


if __name__ == "__main__":
    unittest.main()
