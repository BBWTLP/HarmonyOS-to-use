"""OCR adapter for the existing `OcrEngine` protocol.

The protocol in `grounding.py` stays as it is; this module only adapts a real
engine to it and normalises everything the engine returns. The adapter is the
isolation boundary: a missing engine, a crash, a timeout or nonsense geometry
all become an explicit "no OCR evidence" answer instead of an exception reaching
the grounding pipeline.
"""
from __future__ import annotations

from typing import Any, Protocol

from .visual_regions import DEFAULT_MIN_CONFIDENCE, normalize_regions


class OcrBackend(Protocol):
    """A concrete OCR engine (system OCR, PaddleOCR, a hosted service, ...)."""

    available: bool

    def recognize(self, image_bytes: bytes) -> list[dict[str, Any]]: ...


class OcrAdapter:
    """Make any backend satisfy the grounding `OcrEngine` contract safely."""

    def __init__(self, backend: OcrBackend | None = None, *,
                 display: dict[str, Any] | None = None, rotation: int = 0,
                 min_confidence: float = DEFAULT_MIN_CONFIDENCE,
                 max_regions: int = 64):
        self.backend = backend
        self.display = display
        self.rotation = rotation
        self.min_confidence = min_confidence
        self.max_regions = max_regions
        self.last_error: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.backend is not None and getattr(self.backend, "available", False))

    @property
    def reason(self) -> str:
        if self.backend is None:
            return "no_ocr_backend_configured"
        if not getattr(self.backend, "available", False):
            return getattr(self.backend, "reason", "ocr_backend_unavailable")
        return self.last_error or "ready"

    def with_observation(self, observation: dict[str, Any]) -> "OcrAdapter":
        """Return an adapter bound to one observation's display geometry."""
        display = observation.get("display") or {}
        rotation = display.get("rotation")
        return OcrAdapter(self.backend, display=display,
                          rotation=rotation if isinstance(rotation, int) else 0,
                          min_confidence=self.min_confidence,
                          max_regions=self.max_regions)

    def recognize(self, image_bytes: bytes) -> list[dict[str, Any]]:
        """Return normalised `{text, bounds, confidence}` regions, never a point."""
        if not self.available:
            return []
        try:
            raw = self.backend.recognize(image_bytes)
        except Exception as error:
            # Grounding reads `last_error` through `reason` and records it as a note.
            self.last_error = f"ocr_failed:{type(error).__name__}"
            return []
        regions = normalize_regions(raw, display=self.display, rotation=self.rotation,
                                    min_confidence=self.min_confidence)
        if len(regions) > self.max_regions:
            self.last_error = "ocr_region_budget_exceeded"
        return regions[:self.max_regions]


def build_ocr_engine(backend: OcrBackend | None = None, *,
                     display: dict[str, Any] | None = None,
                     rotation: int = 0) -> OcrAdapter | None:
    """Adapter for a configured backend; None when OCR is not configured."""
    if backend is None:
        return None
    return OcrAdapter(backend, display=display, rotation=rotation)
