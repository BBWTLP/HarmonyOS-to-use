"""Opt-in Weibo/Xiaohongshu foreground-switch acceptance; metadata reports only."""
import argparse
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from harmony_runtime.contracts import RuntimeFault
from harmony_runtime.service import Client

APPS = (("xiaohongshu", "com.xingin.xhs_hos"), ("weibo", "com.sina.weibo.stage"))


def run(args):
    report = {"tested_at": datetime.now(timezone.utc).isoformat(),
              "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "transport": "authenticated_resident_service", "requested_rounds": args.rounds,
              "status": "failed", "steps": []}
    client = Client(args.state_dir.resolve())
    sid = None
    try:
        session = client.call("session", operation="open", device_id="auto")
        sid = session["session_id"]
        if not session["capabilities"].get("foreground_bundle"):
            raise RuntimeFault("unsupported_capability", "Foreground verification required")
        if session["recovery_required"]:
            raise RuntimeFault("recovery_required", "Resolve unknown prior actions first")
        for number in range(1, args.rounds + 1):
            for app, bundle in APPS:
                start = time.monotonic()
                for attempt in range(4):
                    observation = client.call("observe", session_id=sid, mode="FAST", include_image=False)
                    if not observation.get("actionable"):
                        if attempt < 3:
                            continue
                        raise RuntimeFault("capture_unstable", "No stable observation")
                    try:
                        result = client.call("act", arguments={
                            "session_id": sid, "request_id": uuid.uuid4().hex,
                            "observation_id": observation["observation_id"],
                            "action": {"kind": "launch", "bundle": bundle},
                            "expected": {"bundle": bundle}, "timeout_ms": 30000})
                        break
                    except RuntimeFault as exc:
                        # Re-observe only for explicit pre-dispatch rejection.
                        if exc.code != "stale_observation" or attempt == 3:
                            raise
                after = result.get("observation", {})
                row = {"round": number, "app": app,
                       "status": result.get("status"),
                       "blocking_code": (after.get("blocking_dialog") or {}).get("code"),
                       "execution_status": result.get("execution_status"),
                       "verification_status": result.get("verification_status"),
                       "foreground_matches": after.get("foreground_bundle") == bundle,
                       "foreground_consistent": after.get("foreground_consistent"),
                       "actionable": after.get("actionable"),
                       "catalog_count": len(after.get("catalog", [])),
                       "elapsed_ms": round((time.monotonic()-start)*1000),
                       "pre_dispatch_refreshes": attempt}
                row["passed"] = (row["execution_status"] == "executed"
                    and row["verification_status"] == "verified" and row["foreground_matches"]
                    and row["foreground_consistent"] and row["actionable"] and row["catalog_count"] > 0)
                report["steps"].append(row)
                print(json.dumps(row), flush=True)
                if not row["passed"]:
                    raise RuntimeFault("authentication_required" if row["blocking_code"] == "authentication_required" else "switch_verification_failed", "Foreground switch was not verified")
        report["status"] = "passed"
    except RuntimeFault as exc:
        report["error_code"] = exc.code
    except Exception:
        report["error_code"] = "acceptance_error"
    finally:
        if sid:
            try:
                client.call("session", operation="close", session_id=sid)
            except Exception:
                report["close_error_code"] = "session_close_failed"
                report["status"] = "failed"
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "error_code": report.get("error_code")}), flush=True)
    return 0 if report["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--state-dir", type=Path, default=Path(".runtime/acceptance"))
    parser.add_argument("--rounds", type=int, default=3, choices=range(1, 11))
    parser.add_argument("--report", type=Path, default=Path(".runtime/app-switch-acceptance.json"))
    args = parser.parse_args()
    if not args.execute:
        parser.error("--execute is required for real device navigation")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
