import hashlib
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from contextlib import contextmanager, nullcontext
from pathlib import Path
from .contracts import ActRequest, BurstRequest, Expected, WaitCondition, RuntimeFault
from .device import HarmonyDevice
from .device_queue import DeviceQueue
from .device_worker import ProcessDevice
from .journal import Journal
from .observation import canonical, matches, resolve, snapshot, input_value_matches
from .snapshot import (ProviderState, capture_after_readiness, capture_snapshot,
                       provider_choice, readiness_probe)
from .target_identity import AMBIGUOUS, StableTargetMatcher
from .risk import is_sensitive, label_of, scan
from .visual import encode_image, mark_targets
from .timing import Timings


@dataclass
class Session:
    id: str
    owner: str
    serial: str
    expires: float
    paused: threading.Event = field(default_factory=threading.Event)
    observations: dict = field(default_factory=dict)
    generation: int = 0
    closed: bool = False
    #: Cross-observation target re-identification outcomes (RC4-A diagnostics).
    target_match_counts: dict = field(default_factory=dict)
    #: Bounded, metadata-only stale accounting for the current run.
    stale_streak: int = 0
    last_stale_reason: str | None = None


class Runtime:
    """One serialized device worker per serial; session tokens are owner-bound."""
    def __init__(self, state_dir, factory=HarmonyDevice, discover=None, lease_seconds=300):
        self.root = Path(state_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.journal = Journal(self.root / "journal.sqlite3")
        self.supports_foreground = getattr(factory, "supports_foreground", False) is True
        self.factory = ProcessDevice if factory is HarmonyDevice else factory
        self.discover = discover or HarmonyDevice.discover
        self.lease_seconds = lease_seconds
        self.matcher = StableTargetMatcher()
        self.guard = threading.RLock()
        self.shutdown = threading.Condition(self.guard)
        self.resources_closed = False
        self.close_error = None
        self.sessions = {}
        self.devices = {}
        self.device_locks = {}
        self.active = {}
        self.stopped = False
        #: Batched/legacy capture selection and its (bounded) fallback record.
        self.provider_state = ProviderState()

    def session(self, owner, operation, session_id=None, device_id=None, request_id=None,
                evidence_kind=None, attestation=None):
        if operation in ("action_status", "burst_status"):
            if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
                raise RuntimeFault("invalid_arguments", "Status queries require request_id (1–128 characters)")
            if evidence_kind is not None or attestation is not None:
                raise RuntimeFault("invalid_arguments",
                                   "evidence_kind and attestation belong to the reconcile operation")
            with self.guard:
                s = self._session(owner, session_id, allow_paused=True)
                return getattr(self.journal, operation)(s.serial, request_id)
        if operation == "reconcile":
            return self.reconcile(owner, session_id, request_id,
                                  evidence_kind=evidence_kind, attestation=attestation)
        if evidence_kind is not None or attestation is not None:
            raise RuntimeFault("invalid_arguments",
                               "evidence_kind and attestation belong to the reconcile operation")
        if request_id is not None:
            raise RuntimeFault("invalid_arguments", "request_id is only supported for action_status or burst_status")
        if operation == "recover":
            return self.recover(owner, session_id)
        # Device discovery shells out to HDC and may be slow or blocked. It must
        # never run while holding the runtime lock, or one stuck discovery would
        # stall every other client's calls.
        if operation == "open" and (not device_id or device_id == "auto"):
            devices = self.discover()
            if len(devices) != 1:
                raise RuntimeFault("device_selection_required",
                                   f"Select exactly one device from: {devices}")
            device_id = devices[0]
        with self.guard:
            if self.stopped: raise RuntimeFault("runtime_stopped", "Runtime is shutting down")
            if operation == "open":
                for s in self.sessions.values():
                    if s.serial == device_id and (s.expires > time.monotonic() or s.id in self.active):
                        if s.closed: raise RuntimeFault("device_busy", "Closing session still has an in-flight operation")
                        if s.owner != owner: raise RuntimeFault("lease_conflict", "Device already leased by another client")
                        s.expires = time.monotonic() + self.lease_seconds
                        return self._session_result(s)
                s = Session(uuid.uuid4().hex, owner, device_id, time.monotonic()+self.lease_seconds)
                self.sessions[s.id] = s
                self.device_locks.setdefault(device_id, DeviceQueue())
                # Device is initialized on observe/act, never during protocol discovery.
                return self._session_result(s)
            s = self._session(owner, session_id, allow_paused=True)
            if operation == "pause":
                s.generation += 1
                s.paused.set()
                s.observations.clear()
            elif operation == "resume":
                s.generation += 1
                s.paused.clear()
                s.observations.clear()
            elif operation == "close":
                s.paused.set()
                s.closed = True
                s.generation += 1
                s.observations.clear()
                if s.id not in self.active:
                    del self.sessions[s.id]
                return {"status":"closed", "session_id":s.id}
            elif operation != "status": raise RuntimeFault("invalid_arguments", "Unknown session operation")
            return self._session_result(s)

    def history(self, owner, session_id, limit=20, before=None):
        with self.guard:
            s = self._session(owner, session_id, allow_paused=True)
            return self.journal.device_history(s.serial, limit, before)

    def cached_observation(self, owner, session_id, observation_id):
        """Return a live cached observation handle, or None when it is gone.

        Handles are only ever created by observe(); this accessor exists for the
        service-side agent layer, which must reuse the same authority the guard
        will check at dispatch time.
        """
        with self.guard:
            s = self._session(owner, session_id)
            entry = s.observations.get(observation_id)
            if entry is None or time.monotonic() - entry[0] > 15:
                return None
            return entry[1]

    def _session_result(self, s):
        unresolved = self.journal.unresolved(s.serial)
        device_state = self._device_state(s.serial)
        return {"status":"paused" if s.paused.is_set() else "open", "session_id":s.id, "device_id":s.serial, "controller_epoch":s.generation, "device_state":device_state, "worker_quarantined":device_state == "quarantined", "lease_remaining_seconds":max(0,round(s.expires-time.monotonic())), "recovery_required":bool(unresolved), "unresolved_actions":unresolved, "incidents":self.journal.incidents(s.serial), "capabilities":{"tree":True,"screenshot":True,"full":True,"som":True,"burst":True,"foreground_bundle":self.supports_foreground,"ocr":False,"webview":False,"temporal":True,"temporal_watch":True,"grounded_target":True,"pro":False}, "target_match_counts":dict(s.target_match_counts),
                "capture": {**self.provider_state.as_dict(),
                            "stale_consecutive_count": s.stale_streak,
                            "last_stale_reason": s.last_stale_reason}}

    def _device_state(self, serial):
        """Expose worker quarantine so a client can recover instead of retrying.

        A quarantined worker is a *transport* state, not an unresolved write, so
        it is reported separately from `recovery_required`.
        """
        device = self.devices.get(serial)
        return "quarantined" if getattr(device, "quarantined", False) else "ready"

    def _session(self, owner, session_id, allow_paused=False):
        with self.guard:
            s = self.sessions.get(session_id)
            if self.stopped: raise RuntimeFault("runtime_stopped", "Runtime is shutting down")
            if s is None or s.owner != owner or s.closed: raise RuntimeFault("session_invalid", "Unknown session or wrong client")
            if s.expires <= time.monotonic() and s.id not in self.active: raise RuntimeFault("lease_expired", "Open a new session and observe again")
            if s.paused.is_set() and not allow_paused: raise RuntimeFault("cancelled", "Session paused")
            s.expires = time.monotonic()+self.lease_seconds
            return s

    def _check_generation(self, owner, s, generation):
        with self.guard:
            self._session(owner, s.id)
            if s.generation != generation:
                raise RuntimeFault("cancelled", "Request was cancelled by a session transition")

    @contextmanager
    def _worker(self, owner, s, deadline, generation=None, timing=None):
        if generation is None:
            with self.guard:
                self._session(owner, s.id)
                generation = s.generation
        check = lambda: self._check_generation(owner, s, generation)
        lock = self.device_locks[s.serial]
        timing = timing if timing is not None else Timings()
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not timing.call("queue", lock.acquire, timeout=remaining, check=check):
            raise RuntimeFault("timeout", "Budget expired in device queue; no action dispatched")
        try:
            with self.guard:
                check()
                self.active[s.id] = True
            device = timing.call("worker_setup", self._device, s)
            scope = device.budget(deadline, check) if isinstance(device, ProcessDevice) else nullcontext()
            try:
                with scope:
                    yield check
            finally:
                current_device = self.devices.get(s.serial)
                if isinstance(current_device, ProcessDevice) and current_device.quarantined:
                    with self.guard:
                        s.observations.clear()
        finally:
            with self.guard:
                self.active.pop(s.id, None)
                self.shutdown.notify_all()
                if s.closed:
                    self.sessions.pop(s.id, None)
            lock.release()

    def recover(self, owner, session_id):
        """Reconnect and observe only. Unknown physical writes remain unresolved."""
        deadline = time.monotonic() + 30
        with self.guard:
            s = self._session(owner, session_id)
            s.generation += 1
            generation = s.generation
            s.observations.clear()
        with self._worker(owner, s, deadline, generation) as check:
            previous = self.devices[s.serial]
            previous.close()
            if isinstance(previous, ProcessDevice) and previous.process is not None and previous.process.is_alive():
                raise RuntimeFault("device_busy", "Previous device worker has not terminated")
            check()
            del self.devices[s.serial]
            device = self._device(s)
            scope = device.budget(deadline, check) if isinstance(device, ProcessDevice) else nullcontext()
            with scope:
                observation = self._observe(s)
                check()
            with self.guard:
                check()
                closed_incidents = self.journal.reconcile_verified(s.serial, observation)
            result = self._session_result(s)
            result["closed_incidents"] = closed_incidents
            result.update(status="recovered_read_only" if result["recovery_required"] else "recovered", observation=observation)
            return result

    def reconcile(self, owner, session_id, request_id, *, evidence_kind=None,
                  attestation=None):
        """Close one unknown write against fresh evidence. Never dispatches.

        The runtime takes a new observation first (the only device call this path
        makes) and hands it to the journal together with the caller's evidence
        kind. A refused closure leaves the write barrier exactly as it was.
        """
        with self.guard:
            s = self._session(owner, session_id, allow_paused=True)
        observation = self.observe(owner, s.id, mode="FAST")
        with self.guard:
            s = self._session(owner, session_id, allow_paused=True)
            outcome = self.journal.close_incident_with_evidence(
                s.serial, request_id, observation,
                evidence_kind=evidence_kind or "postcondition_verified",
                attestation=attestation)
            result = self._session_result(s)
        result.update(outcome)
        result["observation_id"] = observation["observation_id"]
        result["status"] = ("reconciled" if not result["recovery_required"]
                            else "reconciliation_incomplete")
        return result

    def _device(self, s):
        if s.serial not in self.devices:
            try: self.devices[s.serial] = self.factory(s.serial)
            except Exception as e: raise RuntimeFault("device_unavailable", type(e).__name__) from e
        return self.devices[s.serial]

    def _ready_screen(self, s):
        """Return a confirmed interactive screen, recovering a simple lock first.

        The recovery path is deliberately credential-free: the device driver only
        wakes the display and performs its platform unlock gesture. We always read
        the screen state again before allowing hierarchy capture or a write.
        """
        device = self._device(s)
        state = device.screen_state()
        ready = state.get("screen_on") is True and state.get("screen_locked") is False
        if ready:
            return state

        recoverable = state.get("screen_on") is False or state.get("screen_locked") is True
        if recoverable and all(hasattr(device, name) for name in ("screen_on", "wake_up_display", "unlock")):
            s.observations.clear()
            # Keep the sequence explicit so each physical recovery step is visible
            # to the audit trail and can be exercised by fake devices in tests.
            device.screen_on()
            device.wake_up_display()
            device.unlock()
            for _ in range(3):
                state = device.screen_state()
                if state.get("screen_on") is True and state.get("screen_locked") is False:
                    return state
                time.sleep(0.05)

        code = None
        if state.get("screen_locked") is True:
            code = "screen_locked"
        elif state.get("screen_on") is False:
            code = "screen_off"
        elif state.get("screen_on") is not True or state.get("screen_locked") is not False:
            code = "screen_state_unknown"
        if code:
            s.observations.clear()
            raise RuntimeFault(code, "A confirmed awake, unlocked screen is required; automatic wake/unlock did not confirm readiness")
        return state

    @staticmethod
    def _screen_ready(state) -> bool:
        return state.get("screen_on") is True and state.get("screen_locked") is False

    @staticmethod
    def _screen_fault_code(state) -> str:
        if state.get("screen_locked") is True:
            return "screen_locked"
        if state.get("screen_on") is False:
            return "screen_off"
        return "screen_state_unknown"

    @staticmethod
    def _screen_recoverable(state) -> bool:
        """Only an explicitly off or explicitly locked screen may be recovered."""
        return state.get("screen_on") is False or state.get("screen_locked") is True

    def _require_ready(self, s, state):
        """The shared readiness policy: accept, or fail closed with the exact code.

        Both providers use this. It never turns unknown evidence into readiness,
        and it drops every existing observation handle before refusing.
        """
        if self._screen_ready(state):
            return state
        s.observations.clear()
        raise RuntimeFault(
            self._screen_fault_code(state),
            "A confirmed awake, unlocked screen is required; "
            "automatic wake/unlock did not confirm readiness")

    def _capture_snapshot(self, s, include_image, timing, ready=None):
        """One live capture, gated before any content-bearing device command.

        Legacy reads the screen through ``ready`` first, exactly as before.
        The batched provider runs in two phases: phase 1 reads the live screen
        evidence and nothing else, this method applies the shared readiness
        policy to it, and only an accepted state lets phase 2 (foreground,
        display, hierarchy, screenshot) reach the device. A screen that is off,
        locked or not explicitly known therefore never receives a hierarchy
        command - the capture transaction is not built for it at all.

        A screen that changes *during* the capture is still caught by the
        post-read: the capture is discarded, handles are dropped, and one
        bounded credential-free wake/unlock recovery is attempted before the
        whole sequence is retried.
        """
        device = self._device(s)
        name, _ = provider_choice(device, self.supports_foreground, self.provider_state)
        started_transactions = self.provider_state.transactions

        def completed(snapshot):
            snapshot["transactions"] = self.provider_state.transactions - started_transactions
            snapshot["readiness_gate"] = ("confirmed" if name == "batched" else "legacy")
            return snapshot

        if name == "legacy":
            return completed(capture_snapshot(
                device, include_image=include_image,
                supports_foreground=self.supports_foreground,
                state=self.provider_state, timing=timing, ready=ready))
        for _ in range(3):
            evidence = readiness_probe(device, timing=timing, state=self.provider_state)
            state = evidence["state"]
            if self._screen_ready(state):
                captured = capture_after_readiness(
                    device, include_image=include_image, screen_before=state,
                    readiness_ms=evidence.get("screen_ms"),
                    supports_foreground=self.supports_foreground, timing=timing,
                    state=self.provider_state, ready=ready)
                if self._screen_ready(captured["screen_after"]):
                    return completed(captured)
                state = captured["screen_after"]
            else:
                # Nothing content-bearing was sent: refuse before phase 2.
                self.provider_state.gate_refusals += 1
            # Only an explicitly off or explicitly locked screen may be
            # recovered, and only when the adapter can actually do it; anything
            # else fails closed with the exact fault the single-read path raised.
            recoverable = self._screen_recoverable(state) and all(
                hasattr(device, name) for name in ("screen_on", "wake_up_display", "unlock"))
            if not recoverable:
                self._require_ready(s, state)          # fails closed with the exact code
            s.observations.clear()
            timing.call("screen_recovery", self._wake_unlock, device)
            time.sleep(0.05)
        s.observations.clear()
        raise RuntimeFault(
            self._screen_fault_code(state),
            "A confirmed awake, unlocked screen is required; "
            "automatic wake/unlock did not confirm readiness")

    @staticmethod
    def _wake_unlock(device):
        device.screen_on()
        device.wake_up_display()
        device.unlock()

    def _observe(self, s, include_image=False, mode="FAST", cache=True):
        include_image = include_image or mode == "FULL"
        start = time.monotonic()
        timing = Timings()
        with self.guard:
            generation = s.generation
        captured = self._capture_snapshot(s, include_image, timing,
                                          ready=lambda: self._ready_screen(s))
        tree = captured["tree"]
        display = captured["display"]
        foreground = captured["foreground_before"]
        obs = timing.call("snapshot", snapshot, tree, display, foreground)
        obs["controller_epoch"] = generation
        obs["tree_captured_at"] = obs["captured_at"]
        obs["image_captured_at"] = None
        obs["image_tree_consistent"] = None
        if include_image:
            obs["image_captured_at"] = time.time()
            obs["image"] = timing.call("encode_image", encode_image, captured["image"])
            obs["image_dimensions_match"] = (
                captured["image_dimensions"] == (obs["display"]["width"], obs["display"]["height"]))
            obs["image_tree_skew_ms"] = captured["image_tree_skew_ms"]
            # A short time gap is not evidence that the page stayed unchanged.
            # The bracket around the image is checked against the capture's own
            # tree/display reads; uncertainty is exposed and never retained as
            # an actionable observation.
            obs["image_tree_consistent"] = bool(
                captured["tree_after_fingerprint"] == captured["tree_fingerprint"]
                and captured["display_after"] == captured["display"]
                and captured["image_dimensions"] == (obs["display"]["width"], obs["display"]["height"])
                and captured["image_tree_skew_ms"] is not None
                and 0 <= captured["image_tree_skew_ms"] <= 1000
                and obs["image_dimensions_match"]
            )
            obs["capture_consistency"] = (
                "tree_bracket_matched" if obs["image_tree_consistent"] else "unverified"
            )
        obs["foreground_consistent"] = (
            foreground == captured["foreground_after"]
            and (foreground is None or foreground.get("status") != "unstable")
        )
        if not obs["foreground_consistent"]:
            obs["foreground_bundle"] = None
            if include_image:
                obs["image_tree_consistent"] = False
                obs["capture_consistency"] = "foreground_changed"
        obs["screen_state"] = captured["screen_after"]
        obs["mode"] = mode
        if mode == "FULL":
            obs["tree"] = tree
            obs["grounding_sources"] = ["ui_tree"]
            obs["som"] = {"available": False, "target_count": 0,
                          "reason": "capture_unverified"}
            if obs["image_tree_consistent"]:
                marked, labels = timing.call("annotation", mark_targets,
                                             captured["image"], obs["catalog"])
                obs["annotated_image"] = timing.call("encode_annotation", encode_image, marked)
                obs["som"] = {"available": True, "target_count": len(labels),
                              "observation_id": obs["observation_id"],
                              "labels": labels, "source": "ui_tree"}
        obs["snapshot_capture"] = self._capture_metadata(captured)
        consistency_started = time.monotonic()
        obs["snapshot_consistent"] = bool(captured["consistent"])
        obs["consistency_reason"] = captured["consistency"]
        obs["timing"] = timing.milliseconds()
        obs["timing"]["consistency_check_ms"] = round(
            (time.monotonic() - consistency_started) * 1000, 3)
        obs["capture_ms"]=round((time.monotonic()-start)*1000)
        obs["max_age_ms"]=15000
        obs["actionable"] = obs["foreground_consistent"] and (not include_image or obs["image_tree_consistent"])
        obs["perf"] = self._observe_perf(obs, captured, mode, include_image, start)
        with self.guard:
            self._check_generation(s.owner, s, generation)
            if cache and obs["actionable"]:
                s.observations[obs["observation_id"]] = (time.monotonic(),obs)
            while len(s.observations)>8: del s.observations[next(iter(s.observations))]
        return obs

    @staticmethod
    def _capture_metadata(captured):
        """Redacted capture provenance: counts, enums and hashes only."""
        return {
            "provider": captured["provider"],
            "version": captured["version"],
            "round_trips": captured["round_trips"],
            "span_ms": captured["span_ms"],
            "device_ms": captured["device_ms"],
            "consistent": bool(captured["consistent"]),
            "reason": captured["consistency"],
            "component_errors": list(captured["component_errors"]),
            "fallback_reason": captured.get("fallback_reason"),
            "tree_fingerprint": captured["tree_fingerprint"][:16],
        }

    def _observe_perf(self, obs, captured, mode, include_image, start):
        """The observation's performance record, in the documented metric names.

        Durations, counts and enums only: no UI text, no input values, no
        screenshot content and no device identity.
        """
        section = captured.get("section_ms") or {}
        timing = obs["timing"]
        perf = {
            "observe.mode": mode,
            "observe.wall_ms": round((time.monotonic() - start) * 1000, 3),
            "observe.device_ms": captured["span_ms"],
            "observe.hdc_round_trips": captured["round_trips"],
            "observe.provider": captured["provider"],
            "observe.readiness_gate": captured.get("readiness_gate"),
            "observe.readiness_gate_ms": captured.get("readiness_gate_ms"),
            "observe.capture_transactions": captured.get("transactions"),
            "observe.snapshot_span_ms": captured["span_ms"],
            "observe.consistency_check_ms": timing.get("consistency_check_ms"),
            "observe.consistent": bool(captured["consistent"]),
            "observe.consistency_reason": captured["consistency"],
        }
        for name, metric in (("screen_before", "observe.screen_ms"),
                             ("foreground_before", "observe.foreground_ms"),
                             ("display", "observe.display_ms"),
                             ("tree", "observe.tree_ms"),
                             ("image", "observe.screenshot_ms"),
                             ("tree_after", "observe.tree_after_ms"),
                             ("display_after", "observe.display_after_ms"),
                             ("foreground_after", "observe.foreground_after_ms"),
                             ("screen_after", "observe.screen_after_ms")):
            if section.get(name) is not None:
                perf[metric] = section[name]
        if "encode_image_ms" in timing:
            perf["observe.encode_ms"] = timing["encode_image_ms"]
        return {key: value for key, value in perf.items() if value is not None}

    def observe(self, owner, session_id, include_image=False, mode="FAST"):
        if mode == "TEMPORAL":
            return self._temporal(owner, session_id)
        if mode not in ("FAST", "FULL"):
            raise RuntimeFault("unsupported_capability", "Supported observation modes: FAST, FULL, TEMPORAL")
        with self.guard:
            s=self._session(owner,session_id)
            generation=s.generation
        started = time.monotonic()
        request_timing = Timings()
        with self._worker(owner, s, started+30, generation, request_timing) as check:
            obs = self._observe(s,include_image,mode)
            check()
            # Return the same observation object held by the session cache.
            # Local callers can therefore not accidentally act on a public
            # response whose page identity differs from the cached handle;
            # remote MCP callers still receive a serialized copy.
            obs["timing"].update(request_timing.milliseconds())
            obs["timing"]["request_ms"] = round((time.monotonic()-started)*1000, 3)
            obs["status"] = "ok"
            return obs

    def _temporal(self, owner, session_id):
        """Bounded historical samples; never grant an action handle to past UI."""
        with self.guard:
            s = self._session(owner, session_id)
            generation = s.generation
        started = time.monotonic()
        deadline = started + 3.0
        schedule = (50, 150, 300, 700, 1500)
        frames = []
        status = "ok"
        try:
            with self._worker(owner, s, deadline, generation) as check:
                for planned_ms in schedule:
                    check()
                    delay = started + planned_ms / 1000 - time.monotonic()
                    while delay > 0:
                        s.paused.wait(min(delay, max(0, deadline - time.monotonic())))
                        check()
                        delay = started + planned_ms / 1000 - time.monotonic()
                    check()
                    if time.monotonic() >= deadline:
                        raise RuntimeFault("timeout", "Temporal sampling budget expired")
                    frame_started = time.monotonic()
                    frame = self._observe(s, include_image=True, cache=False)
                    check()
                    frame["planned_offset_ms"] = planned_ms
                    frame["actual_offset_ms"] = round((frame_started - started) * 1000)
                    frame["completed_offset_ms"] = round((time.monotonic() - started) * 1000)
                    frame["visual_digest"] = hashlib.sha256(frame["image"]["base64"].encode("ascii")).hexdigest()
                    frame["actionable"] = False
                    frame["historical"] = True
                    frames.append(frame)
                    if time.monotonic() >= deadline:
                        raise RuntimeFault("timeout", "Temporal sampling budget expired")
        except RuntimeFault as error:
            if error.code != "timeout":
                raise
            status = "timeout"
        consistent = len(frames) >= 2 and all(f["image_tree_consistent"] for f in frames[-2:])
        equal = consistent and all(frames[-1][k] == frames[-2][k] for k in ("fingerprint", "visual_digest"))
        return {"status": status, "mode": "TEMPORAL", "frames": frames,
                "actionable": False, "budget_ms": 3000,
                "timing": {"total_ms": round((time.monotonic() - started) * 1000)},
                "stable_state": {"last_two_samples_equal": bool(equal),
                    "continuous_stability_proven": False,
                    "sample_count": len(frames)},
                "message": "Historical evidence only. Equality between samples does not prove continuous stability. Observe again before acting; vanished targets cannot be recovered from these frames."}

    @staticmethod
    def _policy(action,target):
        # Initial conservative deny rules; this is not the complete M3 safety gate.
        # The term list is shared with the agent layer (`harmony_runtime.risk`),
        # so a candidate classification can never be more permissive than this.
        label = label_of(target)
        if target and target.get("visual_source"):
            # A visual region is a proposed point, not an observed widget: it can
            # only carry the spatial gestures, and it can never receive input.
            if action.kind not in ("tap", "long_press"):
                raise RuntimeFault("unsupported_capability",
                                   "Visual regions support tap and long_press only; "
                                   "input requires an observed text field")
        if action.kind in ("tap","long_press","input_text","replace_text") and is_sensitive(label):
            raise RuntimeFault("approval_required", "Sensitive target blocked. Trusted approval flow is not implemented in this build.")
        if action.kind in ("input_text", "replace_text") and not target.get("focused"):
            raise RuntimeFault("focus_required", "Tap the field and observe its focus before input")
        if action.kind in ("input_text", "replace_text") and not any(x in target["type"].lower() for x in ("input","textfield","textarea")):
            raise RuntimeFault("unsupported_capability", "Input requires an explicit text-field target")

    def act(self, owner, arguments):
        req=ActRequest.model_validate(arguments)
        digest=hashlib.sha256(canonical({"owner":owner,**req.model_dump()}).encode()).hexdigest()
        with self.guard:
            s=self._session(owner,req.session_id)
            generation=s.generation
        started=time.monotonic()
        deadline=started+req.timeout_ms/1000
        timing = Timings()
        with self._worker(owner, s, deadline, generation, timing) as check:
            try:
                result = self._act_locked(s, req, digest, started, deadline, check, timing)
            except RuntimeFault as error:
                if error.code in ("stale_observation", "target_not_found", "target_ambiguous"):
                    self._note_stale(s, error.code)
                raise
        return self._with_action_perf(result, s, timing, started)

    def _note_stale(self, s, reason):
        """Count consecutive pre-dispatch refusals; a dispatch clears the streak."""
        with self.guard:
            s.stale_streak += 1
            s.last_stale_reason = reason

    def _with_action_perf(self, result, s, timing, started):
        """Attach the documented act-path metrics to one action result."""
        if not isinstance(result, dict) or result.get("deduplicated"):
            return result
        measured = timing.milliseconds()
        with self.guard:
            streak, reason = s.stale_streak, s.last_stale_reason
            if result.get("execution_status") == "executed":
                s.stale_streak = 0
                s.last_stale_reason = None
        timings = result.get("timing") or {}
        result["perf"] = {
            "act.preflight_ms": measured.get("preflight_observe_ms"),
            "act.dispatch_ms": measured.get("dispatch_ms"),
            "act.post_observe_ms": measured.get("post_observe_ms", timings.get("verification_ms")),
            "act.total_ms": timings.get("total_ms", round((time.monotonic() - started) * 1000, 3)),
            "act.execution_status": result.get("execution_status"),
            "act.verification_status": result.get("verification_status"),
            "stale.consecutive_count": streak,
            "stale.reason": reason,
            "act.preflight_round_trips": (result.get("preflight_capture") or {}).get("round_trips"),
            "act.preflight_provider": (result.get("preflight_capture") or {}).get("provider"),
            "act.post_observation_id": result.get("after_observation_id"),
        }
        return result

    def _act_locked(self, s, req, digest, started, deadline, check, timing=None):
        """Single dispatch path; caller owns the FIFO slot and the device budget."""
        timing = timing if timing is not None else Timings()
        check()
        cached=self.journal.lookup(req.request_id,digest)
        if cached is not None: return {**cached,"deduplicated":True}
        self.journal.require_reconciled(s.serial)
        self._require_verifiable(req.expected)
        old=s.observations.get(req.observation_id)
        if old is None or time.monotonic()-old[0]>15:
            raise RuntimeFault("stale_observation", "Observe again before acting")
        before=old[1]
        # A v2 grounded handle is bound to the observation it was grounded on.
        # Copying a handle onto a different observation is refused before any
        # target resolution, even when the current page looks the same.
        handle_observation = (req.action.target.observation_id
                              if req.action.target is not None else None)
        if handle_observation is not None and handle_observation != req.observation_id:
            raise RuntimeFault(
                "stale_observation",
                "Grounded target belongs to another observation; observe again")
        # A visual target is revalidated against the current pixels, so the
        # preflight observation must carry an image for those targets only.
        needs_image = bool(req.action.target is not None
                           and req.action.target.visual is not None)
        current=timing.call("preflight_observe", self._observe, s, needs_image)
        preflight_capture = current.get("snapshot_capture")
        if not current["actionable"]:
            raise RuntimeFault("stale_observation", "Foreground changed during capture; observe again")
        if current.get("blocking_dialog") and req.action.kind not in ("back", "home", "launch"):
            raise RuntimeFault("authentication_required", "System authentication blocks interaction; user action is required")
        # Only known volatile text may move between observation and dispatch.
        # Even back/home must remain bound to the observed page. A targeted
        # action additionally requires its entire target subtree to be unchanged.
        target = None
        target_match_note = None
        if req.action.target is not None and not before.get("navigation_fingerprint"):
            raise RuntimeFault("stale_observation", "Observation lacks a page identity")
        if req.action.target is not None and req.action.target.visual is not None:
            # Revalidate the proposed region (geometry + pixels) before the page
            # identity comparison, so the refusal reason is exact instead of a
            # generic stale page. The comparison below still runs afterwards.
            target = resolve(current, req.action.target)
        if current["fingerprint"] != before["fingerprint"]:
            projection = before.get("navigation_fingerprint")
            same_page = bool(projection) and projection == current.get("navigation_fingerprint")
            allowed = req.action.kind in ("back", "home", "swipe", "tap", "long_press", "input_text", "replace_text", "launch")
            if not same_page or not allowed:
                raise RuntimeFault("stale_observation", "Page changed since the referenced observation")
            if req.action.target is not None:
                previous_target = resolve(before, req.action.target)
                # Re-identify the control by its own evidence. The catalog is
                # rebuilt every read, so `action_id`/`parent_action_id` are
                # observation-local handles, the device may report a volatile
                # accessibilityId, and the subtree hash moves with any animated
                # descendant - none of those decide identity (RC4-A).
                match = self.matcher.match(previous_target, current.get("catalog") or [])
                self._record_match(s, match)
                target_match_note = match.as_dict()
                if match.outcome == AMBIGUOUS:
                    raise RuntimeFault(
                        "target_ambiguous",
                        "Two or more targets match equally well; observe again and pick one")
                if not match.resolved:
                    raise RuntimeFault(
                        "stale_observation",
                        f"Target {match.outcome} since the referenced observation: {match.reason}")
                target = match.entry
        target = target or (resolve(current, req.action.target) if req.action.target else None)
        self._policy(req.action,target)
        timing.call("screen_guard", self._ready_screen, s)
        check()
        if time.monotonic()>=deadline: raise RuntimeFault("timeout", "Budget expired before dispatch")
        with self.guard:
            check()
            # Durable dispatch is the cancellation boundary. After it, a write
            # may be in flight and must be reconciled, never blindly replayed.
            # A generic text/bundle condition cannot reconcile a particular input.
            # An explicit `recovery` (the caller's terminal goal condition) is
            # recorded instead of the immediate effect, so a lost response can
            # later be closed against the action's own semantic condition.
            recovery_expected = None
            if req.action.kind != "replace_text":
                condition = req.recovery or req.expected
                if condition is not None:
                    recovery_expected = condition.model_dump()
            timing.call("journal_begin", self.journal.begin, req.request_id,digest,s.serial, expected=recovery_expected, before_fingerprint=before["fingerprint"])
        result={"status":"ok","execution_status":"not_dispatched","verification_status":"inconclusive","before_observation_id":req.observation_id,"after_observation_id":None,"timing":{},"evidence_refs":[],"incident_id":None,"preflight_capture":preflight_capture}
        if target_match_note is not None:
            # Sanitized diagnostic: how the target was re-identified, and with
            # which evidence. Never UI text.
            result["target_match"] = target_match_note
        try:
            timing.call("dispatch", self._device(s).dispatch, req.action,target)
            result["execution_status"]="executed"
            s.observations.clear()
        except Exception:
            result.update(status="execution_unknown",execution_status="unknown",incident_id=uuid.uuid4().hex)
            s.observations.clear()
            result["timing"] = timing.milliseconds()
            result["timing"]["total_ms"] = round((time.monotonic()-started)*1000, 3)
            return self._finish_action(req.request_id, result)
        verification_started = time.monotonic()
        try:
            while True:
                check()
                after=timing.call("post_observe", self._observe, s)
                check()
                result["after_observation_id"]=after["observation_id"]
                result["observation"]=after
                if after.get("blocking_dialog"):
                    result.update(status="authentication_required", incident_id=uuid.uuid4().hex)
                    break
                input_ok = req.action.kind != "replace_text" or input_value_matches(after["catalog"], target, req.action.text)
                if input_ok and (matches(after,req.expected,before["fingerprint"]) if req.expected else req.action.kind == "replace_text"):
                    result["verification_status"]="verified";break
                if req.expected is None and req.action.kind != "replace_text": break
                if time.monotonic()>=deadline:
                    result.update(status="timeout",verification_status="inconclusive",incident_id=uuid.uuid4().hex);break
                s.paused.wait(min(.15,max(0,deadline-time.monotonic())))
        except RuntimeFault as e:
            result.update(status=e.code,incident_id=uuid.uuid4().hex)
        except Exception:
            result.update(status="device_unavailable",incident_id=uuid.uuid4().hex)
        result["timing"] = timing.milliseconds()
        result["timing"]["verification_ms"] = round((time.monotonic()-verification_started)*1000, 3)
        result["timing"]["total_ms"]=round((time.monotonic()-started)*1000, 3)
        return self._finish_action(req.request_id, result)

    def _record_match(self, session, match):
        """Count cross-observation target re-identification outcomes (RC4-A)."""
        with self.guard:
            counts = session.target_match_counts
            counts[match.outcome] = counts.get(match.outcome, 0) + 1

    def _finish_action(self, request_id, result):
        try:
            self.journal.finish(request_id, result)
        except (sqlite3.Error, OSError):
            # The pre-dispatch transaction remains the durable uncertainty barrier.
            # Never advertise verified success when its completion was not saved.
            return {"status": "execution_unknown", "execution_status": "unknown",
                    "verification_status": "inconclusive", "request_id": request_id,
                    "completion_persisted": False, "error_code": "journal_write_failed",
                    "message": "Completion could not be persisted. Do not replay or issue another write; inspect the durable journal and recover storage."}
        return result

    def burst(self, owner, arguments):
        req = BurstRequest.model_validate(arguments)
        digest = hashlib.sha256(canonical({"owner": owner, **req.model_dump()}).encode()).hexdigest()
        with self.guard:
            s = self._session(owner, req.session_id)
            generation = s.generation
        started = time.monotonic()
        deadline = started + req.timeout_ms / 1000
        with self._worker(owner, s, deadline, generation) as check:
            check()
            cached = self.journal.burst_lookup(req.request_id, digest)
            if cached is not None:
                return {**cached, "deduplicated": True}
            for step in req.steps:
                self._require_verifiable(step.expected)
            # Persist every child identifier before the first possible write. A
            # crash between child completion and progress saving is discoverable.
            result = {"status": "running", "request_id": req.request_id,
                      "planned_request_ids": [uuid.uuid4().hex for _ in req.steps],
                      "steps": [], "verified_steps": 0, "stop_reason": None}
            with self.guard:
                check()
                self.journal.begin_burst(req.request_id, digest, s.serial, result)
            observation_id = req.observation_id
            for index, step in enumerate(req.steps):
                child_id = result["planned_request_ids"][index]
                try:
                    check()
                    if time.monotonic() >= deadline:
                        raise RuntimeFault("timeout", "Shared burst budget expired before the next step")
                    watch_deadline = min(deadline, time.monotonic() + step.watch_timeout_ms / 1000) if step.watch_timeout_ms else deadline
                    device = self._device(s)
                    step_scope = device.budget(watch_deadline, check) if isinstance(device, ProcessDevice) else nullcontext()
                    with step_scope:
                        if step.watch_timeout_ms:
                            # Persisted parent prevents retries from rearming a watch.
                            self.journal.require_reconciled(s.serial)
                            while True:
                                check()
                                if time.monotonic() >= watch_deadline:
                                    raise RuntimeFault("watch_expired", "Target did not appear before the watch deadline")
                                observed = self._observe(s)
                                check()
                                try:
                                    resolve(observed, step.action.target)
                                except RuntimeFault as error:
                                    if error.code != "target_not_found":
                                        raise
                                else:
                                    if time.monotonic() >= watch_deadline:
                                        raise RuntimeFault("watch_expired", "Observation arrived after the watch deadline")
                                    observation_id = observed["observation_id"]
                                    break
                                s.paused.wait(min(.05, max(0, watch_deadline - time.monotonic())))
                        action = ActRequest(session_id=req.session_id, request_id=child_id,
                                            observation_id=observation_id, action=step.action,
                                            expected=step.expected, timeout_ms=req.timeout_ms)
                        child_digest = hashlib.sha256(canonical({"owner": owner, **action.model_dump()}).encode()).hexdigest()
                        entry = self._act_locked(s, action, child_digest, time.monotonic(),
                                                 min(deadline, watch_deadline) if step.watch_timeout_ms else deadline, check)
                except RuntimeFault as error:
                    entry = {"status": error.code, "execution_status": "not_dispatched",
                             "verification_status": "inconclusive", "message": str(error)}
                result["steps"].append({"index": index, "request_id": child_id, **entry})
                if entry.get("execution_status") != "executed" or entry.get("verification_status") != "verified":
                    result.update(status="stopped", stop_reason=entry["status"])
                    break
                result["verified_steps"] += 1
                observation_id = entry["after_observation_id"]
                if not self._save_burst(req.request_id, result):
                    result["timing"] = {"total_ms": round((time.monotonic() - started) * 1000)}
                    return result
            else:
                result.update(status="completed", stop_reason="all_steps_verified")
            result["timing"] = {"total_ms": round((time.monotonic() - started) * 1000)}
            self._save_burst(req.request_id, result, finished=True)
            return result

    def _save_burst(self, request_id, result, finished=False):
        try:
            self.journal.save_burst(request_id, result, finished=finished)
        except (sqlite3.Error, OSError):
            # Child outcomes are independently durable. Preserve those outcomes,
            # but never dispatch another child after parent progress is lost.
            result.update(status="interrupted", stop_reason="journal_write_failed",
                          completion_persisted=False,
                          message="Burst progress could not be persisted. Do not replay or resume; query every planned child request before replanning.")
            return False
        return True

    def _require_verifiable(self, expected):
        if expected is not None and expected.bundle is not None and not self.supports_foreground:
            raise RuntimeFault("unsupported_capability", "Foreground application identity is not yet verified on this device adapter; bundle postconditions cannot be evaluated. Use observable page evidence instead.")

    def wait(self,owner,session_id,expected=None,timeout_ms=5000,poll_ms=200,condition=None):
        if not 100<=timeout_ms<=30000 or not 100<=poll_ms<=2000:
            raise RuntimeFault("invalid_arguments", "Wait timeout 100..30000ms, poll 100..2000ms")
        if (expected is None) == (condition is None):
            raise RuntimeFault("invalid_arguments", "Provide exactly one of expected or condition")
        legacy = Expected.model_validate(expected) if expected is not None else None
        condition = WaitCondition.model_validate(condition) if condition is not None else None
        self._require_verifiable(legacy)
        if legacy and legacy.changed:
            raise RuntimeFault("invalid_arguments", "Use a change condition with a baseline observation_id")
        if condition and condition.type == "app_changed" and not self.supports_foreground:
            raise RuntimeFault("unsupported_capability", "Foreground application identity is not verified on this adapter")
        deadline=time.monotonic()+timeout_ms/1000
        with self.guard:
            s=self._session(owner,session_id)
            generation=s.generation
        baseline = None
        baseline_bundle = None
        if condition and condition.observation_id:
            with self.guard:
                old = s.observations.get(condition.observation_id)
                if old is None or time.monotonic() - old[0] > 15:
                    raise RuntimeFault("stale_observation", "Wait requires a fresh baseline from this session")
                baseline = old[1]["fingerprint"]
                baseline_bundle = old[1].get("foreground_bundle")
                if condition.type == "app_changed" and not baseline_bundle:
                    raise RuntimeFault("foreground_unknown", "App change requires a verified baseline application")
        stable_fingerprint = None
        stable_since = None
        samples = 0
        obs = None
        while True:
            self._check_generation(owner,s,generation)
            if time.monotonic() >= deadline:
                return {"status":"timeout", "observation":obs}
            try:
                with self._worker(owner,s,deadline,generation) as check:
                    obs=self._observe(s)
                    check()
            except RuntimeFault as exc:
                if exc.code != "timeout":
                    raise
                return {"status":"timeout", "observation":obs}
            if time.monotonic() >= deadline:
                return {"status":"timeout", "observation":obs}
            if obs.get("blocking_dialog"):
                return {"status": "authentication_required", "observation": obs,
                        "evidence": {"source": "system_authentication_ui"}}
            samples += 1
            matched = matches(obs, legacy) if legacy else False
            evidence = {"source": "ui_tree", "samples": samples}
            if condition:
                kind = condition.type
                if kind in ("change", "fingerprint_changed"):
                    matched = obs["fingerprint"] != baseline
                elif kind == "app_changed":
                    matched = bool(obs.get("foreground_bundle")) and obs["foreground_bundle"] != baseline_bundle
                    evidence["source"] = "system_foreground"
                elif kind in ("text_present", "text_absent"):
                    present = any(item["text"] == condition.value for item in obs["catalog"])
                    matched = present if kind == "text_present" else not present
                elif kind in ("element_present", "element_absent"):
                    selector = condition.target.model_dump(exclude_none=True)
                    present = any(all(item[key] == value for key, value in selector.items()) for item in obs["catalog"])
                    matched = present if kind == "element_present" else not present
                elif kind == "stable":
                    now = time.monotonic()
                    if stable_fingerprint != obs["fingerprint"]:
                        stable_fingerprint, stable_since = obs["fingerprint"], now
                    elapsed = (now - stable_since) * 1000
                    matched = samples >= 2 and elapsed >= condition.stable_ms
                    evidence.update(sampled_unchanged_ms=round(elapsed), continuous_stability_proven=False,
                                    pixel_stability_proven=False)
            if matched and obs.get("actionable", False):
                return {"status":"matched", "observation":obs, "evidence":evidence}
            s.paused.wait(min(poll_ms/1000,max(0,deadline-time.monotonic())))

    def close(self):
        # Stop admission first; active workers retain resources until their
        # dispatch outcomes have been durably recorded.
        with self.shutdown:
            if self.stopped:
                self.shutdown.wait_for(lambda: self.resources_closed)
                if self.close_error is not None:
                    raise self.close_error
                return
            self.stopped = True
            self.shutdown.notify_all()
            for session in self.sessions.values():
                session.generation += 1
                session.paused.set()
                session.observations.clear()
            self.shutdown.wait_for(lambda: not self.active)
            devices = list(self.devices.values())
        error = None
        try:
            for device in devices:
                try:
                    device.close()
                except Exception as exc:
                    if error is None:
                        error = exc
            try:
                self.journal.close()
            except Exception as exc:
                if error is None:
                    error = exc
        finally:
            with self.shutdown:
                self.close_error = error
                self.resources_closed = True
                self.shutdown.notify_all()
        if error is not None:
            raise error
