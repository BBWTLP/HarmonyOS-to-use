"""Read-only final checking.

Programme-checkable predicates are decided by code. A `page_assertion` may use a
model, but the verdict stays pass / fail / inconclusive and inconclusive is
never counted as success. The checker never receives a write tool.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .contracts import PROGRAMMATIC_PREDICATES, Predicate  # noqa: F401  (parity import)

PASS = "pass"
FAIL = "fail"
INCONCLUSIVE = "inconclusive"


class CheckerDenied(RuntimeError):
    """Raised when a caller tries to give the checker a write capability."""

    def __init__(self, message: str = "Checker is read-only"):
        super().__init__(message)


@dataclass
class ConditionVerdict:
    id: str
    verdict: str
    evidence_refs: list[str] = field(default_factory=list)
    detail: str = ""
    decided_by: str = "code"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "verdict": self.verdict,
                "evidence_refs": list(self.evidence_refs), "detail": self.detail,
                "decided_by": self.decided_by}


@dataclass
class CheckReport:
    verdict: str
    conditions: list[ConditionVerdict]
    unobserved: list[str] = field(default_factory=list)
    user_intervention_required: bool = False
    evidence_refs: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict,
                "conditions": [item.to_dict() for item in self.conditions],
                "unobserved": list(self.unobserved),
                "user_intervention_required": self.user_intervention_required,
                "evidence_refs": list(self.evidence_refs),
                "limitations": list(self.limitations)}


def resolve_value(predicate: Predicate, arguments: dict[str, str]) -> Any:
    if predicate.value_ref:
        key = predicate.value_ref
        if key.startswith("arg."):
            key = key[4:]
        if key not in arguments:
            raise KeyError(key)
        return arguments[key]
    return predicate.value


def normalize_target_key(target_key: str) -> str:
    """`id:`, `a11y:` and `type:` qualify the key; plain text is an exact text match."""
    for prefix in ("id:", "a11y:", "type:"):
        if target_key.startswith(prefix):
            return target_key[len(prefix):]
    return target_key


def _match_nodes(observation: dict[str, Any], target_key: str) -> list[dict[str, Any]]:
    catalog = observation.get("catalog") or []
    if target_key.startswith("a11y:"):
        key = normalize_target_key(target_key)
        return [item for item in catalog if item.get("accessibility_id") == key]
    if target_key.startswith("type:"):
        key = normalize_target_key(target_key)
        return [item for item in catalog if item.get("type") == key]
    if target_key.startswith("id:"):
        key = normalize_target_key(target_key)
        return [item for item in catalog if item.get("resource_id") == key]
    key = normalize_target_key(target_key)
    return [item for item in catalog
            if key in (item.get("resource_id"), item.get("accessibility_id"),
                       item.get("text"), item.get("action_id"))]


def evaluate(predicate: Predicate, observation: dict[str, Any], *,
             arguments: dict[str, str] | None = None,
             baseline: dict[str, Any] | None = None,
             assertion_evaluator: Callable[[str, dict[str, Any]], tuple[str, str]] | None = None,
             surface_classifier: Callable[[dict[str, Any]], str] | None = None,
             evidence_refs: Iterable[str] = ()) -> ConditionVerdict:
    arguments = arguments or {}
    obs_ref = f"obs:{observation.get('observation_id')}"
    refs = [obs_ref, *evidence_refs]

    if predicate.type == "all_of":
        verdicts = [evaluate(child, observation, arguments=arguments, baseline=baseline,
                             assertion_evaluator=assertion_evaluator,
                             surface_classifier=surface_classifier, evidence_refs=evidence_refs)
                    for child in predicate.predicates or []]
        if any(item.verdict == FAIL for item in verdicts):
            verdict = FAIL
        elif any(item.verdict == INCONCLUSIVE for item in verdicts):
            verdict = INCONCLUSIVE
        else:
            verdict = PASS
        return ConditionVerdict(predicate.id, verdict,
                                [ref for item in verdicts for ref in item.evidence_refs],
                                detail=f"{len(verdicts)} nested conditions")

    if predicate.type == "foreground_is":
        value = resolve_value(predicate, arguments)
        actual = observation.get("foreground_bundle")
        if actual is None:
            return ConditionVerdict(predicate.id, INCONCLUSIVE, refs,
                                    "foreground identity is not observable")
        return ConditionVerdict(predicate.id, PASS if actual == value else FAIL, refs,
                                f"foreground={actual}")

    if predicate.type == "text_equals":
        value = resolve_value(predicate, arguments)
        present = any(item.get("text") == value for item in observation.get("catalog", []))
        return ConditionVerdict(predicate.id, PASS if present else FAIL, refs,
                                "exact text match" if present else "text not present")

    if predicate.type in ("element_present", "element_absent"):
        nodes = _match_nodes(observation, predicate.target_key or "")
        present = bool(nodes)
        verdict = PASS if (present if predicate.type == "element_present" else not present) else FAIL
        return ConditionVerdict(predicate.id, verdict, refs,
                                "absence describes the observable catalog only"
                                if predicate.type == "element_absent" else f"{len(nodes)} node(s)")

    if predicate.type in ("input_equals", "selected_is"):
        nodes = _match_nodes(observation, predicate.target_key or "")
        if not nodes:
            return ConditionVerdict(predicate.id, INCONCLUSIVE, refs, "target not observed")
        value = resolve_value(predicate, arguments)
        if predicate.type == "input_equals":
            field = [n for n in nodes if n.get("type", "").lower().find("input") >= 0
                     or "editor" in str(n.get("type") or "").lower()
                     or n.get("text_observed")]
            if not field:
                return ConditionVerdict(predicate.id, INCONCLUSIVE, refs, "no input field observed")
            ok = all(n.get("text") == value for n in field)
        else:
            # Device reports selected as "true"/"false" strings.
            def as_flag(item):
                raw = item.get("selected")
                if isinstance(raw, bool):
                    return "true" if raw else "false"
                return str(raw).lower()
            ok = all(as_flag(n) == str(value).lower() for n in nodes)
        return ConditionVerdict(predicate.id, PASS if ok else FAIL, refs,
                                f"{len(nodes)} node(s) compared")

    if predicate.type == "page_changed":
        if baseline is None:
            return ConditionVerdict(predicate.id, INCONCLUSIVE, refs, "no baseline observation")
        changed = observation.get("fingerprint") != baseline.get("fingerprint")
        return ConditionVerdict(predicate.id, PASS if changed else FAIL, refs,
                                "fingerprint comparison")

    if predicate.type == "surface_is":
        # Page identity: which named surface is actually up. Stronger than
        # page_changed, which only proves the fingerprint moved.
        value = resolve_value(predicate, arguments)
        if surface_classifier is None:
            return ConditionVerdict(predicate.id, INCONCLUSIVE, refs,
                                    "no surface classifier configured")
        actual = surface_classifier(observation)
        return ConditionVerdict(predicate.id, PASS if actual == value else FAIL, refs,
                                f"surface={actual}")

    if predicate.type == "page_assertion":
        if assertion_evaluator is None:
            return ConditionVerdict(predicate.id, INCONCLUSIVE, refs,
                                    "no semantic evaluator configured", "unavailable")
        verdict, detail = assertion_evaluator(predicate.description or "", observation)
        return ConditionVerdict(predicate.id, verdict, refs, detail, "model_assisted")

    return ConditionVerdict(predicate.id, INCONCLUSIVE, refs, "unsupported predicate type")


def check(criteria: list[Predicate], observation: dict[str, Any], *,
          arguments: dict[str, str] | None = None,
          baseline: dict[str, Any] | None = None,
          assertion_evaluator: Callable[[str, dict[str, Any]], tuple[str, str]] | None = None,
          surface_classifier: Callable[[dict[str, Any]], str] | None = None,
          incident_free: bool = True) -> CheckReport:
    verdicts = [evaluate(item, observation, arguments=arguments, baseline=baseline,
                         assertion_evaluator=assertion_evaluator,
                         surface_classifier=surface_classifier) for item in criteria]
    unobserved = [item.id for item in verdicts if item.verdict == INCONCLUSIVE]
    limitations = []
    if unobserved:
        limitations.append("some conditions could not be observed; they are not counted as success")
    if not incident_free:
        limitations.append("an unresolved device write blocks a successful verdict")
    if any(item.verdict == FAIL for item in verdicts):
        overall = FAIL
    elif unobserved or not incident_free:
        overall = INCONCLUSIVE
    else:
        overall = PASS
    return CheckReport(verdict=overall, conditions=verdicts, unobserved=unobserved,
                       evidence_refs=[ref for item in verdicts for ref in item.evidence_refs],
                       limitations=limitations)


class ReadOnlyChecker:
    """Adapter that refuses any write capability handed to it."""

    def __init__(self, *, assertion_evaluator: Callable[[str, dict[str, Any]], tuple[str, str]] | None = None,
                 surface_classifier: Callable[[dict[str, Any]], str] | None = None):
        self.assertion_evaluator = assertion_evaluator
        self.surface_classifier = surface_classifier

    def check(self, criteria: list[Predicate], observation: dict[str, Any], **kwargs) -> CheckReport:
        return check(criteria, observation, assertion_evaluator=self.assertion_evaluator,
                     surface_classifier=self.surface_classifier, **kwargs)

    def dispatch(self, *args, **kwargs):  # pragma: no cover - guard rail
        raise CheckerDenied("Checker must not call device write tools")
