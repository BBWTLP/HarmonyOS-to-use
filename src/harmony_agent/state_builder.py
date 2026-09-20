"""Build the bounded decision state and the independent questions.

Candidate identifiers are never shown to the model; it answers with opaque keys
that the service maps back to registered candidates. The state is a projection
of observable facts, not a screenshot description, and it is trimmed to the
Decider's 1024-token budget instead of being silently truncated at the wire.
"""
from __future__ import annotations

from typing import Any, Iterable

from .candidates import CandidateSet
from .decision.providers.decider import MAX_STATE_TOKENS, estimate_tokens
from .contracts import Predicate

#: Facts carried into every decision, in priority order.
ALWAYS_KEYS = ("foreground", "page", "state", "focus", "error")


def observation_facts(observation: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    bundle = observation.get("foreground_bundle")
    lines.append(f"前台应用：{bundle or '未知'}")
    display = observation.get("display") or {}
    lines.append(f"屏幕：{display.get('width')}x{display.get('height')} 旋转{display.get('rotation')}")
    if observation.get("blocking_dialog"):
        lines.append("系统凭据界面正在遮挡")
    texts = [str(item.get("text")) for item in observation.get("catalog", []) if item.get("text")]
    if texts:
        lines.append("可见文字：" + " | ".join(dict.fromkeys(texts)))
    focused = [item for item in observation.get("catalog", []) if item.get("focused")]
    for item in focused:
        lines.append(f"焦点输入框：{item.get('text') or '空'}")
    return lines


def candidate_lines(candidate_set: CandidateSet) -> list[str]:
    if not candidate_set.candidates:
        return ["候选：无（本轮没有可执行目标）"]
    lines = ["可选动作："]
    for index, item in enumerate(candidate_set.candidates, start=1):
        target = item.target
        bits = [f"{index}. {item.description}"]
        if target.identity.get("resource_id"):
            bits.append(f"id={target.identity['resource_id']}")
        if target.identity.get("accessibility_id"):
            bits.append(f"a11y={target.identity['accessibility_id']}")
        state = []
        if target.selected in (True, "true"):
            state.append("已选中")
        if target.checked in (True, "true"):
            state.append("已勾选")
        if target.focused:
            state.append("有焦点")
        if not target.enabled:
            state.append("不可用")
        if state:
            bits.append("状态：" + "/".join(state))
        bits.append(f"来源={target.source}")
        lines.append(" ".join(bits))
    return lines


def build_state(observation: dict[str, Any], candidate_set: CandidateSet, *,
                goal: str, facts: Iterable[str] = (), recent: Iterable[str] = (),
                max_tokens: int = MAX_STATE_TOKENS) -> str:
    """Compose the state string, dropping low-value lines before the budget."""
    head = [f"任务目标：{goal}"]
    recent_lines = [f"最近结果：{item}" for item in list(recent)[-3:]]
    body = [f"已知事实：{text}" for text in facts]
    evidence = observation_facts(observation)
    tail = candidate_lines(candidate_set)

    def render(evidence_lines: list[str], recent_used: list[str]) -> str:
        return "\n".join(head + recent_used + body + evidence_lines + tail)

    text = render(evidence, recent_lines)
    if estimate_tokens(text) <= max_tokens:
        return text
    # Drop low-value evidence first: recent outcomes, then page text lines.
    text = render(evidence, [])
    while estimate_tokens(text) > max_tokens and evidence:
        evidence = evidence[:-1]
        text = render(evidence, [])
    while estimate_tokens(text) > max_tokens and body:
        body.pop(0)
        text = render(evidence, [])
    # Candidate lines are never dropped: they are the sole decision input.
    return render(evidence, [])


def build_questions(candidate_set: CandidateSet, propositions: dict[str, str] | None = None,
                    *, max_candidates: int = 16) -> dict[str, dict[str, Any]]:
    """One choice question plus up to three independent noul propositions."""
    criteria = candidate_set.choice_criteria(max_criteria=max_candidates)
    if len(criteria) < 2:
        return {}
    questions: dict[str, dict[str, Any]] = {
        "action": {
            "type": "choice",
            "instructions": "从给定候选中选出最符合当前任务目标的下一步动作。只使用状态中的事实；没有合适候选时选择“以上皆非”。",
            "criteria": criteria,
        }
    }
    for key, statement in list((propositions or {}).items())[:3]:
        questions[key] = {
            "type": "noul",
            "instructions": f"判断命题是否成立：{statement}",
            "criteria": {"true": "状态中的事实支持该命题", "false": "状态中没有事实支持该命题"},
        }
    return questions


def predicate_propositions(predicates: list[Predicate]) -> dict[str, str]:
    """Turn programme-checkable predicates into noul statements for the shadow run.

    These只用于影子对照：程序谓词的真值由代码判定，模型分数只作参考。
    """
    statements: dict[str, str] = {}
    for index, predicate in enumerate(predicates[:3], start=1):
        if predicate.type == "page_assertion" and predicate.description:
            statements[f"prop{index}"] = predicate.description
        elif predicate.type == "text_equals":
            statements[f"prop{index}"] = f"页面上存在文本“{predicate.value}”"
        elif predicate.type == "input_equals":
            statements[f"prop{index}"] = "目标输入框中的内容与期望一致"
        elif predicate.type == "foreground_is":
            statements[f"prop{index}"] = f"前台应用是 {predicate.value}"
        elif predicate.type == "element_present":
            statements[f"prop{index}"] = f"页面存在元素 {predicate.target_key}"
        elif predicate.type == "element_absent":
            statements[f"prop{index}"] = f"页面不存在元素 {predicate.target_key}"
    return statements
