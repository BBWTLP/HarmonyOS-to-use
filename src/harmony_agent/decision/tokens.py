"""Token budgeting constants and helpers shared with the Decider wire contract.

These are pure functions with no dependency on any provider. The state builder,
memory compression and the offline evaluation tooling all need the same budget,
so they import it here instead of importing the Decider adapter. That keeps the
agent layer importable when the Decider package, service or model is absent.
"""
from __future__ import annotations

#: Budgets of the pinned Decider 2B deployment; the adapter enforces them.
MAX_STATE_TOKENS = 1024
MAX_QUESTION_TOKENS = 1536
MAX_QUESTIONS = 4


def estimate_tokens(text: str) -> int:
    """Conservative token estimate: CJK counts ~1 token, other text ~1 per 3 chars."""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u3000" <= ch <= "\u9fff" or "\uff00" <= ch <= "\uffef")
    other = len(text) - cjk
    return cjk + max(0, other // 3)
