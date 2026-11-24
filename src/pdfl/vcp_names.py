# Portions adapted from Microsoft PowerToys PowerDisplay (MIT License).
# Copyright (c) Microsoft Corporation. All rights reserved.
# See THIRD_PARTY_NOTICES.md for the full MIT license text.

"""MCCS v2.2a VCP code and discrete value name mappings."""

from __future__ import annotations

from typing import Iterable

from pdfl.models import CustomVcpMapping

VCP_BRIGHTNESS = 0x10
VCP_CONTRAST = 0x12
VCP_COLOR_TEMP = 0x14
VCP_INPUT_SOURCE = 0x60
VCP_VOLUME = 0x62
VCP_AUDIO_MUTE = 0x8D
VCP_POWER_MODE = 0xD6

FEATURE_TO_VCP: dict[str, int] = {
    "brightness": VCP_BRIGHTNESS,
    "contrast": VCP_CONTRAST,
    "color_temperature": VCP_COLOR_TEMP,
    "input_source": VCP_INPUT_SOURCE,
    "volume": VCP_VOLUME,
    "power_state": VCP_POWER_MODE,
}

VCP_CODE_NAMES: dict[int, str] = {
    0x00: "Code Page",
    0x01: "Degauss",
    0x02: "New Control Value",
    0x03: "Soft Controls",
    0x04: "Restore Factory Defaults",
    0x05: "Restore Brightness and Contrast",
    0x06: "Restore Factory Geometry",
    0x08: "Restore Color Defaults",
    0x0A: "Restore Factory TV Defaults",
    0x0B: "Color Temperature Increment",
    0x0C: "Color Temperature Request",
    0x0E: "Clock",
    0x0F: "Color Saturation",
    0x10: "Brightness",
    0x11: "Flesh Tone Enhancement",
    0x12: "Contrast",
    0x13: "Backlight Control",
    0x14: "Select Color Preset",
    0x16: "Video Gain: Red",
    0x17: "User Color Vision Compensation",
    0x18: "Video Gain: Green",
    0x1A: "Video Gain: Blue",
    0x1C: "Focus",
    0x1E: "Auto Setup",
    0x1F: "Auto Color Setup",
    0x20: "Horizontal Position",
    0x22: "Horizontal Size",
    0x24: "Horizontal Pincushion",
    0x26: "Horizontal Pincushion Balance",
    0x30: "Vertical Position",
    0x32: "Vertical Size",
    0x3E: "Clock Phase",
    0x44: "Rotation",
    0x52: "Active Control",
    0x54: "Performance Preservation",
    0x59: "6 Axis Saturation: Red",
    0x5A: "6 Axis Saturation: Yellow",
    0x5B: "6 Axis Saturation: Green",
    0x5C: "6 Axis Saturation: Cyan",
    0x5D: "6 Axis Saturation: Blue",
    0x5E: "6 Axis Saturation: Magenta",
    0x60: "Input Source",
    0x62: "Audio Speaker Volume",
    0x63: "Speaker Select",
    0x64: "Audio: Microphone Volume",
    0x66: "Ambient Light Sensor",
    0x6B: "Backlight Level: White",
    0x6C: "Video Black Level: Red",
    0x6D: "Backlight Level: Red",
    0x6E: "Video Black Level: Green",
    0x6F: "Backlight Level: Green",
    0x70: "Video Black Level: Blue",
    0x71: "Backlight Level: Blue",
    0x72: "Gamma",
    0x8D: "Audio Mute",
    0x9B: "6 Axis Hue Control: Red",
    0x9C: "6 Axis Hue Control: Yellow",
    0x9D: "6 Axis Hue Control: Green",
    0x9E: "6 Axis Hue Control: Cyan",
    0x9F: "6 Axis Hue Control: Blue",
    0xA0: "Auto Setup On/Off",
    0xAA: "Screen Orientation",
    0xAC: "Horizontal Frequency",
    0xAE: "Vertical Frequency",
    0xB0: "Settings",
    0xB2: "Flat Panel Sub-Pixel Layout",
    0xB4: "Source Timing Mode",
    0xB6: "Display Technology Type",
    0xB7: "Monitor Status",
    0xC0: "Display Usage Time",
    0xC6: "Enable Display of 'Display Descriptor'",
    0xC8: "Display Controller Type",
    0xC9: "Display Firmware Level",
    0xCA: "OSD",
    0xCC: "OSD Language",
    0xD4: "Stereo Video Mode",
    0xD6: "Power Mode",
    0xD7: "Auxiliary Power Output",
    0xD8: "Scan Mode",
    0xD9: "Image Mode",
    0xDA: "On Screen Display",
    0xDB: "Image Mode",
    0xDC: "Display Application",
    0xDF: "VCP Version",
}

