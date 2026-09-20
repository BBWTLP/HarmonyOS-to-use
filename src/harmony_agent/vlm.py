"""Optional VLM visual grounding provider.

A VLM is a *grounding* provider, never a controller. It receives the goal, the
screenshot, the display geometry, a tree summary and the existing candidates,
and it may only answer with regions. Regions are validated here; anything that
looks like an action, a raw coordinate pair or a device call is discarded, and a
timeout or a malformed answer becomes an explicit "no VLM evidence" result.

Deadline ownership matches the OCR adapter: a production backend declares
``deadline_bounded = True`` and enforces the deadline in its own transport;
otherwise the call is bounded here and the provider is quarantined on the first
timeout, so a hung model costs at most one worker thread and fails fast after
that.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .grounding import GroundingUnavailable
from .provider_deadline import BoundedCaller, DeadlineExceeded
from .visual_regions import normalize_regions

#: A VLM answer below this score is not offered as a candidate.
DEFAULT_MIN_SCORE = 0.35


class VlmBackend(Protocol):
    """A concrete VLM (local server, hosted API, on-device model)."""

    available: bool
    #: Set True only when the transport itself enforces the deadline.
    deadline_bounded: bool

    def locate(self, request: dict[str, Any], image_bytes: bytes) -> list[dict[str, Any]]: ...


@dataclass
class VlmContext:
    goal: str = ""
    subgoal: str = ""
    tree_summary: list[str] = field(default_factory=list)
    existing_candidates: list[dict[str, Any]] = field(default_factory=list)


class VlmGroundingProvider:
    """Adapts a VLM backend to the grounding `ImageMatcher` seam."""

    def __init__(self, backend: VlmBackend | None = None, *,
                 display: dict[str, Any] | None = None, rotation: int = 0,
                 min_score: float = DEFAULT_MIN_SCORE, max_regions: int = 8,
                 timeout_seconds: float = 15.0,
                 context: VlmContext | None = None,
                 caller: BoundedCaller | None = None):
        self.backend = backend
        self.display = display
        self.rotation = rotation
        self.min_score = min_score
        self.max_regions = max_regions
        self.timeout_seconds = timeout_seconds
        self.context = context or VlmContext()
        self._caller = caller or BoundedCaller("vlm", timeout_seconds)
        self.last_error: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.backend is not None and getattr(self.backend, "available", False))

    @property
    def deadline_bounded(self) -> bool:
        return bool(getattr(self.backend, "deadline_bounded", False))

    @property
    def production_ready(self) -> bool:
        return self.available and self.deadline_bounded and not self._caller.quarantined

    @property
    def quarantined(self) -> bool:
        return self._caller.quarantined

    @property
    def reason(self) -> str:
        if self.backend is None:
            return "no_vlm_backend_configured"
        if not getattr(self.backend, "available", False):
            return getattr(self.backend, "reason", "vlm_backend_unavailable")
        if self._caller.quarantined:
            return self._caller.reason
        return self.last_error or "ready"

    def with_context(self, *, goal: str = "", subgoal: str = "",
                     tree_summary: list[str] | None = None,
                     existing_candidates: list[dict[str, Any]] | None = None) -> "VlmGroundingProvider":
        """Attach task context; the region contract itself never changes."""
        return VlmGroundingProvider(
            self.backend, display=self.display, rotation=self.rotation,
            min_score=self.min_score, max_regions=self.max_regions,
            timeout_seconds=self.timeout_seconds,
            context=VlmContext(goal=goal, subgoal=subgoal,
                               tree_summary=list(tree_summary or []),
                               existing_candidates=list(existing_candidates or [])),
            caller=self._caller)

    def with_observation(self, observation: dict[str, Any]) -> "VlmGroundingProvider":
        display = observation.get("display") or {}
        rotation = display.get("rotation")
        provider = VlmGroundingProvider(
            self.backend, display=display,
            rotation=rotation if isinstance(rotation, int) else 0,
            min_score=self.min_score, max_regions=self.max_regions,
            timeout_seconds=self.timeout_seconds, context=self.context,
            caller=self._caller)
        return provider

    def build_request(self, description: str) -> dict[str, Any]:
        """The documented VLM input. The image travels as bytes, beside this."""
        return {"goal": self.context.goal,
                "subgoal": self.context.subgoal,
                "description": description,
                "display": {"width": (self.display or {}).get("width"),
                            "height": (self.display or {}).get("height"),
                            "rotation": self.rotation},
                "tree_summary": list(self.context.tree_summary),
                "existing_candidates": list(self.context.existing_candidates),
                "answer_contract": "regions_only",
                "timeout_seconds": self.timeout_seconds}

    def locate(self, image_bytes: bytes, description: str) -> list[dict[str, Any]]:
        """Return normalised regions; never a coordinate action."""
        if not self.available:
            return []
        if self._caller.quarantined:
            self.last_error = self._caller.reason
            raise GroundingUnavailable("vlm_quarantined",
                                       "The VLM provider is quarantined after a timeout")
        request = self.build_request(description)
        try:
            raw = self._invoke(request, image_bytes)
        except DeadlineExceeded as error:
            raise GroundingUnavailable(
                "vlm_timeout",
                "The VLM did not answer within its deadline") from error
        except TimeoutError as error:
            raise GroundingUnavailable("vlm_timeout",
                                       "The VLM did not answer within its deadline") from error
        except GroundingUnavailable:
            raise
        except Exception as error:
            self.last_error = f"vlm_failed:{type(error).__name__}"
            raise GroundingUnavailable("vlm_failed",
                                       "The VLM backend failed; no visual evidence") from error
        if raw is None:
            self.last_error = "vlm_empty_answer"
            return []
        if not isinstance(raw, (list, tuple)):
            self.last_error = "vlm_invalid_response"
            raise GroundingUnavailable("vlm_invalid_response",
                                       "The VLM answer was not a list of regions")
        if not raw:
            return []
        regions = normalize_regions(raw, display=self.display, rotation=self.rotation,
                                    min_confidence=self.min_score)
        if not regions:
            self.last_error = "vlm_no_usable_region"
            raise GroundingUnavailable(
                "vlm_low_confidence",
                "The VLM answer had no usable region above the score floor")
        return regions[:self.max_regions]

    def _invoke(self, request: dict[str, Any], image_bytes: bytes):
        if self.deadline_bounded:
            return self.backend.locate(request, image_bytes)
        return self._caller.call(lambda: self.backend.locate(request, image_bytes))


def build_vlm_provider(backend: VlmBackend | None = None, *,
                       display: dict[str, Any] | None = None,
                       rotation: int = 0) -> VlmGroundingProvider | None:
    """Provider for a configured backend; None when no VLM is configured."""
    if backend is None:
        return None
    return VlmGroundingProvider(backend, display=display, rotation=rotation)
