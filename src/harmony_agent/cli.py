"""Read-only CLI for tasks, events, results and the evidence index (H04).

Every command opens the state databases read-only, never constructs a device
driver and never dispatches an action. Output is bounded and paginated, missing
evidence is reported as missing instead of being hidden, and nothing binds to a
public interface.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

MAX_LIMIT = 200
DEFAULT_LIMIT = 20


def _open_readonly(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _task_db(state_dir: Path) -> Path:
    return state_dir / "agent-tasks.sqlite3"


def _artifact_db(state_dir: Path) -> Path:
    return state_dir / "artifacts" / "artifacts.sqlite3"


def list_tasks(state_dir: Path, limit: int, offset: int) -> dict[str, Any]:
    connection = _open_readonly(_task_db(state_dir))
    if connection is None:
        return {"status": "not_found", "message": "No agent task database in this state dir",
                "state_dir": str(state_dir)}
    try:
        rows = connection.execute(
            "SELECT task_id, request_key, state, created, updated, result IS NOT NULL AS has_result"
            " FROM tasks ORDER BY created DESC LIMIT ? OFFSET ?",
            (limit, offset)).fetchall()
        total = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    finally:
        connection.close()
    return {"status": "ok", "total": total, "limit": limit, "offset": offset,
            "items": [{"task_id": row["task_id"], "request_key": row["request_key"],
                       "state": row["state"], "created": row["created"],
                       "updated": row["updated"], "has_result": bool(row["has_result"])}
                      for row in rows],
            "note": "Read-only view. This command never dispatches a phone action."}


def task_detail(state_dir: Path, task_id: str, *, events: int, include_result: bool,
                after_seq: int = 0) -> dict[str, Any]:
    connection = _open_readonly(_task_db(state_dir))
    if connection is None:
        return {"status": "not_found", "message": "No agent task database in this state dir"}
    try:
        row = connection.execute(
            "SELECT task_id, request_key, state, controller_epoch, created, updated,"
            " payload, result FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            return {"status": "not_found", "task_id": task_id,
                    "message": "Unknown task id"}
        event_rows = connection.execute(
            "SELECT sequence, type, payload, created FROM task_events"
            " WHERE task_id=? AND sequence>? ORDER BY sequence LIMIT ?",
            (task_id, after_seq, events)).fetchall()
        last_seq = connection.execute(
            "SELECT COALESCE(MAX(sequence),0) FROM task_events WHERE task_id=?",
            (task_id,)).fetchone()[0]
    finally:
        connection.close()
    payload = json.loads(row["payload"])
    detail: dict[str, Any] = {
        "status": "ok", "task_id": row["task_id"], "request_key": row["request_key"],
        "state": row["state"], "controller_epoch": row["controller_epoch"],
        "created": row["created"], "updated": row["updated"],
        "mode": payload.get("mode"),
        "goal_present": bool(payload.get("goal")),
        "scope": (payload.get("scope") or {}).get("allowed_apps"),
        "budget": payload.get("budget"),
        "events": [{"sequence": item["sequence"], "type": item["type"],
                    "created": item["created"], "payload": json.loads(item["payload"])}
                   for item in event_rows],
        "next_seq": event_rows[-1]["sequence"] if event_rows else after_seq,
        "last_seq": int(last_seq),
    }
    if include_result:
        detail["result"] = json.loads(row["result"]) if row["result"] else None
    return detail


def evidence_index(state_dir: Path, *, task_id: str | None, limit: int,
                   offset: int) -> dict[str, Any]:
    connection = _open_readonly(_artifact_db(state_dir))
    if connection is None:
        return {"status": "not_found",
                "message": "No artifact store in this state dir",
                "state_dir": str(state_dir)}
    try:
        where, params = ("WHERE task_id=?", (task_id,)) if task_id else ("", ())
        rows = connection.execute(
            "SELECT artifact_id, task_id, kind, relative_path, sha256, bytes, sensitivity,"
            f" created_at, expires_at FROM artifacts {where} ORDER BY created_at DESC"
            " LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
        total = connection.execute(f"SELECT COUNT(*) FROM artifacts {where}",
                                   params).fetchone()[0]
    finally:
        connection.close()
    blobs = state_dir / "artifacts"
    items = []
    for row in rows:
        path = blobs / row["relative_path"]
        items.append({
            "artifact_id": row["artifact_id"], "task_id": row["task_id"],
            "kind": row["kind"], "sha256": row["sha256"], "bytes": row["bytes"],
            "sensitivity": row["sensitivity"], "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            # A missing file is reported, never silently skipped.
            "present": path.is_file(),
            "missing": not path.is_file(),
        })
    return {"status": "ok", "total": total, "limit": limit, "offset": offset,
            "items": items,
            "missing_files": sum(1 for item in items if item["missing"]),
            "note": "Metadata only: no UI text, no input values, no images. Replay is read-only."}


def replay(state_dir: Path, task_id: str, *, limit: int) -> dict[str, Any]:
    """Reconstruct the recorded timeline without touching the device."""
    detail = task_detail(state_dir, task_id, events=limit, include_result=False)
    if detail.get("status") != "ok":
        return detail
    index = evidence_index(state_dir, task_id=task_id, limit=limit, offset=0)
    return {
        "status": "ok", "task_id": task_id, "state": detail["state"],
        "timeline": detail["events"], "next_seq": detail["next_seq"],
        "last_seq": detail["last_seq"],
        "evidence": index.get("items", []),
        "evidence_missing": index.get("missing_files", 0),
        "read_only": True,
        "note": ("Replay reads durable metadata only; it does not replay or reissue any "
                 "device action, and it is not reachable from a public interface."),
    }


def main(argv: list[str], state_dir: str | Path) -> int:
    parser = argparse.ArgumentParser(prog="agent", description="Read-only agent task tooling")
    sub = parser.add_subparsers(dest="command", required=True)

    tasks = sub.add_parser("tasks", help="List recent tasks (newest first)")
    tasks.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    tasks.add_argument("--offset", type=int, default=0)

    detail = sub.add_parser("task", help="Show one task with its events")
    detail.add_argument("--task-id", required=True)
    detail.add_argument("--events", type=int, default=DEFAULT_LIMIT)
    detail.add_argument("--after-seq", type=int, default=0)
    detail.add_argument("--result", action="store_true", help="Include the final result")

    evidence = sub.add_parser("artifacts", help="List the evidence index")
    evidence.add_argument("--task-id")
    evidence.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    evidence.add_argument("--offset", type=int, default=0)

    replay_cmd = sub.add_parser("replay", help="Read-only timeline replay for one task")
    replay_cmd.add_argument("--task-id", required=True)
    replay_cmd.add_argument("--limit", type=int, default=DEFAULT_LIMIT)

    args = parser.parse_args(argv)
    root = Path(state_dir)
    for name, value in (("limit", getattr(args, "limit", DEFAULT_LIMIT)),
                        ("events", getattr(args, "events", DEFAULT_LIMIT))):
        if value is not None and not 1 <= value <= MAX_LIMIT:
            print(json.dumps({"status": "invalid_arguments",
                              "message": f"--{name} must be between 1 and {MAX_LIMIT}"},
                             ensure_ascii=False))
            return 2
    if getattr(args, "offset", 0) < 0:
        print(json.dumps({"status": "invalid_arguments",
                          "message": "--offset must not be negative"}, ensure_ascii=False))
        return 2
    if args.command == "tasks":
        payload = list_tasks(root, args.limit, args.offset)
    elif args.command == "task":
        payload = task_detail(root, args.task_id, events=args.events,
                              include_result=args.result, after_seq=args.after_seq)
    elif args.command == "artifacts":
        payload = evidence_index(root, task_id=args.task_id, limit=args.limit,
                                 offset=args.offset)
    else:
        payload = replay(root, args.task_id, limit=args.limit)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    if payload.get("status") == "ok":
        return 0
    return 1 if payload.get("status") == "not_found" else 2
