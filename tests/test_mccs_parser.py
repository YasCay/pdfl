"""Tests for recursive-descent MCCS capabilities string parser."""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from pdfl.mccs_parser import (
    MccsCapabilitiesParser,
    parse_ddcutil_capabilities_output,
)

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


class MccsParserTests(unittest.TestCase):
    def test_parses_standard_nested_mccs_string(self) -> None:
        raw = (
            "(prot(monitor)type(lcd)model(PD3220U)cmds(01 02 03 07)"
            "vcp(10 12 14(04 05 06) 60(0F 11 12) 62)mccs_ver(2.2))"
        )
        res = MccsCapabilitiesParser.parse(raw)
        self.assertTrue(res.is_valid)
        self.assertEqual(res.errors, [])
        caps = res.capabilities
        self.assertEqual(caps.protocol, "monitor")
        self.assertEqual(caps.display_type, "lcd")
        self.assertEqual(caps.model, "PD3220U")
        self.assertEqual(caps.mccs_version, "2.2")
        self.assertEqual(caps.supported_commands, [0x01, 0x02, 0x03, 0x07])
        self.assertIn(0x10, caps.vcp_features)
        self.assertIn(0x12, caps.vcp_features)
        self.assertIn(0x62, caps.vcp_features)
        self.assertEqual(caps.vcp_features[0x14].supported_values, [0x04, 0x05, 0x06])
        self.assertEqual(caps.vcp_features[0x60].supported_values, [0x0F, 0x11, 0x12])

    def test_handles_omitted_outer_parens_and_concatenated_hex(self) -> None:
        raw = "prot(monitor)type(lcd)cmds(010203)vcp(10 14(010508))window1(type(PIP)max(1920 1080))"
        res = MccsCapabilitiesParser.parse(raw)
        self.assertTrue(res.is_valid)
        self.assertEqual(res.capabilities.supported_commands, [0x01, 0x02, 0x03])
        self.assertEqual(res.capabilities.vcp_features[0x14].supported_values, [0x01, 0x05, 0x08])
        self.assertIn(1, res.capabilities.windows)

    def test_handles_vcpname_overrides_and_deep_nesting(self) -> None:
        raw = "(vcp(10 F0(01 02))vcpname(F0(My Custom Mode (Warm))))"
        res = MccsCapabilitiesParser.parse(raw)
        self.assertEqual(res.capabilities.vcp_features[0xF0].name, "My Custom Mode (Warm)")

    def test_handles_malformed_or_empty_gracefully(self) -> None:
        empty_res = MccsCapabilitiesParser.parse("")
        self.assertFalse(empty_res.is_valid)
        bad_res = MccsCapabilitiesParser.parse("(prot(monitor)vcp(10 12")
        self.assertGreater(len(bad_res.errors), 0)
        self.assertIn(0x10, bad_res.capabilities.vcp_features)

    def test_parses_dell_and_lg_fixtures(self) -> None:
        dell_txt = (FIXTURES / "ddcutil_caps_dell.txt").read_text(encoding="utf-8")
        dell = parse_ddcutil_capabilities_output(dell_txt)
        self.assertEqual(dell.capabilities.model, "Dell U2723QE")
        self.assertIn(0x62, dell.capabilities.vcp_features)
        self.assertIn(0xD6, dell.capabilities.vcp_features)

        lg_txt = (FIXTURES / "ddcutil_caps_lg.txt").read_text(encoding="utf-8")
        lg = parse_ddcutil_capabilities_output(lg_txt)
        self.assertEqual(lg.capabilities.model, "LG 34WN80C")
        self.assertNotIn(0x62, lg.capabilities.vcp_features)
        self.assertNotIn(0xD6, lg.capabilities.vcp_features)

    def test_fallback_formatted_feature_lines_when_no_raw_string(self) -> None:
        formatted = """
Model: Fallback Monitor
VCP Features:
   Feature: 10 (Brightness)
   Feature: 60 (Input Source)
      Values:
         0f: DisplayPort-1
         11: HDMI-1
"""
        res = parse_ddcutil_capabilities_output(formatted)
        self.assertIn(0x10, res.capabilities.vcp_features)
        self.assertEqual(res.capabilities.vcp_features[0x60].supported_values, [0x0F, 0x11])


if __name__ == "__main__":
    unittest.main()
