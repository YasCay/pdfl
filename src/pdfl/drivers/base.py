"""Abstract driver interfaces for monitor discovery and control."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pdfl.models import MonitorInfo, MonitorRuntimeHandle, OperationResult


@dataclass
class DiscoveredMonitor:
    """Pair of public MonitorInfo and ephemeral MonitorRuntimeHandle."""

    info: MonitorInfo
    handle: MonitorRuntimeHandle


class MonitorDriver(Protocol):
    """Protocol implemented by monitor hardware/demo backends."""

    name: str

    def discover(self) -> tuple[list[DiscoveredMonitor], list[str]]:
        """Discover monitors and return (monitors, non_fatal_warnings_or_errors)."""

    def set_feature(
        self, handle: MonitorRuntimeHandle, feature: str, value: int
    ) -> OperationResult:
        """Apply a feature change (brightness, contrast, volume, input_source, color_temperature, power_state)."""
