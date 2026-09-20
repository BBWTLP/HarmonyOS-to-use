"""Bounded artifact store for screenshots, events and evidence indexes.

Only files inside the configured root are written or deleted. Unresolved writes,
reconciliation records and dedupe tombstones are never removed by ordinary TTL,
because deleting them would let a retry re-execute a device action.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id TEXT PRIMARY KEY,
  task_id TEXT,
  kind TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  sensitivity TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL
);
CREATE INDEX IF NOT EXISTS artifacts_expiry ON artifacts(expires_at);
CREATE INDEX IF NOT EXISTS artifacts_task ON artifacts(task_id);
"""

#: Never reclaimed by TTL: a lost record would allow a replay.
PINNED_KINDS = ("unresolved_action", "reconciliation", "dedupe_tombstone", "approval")


@dataclass(frozen=True)
class Retention:
    image_days: int = 7
    event_days: int = 30
    quota_bytes: int = 2 * 1024 * 1024 * 1024
    floor_bytes: int = 64 * 1024 * 1024


class ArtifactStore:
    def __init__(self, root: str | Path, retention: Retention | None = None):
        self.root = Path(root).resolve()
        (self.root / "blobs").mkdir(parents=True, exist_ok=True)
        self.retention = retention or Retention()
        # The task runner writes evidence from its own thread, so the store is
        # explicitly serialised instead of relying on thread-local connections.
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(self.root / "artifacts.sqlite3",
                                          check_same_thread=False)
        with self.lock:
            self.connection.executescript(SCHEMA)
            self.connection.commit()

    def close(self) -> None:
        with self.lock:
            self.connection.close()

    # -- writes -------------------------------------------------------------
    def put_blob(self, data: bytes, *, kind: str, task_id: str | None = None,
                 sensitivity: str = "private", suffix: str = ".bin",
                 ttl_days: int | None = None) -> dict[str, Any]:
        digest = hashlib.sha256(data).hexdigest()
        artifact_id = "art_" + uuid.uuid4().hex[:24]
        relative = Path("blobs") / f"{artifact_id}{suffix}"
        temporary = self.root / relative.with_suffix(relative.suffix + ".part")
        final = self.root / relative
        self._guard(final)
        with open(temporary, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, final)
        ttl = ttl_days if ttl_days is not None else (
            self.retention.image_days if kind == "image" else self.retention.event_days)
        expires = None if kind in PINNED_KINDS else time.time() + ttl * 86400
        with self.lock:
            self.connection.execute(
                "INSERT INTO artifacts (artifact_id, task_id, kind, relative_path, sha256,"
                " bytes, sensitivity, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (artifact_id, task_id, kind, relative.as_posix(), digest, len(data),
                 sensitivity, time.time(), expires))
            self.connection.commit()
        return {"artifact_id": artifact_id, "sha256": digest, "bytes": len(data),
                "kind": kind, "relative_path": relative.as_posix(),
                "expires_at": expires}

    def put_json(self, payload: dict[str, Any], *, kind: str, task_id: str | None = None,
                 sensitivity: str = "private", ttl_days: int | None = None) -> dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return self.put_blob(data, kind=kind, task_id=task_id, sensitivity=sensitivity,
                             suffix=".json", ttl_days=ttl_days)

    # -- reads --------------------------------------------------------------
    def get(self, artifact_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT artifact_id, task_id, kind, relative_path, sha256, bytes, sensitivity,"
                " created_at, expires_at FROM artifacts WHERE artifact_id=?",
                (artifact_id,)).fetchone()
        if row is None:
            return None
        return self._row(row)

    def read(self, artifact_id: str) -> bytes | None:
        record = self.get(artifact_id)
        if record is None:
            return None
        path = self.root / record["relative_path"]
        self._guard(path)
        if not path.is_file():
            return None
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != record["sha256"]:
            raise RuntimeError("artifact hash mismatch")
        return data

    def index(self, *, task_id: str | None = None, limit: int = 100,
              offset: int = 0) -> dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        where, params = ("WHERE task_id=?", (task_id,)) if task_id else ("", ())
        with self.lock:
            rows = self.connection.execute(
                "SELECT artifact_id, task_id, kind, relative_path, sha256, bytes, sensitivity,"
                " created_at, expires_at FROM artifacts " + where +
                " ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*params, limit, max(0, int(offset)))).fetchall()
            total = self.connection.execute(
                f"SELECT COUNT(*) FROM artifacts {where}", params).fetchone()[0]
        items = []
        for row in rows:
            record = self._row(row)
            record["present"] = (self.root / record["relative_path"]).is_file()
            items.append(record)
        return {"items": items, "total": total, "limit": limit, "offset": max(0, int(offset))}

    def quota(self) -> dict[str, int]:
        with self.lock:
            used = self.connection.execute(
                "SELECT COALESCE(SUM(bytes),0) FROM artifacts").fetchone()[0]
        return {"used_bytes": int(used), "quota_bytes": self.retention.quota_bytes,
                "floor_bytes": self.retention.floor_bytes,
                "free_bytes": int(self.retention.quota_bytes - used)}

    def has_room(self, additional_bytes: int) -> bool:
        state = self.quota()
        return state["used_bytes"] + additional_bytes <= state["quota_bytes"]

    # -- maintenance --------------------------------------------------------
    def sweep(self, now: float | None = None, dry_run: bool = False) -> dict[str, Any]:
        """Delete only expired, non-pinned artifacts inside the store root."""
        moment = time.time() if now is None else now
        placeholders = ",".join("?" for _ in PINNED_KINDS)
        with self.lock:
            rows = self.connection.execute(
                f"SELECT artifact_id, relative_path FROM artifacts"
                f" WHERE expires_at IS NOT NULL AND expires_at <= ?"
                f" AND kind NOT IN ({placeholders})",
                (moment, *PINNED_KINDS)).fetchall()
        removed, missing, bytes_freed = [], [], 0
        for artifact_id, relative in rows:
            path = self.root / relative
            self._guard(path)
            deleted = False
            if path.is_file():
                bytes_freed += path.stat().st_size
                if not dry_run:
                    path.unlink()
                deleted = True
            else:
                missing.append(artifact_id)
            removed.append({"artifact_id": artifact_id, "deleted_file": deleted})
            if not dry_run:
                with self.lock:
                    self.connection.execute("DELETE FROM artifacts WHERE artifact_id=?",
                                            (artifact_id,))
        with self.lock:
            if not dry_run:
                self.connection.commit()
            pinned = self.connection.execute(
                f"SELECT COUNT(*) FROM artifacts WHERE kind IN ({placeholders})",
                PINNED_KINDS).fetchone()[0]
        return {"swept": len(removed), "bytes_freed": bytes_freed, "missing_files": missing,
                "pinned_retained": int(pinned), "dry_run": dry_run}

    def redacted_export(self, task_id: str) -> dict[str, Any]:
        """Metadata-only export: no UI text, no input values, no image bytes."""
        index = self.index(task_id=task_id, limit=200)
        return {"task_id": task_id, "generated_at": datetime.now(timezone.utc).isoformat(),
                "quota": self.quota(),
                "items": [{key: item[key] for key in
                           ("artifact_id", "kind", "sha256", "bytes", "sensitivity",
                            "created_at", "expires_at", "present")} for item in index["items"]]}

    def _row(self, row) -> dict[str, Any]:
        keys = ("artifact_id", "task_id", "kind", "relative_path", "sha256", "bytes",
                "sensitivity", "created_at", "expires_at")
        record = dict(zip(keys, row))
        if record["expires_at"]:
            record["expires_at_iso"] = datetime.fromtimestamp(
                record["expires_at"], timezone.utc).isoformat()
        else:
            record["expires_at_iso"] = None
        record["expired"] = bool(record["expires_at"] and record["expires_at"] <= time.time())
        return record

    def _guard(self, path: Path) -> None:
        resolved = path.resolve()
        if self.root != resolved and self.root not in resolved.parents:
            raise ValueError("artifact path escapes the store root")


def default_retention_from_config(config: dict[str, Any] | None) -> Retention:
    config = config or {}
    artifacts = config.get("artifacts", {})
    return Retention(
        image_days=int(artifacts.get("image_retention_days", 7)),
        event_days=int(artifacts.get("event_retention_days", 30)),
        quota_bytes=int(artifacts.get("quota_mib", 2048)) * 1024 * 1024,
    )
