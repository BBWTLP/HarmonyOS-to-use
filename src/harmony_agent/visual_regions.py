"""Normalisation shared by the visual grounding providers (OCR and VLM).

Both providers hand back untrusted geometry. Everything a provider returns is
parsed, clamped, scored and de-duplicated here before it can become a
`GroundedTarget`, so a broken or hostile backend cannot produce a region the
runtime would later have to trust.
"""
from __future__ import annotations

from typing import Any

#: Regions below this confidence are dropped instead of being offered.
DEFAULT_MIN_CONFIDENCE = 0.3


def normalize_bounds(value: Any) -> tuple[int, int, int, int] | None:
    """Accept a list, tuple or {left, top, right, bottom} dict; else None."""
    if isinstance(value, dict):
        values = [value.get(key) for key in ("left", "top", "right", "bottom")]
    elif isinstance(value, (list, tuple)) and len(value) == 4:
        values = list(value)
    else:
        return None
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in values):
        return None
    left, top, right, bottom = (int(round(float(item))) for item in values)
    if right <= left or bottom <= top:
        return None
    return (left, top, right, bottom)


def display_size(display: dict[str, Any] | None, rotation: int = 0) -> tuple[int, int] | None:
    """Usable pixel bounds of the screen, swapped when the surface is rotated."""
    if not display:
        return None
    width, height = display.get("width"), display.get("height")
    if not isinstance(width, int) or not isinstance(height, int):
        return None
    if width <= 0 or height <= 0:
        return None
    if rotation % 2:
        return (height, width)
    return (width, height)


def clip_and_validate(bounds: tuple[int, int, int, int], size: tuple[int, int] | None,
                      *, margin: int = 0) -> tuple[int, int, int, int] | None:
    """Keep only the on-screen part of a region; None when nothing remains."""
    if size is None:
        return bounds if bounds[2] > bounds[0] and bounds[3] > bounds[1] else None
    width, height = size
    left = max(0, min(width, bounds[0]))
    top = max(0, min(height, bounds[1]))
    right = max(0, min(width, bounds[2]))
    bottom = max(0, min(height, bounds[3]))
    if right - left <= margin or bottom - top <= margin:
        return None
    return (left, top, right, bottom)


def normalize_confidence(value: Any, default: float = 0.5) -> float | None:
    """Clamp a score into [0, 1]; None when it is not a finite number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    score = float(value)
    if score != score or score in (float("inf"), float("-inf")):
        return None
    return max(0.0, min(1.0, score))


def normalize_regions(entries: Any, *, display: dict[str, Any] | None,
                      rotation: int = 0, min_confidence: float = DEFAULT_MIN_CONFIDENCE,
                      default_confidence: float = 0.5) -> list[dict[str, Any]]:
    """Normalise one provider answer into trusted region records.

    Each entry keeps `text`, `bounds`, `confidence` and `method`; malformed,
    off-screen, zero-area or low-confidence entries are dropped. Identical
    regions are reported once.
    """
    if not isinstance(entries, (list, tuple)):
        return []
    size = display_size(display, rotation)
    seen: set[tuple[str, tuple[int, int, int, int]]] = set()
    regions: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        bounds = normalize_bounds(entry.get("bounds"))
        if bounds is None:
            continue
        clipped = clip_and_validate(bounds, size)
        if clipped is None:
            continue
        confidence = normalize_confidence(entry.get("confidence", entry.get("score")),
                                          default_confidence)
        if confidence is None or confidence < min_confidence:
            continue
        text = str(entry.get("text") or entry.get("label") or "").strip()
        method = str(entry.get("method") or entry.get("source") or "region")
        key = (text, clipped)
        if key in seen:
            continue
        seen.add(key)
        regions.append({"text": text, "bounds": clipped, "confidence": confidence,
                        "method": method, "rotation": rotation})
    return regions
