"""OCR adapter for the existing `OcrEngine` protocol.

The protocol in `grounding.py` stays as it is; this module only adapts a real
engine to it and normalises everything the engine returns. The adapter is the
isolation boundary: a missing engine, a crash, a timeout or nonsense geometry
all become an explicit "no OCR evidence" answer instead of an exception reaching
the grounding pipeline.

Deadline ownership: a production backend must declare ``deadline_bounded = True``
and enforce the deadline in its own transport. A backend that does not is called
through `BoundedCaller`, which bounds the caller and quarantines the provider on
the first timeout; such a backend is reported as not production-ready.
"""
from __future__ import annotations

from typing import Any, Protocol

from .provider_deadline import BoundedCaller, DeadlineExceeded
from .visual_regions import DEFAULT_MIN_CONFIDENCE, normalize_regions


class OcrBackend(Protocol):
    """A concrete OCR engine (system OCR, PaddleOCR, a hosted service, ...)."""

    available: bool
    #: Set True only when the transport itself enforces the deadline.
    deadline_bounded: bool

    def recognize(self, image_bytes: bytes) -> list[dict[str, Any]]: ...


class OcrAdapter:
    """Make any backend satisfy the grounding `OcrEngine` contract safely."""

    def __init__(self, backend: OcrBackend | None = None, *,
                 display: dict[str, Any] | None = None, rotation: int = 0,
                 min_confidence: float = DEFAULT_MIN_CONFIDENCE,
                 max_regions: int = 64, timeout_seconds: float = 15.0,
                 caller: BoundedCaller | None = None):
        self.backend = backend
        self.display = display
        self.rotation = rotation
        self.min_confidence = min_confidence
        self.max_regions = max_regions
        self.timeout_seconds = timeout_seconds
        self._caller = caller or BoundedCaller("ocr", timeout_seconds)
        self.last_error: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.backend is not None and getattr(self.backend, "available", False))

    @property
    def deadline_bounded(self) -> bool:
        return bool(getattr(self.backend, "deadline_bounded", False))

    @property
    def production_ready(self) -> bool:
        """Only a backend that owns its deadline is fit for a long-running service."""
        return self.available and self.deadline_bounded and not self._caller.quarantined

    @property
    def quarantined(self) -> bool:
        return self._caller.quarantined

    @property
    def reason(self) -> str:
        if self.backend is None:
            return "no_ocr_backend_configured"
        if not getattr(self.backend, "available", False):
            return getattr(self.backend, "reason", "ocr_backend_unavailable")
        if self._caller.quarantined:
            return self._caller.reason
        return self.last_error or "ready"

    def with_observation(self, observation: dict[str, Any]) -> "OcrAdapter":
        """Return an adapter bound to one observation's display geometry.

        The bounded caller is shared, so a quarantine survives the rebinding.
        """
        display = observation.get("display") or {}
        rotation = display.get("rotation")
        return OcrAdapter(self.backend, display=display,
                          rotation=rotation if isinstance(rotation, int) else 0,
                          min_confidence=self.min_confidence,
                          max_regions=self.max_regions,
                          timeout_seconds=self.timeout_seconds,
                          caller=self._caller)

    def recognize(self, image_bytes: bytes) -> list[dict[str, Any]]:
        """Return normalised `{text, bounds, confidence}` regions, never a point."""
        if not self.available:
            return []
        if self._caller.quarantined:
            self.last_error = self._caller.reason
            return []
        try:
            raw = self._invoke(image_bytes)
        except DeadlineExceeded:
            self.last_error = "ocr_timeout"
            return []
        except Exception as error:
            # Grounding reads `last_error` through `reason` and records it as a note.
            self.last_error = f"ocr_failed:{type(error).__name__}"
            return []
        regions = normalize_regions(raw, display=self.display, rotation=self.rotation,
                                    min_confidence=self.min_confidence)
        if len(regions) > self.max_regions:
            self.last_error = "ocr_region_budget_exceeded"
        return regions[:self.max_regions]

    def _invoke(self, image_bytes: bytes):
        if self.deadline_bounded:
            return self.backend.recognize(image_bytes)
        return self._caller.call(lambda: self.backend.recognize(image_bytes))


def build_ocr_engine(backend: OcrBackend | None = None, *,
                     display: dict[str, Any] | None = None,
                     rotation: int = 0) -> OcrAdapter | None:
    """Adapter for a configured backend; None when OCR is not configured."""
    if backend is None:
        return None
    return OcrAdapter(backend, display=display, rotation=rotation)
