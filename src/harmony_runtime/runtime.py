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
from .observation import canonical, matches, resolve, snapshot
from .visual import encode_image, mark_targets


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


class Runtime:
    """One serialized device worker per serial; session tokens are owner-bound."""
    def __init__(self, state_dir, factory=HarmonyDevice, discover=None, lease_seconds=300):
        self.root = Path(state_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.journal = Journal(self.root / "journal.sqlite3")
        self.factory = ProcessDevice if factory is HarmonyDevice else factory
        self.discover = discover or HarmonyDevice.discover
        self.lease_seconds = lease_seconds
        self.guard = threading.RLock()
        self.shutdown = threading.Condition(self.guard)
        self.resources_closed = False
        self.close_error = None
        self.sessions = {}
        self.devices = {}
        self.device_locks = {}
        self.active = {}
        self.stopped = False

    def session(self, owner, operation, session_id=None, device_id=None, request_id=None):
        if operation in ("action_status", "burst_status"):
            if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
                raise RuntimeFault("invalid_arguments", "Status queries require request_id (1–128 characters)")
            with self.guard:
                s = self._session(owner, session_id, allow_paused=True)
                return getattr(self.journal, operation)(s.serial, request_id)
        if request_id is not None:
            raise RuntimeFault("invalid_arguments", "request_id is only supported for action_status or burst_status")
        if operation == "recover":
            return self.recover(owner, session_id)
        with self.guard:
            if self.stopped: raise RuntimeFault("runtime_stopped", "Runtime is shutting down")
            if operation == "open":
                if not device_id or device_id == "auto":
                    devices = self.discover()
                    if len(devices) != 1:
                        raise RuntimeFault("device_selection_required", f"Select exactly one device from: {devices}")
                    device_id = devices[0]
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

    def _session_result(self, s):
        unresolved = self.journal.unresolved(s.serial)
        return {"status":"paused" if s.paused.is_set() else "open", "session_id":s.id, "device_id":s.serial, "lease_remaining_seconds":max(0,round(s.expires-time.monotonic())), "recovery_required":bool(unresolved), "unresolved_actions":unresolved, "incidents":self.journal.incidents(s.serial), "capabilities":{"tree":True,"screenshot":True,"full":True,"som":True,"burst":True,"foreground_bundle":False,"ocr":False,"webview":False,"temporal":True,"temporal_watch":True,"pro":False}}

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
    def _worker(self, owner, s, deadline, generation=None):
        if generation is None:
            with self.guard:
                self._session(owner, s.id)
                generation = s.generation
        check = lambda: self._check_generation(owner, s, generation)
        lock = self.device_locks[s.serial]
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not lock.acquire(timeout=remaining, check=check):
            raise RuntimeFault("timeout", "Budget expired in device queue; no action dispatched")
        try:
            with self.guard:
                check()
                self.active[s.id] = True
            device = self._device(s)
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

    def _device(self, s):
        if s.serial not in self.devices:
            try: self.devices[s.serial] = self.factory(s.serial)
            except Exception as e: raise RuntimeFault("device_unavailable", type(e).__name__) from e
        return self.devices[s.serial]

    def _ready_screen(self, s):
        state = self._device(s).screen_state()
        code = None
        if state.get("screen_locked") is True:
            code = "screen_locked"
        elif state.get("screen_on") is False:
            code = "screen_off"
        elif state.get("screen_on") is not True or state.get("screen_locked") is not False:
            code = "screen_state_unknown"
        if code:
            s.observations.clear()
            raise RuntimeFault(code, "A confirmed awake, unlocked screen is required; unlock the phone and observe again")
        return state

    def _observe(self, s, include_image=False, mode="FAST", cache=True):
        include_image = include_image or mode == "FULL"
        start = time.monotonic()
        with self.guard:
            generation = s.generation
        d = self._device(s)
        self._ready_screen(s)
        tree = d.tree()
        obs = snapshot(tree,d.display())
        obs["tree_captured_at"] = obs["captured_at"]
        obs["image_captured_at"] = None
        if include_image:
            image = d.screenshot()
            obs["image_captured_at"] = time.time()
            obs["image"] = encode_image(image)
            obs["image_dimensions_match"] = image.size == (obs["display"]["width"], obs["display"]["height"])
            obs["image_tree_skew_ms"]=round((obs["image_captured_at"]-obs["tree_captured_at"])*1000)
            # A short time gap is not evidence that the page stayed unchanged.
            # Bracket the image with hierarchy/display reads; expose uncertainty
            # and never retain an unstable capture as an actionable observation.
            after = snapshot(d.tree(), d.display())
            obs["image_tree_consistent"] = (
                obs["fingerprint"] == after["fingerprint"]
                and 0 <= obs["image_tree_skew_ms"] <= 1000
                and obs["image_dimensions_match"]
            )
            obs["capture_consistency"] = (
                "tree_bracket_matched" if obs["image_tree_consistent"] else "unverified"
            )
        obs["screen_state"] = self._ready_screen(s)
        obs["mode"] = mode
        if mode == "FULL":
            obs["tree"] = tree
            obs["grounding_sources"] = ["ui_tree"]
            obs["som"] = {"available": False, "target_count": 0,
                          "reason": "capture_unverified"}
            if obs["image_tree_consistent"]:
                marked, labels = mark_targets(image, obs["catalog"])
                obs["annotated_image"] = encode_image(marked)
                obs["som"] = {"available": True, "target_count": len(labels),
                              "observation_id": obs["observation_id"],
                              "labels": labels, "source": "ui_tree"}
        obs["capture_ms"]=round((time.monotonic()-start)*1000)
        obs["max_age_ms"]=15000
        obs["actionable"] = not include_image or obs["image_tree_consistent"]
        with self.guard:
            self._check_generation(s.owner, s, generation)
            if cache and obs["actionable"]:
                s.observations[obs["observation_id"]] = (time.monotonic(),obs)
            while len(s.observations)>8: del s.observations[next(iter(s.observations))]
        return obs

    def observe(self, owner, session_id, include_image=False, mode="FAST"):
        if mode == "TEMPORAL":
            return self._temporal(owner, session_id)
        if mode not in ("FAST", "FULL"):
            raise RuntimeFault("unsupported_capability", "Supported observation modes: FAST, FULL, TEMPORAL")
        with self.guard:
            s=self._session(owner,session_id)
            generation=s.generation
        with self._worker(owner, s, time.monotonic()+30, generation) as check:
            obs = self._observe(s,include_image,mode)
            check()
            return {"status":"ok", **obs}

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
        danger=("支付","付款","转账","购买","下单","删除","卸载","清空","发送","提交","允许","授权","密码","验证码","pay","purchase","delete","send","submit","password","permission")
        label = " ".join(str(target.get(k,"")) for k in ("text","resource_id","type")) .lower() if target else ""
        if action.kind in ("tap","long_press","input_text") and any(x in label for x in danger):
            raise RuntimeFault("approval_required", "Sensitive target blocked. Trusted approval flow is not implemented in this build.")
        if action.kind == "input_text" and not target.get("focused"):
            raise RuntimeFault("focus_required", "Tap the field and observe its focus before input")
        if action.kind == "input_text" and not any(x in target["type"].lower() for x in ("input","textfield","textarea")):
            raise RuntimeFault("unsupported_capability", "Input requires an explicit text-field target")

    def act(self, owner, arguments):
        req=ActRequest.model_validate(arguments)
        digest=hashlib.sha256(canonical({"owner":owner,**req.model_dump()}).encode()).hexdigest()
        with self.guard:
            s=self._session(owner,req.session_id)
            generation=s.generation
        started=time.monotonic()
        deadline=started+req.timeout_ms/1000
        with self._worker(owner, s, deadline, generation) as check:
            return self._act_locked(s, req, digest, started, deadline, check)

    def _act_locked(self, s, req, digest, started, deadline, check):
        """Single dispatch path; caller owns the FIFO slot and the device budget."""
        check()
        cached=self.journal.lookup(req.request_id,digest)
        if cached is not None: return {**cached,"deduplicated":True}
        self.journal.require_reconciled(s.serial)
        self._require_verifiable(req.expected)
        old=s.observations.get(req.observation_id)
        if old is None or time.monotonic()-old[0]>15:
            raise RuntimeFault("stale_observation", "Observe again before acting")
        before=old[1]
        current=self._observe(s)
        if current["fingerprint"] != before["fingerprint"]:
            raise RuntimeFault("stale_observation", "Page changed since the referenced observation")
        target=resolve(current,req.action.target) if req.action.target else None
        self._policy(req.action,target)
        self._ready_screen(s)
        check()
        if time.monotonic()>=deadline: raise RuntimeFault("timeout", "Budget expired before dispatch")
        with self.guard:
            check()
            # Durable dispatch is the cancellation boundary. After it, a write
            # may be in flight and must be reconciled, never blindly replayed.
            self.journal.begin(req.request_id,digest,s.serial, expected=req.expected.model_dump() if req.expected else None, before_fingerprint=before["fingerprint"])
        result={"status":"ok","execution_status":"not_dispatched","verification_status":"inconclusive","before_observation_id":req.observation_id,"after_observation_id":None,"timing":{},"evidence_refs":[],"incident_id":None}
        try:
            self._device(s).dispatch(req.action,target)
            result["execution_status"]="executed"
            s.observations.clear()
        except Exception:
            result.update(status="execution_unknown",execution_status="unknown",incident_id=uuid.uuid4().hex)
            s.observations.clear()
            return self._finish_action(req.request_id, result)
        try:
            while True:
                check()
                after=self._observe(s)
                check()
                result["after_observation_id"]=after["observation_id"]
                result["observation"]=after
                if req.expected and matches(after,req.expected,before["fingerprint"]):
                    result["verification_status"]="verified";break
                if req.expected is None: break
                if time.monotonic()>=deadline:
                    result.update(status="timeout",verification_status="inconclusive",incident_id=uuid.uuid4().hex);break
                s.paused.wait(min(.15,max(0,deadline-time.monotonic())))
        except RuntimeFault as e:
            result.update(status=e.code,incident_id=uuid.uuid4().hex)
        except Exception:
            result.update(status="device_unavailable",incident_id=uuid.uuid4().hex)
        result["timing"]["total_ms"]=round((time.monotonic()-started)*1000)
        return self._finish_action(req.request_id, result)

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

    @staticmethod
    def _require_verifiable(expected):
        if expected is not None and expected.bundle is not None:
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
        if condition and condition.type == "app_changed":
            raise RuntimeFault("unsupported_capability", "Foreground application identity is not verified on this adapter")
        deadline=time.monotonic()+timeout_ms/1000
        with self.guard:
            s=self._session(owner,session_id)
            generation=s.generation
        baseline = None
        if condition and condition.observation_id:
            with self.guard:
                old = s.observations.get(condition.observation_id)
                if old is None or time.monotonic() - old[0] > 15:
                    raise RuntimeFault("stale_observation", "Wait requires a fresh baseline from this session")
                baseline = old[1]["fingerprint"]
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
            samples += 1
            matched = matches(obs, legacy) if legacy else False
            evidence = {"source": "ui_tree", "samples": samples}
            if condition:
                kind = condition.type
                if kind in ("change", "fingerprint_changed"):
                    matched = obs["fingerprint"] != baseline
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
            if matched:
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
