"""Short A/B benchmark for the P0.5 observation data plane.

Compares the legacy six-read observation against the batched device snapshot on
the *same device and page*, through the real MCP stdio boundary. One invocation
manages both service processes itself so the only difference between the two
phases is ``HARMONY_OBSERVE_BATCHED``.

Reports metadata only: durations, counts, enums, booleans and hashes. It never
records UI text, screenshots or input values.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from agent_harness import AgentHarness  # noqa: E402

HDC_DEFAULT = r"F:\DevEco Studio\sdk\default\openharmony\toolchains\hdc.exe"


def percentile(values, fraction):
    ordered = sorted(float(value) for value in values if isinstance(value, (int, float)))
    if not ordered:
        return None
    return round(ordered[min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))], 3)


def distribution(values):
    clean = [v for v in values if isinstance(v, (int, float))]
    return {"count": len(clean), "p50": percentile(clean, .5), "p95": percentile(clean, .95),
            "min": round(min(clean), 3) if clean else None,
            "max": round(max(clean), 3) if clean else None}


class Service:
    """One resident runtime process with an explicit observation provider."""

    def __init__(self, state_dir, batched, hdc):
        self.state_dir = Path(state_dir)
        self.batched = batched
        self.hdc = hdc
        self.process = None

    def __enter__(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "endpoint.json").unlink(missing_ok=True)
        env = dict(os.environ)
        env["HARMONY_HDC"] = self.hdc
        env["HARMONY_OBSERVE_BATCHED"] = "1" if self.batched else "0"
        self.log = open(self.state_dir / "service.log", "a", encoding="utf-8")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "harmony_runtime.cli", "serve",
             "--state-dir", str(self.state_dir)],
            cwd=str(REPO_ROOT), env=env, stdout=self.log, stderr=self.log)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if (self.state_dir / "endpoint.json").exists():
                return self
            if self.process.poll() is not None:
                raise SystemExit("runtime service exited during startup; see service.log")
            time.sleep(0.2)
        raise SystemExit("runtime service did not publish an endpoint within 30s")

    def __exit__(self, *exc):
        try:
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
                self.process.wait(timeout=20)
        finally:
            self.log.close()
            (self.state_dir / "endpoint.json").unlink(missing_ok=True)


async def collect(state_dir, mode, samples, image, act_text, act_kind, prepare_bundle):
    import asyncio
    async with AgentHarness(state_dir=state_dir) as harness:
        await harness.open()
        prepared = "not_attempted"
        if prepare_bundle:
            observation = await harness.observe(mode="FAST")
            if observation.get("foreground_bundle") != prepare_bundle:
                await harness.act(observation_id=observation["observation_id"],
                                  action={"kind": "launch", "bundle": prepare_bundle},
                                  expected={"bundle": prepare_bundle}, timeout_ms=15000)
                await asyncio.sleep(1.5)
            prepared = "ok"
        rows = []
        for index in range(1, samples + 1):
            if mode in ("fast", "full"):
                started = time.perf_counter()
                try:
                    observation = await harness.observe(
                        mode="FULL" if mode == "full" else "FAST", include_image=image)
                    row = {"index": index, "status": "ok",
                           "client_ms": round((time.perf_counter() - started) * 1000, 3),
                           "capture_ms": observation.get("capture_ms"),
                           "timing": observation.get("timing"),
                           "perf": observation.get("perf"),
                           "capture": observation.get("snapshot_capture"),
                           "actionable": observation.get("actionable"),
                           "consistent": observation.get("snapshot_consistent"),
                           "reason": observation.get("consistency_reason")}
                except Exception as error:
                    row = {"index": index, "status": "failed",
                           "error": getattr(error, "code", type(error).__name__),
                           "client_ms": round((time.perf_counter() - started) * 1000, 3)}
                rows.append(row)
                continue
            # act path: the caller observes, the runtime runs its own live guard
            try:
                observation = await harness.observe(mode="FAST")
                observed = time.perf_counter()
                if act_kind == "launch":
                    action = {"kind": "launch", "bundle": act_text}
                    expected = {"bundle": act_text}
                elif act_kind == "back":
                    action, expected = {"kind": "back"}, None
                else:
                    action = {"kind": "tap", "target": {"text": act_text}}
                    expected = None
                result = await harness.act(observation_id=observation["observation_id"],
                                           action=action, expected=expected, timeout_ms=8000)
                finished = time.perf_counter()
                rows.append({"index": index, "status": result.get("status", "ok"),
                             "caller_observe_ms": observation.get("capture_ms"),
                             "act_ms": round((finished - observed) * 1000, 3),
                             "client_ms": round((finished - (observed - observation.get("capture_ms", 0) / 1000)) * 1000, 3),
                             "perf": result.get("perf"),
                             "preflight_capture": result.get("preflight_capture"),
                             "execution_status": result.get("execution_status"),
                             "verification_status": result.get("verification_status"),
                             "after_observation_id": result.get("after_observation_id"),
                             "observation_id": observation.get("observation_id")})
            except Exception as error:
                rows.append({"index": index, "status": "failed",
                             "error": getattr(error, "code", type(error).__name__)})
        return {"prepared": prepared, "rows": rows}


def summarize(rows):
    successful = [row for row in rows if row.get("status") == "ok"]
    phases = sorted({key for row in successful for key in (row.get("timing") or {})})
    metrics = sorted({key for row in successful for key in (row.get("perf") or {})})
    return {
        "attempted": len(rows), "successful": len(successful),
        "failed": len(rows) - len(successful),
        "client_ms": distribution([row.get("client_ms") for row in rows]),
        "capture_ms": distribution([row.get("capture_ms") for row in successful]),
        "phases": {key: distribution([(row.get("timing") or {}).get(key) for row in successful])
                   for key in phases},
        "perf": {key: distribution([(row.get("perf") or {}).get(key) for row in successful])
                 for key in metrics
                 if all(isinstance((row.get("perf") or {}).get(key), (int, float))
                        for row in successful)},
        "perf_enums": {key: sorted({str((row.get("perf") or {}).get(key)) for row in successful})
                       for key in metrics
                       if not all(isinstance((row.get("perf") or {}).get(key), (int, float))
                                  for row in successful)},
        "round_trips": sorted({(row.get("capture") or {}).get("round_trips")
                               for row in successful}),
        "providers": sorted({str((row.get("capture") or {}).get("provider"))
                             for row in successful}),
        "consistency_reasons": sorted({str(row.get("reason")) for row in successful}),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", default=".runtime/p05/bench")
    parser.add_argument("--mode", choices=["fast", "full", "act"], default="fast")
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--image", action="store_true")
    parser.add_argument("--act-kind", default="tap")
    parser.add_argument("--act-text", default="首页")
    parser.add_argument("--prepare-bundle", default="")
    parser.add_argument("--phases", default="both", choices=["both", "legacy", "batched"])
    parser.add_argument("--out", default="")
    parser.add_argument("--hdc", default=os.environ.get("HARMONY_HDC", HDC_DEFAULT))
    args = parser.parse_args()
    import asyncio

    phases = {"both": (False, True), "legacy": (False,), "batched": (True,)}[args.phases]
    report = {"schema_version": 1, "mode": args.mode, "samples": args.samples,
              "include_image": args.image, "act": {"kind": args.act_kind, "text": args.act_text},
              "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "phases": {}}
    for batched in phases:
        name = "batched" if batched else "legacy"
        with Service(args.state_dir, batched, args.hdc):
            collected = asyncio.run(collect(args.state_dir, args.mode, args.samples,
                                            args.image, args.act_text, args.act_kind,
                                            args.prepare_bundle))
        report["phases"][name] = {"prepared": collected["prepared"],
                                  "summary": summarize(collected["rows"]),
                                  "samples": collected["rows"]}
    legacy, batched_phase = report["phases"].get("legacy"), report["phases"].get("batched")
    if legacy and batched_phase:
        for key in ("client_ms", "capture_ms"):
            before = legacy["summary"][key]["p50"]
            after = batched_phase["summary"][key]["p50"]
            if before and after:
                report.setdefault("comparison", {})[key + "_p50_reduction_pct"] = round(
                    (before - after) / before * 100, 2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
                                  encoding="utf-8")
    print(json.dumps({name: phase["summary"] for name, phase in report["phases"].items()},
                     ensure_ascii=False, indent=2, sort_keys=True)[:6000])
    if report.get("comparison"):
        print(json.dumps(report["comparison"], ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
