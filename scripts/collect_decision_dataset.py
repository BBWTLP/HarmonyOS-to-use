"""F04 step 1: collect labelled Chinese states from the real device.

Each sample is the service's own candidate set for one observed state plus a
label taken from the scenario definition (the target the operator intended), not
from any model. Splits follow the architecture: development / calibration /
holdout, grouped so the same scenario-round never crosses splits.

Raw states are written under `.runtime/` (local, protected) and are not
committed; only the manifest and metrics are.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_harness import (AgentHarness, HarnessError, ServiceClientHarness, WEIBO,
                           find_node, surface_kind)
from harmony_agent.candidates import CandidateRegistry
from harmony_agent.checker import _match_nodes
from harmony_agent.contracts import Predicate
from harmony_agent.evals import StateSample, dataset_digest, save_dataset
from harmony_agent.grounding import GroundingIntent
from harmony_agent.state_builder import build_state

DISCOVER_TAB = "发现"
HOME_TAB = "首页"
MESSAGE_TAB = "消息"
ME_TAB = "我"

#: (scenario, entry, intent text, tag)
SCENARIOS = (
    ("tap_discover", "tabs", DISCOVER_TAB, "bottom_nav"),
    ("tap_messages", "tabs", MESSAGE_TAB, "bottom_nav"),
    ("tap_me", "tabs", ME_TAB, "bottom_nav"),
    ("tap_home", "tabs", HOME_TAB, "bottom_nav"),
    ("tap_absent", "tabs", "不存在的入口按钮", "no_candidate"),
    ("tap_discover_repeat", "discover", DISCOVER_TAB, "bottom_nav"),
)


async def goto_tabs(harness: AgentHarness) -> dict:
    """Reach a page that shows the bottom navigation, tolerating page churn."""
    last: dict | None = None
    for _ in range(8):
        observation = await harness.stable_observation()
        last = observation
        try:
            if observation.get("foreground_bundle") != WEIBO:
                await harness.act(observation_id=observation["observation_id"],
                                  action={"kind": "launch", "bundle": WEIBO},
                                  expected={"bundle": WEIBO}, timeout_ms=15000)
                continue
            if surface_kind(observation) == "tabs":
                return observation
            node = find_node(observation, text=HOME_TAB)
            if node is None:
                await asyncio.sleep(0.5)
                await harness.act(observation_id=observation["observation_id"],
                                  action={"kind": "back"},
                                  expected={"changed": True}, timeout_ms=10000)
            else:
                await asyncio.sleep(0.5)
                await harness.act(observation_id=observation["observation_id"],
                                  action={"kind": "tap", "target": {"text": HOME_TAB}},
                                  expected={"changed": True}, timeout_ms=10000)
        except HarnessError:
            await asyncio.sleep(0.8)
            continue
    return last if last is not None else await harness.observe(mode="FAST")


async def goto_discover(harness: AgentHarness) -> dict:
    for _ in range(6):
        observation = await goto_tabs(harness)
        if surface_kind(observation) == "discover":
            return observation
        node = find_node(observation, text=DISCOVER_TAB)
        if node is None:
            continue
        try:
            await asyncio.sleep(0.4)
            await harness.act(observation_id=observation["observation_id"],
                              action={"kind": "tap", "target": {"text": DISCOVER_TAB}},
                              expected={"changed": True}, timeout_ms=10000)
        except HarnessError:
            continue
    return await harness.observe(mode="FAST")


def label_for(observation: dict, intent_text: str, candidate_set) -> str:
    """The candidate the operator intended: the label's own actionable target.

    Grounding registers the nearest clickable ancestor of a text label, so the
    label itself is rarely the candidate. Walk the same ancestry here instead of
    comparing the label's fingerprint.
    """
    matches = _match_nodes(observation, intent_text)
    if len(matches) != 1:
        return "none"
    by_id = {node["action_id"]: node for node in observation.get("catalog", [])
             if node.get("action_id")}
    node = matches[0]
    seen: set[str] = set()
    while node.get("parent_action_id") in by_id and node["action_id"] not in seen:
        seen.add(node["action_id"])
        if node.get("clickable"):
            break
        node = by_id[node["parent_action_id"]]
    for item in candidate_set.candidates:
        if item.target.local_fingerprint == node.get("target_fingerprint"):
            return item.candidate.candidate_id
    return "none"


def split_for(scenario: str, index: int, total: int) -> str:
    """Group by scenario so a scenario never crosses splits."""
    bucket = (index - 1) * 3 // total
    return ("development", "calibration", "holdout")[min(2, bucket)]


async def main(args) -> int:
    samples: list[StateSample] = []
    registry = CandidateRegistry()
    manifest: dict = {"schema_version": 1, "task": "F04 dataset collection",
                      "app": WEIBO, "rounds_per_scenario": args.rounds,
                      "collected": [], "status": "not_ready"}
    harness_class = ServiceClientHarness if args.transport == "service" else AgentHarness
    harness_kwargs = ({"state_dir": args.state_dir} if args.transport == "service"
                      else {"state_dir": args.state_dir, "agent_tools": False})
    async with harness_class(**harness_kwargs) as harness:
        await harness.open(args.device_id)
        for scenario, entry, intent_text, tag in SCENARIOS:
            for index in range(1, args.rounds + 1):
                try:
                    state = (await goto_discover(harness) if entry == "discover"
                             else await goto_tabs(harness))
                    # FAST keeps the capture actionable (cached as a handle) on
                    # live pages; the text Decider never consumes images.
                    observation = await harness.observe(mode="FAST")
                except HarnessError as error:
                    entry_record = {"scenario": scenario, "round": index,
                                    "error": error.code, "message": str(error)[:160]}
                    manifest["collected"].append(entry_record)
                    print(json.dumps(entry_record, ensure_ascii=False), flush=True)
                    continue
                intent = GroundingIntent(action_kind="tap", text=intent_text)
                candidates: list[dict] = []
                note = None
                candidate_set = None
                try:
                    candidate_set = registry.build(
                        task_id=f"dataset-{scenario}", subgoal_id=f"round-{index}",
                        scope_id="dataset", observation=observation,
                        controller_epoch=int(observation.get("controller_epoch", 0)),
                        intent=intent, action_kind="tap", arguments={},
                        expected_predicates=[Predicate(id="p", type="foreground_is",
                                                       value=WEIBO)])
                    candidates = [{"candidate_id": item.candidate.candidate_id,
                                   "description": item.description,
                                   "action_kind": item.candidate.action_kind,
                                   "risk_class": item.candidate.risk_class}
                                  for item in candidate_set.candidates]
                    label = label_for(observation, intent_text, candidate_set)
                    layers = (candidate_set.grounding.layers_used
                              if candidate_set.grounding else [])
                except Exception as error:
                    # A capture that is not actionable, or a layer that returns no
                    # evidence, is still a valid calibration state: it records the
                    # "no candidate / reobserve" case instead of being dropped.
                    note = getattr(error, "code", type(error).__name__)
                    label = "none"
                    layers = []
                if candidate_set is not None and not note:
                    state_text = build_state(observation, candidate_set,
                                             goal=f"打开{intent_text}")
                else:
                    state_text = (f"任务目标：打开{intent_text}\n"
                                  f"前台应用：{observation.get('foreground_bundle')}\n"
                                  "候选：无（本轮没有可执行目标）")
                sample = StateSample(
                    state_id=f"{scenario}-{index:03d}",
                    split=split_for(scenario, index, args.rounds),
                    goal=f"打开{intent_text}",
                    state_text=state_text,
                    candidates=candidates,
                    label=label,
                    app=WEIBO, tags=[tag] + ([f"no_candidate:{note}"] if note else []),
                )
                samples.append(sample)
                manifest["collected"].append({
                    "scenario": scenario, "round": index, "state_id": sample.state_id,
                    "split": sample.split, "candidates": len(candidates),
                    "labelled": label != "none",
                    "layers": layers, "note": note,
                    "state_chars": len(sample.state_text)})
                print(json.dumps(manifest["collected"][-1], ensure_ascii=False), flush=True)
    digest = save_dataset(args.dataset, samples) if samples else {"sha256": None, "splits": {}}
    manifest["dataset"] = str(args.dataset)
    manifest["dataset_digest"] = digest
    manifest["states"] = len(samples)
    manifest["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    manifest["status"] = "ok" if len(samples) >= args.rounds * 3 else "not_ready"
    manifest["note"] = ("Labels come from the scenario's intended target and the "
                        "service's own candidate set; no model produced a label.")
    target = Path(args.report)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    print(json.dumps({key: manifest[key] for key in ("states", "dataset_digest", "status")},
                     ensure_ascii=False))
    return 0 if manifest["status"] == "ok" else 1


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", default=".runtime/agent-state")
    parser.add_argument("--dataset", default=".runtime/evals/decision/states.jsonl")
    parser.add_argument("--report", default="docs/acceptance/2026-09-20/decider-dataset.json")
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--transport", choices=("stdio", "service"), default="service")
    parser.add_argument("--device-id")
    parser.add_argument("--execute", action="store_true",
                        help="Acknowledge that observation may wake or unlock the phone")
    args = parser.parse_args(argv)
    if not args.execute:
        parser.error("collect-decision-dataset requires --execute")
    return args


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(parse_args())))
