"""Tests for MCCS VCP code/value naming and custom mapping precedence."""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from pdfl.models import CustomVcpMapping
from pdfl.vcp_names import get_code_name, get_value_name


class VcpNamesTests(unittest.TestCase):
    def test_builtin_codes_and_values(self) -> None:
        self.assertEqual(get_code_name(0x10), "Brightness")
        self.assertEqual(get_code_name(0x60), "Input Source")
        self.assertEqual(get_code_name(0xE5), "Manufacturer Specific (0xE5)")
        self.assertEqual(get_value_name(0x14, 0x05), "6500K")
        self.assertEqual(get_value_name(0x60, 0x11), "HDMI-1")
        self.assertEqual(get_value_name(0x60, 0x1B), "USB-C")
        self.assertEqual(get_value_name(0xD6, 0x02), "Standby")
        self.assertEqual(get_value_name(0x60, 0x77), "0x77")

    def test_custom_mapping_precedence(self) -> None:
        mappings = [
            CustomVcpMapping(vcp_code=0x60, value=0x11, custom_name="Global HDMI", apply_to_all=True),
            CustomVcpMapping(
                vcp_code=0x60,
                value=0x11,
                custom_name="Work Laptop",
                apply_to_all=False,
                target_monitor_id="mon-a",
            ),
        ]
        # Monitor-specific wins over global
        self.assertEqual(get_value_name(0x60, 0x11, mappings, "mon-a"), "Work Laptop")
        # Other monitor gets global override
        self.assertEqual(get_value_name(0x60, 0x11, mappings, "mon-b"), "Global HDMI")
        # Unmapped value falls back to built-in
        self.assertEqual(get_value_name(0x60, 0x0F, mappings, "mon-a"), "DisplayPort-1")


if __name__ == "__main__":
    unittest.main()
