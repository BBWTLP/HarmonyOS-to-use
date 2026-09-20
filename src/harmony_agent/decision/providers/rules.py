"""Deterministic rules baseline.

Rules own value equality, selected state, package identity, counts, budget and
state transitions. They never call a model and never invent a candidate.
"""
from __future__ import annotations

import time

from .base import ProviderCapabilities, ProviderResult


class RulesProvider:
    def __init__(self, rules_id: str = "rules-baseline-1"):
        self.rules_id = rules_id

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider="rules",
            supports_choice=True,
            supports_noul=True,
            max_candidates=32,
            max_questions=4,
            requires_calibration=False,
        )

    def native(self, rule_id: str) -> dict:
        return {"score_semantics": "deterministic_rule", "rule_id": rule_id}

    async def evaluate(self, context, questions, *, remaining_model_calls=None) -> ProviderResult:
        started = time.perf_counter()
        candidates = context.get("candidates") or []
        answers: dict = {}
        rule_id = "no_rule_matched"
        if not candidates:
            rule_id = "no_candidate_reobserve"
        elif len(candidates) == 1:
            rule_id = "single_candidate"
            answers["action"] = {
                "type": "choice",
                "choice": candidates[0]["candidate_id"],
                "confidence": 1.0,
                "certainty": 1.0,
                "probabilities": {candidates[0]["candidate_id"]: 1.0},
            }
        else:
            unique = _unique_identity_match(candidates)
            if unique is not None:
                rule_id = "unique_stable_identity"
                answers["action"] = {
                    "type": "choice",
                    "choice": unique,
                    "confidence": 1.0,
                    "certainty": 1.0,
                    "probabilities": {unique: 1.0},
                }
        if "action" not in answers:
            probabilities = {item["candidate_id"]: 0.0 for item in candidates}
            answers["action"] = {
                "type": "choice",
                "choice": "cand_none_applicable",
                "confidence": 1.0,
                "certainty": 1.0,
                "probabilities": {**probabilities, "cand_none_applicable": 1.0},
            }
        return ProviderResult(
            provider="rules",
            model_revision=f"rules:{self.rules_id}",
            answer=answers,
            native_scores=self.native(rule_id),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
            raw_meta={"model_calls": 0},
        )


def _unique_identity_match(candidates: list[dict]) -> str | None:
    """One candidate carries a stable identity that no sibling carries."""
    keyed = [c for c in candidates if c.get("identity")]
    signatures = {}
    for item in candidates:
        identity = item.get("identity") or {}
        for key in ("accessibility_id", "resource_id", "hierarchy"):
            value = identity.get(key)
            if value:
                signatures.setdefault((key, value), []).append(item["candidate_id"])
    winners = [ids[0] for ids in signatures.values() if len(ids) == 1 and len(ids) == len(
        [c for c in keyed if c["candidate_id"] == ids[0]])]
    if len(winners) == 1 and len(candidates) > 1:
        return winners[0]
    return None
