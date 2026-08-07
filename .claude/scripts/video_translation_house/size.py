"""Compute full frame dimensions from a single axis + an aspect ratio.

Pure arithmetic, no I/O. Given one axis (width OR height) and a target aspect ratio
(16:9 by default — the YouTube standard), return the complete WxH, rounded to EVEN
integers because H.264/yuv420p requires even dimensions. Used to size the still-image
canvas (TASK 1) and as a general operator helper:

    vid_cli.py size w 1920   -> 1920x1080
    vid_cli.py size h 1080   -> 1920x1080
    vid_cli.py size w 1042   -> 1042x586   (16:9, even-rounded)
"""
from __future__ import annotations

from typing import Any

from .errors import VideoTranslationHouseError


def _even(n: float) -> int:
    """Round to the nearest even integer (>= 2); H.264/yuv420p needs even dims."""
    i = int(round(n))
    if i % 2 != 0:
        i += 1
    return max(2, i)


def parse_aspect(aspect: str) -> tuple[int, int]:
    """Parse ``"16:9"`` / ``"4:3"`` / ``"1:1"`` into (w_ratio, h_ratio)."""
    text = str(aspect).strip().replace("x", ":").replace("/", ":")
    parts = text.split(":")
    if len(parts) != 2:
        raise VideoTranslationHouseError(
            f"aspect must look like 'W:H' (e.g. 16:9), got {aspect!r}"
        )
    try:
        w, h = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise VideoTranslationHouseError(f"aspect ratio parts must be integers: {aspect!r}") from exc
    if w <= 0 or h <= 0:
        raise VideoTranslationHouseError(f"aspect ratio parts must be positive: {aspect!r}")
    return w, h


def compute_dimensions(axis: str, value: int, *, aspect: str = "16:9") -> dict[str, Any]:
    """From one axis (``"w"`` or ``"h"``) and its pixel size, derive the full frame.

    The other axis is computed from ``aspect`` and both are snapped to even integers.
    Returns a dict with ``width``/``height``/``aspect``/``label`` (``"WxH"``).
    """
    axis = str(axis).strip().lower()
    if axis in ("w", "width"):
        axis = "w"
    elif axis in ("h", "height"):
        axis = "h"
    else:
        raise VideoTranslationHouseError(f"axis must be 'w' or 'h', got {axis!r}")
    value = int(value)
    if value <= 0:
        raise VideoTranslationHouseError(f"size value must be positive, got {value}")

    aw, ah = parse_aspect(aspect)
    if axis == "w":
        width = _even(value)
        height = _even(value * ah / aw)
    else:
        height = _even(value)
        width = _even(value * aw / ah)
    return {
        "width": width,
        "height": height,
        "aspect": f"{aw}:{ah}",
        "label": f"{width}x{height}",
        "given": {"axis": axis, "value": value},
    }
