"""Author a practice wave (P3-01/P3-02).

Writes a wave spec that `run_practice.py` can execute. The default spec is the
offline mock-device catalogue: a clean search task, a task whose goal is not
reached (negative control), a fragile success and an unknown-write fault. Nothing
here touches a phone; a mock wave is never device evidence.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from harmony_agent.fake_device import MOCK_APP

SEARCH_STEPS = "tap:搜索|replace_text:id:search_input=query|tap:id:search_confirm_btn"


def task(*, request_id: str, steps: str, criteria: list[dict], goal: str,
         query: str | None = None, budget: dict | None = None) -> dict:
    arguments: dict[str, str] = {"steps": steps}
    if query is not None:
        arguments["query"] = query
    return {
        "schema_version": "2.0",
        "request_id": request_id,
        "mode": "auto",
        "goal": goal,
        "scope": {"device_ref": "mock-device", "allowed_apps": [MOCK_APP],
                  "allowed_actions": ["tap", "replace_text", "back"],
                  "cloud_data_policy": "disabled"},
        "success_criteria": criteria,
        "arguments": arguments,
        "budget": budget or {"max_dispatches": 6, "max_seconds": 60,
                             "max_model_calls": 4},
        "model_profile": "local_shadow",
    }


def default_wave(wave_id: str) -> dict:
    return {
        "schema_version": 1,
        "wave_id": wave_id,
        "adapter": "mock_device",
        "note": "offline practice catalogue; not device evidence",
        "branches": [
            {"branch_id": "branch_a", "label": "搜索闭环", "expect": "verified",
             "risk_class": "R1",
             "task": task(request_id=f"{wave_id}-a", steps=SEARCH_STEPS,
                          goal="在练习应用里搜索并确认结果页",
                          query="鸿蒙",
                          criteria=[
                              {"id": "query", "type": "input_equals",
                               "target_key": "id:search_input", "value_ref": "query"},
                              {"id": "results", "type": "text_equals",
                               "value": "鸿蒙 的相关结果"}])},
            {"branch_id": "branch_b", "label": "目标未达成", "expect": "failed",
             "risk_class": "R1",
             "task": task(request_id=f"{wave_id}-b", steps="tap:搜索",
                          goal="打开一个并不存在的页面的控件",
                          criteria=[{"id": "about", "type": "element_present",
                                     "target_key": "版本信息"}])},
            {"branch_id": "branch_c", "label": "脆弱成功", "expect": "verified",
             "risk_class": "R1", "faults": {"noop_taps": 1},
             "task": task(request_id=f"{wave_id}-c", steps="tap:搜索",
                          goal="第一次点击无效果后的重观察",
                          criteria=[{"id": "search", "type": "element_present",
                                     "target_key": "热搜榜"}])},
            {"branch_id": "branch_d", "label": "未知写入", "expect": "blocked",
             "risk_class": "R1", "faults": {"unknown_writes": 1},
             "task": task(request_id=f"{wave_id}-d", steps="tap:搜索",
                          goal="写入结果未知时必须阻断",
                          criteria=[{"id": "search", "type": "element_present",
                                     "target_key": "热搜榜"}])},
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Author an offline practice wave")
    parser.add_argument("--wave-id", default="wave_default")
    parser.add_argument("--out", default=".runtime/rsi/waves/default.json")
    args = parser.parse_args()
    spec = default_wave(args.wave_id)
    out = Path(args.out)
    if not out.is_absolute():
        out = REPO_ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "written", "path": str(out),
                      "branches": [item["branch_id"] for item in spec["branches"]]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
