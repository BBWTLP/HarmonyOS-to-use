"""Opt-in sleep/wake/unlock acceptance via the resident Runtime; metadata only."""
import argparse
import hashlib
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from harmony_runtime.contracts import RuntimeFault
from harmony_runtime.device import find_hdc, parse_screen_state
from harmony_runtime.service import Client


def run(args):
    report = {
        "tested_at": datetime.now(timezone.utc).isoformat(),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "transport": "authenticated_resident_service",
        "requested_rounds": args.rounds,
        "rounds": [],
        "status": "failed",
    }
    client = Client(args.state_dir.resolve())
    sid = None
    try:
        hdc = find_hdc()
        deadline = time.monotonic() + args.lease_wait_seconds
        while True:
            try:
                session = client.call("session", operation="open", device_id=args.device)
                break
            except RuntimeFault as exc:
                # A lease rejection occurs before acquisition. Never retry device actions.
                if exc.code != "lease_conflict" or time.monotonic() >= deadline:
                    raise
                time.sleep(min(2, max(0, deadline - time.monotonic())))
        sid = session["session_id"]

        def shell(command):
            result = subprocess.run(
                [hdc, "-t", session["device_id"], "shell", command],
                capture_output=True, timeout=15,
            )
            if result.returncode:
                raise RuntimeFault("hdc_failed", "HDC command failed")
            return result.stdout.decode("utf-8", errors="replace")

        def state():
            return parse_screen_state(
                shell("hidumper -s PowerManagerService -a '-a'"),
                shell("hidumper -s ScreenlockService -a -all"),
            )

        ready = {"screen_on": True, "screen_locked": False}
        for number in range(1, args.rounds + 1):
            row = {"round": number, "passed": False}
            report["rounds"].append(row)
            shell("power-shell suspend")
            # Readiness is measured below; this delay alone is not proof of sleep.
            time.sleep(0.8)
            row["before"] = state()
            if row["before"] != {"screen_on": False, "screen_locked": True}:
                raise RuntimeFault("sleep_lock_not_confirmed", "Device did not confirm both sleep and lock")
            start = time.monotonic()
            observation = client.call("observe", session_id=sid, include_image=False, mode="FAST")
            row.update(
                round_trip_ms=round((time.monotonic() - start) * 1000),
                capture_ms=observation.get("capture_ms"),
                observed_state=observation.get("screen_state"),
                catalog_count=len(observation.get("catalog", [])),
                actionable=observation.get("actionable") is True,
                observation_received=bool(observation.get("observation_id")),
                after=state(),
            )
            row["passed"] = (
                row["after"] == ready and row["observed_state"] == ready
                and row["catalog_count"] > 0 and row["actionable"]
                and row["observation_received"]
            )
            print(json.dumps(row), flush=True)
            if not row["passed"]:
                raise RuntimeFault("wake_verification_failed", "Readiness was not confirmed")
        report["status"] = "passed"
    except RuntimeFault as exc:
        report["error_code"] = exc.code
    except subprocess.TimeoutExpired:
        report["error_code"] = "hdc_timeout"
    except Exception:
        # Do not retain raw exceptions, UI data, endpoint tokens, or device identifiers.
        report["error_code"] = "acceptance_error"
    finally:
        if sid:
            try:
                client.call("session", operation="close", session_id=sid)
            except RuntimeFault as exc:
                report["close_error_code"] = exc.code
                report["status"] = "failed"
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "error_code": report.get("error_code")}), flush=True)
    return 0 if report["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Allow intentional device sleep for verification")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--device", default="auto")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--lease-wait-seconds", type=float, default=0, help="Bounded wait only for a rejected session acquisition")
    parser.add_argument("--report", type=Path, default=Path(".runtime/wake-unlock-acceptance.json"))
    args = parser.parse_args()
    if not args.execute:
        parser.error("--execute is required because this test puts the phone to sleep")
    if not 1 <= args.rounds <= 10:
        parser.error("--rounds must be between 1 and 10")
    if not 0 <= args.lease_wait_seconds <= 300:
        parser.error("--lease-wait-seconds must be between 0 and 300")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