VCP_VALUE_NAMES: dict[int, dict[int, str]] = {
    0x14: {
        0x01: "sRGB",
        0x02: "Display Native",
        0x03: "4000K",
        0x04: "5000K",
        0x05: "6500K",
        0x06: "7500K",
        0x07: "8200K",
        0x08: "9300K",
        0x09: "10000K",
        0x0A: "11500K",
        0x0B: "User 1",
        0x0C: "User 2",
        0x0D: "User 3",
    },
    0x60: {
        0x01: "VGA-1",
        0x02: "VGA-2",
        0x03: "DVI-1",
        0x04: "DVI-2",
        0x05: "Composite Video 1",
        0x06: "Composite Video 2",
        0x07: "S-Video-1",
        0x08: "S-Video-2",
        0x09: "Tuner-1",
        0x0A: "Tuner-2",
        0x0B: "Tuner-3",
        0x0C: "Component Video 1",
        0x0D: "Component Video 2",
        0x0E: "Component Video 3",
        0x0F: "DisplayPort-1",
        0x10: "DisplayPort-2",
        0x11: "HDMI-1",
        0x12: "HDMI-2",
        0x1B: "USB-C",
    },
    0xD6: {
        0x01: "On",
        0x02: "Standby",
        0x03: "Suspend",
        0x04: "Off (DPM)",
        0x05: "Off (Hard)",
    },
    0x8D: {
        0x01: "Muted",
        0x02: "Unmuted",
    },
    0xDC: {
        0x00: "Standard/Default",
        0x01: "Productivity",
        0x02: "Mixed",
        0x03: "Movie",
        0x04: "User Defined",
        0x05: "Games",
        0x06: "Sports",
        0x07: "Professional (calibration)",
        0x0A: "Demonstration",
        0xF0: "Dynamic Contrast",
    },
    0x62: {
        0x00: "Mute",
    },
    0xDB: {
        0x00: "Standard",
        0x01: "Multimedia",
        0x02: "Movie",
        0x03: "Game",
        0x04: "Paper",
        0x05: "Color Temp",
        0x06: "Custom Color",
    },
}


def get_code_name(code: int) -> str:
    """Return human-readable MCCS name for a VCP code."""
    if code in VCP_CODE_NAMES:
        return VCP_CODE_NAMES[code]
    if 0xE0 <= code <= 0xFF:
        return f"Manufacturer Specific (0x{code:02X})"
    return f"Unknown (0x{code:02X})"


def get_value_name(
    vcp_code: int,
    value: int,
    custom_mappings: Iterable[CustomVcpMapping] | None = None,
    monitor_id: str = "",
) -> str:
    """Resolve display label for a VCP discrete value.

    Priority:
    1. Monitor-specific custom mapping
    2. Global custom mapping (apply_to_all=True)
    3. Built-in MCCS value table
    4. Hex fallback (e.g. '0x1A')
    """
    if custom_mappings:
        mappings_list = list(custom_mappings)
        if monitor_id:
            for mapping in mappings_list:
                if (
                    mapping.vcp_code == vcp_code
                    and mapping.value == value
                    and not mapping.apply_to_all
                    and mapping.target_monitor_id == monitor_id
                    and mapping.custom_name.strip()
                ):
                    return mapping.custom_name.strip()
        for mapping in mappings_list:
            if (
                mapping.vcp_code == vcp_code
                and mapping.value == value
                and mapping.apply_to_all
                and mapping.custom_name.strip()
            ):
                return mapping.custom_name.strip()

    builtin = VCP_VALUE_NAMES.get(vcp_code, {}).get(value)
    if builtin is not None:
        return builtin
    return f"0x{value:02X}"
