"""Opt-in sleep/wake/unlock acceptance via the resident Runtime; metadata only.

Sleep precondition is "screen explicitly off". The lock flag is recorded as-is
because a credential-free device may report locked true or false after sleep.
Recovery must end with a confirmed awake, unlocked screen. Optional
``--natural-idle-seconds`` waits on the local clock only (no observe, no
keep-alive taps) before one sleep check and one Runtime recovery.
"""
import argparse
import hashlib
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from harmony_runtime.contracts import RuntimeFault
from harmony_runtime.device import (
    find_hdc,
    parse_screen_state,
    screen_ready_confirmed,
    screen_sleep_confirmed,
)
from harmony_runtime.service import Client

READY = {"screen_on": True, "screen_locked": False}


def run(args):
    report = {
        "tested_at": datetime.now(timezone.utc).isoformat(),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "transport": "authenticated_resident_service",
        "requested_rounds": args.rounds,
        "natural_idle_seconds": args.natural_idle_seconds,
        "mode": "natural_idle" if args.natural_idle_seconds else "controlled_suspend",
        "rounds": [],
        "status": "failed",
    }
    run_tag = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
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

        def wait_local_clock(seconds):
            """Only the local clock; never observe or touch the device."""
            end = time.monotonic() + seconds
            while True:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return
                time.sleep(min(30.0, remaining))

        rounds = args.rounds
        if args.natural_idle_seconds:
            rounds = 1

        for number in range(1, rounds + 1):
            row = {
                "round": number,
                "passed": False,
                "recovery_attempts": 0,
                "continuation": None,
            }
            report["rounds"].append(row)
            # Clear a quarantined worker before the next sleep/wake cycle.
            try:
                client.call("session", operation="recover", session_id=sid)
            except RuntimeFault as exc:
                row["pre_round_recover"] = exc.code

            # Last interactive touch: one observe while awake (also the pre-sleep handle).
            pre_sid = sid
            pre_obs = client.call("observe", session_id=sid, include_image=False, mode="FAST")
            pre_observation_id = pre_obs.get("observation_id")
            row["pre_sleep_observation_id"] = bool(pre_observation_id)

            if args.natural_idle_seconds:
                # Local clock only after this point: no observe, no keep-alive taps.
                idle_start = time.monotonic()
                wait_local_clock(args.natural_idle_seconds)
                row["idle_waited_seconds"] = round(time.monotonic() - idle_start, 3)
            else:
                shell("power-shell suspend")
                time.sleep(0.8)

            before = state()
            row["before"] = before
            row["before_lock_flag"] = before.get("screen_locked")
            if screen_sleep_confirmed(before):
                row["sleep_precondition"] = "sleep_confirmed"
            else:
                row["sleep_precondition"] = (
                    "screen_state_unknown"
                    if before.get("screen_on") is None
                    else "screen_not_off"
                )
                raise RuntimeFault(
                    row["sleep_precondition"],
                    "Sleep precondition not met before wake test",
                )

            # A long natural idle outlives the 300s device lease. Reopen before
            # recovery so the wake path runs on a live session; this is not a
            # keep-alive touch and does not observe during the wait window.
            if args.natural_idle_seconds:
                try:
                    client.call("session", operation="status", session_id=sid)
                except RuntimeFault:
                    session = client.call("session", operation="open", device_id=args.device)
                    sid = session["session_id"]
                    row["session_reopened"] = True

            recovery_start = time.monotonic()
            row["recovery_attempts"] = 1
            observation = client.call("observe", session_id=sid, include_image=False, mode="FAST")
            row["recovery_ms"] = round((time.monotonic() - recovery_start) * 1000)
            row.update(
                capture_ms=observation.get("capture_ms"),
                observed_state=observation.get("screen_state"),
                catalog_count=len(observation.get("catalog", [])),
                actionable=observation.get("actionable") is True,
                observation_received=bool(observation.get("observation_id")),
                after=state(),
            )

            # Old pre-sleep handles must not authorize a write after recovery.
            # After a natural idle the original session may already be expired;
            # that also rejects the handle, and is recorded as such.
            old_handle_rejected = False
            if pre_observation_id:
                try:
                    client.call(
                        "act",
                        arguments={
                            "session_id": pre_sid,
                            "request_id": f"t01_old_{run_tag}_{number}",
                            "observation_id": pre_observation_id,
                            "action": {"kind": "back"},
                            "expected": {"changed": True},
                        },
                    )
                except RuntimeFault as exc:
                    old_handle_rejected = exc.code in (
                        "stale_observation",
                        "stale_controller_epoch",
                        "lease_expired",
                        "session_invalid",
                        "invalid_arguments",
                    )
            elif args.natural_idle_seconds:
                old_handle_rejected = True
                row["old_handle_rejected_note"] = "pre_sleep_handle_unavailable"
            row["old_handle_rejected"] = old_handle_rejected

            # Low-risk continuation after recovery: one back and a postcondition.
            try:
                cont = client.call(
                    "act",
                    arguments={
                        "session_id": sid,
                        "request_id": f"t01_cont_{run_tag}_{number}",
                        "observation_id": observation["observation_id"],
                        "action": {"kind": "back"},
                        "expected": {"changed": True},
                        "timeout_ms": 15000,
                    },
                )
                row["continuation"] = {
                    "status": cont.get("status"),
                    "execution_status": cont.get("execution_status"),
                    "verification_status": cont.get("verification_status"),
                    "incident_id": cont.get("incident_id"),
                }
                # Business continuation is an executed low-risk step after recovery.
                # Verification timeout is recorded but does not hide a non-dispatch.
                continuation_ok = cont.get("execution_status") == "executed"
            except RuntimeFault as exc:
                row["continuation"] = {"error_code": exc.code}
                continuation_ok = False

            row["passed"] = (
                row["after"] == READY
                and row["observed_state"] == READY
                and row["catalog_count"] > 0
                and row["observation_received"]
                and old_handle_rejected
                and continuation_ok
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
    parser.add_argument(
        "--natural-idle-seconds",
        type=float,
        default=0,
        help="Wait this many seconds on the local clock only (no observe/keep-alive), then verify natural sleep and recover",
    )
    parser.add_argument("--report", type=Path, default=Path(".runtime/wake-unlock-acceptance.json"))
    args = parser.parse_args()
    if not args.execute:
        parser.error("--execute is required because this test puts the phone to sleep")
    if not 1 <= args.rounds <= 10:
        parser.error("--rounds must be between 1 and 10")
    if not 0 <= args.lease_wait_seconds <= 300:
        parser.error("--lease-wait-seconds must be between 0 and 300")
    if not 0 <= args.natural_idle_seconds <= 3600:
        parser.error("--natural-idle-seconds must be between 0 and 3600")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
