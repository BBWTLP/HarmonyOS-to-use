"""Provider protocol shared by rules, Decider and future optional models."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class ProviderUnavailable(RuntimeError):
    """The provider could not answer; callers fall back to the baseline."""

    def __init__(self, code: str, message: str, *, retry_after_ms: int | None = None):
        self.code = code
        self.retry_after_ms = retry_after_ms
        super().__init__(message)


@dataclass(frozen=True)
class ProviderCapabilities:
    provider: str
    supports_choice: bool
    supports_noul: bool
    max_candidates: int
    max_questions: int
    requires_calibration: bool
    emits_coordinates: bool = False
    accepts_images: bool = False


@dataclass
class ProviderResult:
    provider: str
    model_revision: str
    answer: dict[str, Any]
    native_scores: dict[str, Any]
    elapsed_ms: float
    raw_meta: dict[str, Any] = field(default_factory=dict)
    reason_code: str = "ok"


class DecisionProvider(Protocol):
    def capabilities(self) -> ProviderCapabilities: ...

    async def evaluate(self, context: dict[str, Any], questions: dict[str, Any],
                       *, remaining_model_calls: int | None = None) -> ProviderResult: ...
