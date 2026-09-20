"""Stable target identity across two observations (RC4-A).

An observation's catalog is rebuilt from scratch on every read. Its
``action_id`` / ``parent_action_id`` are therefore *observation-local handles*,
not identities, and a device may legitimately report volatile fields on an
otherwise unchanged control (the acceptance device's Weibo search entry carries
an auto-increment ``accessibilityId``: 31985 -> 31995 -> 32000). The subtree
hash moves whenever any descendant animates.

None of those may decide whether a grounded control is still the same control.
`StableTargetMatcher` re-locates the previously resolved entry inside a new
catalog using evidence that actually describes the control:

```text
hard        type, bundle, host window, resource id, enabled, clickable,
            semantic label (text/description/hint), geometry within tolerance
scored      resource id (4), hierarchy/tree path (3), label (2),
            accessibility id (2), host window (1), geometry (1),
            subtree hash (1, corroboration only)
ignored     action_id, parent_action_id
diagnostic  target_fingerprint - reported as content/change evidence, never as
            the sole action identity
```

Outcomes: ``exact`` (identical apart from the local handles), ``stable_rebind``
(one unique stable match that survived volatile drift), ``ambiguous`` (two or
more equally good candidates - never dispatched), ``missing`` (no candidate) and
``changed`` (a candidate sits in the same slot but a hard semantic property
changed).

This module never dispatches and never relaxes a guard: it only answers "is the
control we grounded on still here, and which node is it now".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Observation-local handles. They identify a node *within one catalog only*.
LOCAL_HANDLES = ("action_id", "parent_action_id")

#: Bounds may jitter by a few pixels without meaning a different control.
BOUNDS_TOLERANCE_PX = 3

#: A match needs at least the tree path (3) or an equivalent combination of
#: independent evidence. Below this the rebind is refused rather than guessed.
MIN_STABLE_SCORE = 3

EXACT = "exact"
STABLE_REBIND = "stable_rebind"
AMBIGUOUS = "ambiguous"
MISSING = "missing"
CHANGED = "changed"

OUTCOMES = (EXACT, STABLE_REBIND, AMBIGUOUS, MISSING, CHANGED)


@dataclass(frozen=True)
class MatchResult:
    outcome: str
    entry: dict[str, Any] | None = None
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def resolved(self) -> bool:
        return self.outcome in (EXACT, STABLE_REBIND) and self.entry is not None

    def as_dict(self) -> dict[str, Any]:
        return {"outcome": self.outcome, "reason": self.reason,
                "evidence": self.evidence}


def _bounds(entry: dict[str, Any]) -> list[int]:
    value = entry.get("bounds") or entry.get("hit_bounds") or [0, 0, 0, 0]
    return [int(v) for v in value]


def _geometry_close(previous: dict[str, Any], candidate: dict[str, Any],
                    tolerance: int = BOUNDS_TOLERANCE_PX) -> bool:
    first, second = _bounds(previous), _bounds(candidate)
    return all(abs(first[i] - second[i]) <= tolerance for i in range(4))


def _semantics(entry: dict[str, Any]) -> tuple[str, str, str]:
    return (entry.get("text") or "", entry.get("description") or "",
            entry.get("hint") or "")


def _label(entry: dict[str, Any]) -> str:
    return entry.get("text") or entry.get("description") or entry.get("hint") or ""


def hard_rejections(previous: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    """Properties that must hold for the two entries to be the same control."""
    reasons: list[str] = []
    if previous.get("type") != candidate.get("type"):
        reasons.append("type")
    if (previous.get("bundle") or "") != (candidate.get("bundle") or ""):
        reasons.append("bundle")
    if bool(previous.get("enabled")) != bool(candidate.get("enabled")):
        reasons.append("enabled")
    if bool(previous.get("clickable")) != bool(candidate.get("clickable")):
        reasons.append("clickable")
    previous_window = previous.get("host_window_id") or ""
    candidate_window = candidate.get("host_window_id") or ""
    if previous_window and candidate_window and previous_window != candidate_window:
        reasons.append("host_window_id")
    previous_resource = previous.get("resource_id") or ""
    if previous_resource and (candidate.get("resource_id") or "") != previous_resource:
        reasons.append("resource_id")
    if _semantics(previous) != _semantics(candidate):
        reasons.append("label")
    if not _geometry_close(previous, candidate):
        reasons.append("geometry")
    return reasons


def stable_score(previous: dict[str, Any],
                 candidate: dict[str, Any]) -> tuple[int, dict[str, int]]:
    """How many independent pieces of stable evidence agree."""
    score = 0
    detail: dict[str, int] = {}

    def award(name: str, points: int) -> None:
        nonlocal score
        score += points
        detail[name] = points

    previous_resource = previous.get("resource_id") or ""
    if previous_resource and previous_resource == (candidate.get("resource_id") or ""):
        award("resource_id", 4)
    previous_hierarchy = previous.get("hierarchy") or ""
    if previous_hierarchy and previous_hierarchy == (candidate.get("hierarchy") or ""):
        award("hierarchy", 3)
    if _label(previous) and _label(previous) == _label(candidate):
        award("label", 2)
    previous_accessibility = previous.get("accessibility_id") or ""
    if previous_accessibility and previous_accessibility == (
            candidate.get("accessibility_id") or ""):
        # Weibo's search entry reports an auto-increment id here, so this is
        # weighted as one voice among several rather than as an identity.
        award("accessibility_id", 2)
    previous_window = previous.get("host_window_id") or ""
    if previous_window and previous_window == (candidate.get("host_window_id") or ""):
        award("host_window_id", 1)
    if _geometry_close(previous, candidate):
        award("geometry", 1)
    previous_subtree = previous.get("target_fingerprint") or ""
    if previous_subtree and previous_subtree == (candidate.get("target_fingerprint") or ""):
        # Corroboration only: an animating descendant changes this while the
        # control is untouched, so it can never be the deciding evidence.
        award("subtree", 1)
    return score, detail


def _same_slot(previous: dict[str, Any], candidate: dict[str, Any]) -> bool:
    """Could this candidate be the *same place* as the old target?"""
    if previous.get("type") != candidate.get("type"):
        return False
    previous_resource = previous.get("resource_id") or ""
    if previous_resource and previous_resource == (candidate.get("resource_id") or ""):
        return True
    previous_hierarchy = previous.get("hierarchy") or ""
    if previous_hierarchy and previous_hierarchy == (candidate.get("hierarchy") or ""):
        return True
    return _geometry_close(previous, candidate)


def identical_apart_from_handles(previous: dict[str, Any],
                                 candidate: dict[str, Any]) -> bool:
    keys = (set(previous) | set(candidate)) - set(LOCAL_HANDLES)
    return all(previous.get(key) == candidate.get(key) for key in keys)


class StableTargetMatcher:
    """Deterministic cross-observation target re-identification."""

    def __init__(self, *, min_score: int = MIN_STABLE_SCORE,
                 tolerance: int = BOUNDS_TOLERANCE_PX):
        self.min_score = min_score
        self.tolerance = tolerance

    def match(self, previous: dict[str, Any] | None,
              catalog: list[dict[str, Any]]) -> MatchResult:
        if not previous:
            return MatchResult(MISSING, None, "no previous target to match")
        compatible: list[tuple[int, dict[str, int], dict[str, Any]]] = []
        changed: list[tuple[dict[str, Any], list[str]]] = []
        for entry in catalog:
            reasons = hard_rejections(previous, entry)
            if reasons:
                if _same_slot(previous, entry):
                    changed.append((entry, reasons))
                continue
            score, detail = stable_score(previous, entry)
            compatible.append((score, detail, entry))
        if not compatible:
            if changed:
                reasons = changed[0][1]
                return MatchResult(CHANGED, None,
                                   f"same slot but changed: {','.join(sorted(reasons))}",
                                   {"changed_fields": sorted(reasons),
                                    "candidates": len(changed)})
            return MatchResult(MISSING, None, "no candidate carries this identity",
                               {"candidates": 0})
        best = max(score for score, _, _ in compatible)
        top = [item for item in compatible if item[0] == best]
        if len(top) > 1:
            return MatchResult(AMBIGUOUS, None,
                               f"{len(top)} candidates tie at score {best}",
                               {"score": best, "candidates": len(top)})
        score, detail, entry = top[0]
        if score < self.min_score:
            return MatchResult(MISSING, None,
                               f"best evidence score {score} is below {self.min_score}",
                               {"score": score, "evidence": detail})
        if identical_apart_from_handles(previous, entry):
            return MatchResult(EXACT, entry, "identical apart from local handles",
                               {"score": score, "evidence": detail})
        return MatchResult(STABLE_REBIND, entry,
                           "unique stable match survived volatile drift",
                           {"score": score, "evidence": detail})


__all__ = ["AMBIGUOUS", "CHANGED", "EXACT", "LOCAL_HANDLES", "MISSING", "MatchResult",
           "OUTCOMES", "STABLE_REBIND", "StableTargetMatcher", "hard_rejections",
           "identical_apart_from_handles", "stable_score"]
