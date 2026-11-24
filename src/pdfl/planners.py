# Portions adapted from Microsoft PowerToys PowerDisplay (MIT License).
# Copyright (c) Microsoft Corporation. All rights reserved.
# See THIRD_PARTY_NOTICES.md for the full MIT license text.

"""Pure planning helpers: stable monitor IDs, linked brightness, wheel steps, and Wayland rotation layout."""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable


def clamp_percent(value: int | float) -> int:
    """Clamp any numeric percentage into [0, 100]."""
    return max(0, min(100, int(round(value))))


def build_stable_ddc_id(
    mfg_id: str = "",
    model: str = "",
    serial: str = "",
    edid_hex: str = "",
    connector: str = "",
    fallback_index: int = 0,
) -> str:
    """Construct a persistent monitor ID independent of temporary /dev/i2c-N bus numbers."""
    clean_mfg = (mfg_id or "UNK").strip().upper()
    clean_model = "_".join((model or "Display").strip().split())
    clean_serial = (serial or "").strip()
    if clean_serial:
        return f"ddc:{clean_mfg}:{clean_model}:{clean_serial}"
    if edid_hex:
        digest = hashlib.sha256(edid_hex.lower().encode("ascii")).hexdigest()[:12]
        return f"ddc:{clean_mfg}:{clean_model}:edid-{digest}"
    if connector:
        return f"ddc:{clean_mfg}:{clean_model}:{connector}"
    return f"ddc:{clean_mfg}:{clean_model}:idx-{fallback_index}"


def build_stable_internal_id(
    connector: str = "eDP-1",
    vendor: str = "",
    product: str = "",
    sysfs_name: str = "",
) -> str:
    """Construct a persistent ID for an internal backlight panel."""
    parts = [p.strip() for p in (connector, vendor, product) if p and p.strip()]
    if parts:
        return "internal:" + ":".join(parts)
    if sysfs_name:
        return f"internal:{sysfs_name}"
    return "internal:eDP-1"


@dataclass(frozen=True)
class LinkTarget:
    """Monitor snapshot relevant for linked-brightness seeding."""

    monitor_id: str
    monitor_number: int
    brightness: int


class LinkedBrightnessPlanner:
    """Computes the initial master slider value when linked brightness turns on."""

    @staticmethod
    def seed(targets: Iterable[LinkTarget]) -> int | None:
        items = list(targets)
        if not items:
            return None
        items.sort(
            key=lambda t: (
                t.monitor_number if t.monitor_number > 0 else 1_000_000,
                t.monitor_id,
            )
        )
        return clamp_percent(items[0].brightness)


@dataclass(frozen=True)
class WheelTarget:
    """Monitor snapshot relevant for panel icon mouse-wheel adjustment."""

    monitor_id: str
    is_primary: bool
    supports_brightness: bool
    brightness_writable: bool
    hidden: bool
    current_brightness: int | None


@dataclass(frozen=True)
class WheelAdjustment:
    """Computed brightness change for one monitor."""

    monitor_id: str
    brightness: int


class WheelAdjustmentPlanner:
    """Selects eligible monitors and computes clamped target brightness values."""

    @staticmethod
    def is_eligible(mode: str, target: WheelTarget) -> bool:
        if mode == "disabled":
            return False
        if (
            not target.monitor_id
            or target.hidden
            or not target.supports_brightness
            or not target.brightness_writable
            or target.current_brightness is None
        ):
            return False
        if mode == "primary":
            return target.is_primary
        if mode == "all":
            return True
        return False

    @staticmethod
    def plan(
        mode: str, targets: Iterable[WheelTarget], delta: int
    ) -> list[WheelAdjustment]:
        if mode == "disabled" or delta == 0:
            return []
        bounded_delta = max(-100, min(100, int(delta)))
        adjustments: list[WheelAdjustment] = []
        for target in targets:
            if not WheelAdjustmentPlanner.is_eligible(mode, target):
                continue
            assert target.current_brightness is not None
            next_val = clamp_percent(target.current_brightness + bounded_delta)
            adjustments.append(WheelAdjustment(target.monitor_id, next_val))
        return adjustments


DEGREES_TO_MUTTER_TRANSFORM: dict[int, int] = {
    0: 0,
    90: 1,
    180: 2,
    270: 3,
}
MUTTER_TRANSFORM_TO_DEGREES: dict[int, int] = {
    0: 0,
    1: 90,
    2: 180,
    3: 270,
    4: 0,
    5: 90,
    6: 180,
    7: 270,
}


