"""Experience retrieval by App/build/goal (T11).

Online runs only read frozen snapshots. Retrieval is conservative: no match is
a valid answer, and a hit must still be re-grounded on the live observation.
"""
from __future__ import annotations

import time
from typing import Any, Iterable

from .experience import Experience


def experience_matches(entry: Experience | dict[str, Any], *,
                       app: str | None = None,
                       build: str | None = None,
                       goal_pattern: str | None = None,
                       now: float | None = None) -> bool:
    scope = entry.scope if isinstance(entry, Experience) else (entry.get("scope") or {})
    if app and str(scope.get("app") or "") != app:
        return False
    if build and str(scope.get("build") or "") not in ("", str(build)):
        return False
    now = time.time() if now is None else now
    expires = getattr(entry, "expires_at", None)
    if expires is None and isinstance(entry, dict):
        expires = entry.get("expires_at")
    if expires is not None and now > float(expires):
        return False
    if goal_pattern:
        if isinstance(entry, dict):
            trigger = entry.get("trigger") or {}
            goal = str(trigger.get("goal_pattern") or "")
        else:
            trigger = getattr(entry, "trigger", None)
            goal = str(getattr(trigger, "goal_pattern", "") or "")
        if goal_pattern.lower() not in goal.lower():
            return False
    outcome = getattr(entry, "outcome", None)
    if outcome is None and isinstance(entry, dict):
        outcome = entry.get("outcome")
    return outcome == "verified"


def retrieve(entries: Iterable[Experience | dict[str, Any]], *,
             app: str | None = None,
             build: str | None = None,
             goal_pattern: str | None = None,
             limit: int = 8,
             now: float | None = None) -> list[dict[str, Any]]:
    """Return at most `limit` matching verified experiences (metadata only)."""
    hits = []
    for entry in entries:
        if not experience_matches(entry, app=app, build=build,
                                  goal_pattern=goal_pattern, now=now):
            continue
        if isinstance(entry, Experience):
            hits.append(entry.model_dump())
        else:
            hits.append(dict(entry))
        if len(hits) >= limit:
            break
    return hits
