"""VerifierProvider protocol (P2-05).

A verifier answers one question about the *current* observation: did the declared
postconditions hold? It holds no write capability, no dispatcher and no private
actor reasoning. The verdict is three-valued — ``pass`` / ``fail`` /
``inconclusive`` — and ``inconclusive`` is never counted as success.

`CodeVerifier` is the deterministic adapter over :class:`ReadOnlyChecker`, so the
existing code-checked predicates keep deciding themselves. A semantic verifier
(model- or human-backed) is admitted only through :func:`assert_read_only`, which
refuses a provider that declares any write capability or tries to bind one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .checker import FAIL, INCONCLUSIVE, PASS, CheckerDenied, CheckReport, ReadOnlyChecker
from .contracts import Predicate

VERDICTS = (PASS, FAIL, INCONCLUSIVE)


class VerifierDenied(RuntimeError):
    """Raised when a verifier is asked to take a capability it must not have."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class VerifierCapabilities:
    """Declared capabilities. Anything write-shaped must stay empty."""

    write_tools: tuple[str, ...] = ()
    may_dispatch: bool = False
    sees_actor_reasoning: bool = False
    independent: bool = True

    def problems(self) -> list[str]:
        found = []
        if self.write_tools:
            found.append("write_tools_declared")
        if self.may_dispatch:
            found.append("dispatch_capability_declared")
        if self.sees_actor_reasoning:
            found.append("actor_reasoning_visible")
        if not self.independent:
            found.append("not_independent")
        return found


@dataclass(frozen=True)
class VerificationRequest:
    predicates: list[Predicate]
    observation: dict[str, Any]
    arguments: dict[str, str] = field(default_factory=dict)
    incident_free: bool = True
    task_id: str = ""
    observation_id: str = ""


@dataclass
class VerificationOutcome:
    verdict: str
    report: CheckReport
    verifier: str = "code"
    revision: str = "checker:1"
    evidence_refs: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "verifier": self.verifier,
                "revision": self.revision,
                "evidence_refs": list(self.evidence_refs) or list(self.report.evidence_refs),
                "limitations": list(self.limitations) or list(self.report.limitations),
                "report": self.report.to_dict()}


@runtime_checkable
class VerifierProvider(Protocol):
    """Structural protocol for code, model or human verifiers."""

    capabilities: VerifierCapabilities

    def verify(self, request: VerificationRequest) -> VerificationOutcome:  # pragma: no cover
        ...


class CodeVerifier:
    """Deterministic verifier: every programmatic predicate is decided by code."""

    name = "code"
    revision = "checker:1"
    capabilities = VerifierCapabilities()

    def __init__(self, checker: ReadOnlyChecker | None = None):
        self.checker = checker or ReadOnlyChecker()

    def verify(self, request: VerificationRequest) -> VerificationOutcome:
        report = self.checker.check(request.predicates, request.observation,
                                    arguments=request.arguments,
                                    incident_free=request.incident_free)
        return VerificationOutcome(verdict=report.verdict, report=report,
                                   verifier=self.name, revision=self.revision,
                                   evidence_refs=list(report.evidence_refs),
                                   limitations=list(report.limitations))


def assert_read_only(provider: VerifierProvider) -> VerifierProvider:
    """Fail closed unless the provider declares itself capability-free.

    A verifier that declares a write tool, a dispatcher or access to the actor's
    private reasoning cannot produce an independent verdict, so the caller must
    not treat its answer as evidence.
    """
    if not callable(getattr(provider, "verify", None)):
        raise VerifierDenied("missing_verify", "verifier does not implement verify()")
    capabilities = getattr(provider, "capabilities", None)
    if not isinstance(capabilities, VerifierCapabilities):
        raise VerifierDenied("capabilities_undeclared",
                             "verifier must declare VerifierCapabilities")
    problems = capabilities.problems()
    if problems:
        raise VerifierDenied(problems[0], "verifier is not read-only and independent")
    return provider


class ReadOnlyVerifierMount:
    """Guard object handed to a verifier: it can read, and refuses to write.

    A semantic verifier receives this instead of a runtime facade. Every write
    method raises `CheckerDenied`, so "the verifier fixed it itself" is impossible
    by construction rather than by convention.
    """

    def __init__(self, observation: dict[str, Any]):
        self.observation = observation

    def read_observation(self) -> dict[str, Any]:
        return self.observation

    def act(self, *args, **kwargs):  # pragma: no cover - always raises
        raise CheckerDenied("A verifier has no write tool")

    def dispatch(self, *args, **kwargs):  # pragma: no cover - always raises
        raise CheckerDenied("A verifier has no dispatcher")

    def wait(self, *args, **kwargs):  # pragma: no cover - always raises
        raise CheckerDenied("A verifier may not steer the device")


def verify_once(provider: VerifierProvider,
                request: VerificationRequest) -> VerificationOutcome:
    """Run a declared read-only verifier, normalising the verdict."""
    assert_read_only(provider)
    outcome = provider.verify(request)
    if outcome.verdict not in VERDICTS:
        raise VerifierDenied("unknown_verdict",
                             f"verifier returned {outcome.verdict!r}")
    outcome.evidence_refs = list(outcome.evidence_refs or outcome.report.evidence_refs)
    if outcome.verdict == PASS and not outcome.evidence_refs:
        # A pass with no evidence is not evidence: downgrade rather than trust it.
        outcome.verdict = INCONCLUSIVE
        outcome.limitations = [*outcome.limitations, "pass_without_evidence"]
    return outcome
