"""Run an authored practice wave offline (Phase 1 / Phase 2 of the RSI loop).

Every branch runs against the mock device through the production Runtime, so the
wave exercises the guard, journal, epoch, candidate and checker path without a
phone. Experiences merge only after the barrier and only with an approval
reference; the merged snapshot is frozen for later read-only reuse.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from harmony_agent.fake_device import FaultProfile
from harmony_agent.memory_store import MemoryStore
from harmony_agent.rsi import PracticeBranch, WaveRunner


def load_wave(path: Path) -> list[PracticeBranch]:
    spec = json.loads(path.read_text(encoding="utf-8"))
    branches = []
    for item in spec.get("branches", []):
        faults = item.get("faults") or {}
        branches.append(PracticeBranch(
            branch_id=str(item["branch_id"]),
            task=dict(item["task"]),
            label=str(item.get("label", "")),
            expect=str(item.get("expect", "verified")),
            risk_class=str(item.get("risk_class", "R1")),
            faults=FaultProfile(unknown_writes=int(faults.get("unknown_writes", 0)),
                                noop_taps=int(faults.get("noop_taps", 0)),
                                black_screen=bool(faults.get("black_screen", False)))
            if faults else None))
    return branches


def main() -> int:
    parser = argparse.ArgumentParser(description="Run an offline practice wave")
    parser.add_argument("--wave", default=".runtime/rsi/waves/default.json")
    parser.add_argument("--root", default=".runtime/rsi")
    parser.add_argument("--memory-root", default=None,
                        help="defaults to <root>/memory")
    parser.add_argument("--approval-ref", default="",
                        help="policy/human approval reference; without it nothing merges")
    parser.add_argument("--report", default=".runtime/rsi/wave-report.json")
    parser.add_argument("--serial", action="store_true",
                        help="run branches one at a time instead of in parallel")
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--branch-timeout", type=float, default=180.0)
    args = parser.parse_args()

    def resolve(value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else REPO_ROOT / path

    root = resolve(args.root)
    branches = load_wave(resolve(args.wave))
    store = MemoryStore(resolve(args.memory_root) if args.memory_root
                        else root / "memory")
    existing = store.latest()
    memory = store.freeze(existing[0].version) if existing else None
    runner = WaveRunner(root / "waves", memory_store=store,
                        max_workers=args.max_workers,
                        branch_timeout=args.branch_timeout)
    if memory is None:
        # The first wave has no snapshot yet: start from an empty frozen mount.
        empty = store.commit([_bootstrap_experience()])
        memory = store.freeze(empty.version)
    try:
        report = runner.run(branches, memory=memory, approval_ref=args.approval_ref,
                            parallel=not args.serial)
    except Exception as error:
        print(json.dumps({"status": "error", "error": f"{type(error).__name__}: {error}"},
                         ensure_ascii=False))
        return 1
    payload = report.to_dict()
    payload["adapter"] = "mock_device"
    payload["limitations"] = [*payload["limitations"],
                              "offline mock device only: no device-verified claim"]
    out = resolve(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": payload["status"], "report": str(out),
                      "branches": {item["branch_id"]: item["outcome_kind"]
                                   for item in payload["branches"]},
                      "merged": len(payload["merged_experience_ids"]),
                      "blocked": payload["blocked_branch_ids"]}, ensure_ascii=False))
    return 0 if payload["status"] in ("MERGED", "MERGED_WITH_BLOCKED",
                                      "NO_MEMORY_CHANGE") else 1


def _bootstrap_experience():
    """The first snapshot needs one entry; it records the empty-state baseline."""
    from harmony_agent.experience import (ExperienceGrounding, ExperienceScope,
                                          ExperienceTrigger, ProcedureStep,
                                          build_experience)
    return build_experience(
        scope=ExperienceScope(app="com.example.practice", build="mock-1",
                              device_capability="mock_driver"),
        trigger=ExperienceTrigger(foreground="com.example.practice",
                                  facts=["bootstrap"], goal_pattern="bootstrap"),
        grounding=ExperienceGrounding(layers=["page_identity"], identity="bootstrap"),
        procedure=[ProcedureStep(intent={"action_kind": "back"},
                                 expected={"predicate": "page_changed"})],
        outcome="verified", risk_class="R0",
        evidence_refs=["bootstrap:offline"], source_task_id="bootstrap",
        ttl_days=1)


if __name__ == "__main__":
    raise SystemExit(main())
