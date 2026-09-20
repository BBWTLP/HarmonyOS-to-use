"""Offline decision evaluation: splits, metrics and threshold fitting.

The dataset is a list of states, each with the candidate set the service
registered and a label: the candidate id a competent operator would accept, or
`none` when no candidate is acceptable. Metrics are reported per provider and
per split; thresholds are fitted **only** on the calibration split.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .decision.providers.decider import DeciderProvider, estimate_tokens
from .decision.router import evaluate_propositions

NONE_LABEL = "none"


@dataclass
class StateSample:
    state_id: str
    split: str
    goal: str
    state_text: str
    candidates: list[dict[str, str]]
    label: str
    propositions: dict[str, str] = field(default_factory=dict)
    app: str = ""
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"state_id": self.state_id, "split": self.split, "goal": self.goal,
                "state_text": self.state_text, "candidates": self.candidates,
                "label": self.label, "propositions": self.propositions,
                "app": self.app, "tags": self.tags}

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> "StateSample":
        return StateSample(state_id=payload["state_id"], split=payload["split"],
                           goal=payload.get("goal", ""), state_text=payload["state_text"],
                           candidates=list(payload["candidates"]), label=payload["label"],
                           propositions=dict(payload.get("propositions") or {}),
                           app=payload.get("app", ""), tags=list(payload.get("tags") or []))


def load_dataset(path: str | Path) -> list[StateSample]:
    samples = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            samples.append(StateSample.from_dict(json.loads(line)))
    return samples


def save_dataset(path: str | Path, samples: Iterable[StateSample]) -> dict[str, Any]:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(sample.to_dict(), ensure_ascii=False, sort_keys=True) for sample in samples]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dataset_digest(samples)


def dataset_digest(samples: Iterable[StateSample]) -> dict[str, Any]:
    digest = hashlib.sha256()
    counts: dict[str, int] = {}
    for sample in samples:
        digest.update(json.dumps(sample.to_dict(), sort_keys=True, ensure_ascii=False).encode("utf-8"))
        counts[sample.split] = counts.get(sample.split, 0) + 1
    return {"sha256": digest.hexdigest(), "splits": counts,
            "total": sum(counts.values())}


def expected_calibration_error(pairs: list[tuple[float, bool]], bins: int = 10) -> float:
    if not pairs:
        return 0.0
    total = len(pairs)
    error = 0.0
    for index in range(bins):
        low, high = index / bins, (index + 1) / bins
        bucket = [(confidence, correct) for confidence, correct in pairs
                  if low < confidence <= high or (index == 0 and confidence == 0.0)]
        if not bucket:
            continue
        accuracy = sum(1 for _, correct in bucket if correct) / len(bucket)
        confidence = sum(value for value, _ in bucket) / len(bucket)
        error += (len(bucket) / total) * abs(accuracy - confidence)
    return round(error, 4)


def brier_score(pairs: list[tuple[float, bool]]) -> float:
    if not pairs:
        return 0.0
    return round(sum((value - (1.0 if correct else 0.0)) ** 2
                     for value, correct in pairs) / len(pairs), 4)


def build_choice_question(sample: StateSample) -> dict[str, Any]:
    criteria = {item["candidate_id"]: item["description"] for item in sample.candidates}
    criteria["cand_none_applicable"] = "以上候选都不符合当前目标"
    if len(criteria) > 16:
        criteria = dict(list(criteria.items())[:15] + [("cand_none_applicable",
                                                        "以上候选都不符合当前目标")])
    return {"type": "choice",
            "instructions": "从给定候选中选出最符合当前任务目标的下一步动作。只使用状态中的事实；没有合适候选时选择“以上皆非”。",
            "criteria": criteria}


async def evaluate_with_decider(samples: list[StateSample], provider: DeciderProvider,
                                *, max_samples: int | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sample in samples[:max_samples] if max_samples else samples:
        question = build_choice_question(sample)
        questions = {"action": question}
        for key, statement in list(sample.propositions.items())[:3]:
            questions[key] = {"type": "noul",
                              "instructions": f"判断命题是否成立：{statement}",
                              "criteria": {"true": "状态中的事实支持该命题",
                                           "false": "状态中没有事实支持该命题"}}
        state_text = sample.state_text
        if estimate_tokens(state_text) > 1024:
            state_text = state_text[:3072]
        row: dict[str, Any] = {"state_id": sample.state_id, "split": sample.split,
                               "label": sample.label, "tags": sample.tags}
        try:
            result = await provider.evaluate({"state": state_text, "controller_epoch": 0},
                                             questions)
            answer = result.answer["action"]
            row.update(provider="decider", choice=answer["choice"],
                       confidence=answer["confidence"], certainty=answer["certainty"],
                       elapsed_ms=result.elapsed_ms,
                       probabilities=answer["probabilities"])
            noul = {key: value["noul"] for key, value in result.answer.items()
                    if value.get("type") == "noul"}
            if noul:
                row["noul"] = noul
        except Exception as error:
            row.update(provider="decider", error=getattr(error, "code", type(error).__name__))
        rows.append(row)
    return rows


def rules_baseline(samples: list[StateSample]) -> list[dict[str, Any]]:
    """Rules answer only when the candidate set is unambiguous."""
    rows = []
    for sample in samples:
        if len(sample.candidates) == 1:
            rows.append({"state_id": sample.state_id, "split": sample.split,
                         "label": sample.label, "choice": sample.candidates[0]["candidate_id"],
                         "confidence": 1.0, "certainty": 1.0, "provider": "rules"})
        else:
            rows.append({"state_id": sample.state_id, "split": sample.split,
                         "label": sample.label, "choice": "cand_none_applicable",
                         "confidence": 1.0, "certainty": 1.0, "provider": "rules"})
    return rows


def summarise(rows: list[dict[str, Any]], *, threshold_field: str = "confidence",
              threshold: float = 0.0) -> dict[str, Any]:
    """Coverage / accuracy / error-rate accounting, never accuracy alone."""
    accepted = []
    refused = 0
    errors = 0
    pairs: list[tuple[float, bool]] = []
    for row in rows:
        value = row.get(threshold_field, 0.0)
        choice = row.get("choice")
        if row.get("error") or choice in (None, "cand_none_applicable") or value < threshold:
            refused += 1
            continue
        correct = choice == row["label"]
        accepted.append(row)
        pairs.append((float(value), correct))
        if not correct:
            errors += 1
    total = len(rows)
    return {
        "samples": total,
        "accepted": len(accepted),
        "refused": refused,
        "coverage": round(len(accepted) / total, 4) if total else 0.0,
        "accepted_correct": len(accepted) - errors,
        "accepted_errors": errors,
        "error_rate_among_accepted": round(errors / len(accepted), 4) if accepted else None,
        "error_denominator": len(accepted),
        "brier": brier_score(pairs),
        "ece": expected_calibration_error(pairs),
    }


def summarise_by_split(rows: list[dict[str, Any]], **kwargs) -> dict[str, Any]:
    splits: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        splits.setdefault(row.get("split", "unknown"), []).append(row)
    return {name: summarise(items, **kwargs) for name, items in sorted(splits.items())}


def fit_thresholds(calibration_rows: list[dict[str, Any]], *,
                   max_error_rate: float = 0.05,
                   min_coverage: float = 0.2) -> dict[str, Any]:
    """Choose the lowest confidence gate that keeps accepted errors in budget."""
    best: dict[str, Any] | None = None
    for gate in [round(0.05 * step, 2) for step in range(0, 20)]:
        summary = summarise(calibration_rows, threshold=gate)
        if summary["accepted"] == 0:
            continue
        if summary["coverage"] < min_coverage:
            continue
        error_rate = summary["error_rate_among_accepted"] or 0.0
        if error_rate <= max_error_rate:
            candidate = {"threshold": gate, "confidence_gate": gate, **summary}
            if best is None or candidate["coverage"] > best["coverage"]:
                best = candidate
    return best or {"threshold": None, "confidence_gate": None,
                    "reason": "no gate met the error budget on the calibration split"}