def _logical_dimensions(
    mode_width: int,
    mode_height: int,
    scale: float,
    transform: int,
    layout_mode: int,
) -> tuple[int, int]:
    """Compute logical monitor dimensions in Mutter coordinate space."""
    swapped = (transform % 4) in (1, 3)
    phys_w, phys_h = (mode_height, mode_width) if swapped else (mode_width, mode_height)
    if layout_mode == 1 and scale > 0:
        return max(1, round(phys_w / scale)), max(1, round(phys_h / scale))
    return phys_w, phys_h


def recompute_logical_layout_for_rotation(
    logical_monitors: list[dict[str, Any]],
    active_modes: dict[str, tuple[int, int, str]],
    target_connector: str,
    new_transform: int,
    layout_mode: int = 1,
) -> list[dict[str, Any]]:
    """Return updated logical monitor configs avoiding overlaps/gaps after rotation.

    `logical_monitors` items are dicts with keys:
      x, y, scale, transform, is_primary, connectors (list[str])
    `active_modes` maps connector -> (width, height, mode_id)
    """
    updated: list[dict[str, Any]] = []
    # Sort left-to-right, top-to-bottom so adjacent monitors reflow cleanly.
    ordered = sorted(logical_monitors, key=lambda lm: (int(lm["x"]), int(lm["y"])))

    old_boxes: list[tuple[int, int, int, int, dict[str, Any], int]] = []
    for lm in ordered:
        conn = lm["connectors"][0]
        w, h, _ = active_modes[conn]
        old_lw, old_lh = _logical_dimensions(
            w, h, float(lm["scale"]), int(lm["transform"]), layout_mode
        )
        t = new_transform if target_connector in lm["connectors"] else int(lm["transform"])
        old_boxes.append((int(lm["x"]), int(lm["y"]), old_lw, old_lh, lm, t))

    if len(old_boxes) == 1:
        _, _, _, _, lm, t = old_boxes[0]
        updated.append(
            {
                "x": int(lm["x"]),
                "y": int(lm["y"]),
                "scale": float(lm["scale"]),
                "transform": t,
                "is_primary": bool(lm["is_primary"]),
                "connectors": list(lm["connectors"]),
            }
        )
        return updated

    # Reflow horizontally if monitors were arranged side-by-side, or vertically if stacked.
    xs = [b[0] for b in old_boxes]
    ys = [b[1] for b in old_boxes]
    horizontal_span = max(xs) - min(xs)
    vertical_span = max(ys) - min(ys)
    flow_horizontal = horizontal_span >= vertical_span

    cursor = 0
    base_y = old_boxes[0][1]
    base_x = old_boxes[0][0]
    for _x, _y, _ow, _oh, lm, t in old_boxes:
        conn = lm["connectors"][0]
        w, h, _ = active_modes[conn]
        new_lw, new_lh = _logical_dimensions(
            w, h, float(lm["scale"]), t, layout_mode
        )
        nx = cursor if flow_horizontal else base_x
        ny = base_y if flow_horizontal else cursor
        updated.append(
            {
                "x": nx,
                "y": ny,
                "scale": float(lm["scale"]),
                "transform": t,
                "is_primary": bool(lm["is_primary"]),
                "connectors": list(lm["connectors"]),
            }
        )
        cursor += new_lw if flow_horizontal else new_lh

    return updated


class DebouncedCoalescer:
    """Thread-safe per-key debouncer that executes only the latest scheduled value."""

    def __init__(self, delay_seconds: float = 0.2) -> None:
        self._delay = max(0.0, delay_seconds)
        self._lock = threading.Lock()
        self._timers: dict[str, threading.Timer] = {}
        self._closed = False

    def schedule(self, key: str, fn: Callable[[], None]) -> None:
        with self._lock:
            if self._closed:
                return
            prev = self._timers.pop(key, None)
            if prev is not None:
                prev.cancel()
            if self._delay <= 0:
                pass_direct = True
            else:
                pass_direct = False
                timer = threading.Timer(self._delay, self._run, args=(key, fn))
                timer.daemon = True
                self._timers[key] = timer
                timer.start()
        if pass_direct:
            fn()

    def _run(self, key: str, fn: Callable[[], None]) -> None:
        with self._lock:
            self._timers.pop(key, None)
            if self._closed:
                return
        fn()

    def cancel_all(self) -> None:
        with self._lock:
            self._closed = True
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
