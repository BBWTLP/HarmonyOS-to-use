"""P0.5-A.1 device safety smoke: a dark screen must never receive content reads.

Five checks, metadata only, no action dispatch:

1. an unlocked observation is normal (batched, gated, actionable);
2. with the display suspended the readiness transaction is the *only* thing
   sent, and it carries no hierarchy/screenshot/foreground command;
3. the runtime's own wake/unlock recovery still works;
4. the observation after recovery is normal again;
5. nothing was dispatched (the action journal stays empty).

The proof in step 2 is taken from the exact script that reached the phone: the
device method is wrapped before the probe, so the script text is captured as it
is issued. That is what makes "the hierarchy was never captured" checkable on
real hardware instead of a substring hope.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from contextlib import nullcontext
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from harmony_runtime import snapshot as S            # noqa: E402
from harmony_runtime.contracts import RuntimeFault   # noqa: E402
from harmony_runtime.device import HarmonyDevice, find_hdc  # noqa: E402
from harmony_runtime.runtime import Runtime          # noqa: E402

CONTENT_TOKENS = ("uitest", "snapshot_display", "base64", "WindowManagerService",
                  "AbilityManagerService", "DisplayManagerService")


def device_shell(serial, command, timeout=60):
    child = subprocess.run([find_hdc(), "-t", serial, "shell", command],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
    return child.stdout


def check_no_content(script):
    offenders = [token for token in CONTENT_TOKENS if token in script]
    return offenders


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", default=".runtime/p05/a1-smoke")
    parser.add_argument("--out", default=".runtime/p05/a1-smoke.json")
    parser.add_argument("--hdc", default="")
    args = parser.parse_args()
    if args.hdc:
        import os
        os.environ["HARMONY_HDC"] = args.hdc

    report = {"schema_version": 1, "checks": {}, "status": "not_ready"}
    root = (REPO_ROOT / args.state_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    runtime = Runtime(root, factory=HarmonyDevice)
    try:
        session = runtime.session("smoke", "open")
        serial, sid = session["device_id"], session["session_id"]
        device = runtime._device(runtime._session("smoke", sid))

        # 2a. record what actually reaches the device from now on.
        recorded = []
        original = device.batch_probe

        def wrapped(script):
            recorded.append(script)
            return original(script)

        device.batch_probe = wrapped

        # 1. an ordinary gated observation.
        first = runtime.observe("smoke", sid)
        report["checks"]["unlocked_observation"] = {
            "actionable": first.get("actionable"),
            "provider": first["snapshot_capture"]["provider"],
            "transactions": first["perf"].get("observe.capture_transactions"),
            "round_trips": first["perf"].get("observe.hdc_round_trips"),
            "readiness_gate": first["perf"].get("observe.readiness_gate"),
            "readiness_gate_ms": first["perf"].get("observe.readiness_gate_ms"),
            "capture_ms": first.get("capture_ms"),
        }

        # 2. suspend the display and ask only for the readiness evidence.
        device_shell(serial, "power-shell suspend")
        time.sleep(1.0)
        recorded.clear()
        provider_state = S.ProviderState()
        # The device worker binds every call to an operation budget; this is the
        # same bounded scope the runtime uses, just for one direct probe.
        scope = (device.budget(time.monotonic() + 30, None)
                 if hasattr(device, "budget") else nullcontext())
        with scope:
            evidence = S.readiness_probe(device, state=provider_state)
        scripts = list(recorded)
        offenders = check_no_content(scripts[0]) if scripts else ["no transaction recorded"]
        report["checks"]["sleeping_gate"] = {
            "screen": evidence["state"],
            "transactions_issued": len(scripts),
            "phase1_offenders": offenders,
            "phase1_commands": [line.strip() for line in scripts[0].splitlines()
                                if line.strip().startswith(("time {", "{ "))] if scripts else [],
        }
        refusal = None
        try:
            runtime._require_ready(runtime._session("smoke", sid), evidence["state"])
        except RuntimeFault as error:
            refusal = error.code
        report["checks"]["sleeping_gate"]["refusal_code"] = refusal

        # 3/4. the ordinary path recovers the screen and observes again.
        recovered_started = time.monotonic()
        second = runtime.observe("smoke", sid)
        report["checks"]["recovery_observation"] = {
            "actionable": second.get("actionable"),
            "provider": second["snapshot_capture"]["provider"],
            "elapsed_ms": round((time.monotonic() - recovered_started) * 1000, 3),
            "screen_after": second.get("screen_state"),
            "gate_refusals_total": runtime.provider_state.gate_refusals,
        }
        full = runtime.observe("smoke", sid, mode="FULL")
        report["checks"]["full_observation"] = {
            "actionable": full.get("actionable"),
            "provider": full["snapshot_capture"]["provider"],
            "transactions": full["perf"].get("observe.capture_transactions"),
            "image_tree_consistent": full.get("image_tree_consistent"),
            "som_available": (full.get("som") or {}).get("available"),
            "capture_ms": full.get("capture_ms"),
        }

        # 5. no write was ever dispatched.
        history = runtime.journal.history()
        report["checks"]["no_dispatch"] = {"journal_entries": len(history)}

        ok = (report["checks"]["unlocked_observation"]["actionable"] is True
              and not offenders
              and refusal in ("screen_off", "screen_locked", "screen_state_unknown")
              and report["checks"]["recovery_observation"]["actionable"] is True
              and report["checks"]["full_observation"]["actionable"] is True
              and report["checks"]["recovery_observation"]["gate_refusals_total"] >= 1
              and len(history) == 0)
        report["status"] = "ok" if ok else "not_ready"
        report["provider_state"] = provider_state.as_dict()
        report["runtime_provider_state"] = runtime.provider_state.as_dict()
    finally:
        runtime.close()
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                             sort_keys=True), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
