# Portions adapted from Microsoft PowerToys PowerDisplay (MIT License).
# Copyright (c) Microsoft Corporation. All rights reserved.
# See THIRD_PARTY_NOTICES.md for the full MIT license text.

"""Recursive-descent parser for DDC/CI MCCS capabilities strings."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pdfl.models import VcpFeatureSpec
from pdfl.vcp_names import get_code_name

_HEX_CHARS = frozenset("0123456789abcdefABCDEF")


@dataclass
class MccsCapabilities:
    """Structured representation of a monitor's MCCS capabilities string."""

    raw: str = ""
    protocol: str = ""
    display_type: str = ""
    model: str = ""
    mccs_version: str = ""
    supported_commands: list[int] = field(default_factory=list)
    vcp_features: dict[int, VcpFeatureSpec] = field(default_factory=dict)
    windows: dict[int, str] = field(default_factory=dict)


@dataclass
class MccsParseResult:
    """Parsed capabilities plus non-fatal parse warnings."""

    capabilities: MccsCapabilities
    errors: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return bool(
            self.capabilities.vcp_features
            or self.capabilities.protocol
            or self.capabilities.model
        )


class MccsCapabilitiesParser:
    """Recursive-descent parser handling nested parentheses and MCCS variants."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._pos = 0
        self._length = len(text)
        self._errors: list[str] = []

    @classmethod
    def parse(cls, capabilities_string: str | None) -> MccsParseResult:
        if not capabilities_string or not capabilities_string.strip():
            return MccsParseResult(MccsCapabilities(), [])
        return cls(capabilities_string)._parse_capabilities()

    def _peek(self) -> str:
        return self._text[self._pos] if self._pos < self._length else ""

    def _advance(self) -> None:
        self._pos += 1

    def _skip_ws(self) -> None:
        while self._pos < self._length and self._text[self._pos].isspace():
            self._pos += 1

    def _parse_capabilities(self) -> MccsParseResult:
        caps = MccsCapabilities(raw=self._text.strip())
        self._skip_ws()
        has_outer_parens = self._peek() == "("
        if has_outer_parens:
            self._advance()

        while self._pos < self._length:
            self._skip_ws()
            if self._pos >= self._length:
                break
            if self._peek() == ")":
                if has_outer_parens:
                    self._advance()
                break
            segment = self._parse_segment()
            if segment is not None:
                name, content = segment
                self._apply_segment(caps, name, content)

        return MccsParseResult(caps, self._errors)

    def _parse_segment(self) -> tuple[str, str] | None:
        self._skip_ws()
        start = self._pos
        while self._pos < self._length and (
            self._text[self._pos].isalnum() or self._text[self._pos] == "_"
        ):
            self._pos += 1
        ident = self._text[start : self._pos]
        if not ident:
            if self._pos < self._length:
                self._advance()
            return None

        self._skip_ws()
        if self._peek() != "(":
            self._errors.append(
                f"Expected '(' after identifier '{ident}' at position {self._pos}"
            )
            return None

        self._advance()
        content = self._parse_balanced_content()
        if self._peek() == ")":
            self._advance()
        else:
            self._errors.append(
                f"Expected ')' to close segment '{ident}' at position {self._pos}"
            )
        return ident, content

    def _parse_balanced_content(self) -> str:
        start = self._pos
        depth = 1
        while self._pos < self._length and depth > 0:
            ch = self._text[self._pos]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            self._pos += 1
        return self._text[start : self._pos]

    def _apply_segment(self, caps: MccsCapabilities, name: str, content: str) -> None:
        key = name.lower()
        if key == "prot":
            caps.protocol = content.strip()
        elif key == "type":
            caps.display_type = content.strip()
        elif key == "model":
            caps.model = content.strip()
        elif key == "mccs_ver":
            caps.mccs_version = content.strip()
        elif key == "cmds":
            caps.supported_commands = _parse_hex_list(content)
        elif key == "vcp":
            caps.vcp_features = _parse_vcp_entries(content)
        elif key == "vcpname":
            _parse_vcp_names(content, caps)
        elif key.startswith("window") and key[6:].isdigit():
            caps.windows[int(key[6:])] = content.strip()


def _parse_hex_list(content: str) -> list[int]:
    """Parse both space-separated ('01 02 03') and concatenated ('010203') hex bytes."""
    values: list[int] = []
    i = 0
    n = len(content)
    while i < n:
        if content[i].isspace():
            i += 1
            continue
        if (
            i + 1 < n
            and content[i] in _HEX_CHARS
            and content[i + 1] in _HEX_CHARS
        ):
            values.append(int(content[i : i + 2], 16))
            i += 2
        else:
            i += 1
    return values


def _parse_vcp_entries(content: str) -> dict[int, VcpFeatureSpec]:
    """Parse VCP entries inside vcp(...): hex_byte [ '(' hex_list ')' ]."""
    features: dict[int, VcpFeatureSpec] = {}
    i = 0
    n = len(content)
    while i < n:
        while i < n and content[i].isspace():
            i += 1
        if i >= n:
            break
        if (
            i + 1 < n
            and content[i] in _HEX_CHARS
            and content[i + 1] in _HEX_CHARS
        ):
            code = int(content[i : i + 2], 16)
            i += 2
            while i < n and content[i].isspace():
                i += 1
            supported: list[int] = []
            if i < n and content[i] == "(":
                i += 1
                val_start = i
                depth = 1
                while i < n and depth > 0:
                    if content[i] == "(":
                        depth += 1
                    elif content[i] == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    i += 1
                supported = _parse_hex_list(content[val_start:i])
                if i < n and content[i] == ")":
                    i += 1
            features[code] = VcpFeatureSpec(
                code=code,
                name=get_code_name(code),
                supported_values=supported,
            )
        else:
            i += 1
    return features


def _parse_vcp_names(content: str, caps: MccsCapabilities) -> None:
    """Parse vcpname(F0(Custom 1) F1(Custom 2)) overrides."""
    i = 0
    n = len(content)
    while i < n:
        while i < n and content[i].isspace():
            i += 1
        if i + 1 < n and content[i] in _HEX_CHARS and content[i + 1] in _HEX_CHARS:
            code = int(content[i : i + 2], 16)
            i += 2
            while i < n and content[i].isspace():
                i += 1
            if i < n and content[i] == "(":
                i += 1
                start = i
                depth = 1
                while i < n and depth > 0:
                    if content[i] == "(":
                        depth += 1
                    elif content[i] == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    i += 1
                custom_name = content[start:i].strip()
                if i < n and content[i] == ")":
                    i += 1
                existing = caps.vcp_features.get(code)
                if existing is not None:
                    existing.name = custom_name or existing.name
                else:
                    caps.vcp_features[code] = VcpFeatureSpec(
                        code=code, name=custom_name or get_code_name(code)
                    )
        else:
            i += 1


_RAW_CAP_LINE_RE = re.compile(r"^\s*(?:Raw capabilities string:\s*)?(\(.*\))\s*$", re.IGNORECASE)
_FEATURE_LINE_RE = re.compile(r"^\s*Feature:\s*([0-9A-Fa-f]{2})\b(.*)$")
_VALUE_LINE_RE = re.compile(r"^\s*(?:([0-9A-Fa-f]{2})\s*[:\-]\s*(.*)|Values:\s*(.*))$")


def extract_raw_capabilities_string(ddcutil_output: str) -> str:
    """Extract the raw MCCS string from ddcutil capabilities output if present."""
    for line in ddcutil_output.splitlines():
        match = _RAW_CAP_LINE_RE.match(line)
        if match:
            candidate = match.group(1).strip()
            if "vcp(" in candidate.lower() or "prot(" in candidate.lower():
                return candidate
    stripped = ddcutil_output.strip()
    if stripped.startswith("(") and "vcp(" in stripped.lower():
        return stripped
    return ""


def parse_ddcutil_capabilities_output(ddcutil_output: str) -> MccsParseResult:
    """Parse raw MCCS string or fall back to ddcutil formatted Feature lines."""
    raw = extract_raw_capabilities_string(ddcutil_output)
    if raw:
        return MccsCapabilitiesParser.parse(raw)

    caps = MccsCapabilities(raw="")
    current_code: int | None = None
    in_values = False

    for line in ddcutil_output.splitlines():
        m_feat = _FEATURE_LINE_RE.match(line)
        if m_feat:
            current_code = int(m_feat.group(1), 16)
            desc = m_feat.group(2).strip().lstrip("(").rstrip(")").strip()
            caps.vcp_features[current_code] = VcpFeatureSpec(
                code=current_code,
                name=desc or get_code_name(current_code),
            )
            in_values = False
            continue
        if current_code is not None:
            stripped = line.strip()
            if stripped.startswith("Values:"):
                in_values = True
                rest = stripped[len("Values:") :].strip()
                if rest:
                    for token in re.findall(r"\b([0-9A-Fa-f]{2})\b", rest):
                        val = int(token, 16)
                        if val not in caps.vcp_features[current_code].supported_values:
                            caps.vcp_features[current_code].supported_values.append(val)
                continue
            if in_values or stripped.startswith(("0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "a", "b", "c", "d", "e", "f", "A", "B", "C", "D", "E", "F")):
                m_val = re.match(r"^([0-9A-Fa-f]{2})\s*[:\-]", stripped)
                if m_val:
                    val = int(m_val.group(1), 16)
                    if val not in caps.vcp_features[current_code].supported_values:
                        caps.vcp_features[current_code].supported_values.append(val)
            else:
                in_values = False

    return MccsParseResult(caps, [])
