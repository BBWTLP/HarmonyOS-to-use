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


def provider_health(provider: Any) -> dict[str, Any]:
    """Best-effort, provider-agnostic health snapshot.

    A provider may implement ``health_snapshot()`` for an exact local report.
    Otherwise the snapshot is derived from the protocol surface only, so the
    caller never needs to know a concrete adapter's private attributes. This
    function is synchronous and never performs I/O or calls a model.
    """
    if provider is None:
        return {"provider": "none", "revision": None, "health": "absent",
                "circuit_open": False}
    snapshot = getattr(provider, "health_snapshot", None)
    if callable(snapshot):
        try:
            report = dict(snapshot())
        except Exception as error:  # a broken provider must not break diagnostics
            return {"provider": _provider_name(provider), "revision": None,
                    "health": "unavailable", "circuit_open": False,
                    "error": type(error).__name__}
        report.setdefault("provider", _provider_name(provider))
        report.setdefault("revision", getattr(provider, "model_revision", None))
        report.setdefault("circuit_open", False)
        return report
    return {"provider": _provider_name(provider),
            "revision": getattr(provider, "model_revision", None),
            "health": "ok",
            "circuit_open": False}


def _provider_name(provider: Any) -> str:
    capabilities = getattr(provider, "capabilities", None)
    if callable(capabilities):
        try:
            return str(capabilities().provider)
        except Exception:
            pass
    return str(getattr(provider, "provider", type(provider).__name__))
