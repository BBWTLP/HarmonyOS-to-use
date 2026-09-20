"""Runtime adapter for the pinned local Decider 2B service (choice + noul).

The adapter only sends pre-generated candidate ids and propositions. Model
output is normalised into native `choice` / `noul` shapes with the original
scores preserved; no coordinate, parameter or authorization can come back
through this interface. Busy (503) and deadline (504) results trip a circuit
breaker that falls back to the baseline controller.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from pathlib import Path
from typing import Any

from ..tokens import MAX_QUESTIONS, MAX_QUESTION_TOKENS, MAX_STATE_TOKENS, estimate_tokens
from .base import ProviderCapabilities, ProviderResult, ProviderUnavailable

DEFAULT_BASE_URL = "http://127.0.0.1:8765"
DEFAULT_REVISION = "7789eb65d5cf519737608e218fa88819bddea0af"
DEFAULT_TOKEN_FILE = Path("services/decider/.runtime/api-token")

MAX_BODY_BYTES = 65536

#: Re-exported for callers that already import the budgets from this module.
__all__ = ["CircuitBreaker", "DeciderError", "DeciderProvider", "DEFAULT_BASE_URL",
           "DEFAULT_REVISION", "DEFAULT_TOKEN_FILE", "MAX_BODY_BYTES", "MAX_QUESTIONS",
           "MAX_QUESTION_TOKENS", "MAX_STATE_TOKENS", "estimate_tokens", "fetch_health",
           "run_health"]


class DeciderError(ProviderUnavailable):
    pass


class CircuitBreaker:
    """Trip on repeated transport/busy failures; never retry inside an inference."""

    def __init__(self, threshold: int = 3, cooldown_seconds: float = 30.0):
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self.failures = 0
        self.opened_at: float | None = None
        self.last_code: str | None = None

    @property
    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if time.monotonic() - self.opened_at >= self.cooldown_seconds:
            self.opened_at = None
            self.failures = 0
            return False
        return True

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None
        self.last_code = None

    def record_failure(self, code: str) -> None:
        self.failures += 1
        self.last_code = code
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()


class DeciderProvider:
    """Async HTTP adapter with explicit revision, epoch and membership checks."""

    def __init__(self, *, base_url: str = DEFAULT_BASE_URL,
                 token_file: str | Path = DEFAULT_TOKEN_FILE,
                 revision: str = DEFAULT_REVISION,
                 timeout_seconds: float = 15.0,
                 breaker: CircuitBreaker | None = None,
                 transport=None):
        self.base_url = base_url.rstrip("/")
        self.token_file = Path(token_file)
        self.revision = revision
        self.timeout_seconds = timeout_seconds
        self.breaker = breaker or CircuitBreaker()
        self._transport = transport
        self._token: str | None = None

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider="decider",
            supports_choice=True,
            supports_noul=True,
            max_candidates=16,
            max_questions=MAX_QUESTIONS,
            requires_calibration=True,
        )

    def health_snapshot(self) -> dict[str, Any]:
        """Local, synchronous health: never performs HTTP and never calls a model."""
        return {"provider": "decider",
                "revision": self.revision,
                "health": "circuit_open" if self.breaker.is_open else "ok",
                "circuit_open": self.breaker.is_open,
                "consecutive_failures": self.breaker.failures,
                "last_failure_code": self.breaker.last_code}

    def token(self) -> str:
        if self._token is None:
            try:
                value = self.token_file.read_text(encoding="utf-8").strip()
            except OSError as error:
                raise DeciderError("token_unavailable", "Local Decider token file is not readable") from error
            if not value:
                raise DeciderError("token_unavailable", "Local Decider token file is empty")
            self._token = value
        return self._token

    def build_payload(self, state: str, questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(state, str) or not state.strip():
            raise DeciderError("invalid_request", "State must be a non-empty string")
        if not 1 <= len(questions) <= MAX_QUESTIONS:
            raise DeciderError("invalid_request", f"Send 1..{MAX_QUESTIONS} independent questions")
        if estimate_tokens(state) > MAX_STATE_TOKENS:
            raise DeciderError("state_too_long", "State exceeds the 1024-token budget; compact evidence")
        for key, question in questions.items():
            if question.get("type") == "choice":
                criteria = question.get("criteria") or {}
                if not 2 <= len(criteria) <= 16:
                    raise DeciderError("invalid_request", "A choice question needs 2..16 candidates")
            elif question.get("type") == "noul":
                pass
            else:
                raise DeciderError("invalid_request", "Only choice and noul questions are supported")
            rendered = f"{question.get('instructions', '')} " + " ".join(
                str(value) for value in (question.get("criteria") or {}).values())
            if estimate_tokens(rendered) > MAX_QUESTION_TOKENS:
                raise DeciderError("invalid_request", f"Rendered question {key} exceeds 1536 tokens")
        payload = {"state": state, "questions": questions, "independent": True, "layout": "state_first"}
        if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_BODY_BYTES:
            raise DeciderError("request_too_large", "Request body exceeds 64 KiB")
        return payload

    async def evaluate(self, context: dict[str, Any], questions: dict[str, Any],
                       *, remaining_model_calls: int | None = None) -> ProviderResult:
        if self.breaker.is_open:
            raise DeciderError("circuit_open", "Decider circuit breaker is open; baseline controller in use")
        if remaining_model_calls is not None and remaining_model_calls <= 0:
            raise DeciderError("budget_exhausted", "No model calls remain in the task budget")

        state = context["state"]
        payload = self.build_payload(state, questions)
        allowed = _allowed_candidates(questions)
        epoch = context.get("controller_epoch")
        started = time.perf_counter()
        status, body, headers = await self._post(payload)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        if status == 503:
            self.breaker.record_failure("model_busy")
            raise DeciderError("model_busy", "Decider is busy; use the baseline controller")
        if status == 504:
            self.breaker.record_failure("model_timeout")
            raise DeciderError("model_timeout", "Decider inference deadline exceeded")
        if status != 200:
            self.breaker.record_failure(f"http_{status}")
            raise DeciderError(f"http_{status}", "Decider returned an error; baseline controller in use")

        deployment = body.get("deployment") if isinstance(body, dict) else None
        if not isinstance(deployment, dict) or deployment.get("model_revision") != self.revision:
            self.breaker.record_failure("revision_mismatch")
            raise DeciderError("revision_mismatch", "Decider revision does not match the pinned revision")

        answers = body.get("answers")
        if not isinstance(answers, dict) or not answers:
            self.breaker.record_failure("malformed_response")
            raise DeciderError("malformed_response", "Decider response has no answers")

        normalised = _normalise(answers, questions, allowed)
        self.breaker.record_success()
        return ProviderResult(
            provider="decider",
            model_revision=str(deployment.get("model_revision")),
            answer=normalised,
            native_scores={
                "score_semantics": "decider_systemone",
                "answers": normalised,
            },
            elapsed_ms=elapsed_ms,
            raw_meta={
                "mode": deployment.get("mode"),
                "precision": deployment.get("precision"),
                "backend": deployment.get("backend"),
                "project_calibration": deployment.get("project_calibration"),
                "inference_ms": deployment.get("inference_ms"),
                "controller_epoch": epoch,
                "http_headers": {k.lower(): v for k, v in (headers or {}).items()
                                 if k.lower() in ("content-length", "date")},
                "model_calls": 1,
            },
        )

    async def _post(self, payload: dict[str, Any]):
        token = self.token()
        if self._transport is not None:
            try:
                result = self._transport(payload, token, self.timeout_seconds)
                if hasattr(result, "__await__"):
                    result = await result
            except Exception as error:
                # An injected transport fails like the HTTP path does, so the
                # failure taxonomy stays the same whichever seam is used.
                self.breaker.record_failure("transport_error")
                raise DeciderError("transport_error",
                                   "Decider transport is unreachable") from error
            return result
        try:
            import httpx2 as httpx
        except ModuleNotFoundError:  # pragma: no cover - fallback for other envs
            import httpx
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            try:
                response = await client.post(f"{self.base_url}/v1/systemone", json=payload, headers=headers)
            except Exception as error:
                self.breaker.record_failure("transport_error")
                raise DeciderError("transport_error", "Decider service is unreachable") from error
        try:
            body = response.json()
        except Exception:
            body = {}
        return response.status_code, body, dict(response.headers)

    async def health(self) -> dict[str, Any]:
        try:
            import httpx2 as httpx
        except ModuleNotFoundError:  # pragma: no cover
            import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{self.base_url}/health")
        return response.json()


def _allowed_candidates(questions: dict[str, dict[str, Any]]) -> dict[str, set[str]]:
    allowed: dict[str, set[str]] = {}
    for key, question in questions.items():
        if question.get("type") == "choice":
            allowed[key] = set(question.get("criteria") or {})
    return allowed


def _normalise(answers: dict[str, Any], questions: dict[str, dict[str, Any]],
               allowed: dict[str, set[str]]) -> dict[str, Any]:
    """Reject free text, unknown candidates, coordinates and non-finite scores."""
    normalised: dict[str, Any] = {}
    for key, question in questions.items():
        answer = answers.get(key)
        if not isinstance(answer, dict):
            raise DeciderError("malformed_response", f"Question {key} has no structured answer")
        kind = question["type"]
        if kind == "choice":
            probabilities = answer.get("probabilities")
            choice = answer.get("choice")
            if not isinstance(probabilities, dict) or not isinstance(choice, str):
                raise DeciderError("malformed_response", f"Question {key} is not a choice answer")
            if set(probabilities) - allowed[key] or choice not in allowed[key]:
                raise DeciderError("candidate_not_registered",
                                   f"Question {key} answered outside the registered candidate set")
            values = []
            for name, value in probabilities.items():
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise DeciderError("non_finite_score", f"Question {key} returned a non-numeric score")
                number = float(value)
                if math.isnan(number) or math.isinf(number) or not 0.0 <= number <= 1.0:
                    raise DeciderError("non_finite_score", f"Question {key} returned an invalid score")
                values.append(number)
            total = sum(values)
            if abs(total - 1.0) > 0.05:
                raise DeciderError("score_not_normalised", f"Question {key} probabilities do not sum to 1")
            normalised[key] = {
                "type": "choice",
                "choice": choice,
                "confidence": _unit(answer.get("confidence", max(probabilities.values()))),
                "certainty": _unit(answer.get("certainty", 0.0)),
                "probabilities": {name: round(float(value), 4) for name, value in probabilities.items()},
            }
        else:
            score = answer.get("noul", answer.get("yes"))
            if score is None:
                raise DeciderError("malformed_response", f"Question {key} has no noul score")
            normalised[key] = {"type": "noul", "noul": _unit(score)}
    return normalised


def _unit(value: Any) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise DeciderError("non_finite_score", "Score is not a number")
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        raise DeciderError("non_finite_score", "Score is not finite")
    return max(0.0, min(1.0, round(number, 4)))


async def fetch_health(base_url: str = DEFAULT_BASE_URL) -> dict[str, Any]:
    """GET /health without a token; used by deployment and shadow checks."""
    try:
        import httpx2 as httpx
    except ModuleNotFoundError:  # pragma: no cover
        import httpx
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(f"{base_url.rstrip('/')}/health")
        return response.json()


def run_health(base_url: str = DEFAULT_BASE_URL) -> dict[str, Any]:
    return asyncio.run(fetch_health(base_url))
