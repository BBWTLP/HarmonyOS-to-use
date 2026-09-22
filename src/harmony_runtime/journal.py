import hashlib
import json
import secrets
import shutil
from pathlib import Path
import sqlite3
import threading
import time
from .observation import canonical, matches
from .contracts import Expected
from .contracts import RuntimeFault


class Journal:
    def __init__(self, path):
        self.storage_root = Path(path).resolve().parent
        self.minimum_free_bytes = 64 * 1024 * 1024
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS actions (request_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, state TEXT NOT NULL, result TEXT, created REAL NOT NULL)")
        # Trusted non-execution evidence: 0 only while the request was admitted
        # but the worker never entered device dispatch. Historical rows default
        # to 1 so an old unknown cannot be closed as not_executed by attestation.
        try:
            self.db.execute("ALTER TABLE actions ADD COLUMN dispatch_started INTEGER NOT NULL DEFAULT 1")
        except sqlite3.OperationalError:
            pass
        self.db.execute("CREATE TABLE IF NOT EXISTS action_devices (request_id TEXT PRIMARY KEY, device_key TEXT NOT NULL)")
        self.db.execute("CREATE INDEX IF NOT EXISTS action_devices_key ON action_devices(device_key)")
        self.db.execute("CREATE TABLE IF NOT EXISTS recovery_conditions (request_id TEXT PRIMARY KEY, expected TEXT, before_fingerprint TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS incidents (incident_id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, status TEXT NOT NULL, created REAL NOT NULL, closed REAL, evidence TEXT)")
        self.db.execute("UPDATE actions SET state='execution_unknown' WHERE state='dispatching'")
        self.db.execute("INSERT OR IGNORE INTO incidents (incident_id,request_id,status,created) "
                        "SELECT lower(hex(randomblob(16))),request_id,'open',created FROM actions "
                        "WHERE state IN ('execution_unknown','unknown')")
        self.db.execute("CREATE TABLE IF NOT EXISTS bursts (request_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, device_key TEXT NOT NULL, state TEXT NOT NULL, result TEXT NOT NULL, created REAL NOT NULL)")
        self.db.execute("UPDATE bursts SET state='interrupted' WHERE state='running'")
        self.db.commit()

    def lookup(self, request_id, digest):
        with self.lock:
            if self.db.execute("SELECT 1 FROM bursts WHERE request_id=?", (request_id,)).fetchone():
                raise RuntimeFault("request_conflict", "request_id already used for a burst")
            row = self.db.execute("SELECT payload_hash,state,result FROM actions WHERE request_id=?", (request_id,)).fetchone()
        if not row: return None
        if row[0] != digest: raise RuntimeFault("request_conflict", "request_id already used with different arguments")
        if row[2]: return json.loads(row[2])
        return {"status":"execution_unknown", "execution_status":"unknown", "verification_status":"inconclusive", "message":"Previous dispatch has no durable result. Observe before deciding what to do; do not replay."}

    @staticmethod
    def device_key(serial):
        return hashlib.sha256(serial.encode("utf-8")).hexdigest()

    def unresolved(self, serial):
        """Device-bound uncertainty survives sessions and process restarts.

        Legacy records without a device binding conservatively apply to all devices.
        Return metadata only, never prior UI or user-entered text.
        """
        with self.lock:
            rows = self.db.execute(
                "SELECT a.request_id,a.state,a.created FROM actions a "
                "LEFT JOIN action_devices d ON a.request_id=d.request_id "
                "WHERE a.state IN ('dispatching','execution_unknown','unknown') "
                "AND (d.device_key=? OR d.request_id IS NULL) ORDER BY a.created",
                (self.device_key(serial),),
            ).fetchall()
        return [{"request_id":r[0], "execution_status":"unknown", "created":r[2]} for r in rows]

    def action_status(self, serial, request_id):
        """Read durable dispatch metadata without exposing stored observation payloads."""
        with self.lock:
            row = self.db.execute(
                "SELECT a.state,a.created,a.result FROM actions a "
                "JOIN action_devices d ON a.request_id=d.request_id "
                "WHERE a.request_id=? AND d.device_key=?",
                (request_id, self.device_key(serial)),
            ).fetchone()
        if row is None:
            return {"status": "not_found", "request_id": request_id,
                    "message": "No device-bound durable record found. This is not proof that an in-flight request will not dispatch."}
        state, created, stored = row
        result = json.loads(stored) if stored else {}
        return {"status": "recorded", "request_id": request_id, "created": created,
                "execution_status": "unknown" if state in ("dispatching", "execution_unknown", "unknown") else state,
                "verification_status": result.get("verification_status", "inconclusive"),
                "in_flight_or_interrupted": state == "dispatching",
                "message": "Journal evidence only; this query does not replay, verify current UI, or clear unresolved writes."}

    def require_reconciled(self, serial):
        if self.unresolved(serial):
            raise RuntimeFault("reconciliation_required", "A previous write has unknown execution. Inspect session status and fresh observations; do not issue a replacement write before reconciliation.")

    def _require_unused_id(self, request_id):
        # Recheck under the insertion lock: independent devices may both have
        # passed their earlier lookup before either reaches durable admission.
        if self.db.execute("SELECT 1 FROM actions WHERE request_id=? UNION ALL SELECT 1 FROM bursts WHERE request_id=?", (request_id, request_id)).fetchone():
            raise RuntimeFault("request_conflict", "request_id already durably admitted; query its status")

    @staticmethod
    def recovery_digest(text, salt):
        return hashlib.sha256((salt + "\0" + text).encode("utf-8")).hexdigest()

    @classmethod
    def recovery_record(cls, expected):
        if not expected:
            return None
        condition = Expected.model_validate(expected)
        # Only text-based, non-bundle conditions support automatic recovery.
        # A salted digest avoids retaining the original label, but is not encryption.
        if not condition.text or condition.bundle:
            return None
        salt = secrets.token_hex(32)
        return {"format": "text_digest_v1", "salt": salt,
                "text_digest": cls.recovery_digest(condition.text, salt),
                "changed": condition.changed}

    @classmethod
    def recovery_matches(cls, stored, observation, before):
        if not isinstance(stored, dict):
            return False
        if "format" in stored:
            if stored.get("format") != "text_digest_v1":
                return False
            if set(stored) != {"format", "salt", "text_digest", "changed"}:
                return False
            if type(stored["changed"]) is not bool:
                return False
            for field in ("salt", "text_digest"):
                value = stored[field]
                if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                    return False
            if stored.get("changed") and observation["fingerprint"] == before:
                return False
            # An unchanged page cannot prove this action caused the text: the
            # condition may already have held before dispatch.
            if observation.get("fingerprint") == before:
                return False
            return any(secrets.compare_digest(cls.recovery_digest(item["text"], stored["salt"]),
                                              stored["text_digest"])
                       for item in observation["catalog"])
        # Read compatibility for existing databases; no secure-erasure claim.
        condition = Expected.model_validate(stored)
        return bool(condition.text and not condition.bundle and matches(observation, condition, before))

    def require_storage(self):
        """Admission reserve only; cannot guarantee space remains after dispatch."""
        try:
            free = shutil.disk_usage(self.storage_root).free
        except OSError as exc:
            raise RuntimeFault("storage_unavailable", "Cannot inspect journal storage; no new action admitted") from exc
        if free < self.minimum_free_bytes:
            raise RuntimeFault("storage_pressure", "Journal volume has less than 64 MiB free; free space before issuing new actions")

    def begin(self, request_id, digest, serial=None, expected=None, before_fingerprint=None):
        recovery = self.recovery_record(expected)
        with self.lock, self.db:
            self.require_storage()
            self._require_unused_id(request_id)
            if serial is not None:
                self.require_reconciled(serial)
            self.db.execute(
                "INSERT INTO actions (request_id, payload_hash, state, result, created, dispatch_started) "
                "VALUES (?,?,?,?,?,0)",
                (request_id, digest, "dispatching", None, time.time()))
            self.db.execute("INSERT INTO recovery_conditions VALUES (?,?,?)", (request_id, canonical(recovery) if recovery else None, before_fingerprint))
            if serial is not None:
                self.db.execute("INSERT INTO action_devices VALUES (?,?)", (request_id,self.device_key(serial)))

    def mark_dispatch_started(self, request_id):
        """Record that the worker is about to enter device dispatch.

        After this point non-execution cannot be attested: the write may have
        reached the device even if the UI fingerprint later matches again.
        """
        with self.lock, self.db:
            self.db.execute(
                "UPDATE actions SET dispatch_started=1 WHERE request_id=?",
                (request_id,))

    @staticmethod
    def stored_result(result):
        """Persist execution metadata, excluding live UI and arbitrary extra payloads."""
        keys = ("status", "execution_status", "verification_status", "before_observation_id",
                "after_observation_id", "incident_id", "request_id", "index", "verified_steps",
                "stop_reason", "planned_request_ids")
        stored = {key: result[key] for key in keys if key in result}
        if "timing" in result:
            stored["timing"] = {key: value for key, value in result["timing"].items()
                                if key.endswith("_ms") and type(value) in (int, float)}
        if "steps" in result:
            stored["steps"] = [Journal.stored_result(step) for step in result["steps"]]
        stored["observation_retained"] = False
        return stored

    def finish(self, request_id, result):
        with self.lock, self.db:
            if result.get("incident_id"):
                self.db.execute("INSERT OR IGNORE INTO incidents VALUES (?,?,'open',?,NULL,NULL)",
                                (result["incident_id"], request_id, time.time()))
            self.db.execute("UPDATE actions SET state=?,result=? WHERE request_id=?",
                            (result["execution_status"], canonical(self.stored_result(result)), request_id))

    def burst_lookup(self, request_id, digest):
        with self.lock:
            if self.db.execute("SELECT 1 FROM actions WHERE request_id=?", (request_id,)).fetchone():
                raise RuntimeFault("request_conflict", "request_id already used for an action")
            row = self.db.execute("SELECT payload_hash,state,result FROM bursts WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            return None
        if row[0] != digest:
            raise RuntimeFault("request_conflict", "request_id already used with different burst arguments")
        result = json.loads(row[2])
        if row[1] != "finished":
            result.update(status="interrupted", stop_reason="reconciliation_required",
                          message="This sequence will not resume or replay. Query every planned child request and observe before replanning.")
        return result

    def begin_burst(self, request_id, digest, serial, result):
        with self.lock, self.db:
            self.require_storage()
            self._require_unused_id(request_id)
            self.require_reconciled(serial)
            self.db.execute("INSERT INTO bursts VALUES (?,?,?,'running',?,?)",
                            (request_id, digest, self.device_key(serial), canonical(self.stored_result(result)), time.time()))

    def save_burst(self, request_id, result, finished=False):
        with self.lock, self.db:
            self.db.execute("UPDATE bursts SET state=?,result=? WHERE request_id=?",
                            ("finished" if finished else "running", canonical(self.stored_result(result)), request_id))

    def burst_status(self, serial, request_id):
        with self.lock:
            row = self.db.execute("SELECT state,result FROM bursts WHERE request_id=? AND device_key=?",
                                  (request_id, self.device_key(serial))).fetchone()
        if row is None:
            return {"status": "not_found", "request_id": request_id,
                    "message": "No device-bound burst record. This is not proof of non-execution."}
        stored = json.loads(row[1])
        children = [self.action_status(serial, child) for child in stored["planned_request_ids"]]
        return {"status": "recorded", "request_id": request_id, "state": row[0],
                "stop_reason": stored.get("stop_reason"), "children": children,
                "message": "Durable metadata only. A running or interrupted burst is never replayed by this query."}

    def incidents(self, serial):
        with self.lock:
            rows = self.db.execute(
                "SELECT i.incident_id,i.request_id,i.status,i.created,i.closed,i.evidence "
                "FROM incidents i JOIN action_devices d ON i.request_id=d.request_id "
                "WHERE d.device_key=? ORDER BY i.created", (self.device_key(serial),)).fetchall()
        return [{"incident_id": r[0], "request_id": r[1], "status": r[2],
                 "created": r[3], "closed": r[4], "evidence": json.loads(r[5]) if r[5] else None} for r in rows]

    def reconcile_verified(self, serial, observation):
        """Close only executed actions whose original semantic condition now holds.

        Unknown dispatch remains unknown. A changed-only condition cannot distinguish
        recovery from unrelated navigation, and never automatically closes an incident.
        Historical action results remain immutable; closure has its own evidence.
        """
        closed = []
        with self.lock, self.db:
            rows = self.db.execute(
                "SELECT i.incident_id,c.expected,c.before_fingerprint,i.created FROM incidents i "
                "JOIN actions a ON i.request_id=a.request_id "
                "JOIN action_devices d ON i.request_id=d.request_id "
                "JOIN recovery_conditions c ON i.request_id=c.request_id "
                "WHERE i.status='open' AND a.state='executed' AND d.device_key=?",
                (self.device_key(serial),)).fetchall()
            for incident_id, stored, before, created in rows:
                if not stored or observation.get("captured_at", 0) < created:
                    continue
                if not self.recovery_matches(json.loads(stored), observation, before):
                    continue
                evidence = {"observation_id": observation["observation_id"],
                            "fingerprint": observation["fingerprint"],
                            "captured_at": observation["captured_at"],
                            "reason": "original_text_postcondition_observed"}
                self.db.execute("UPDATE incidents SET status='closed',closed=?,evidence=? WHERE incident_id=?",
                                (time.time(), canonical(evidence), incident_id))
                closed.append(incident_id)
        return closed

    #: Evidence kinds an operator or agent may use to close one unknown write.
    RECONCILIATION_KINDS = ("postcondition_verified", "not_executed")

    def close_incident_with_evidence(self, serial, request_id, observation, *,
                                     evidence_kind, attestation=None):
        """Close one unknown write against fresh evidence, never by replay.

        Two kinds are accepted, and both need an observation newer than the
        incident:

        * ``postcondition_verified``: the action's *original* semantic
          postcondition currently holds (salted digest match), so the write is
          known to have taken effect.
        * ``not_executed``: only with a trusted journal record that the worker
          never entered device dispatch (``dispatch_started == 0``). A free-text
          attestation is recorded for audit but is never sufficient: an
          unchanged page fingerprint cannot prove non-execution once dispatch
          may have run. Without that trusted record this kind is refused.

        Anything else — a changed page without the postcondition, a stale
        observation, a missing pre-dispatch fingerprint — is refused. The action
        row keeps its original unknown result; closure is recorded as separate
        evidence, and no device call is made here.
        """
        if evidence_kind not in self.RECONCILIATION_KINDS:
            raise RuntimeFault("invalid_arguments",
                               "evidence_kind must be postcondition_verified or not_executed")
        if len(request_id or "") > 128 or not request_id:
            raise RuntimeFault("invalid_arguments", "request_id is required")
        if evidence_kind == "not_executed" and not (attestation or "").strip():
            raise RuntimeFault("attestation_required",
                               "not_executed reconciliation requires an attestation string")
        if attestation is not None and len(attestation) > 300:
            raise RuntimeFault("invalid_arguments", "attestation must be at most 300 characters")
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT i.incident_id,i.status,i.created,c.expected,c.before_fingerprint,"
                "a.dispatch_started "
                "FROM incidents i JOIN actions a ON i.request_id=a.request_id "
                "JOIN action_devices d ON i.request_id=d.request_id "
                "LEFT JOIN recovery_conditions c ON i.request_id=c.request_id "
                "WHERE i.request_id=? AND d.device_key=?",
                (request_id, self.device_key(serial))).fetchone()
            if row is None:
                # An unbound legacy record is deliberately not closeable per device.
                raise RuntimeFault("unknown_request",
                                   "No device-bound incident exists for this request_id")
            incident_id, status, created, stored, before, dispatch_started = row
            if status != "open":
                raise RuntimeFault("incident_already_closed",
                                   "This incident already has a closure record")
            if observation is None or observation.get("captured_at", 0) < created:
                raise RuntimeFault("evidence_stale",
                                   "A fresh observation captured after the incident is required")
            if evidence_kind == "postcondition_verified":
                if not stored or not self.recovery_matches(json.loads(stored), observation,
                                                           before):
                    raise RuntimeFault(
                        "evidence_insufficient",
                        "The original postcondition does not hold in this observation")
                reason = "original_text_postcondition_observed"
                state = "reconciled_verified"
            else:
                # Trusted non-execution requires a durable "never entered
                # dispatch" record bound to this request. Attestation and an
                # unchanged fingerprint are not sufficient once dispatch may
                # have reached the device.
                if dispatch_started:
                    raise RuntimeFault(
                        "evidence_insufficient",
                        "Dispatch was entered; non-execution cannot be attested "
                        "from a free-text statement or an unchanged page")
                reason = "trusted_never_dispatched"
                state = "reconciled_not_executed"
            evidence = {"observation_id": observation.get("observation_id"),
                        "fingerprint": observation.get("fingerprint"),
                        "captured_at": observation.get("captured_at"),
                        "reason": reason, "evidence_kind": evidence_kind,
                        "attestation": attestation, "closed_at": time.time()}
            self.db.execute(
                "UPDATE incidents SET status='closed',closed=?,evidence=? WHERE incident_id=?",
                (time.time(), canonical(evidence), incident_id))
            self.db.execute("UPDATE actions SET state=? WHERE request_id=?",
                            (state, request_id))
        return {"request_id": request_id, "incident_id": incident_id,
                "status": "closed", "action_state": state, "evidence": evidence,
                "replay": "never", "message": "Closure is recorded evidence; the original "
                                              "dispatch is never replayed"}

    def device_history(self, serial, limit=20, before=None):
        """Page durable action metadata; never return stored UI, input or recovery text."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise RuntimeFault("invalid_arguments", "limit must be an integer from 1 to 100")
        if before is not None and (type(before) is not int or before <= 0 or before > 9223372036854775807):
            raise RuntimeFault("invalid_arguments", "before must be a positive journal cursor")
        with self.lock:
            rows = self.db.execute(
                "SELECT a.rowid,a.request_id,a.state,a.created,a.result,i.incident_id,i.status,i.closed "
                "FROM actions a JOIN action_devices d ON a.request_id=d.request_id "
                "LEFT JOIN incidents i ON i.request_id=a.request_id "
                "WHERE d.device_key=? AND (? IS NULL OR a.rowid<?) "
                "ORDER BY a.rowid DESC LIMIT ?",
                (self.device_key(serial), before, before, limit + 1)).fetchall()
        items = []
        for row in rows[:limit]:
            stored = json.loads(row[4]) if row[4] else {}
            verification = stored.get("verification_status", "inconclusive")
            if verification not in ("verified", "failed", "inconclusive", "not_requested"):
                verification = "inconclusive"
            items.append({"request_id": row[1], "created": row[3],
                          "execution_status": "unknown" if row[2] in ("dispatching", "execution_unknown", "unknown") else row[2],
                          "verification_status": verification,
                          "incident": {"incident_id": row[5], "status": row[6], "closed": row[7]} if row[5] else None})
        return {"items": items, "next_before": rows[limit-1][0] if len(rows) > limit else None,
                "evidence": "durable_metadata",
                "message": "Recorded actions only; omitted requests may not have reached dispatch admission. History never replays actions or proves current UI state."}

    def history(self, limit=20):
        with self.lock:
            return [{"request_id":r[0],"state":r[1],"created":r[2]} for r in self.db.execute("SELECT request_id,state,created FROM actions ORDER BY created DESC LIMIT ?", (min(max(limit,1),100),))]

    def close(self): self.db.close()
