"""Offline coverage for the batched device snapshot and its readiness gate.

The privacy invariant is proven at the level of *device commands*, not of
observation flags: every double here records the commands a script would
actually execute, so "the hierarchy was never captured" is asserted directly
and cannot be satisfied by a discarded capture.
"""
import json
import os
import tempfile
import unittest

from harmony_runtime import snapshot as S
from harmony_runtime.contracts import RuntimeFault
from harmony_runtime.runtime import Runtime
from test_runtime import FakeDevice

MARKER = "HRSNAPTEST0001"

#: The shell's own `time` report, which the device appends to every non-payload
#: command and which the runtime strips before parsing.
TIME_LINE = "\n  0m00.20s real     0m00.01s user     0m00.01s system"

WINDOW_OK = "Focus window: 42\n"
MISSIONS_OK = ("Mission ID #42 mission name #[#com.example.app:entry:Entry]\n"
               "  bundle name [com.example.app]\n  AbilityRecord ID #7\n"
               "  state #FOREGROUND\n  app state #FOREGROUND\n")
DISPLAY_OK = "[DISPLAY INFO]\nWidth: 1080\nHeight: 2340\nRotation: 0\n"
TREE = {"attributes": {"bundleName": "com.example.app", "visible": "true",
                       "bounds": "[0,0][1080,2340]", "type": "Root"},
        "children": [{"attributes": {"type": "Button", "text": "Go",
                                     "bounds": "[0,0][100,100]", "clickable": "true"},
                      "children": []}]}

# -- device-side evidence builders ------------------------------------------


def power_text(kind):
    if kind == "awake":
        return "Power state machine\n  Current State: AWAKE\n"
    if kind == "sleep":
        return "Power state machine\n  Current State: SLEEP\n"
    if kind == "conflict":
        return "Current State: AWAKE\nScreen on reason\nCurrent State: SLEEP\n"
    return "Power state machine\n"          # absent evidence


def lock_text(kind):
    if kind == "false":
        return "* screenLocked false\n"
    if kind == "true":
        return "* screenLocked true\n"
    if kind == "conflict":
        return "* screenLocked false\n* screenLocked true\n"
    return "* deviceLocked false\n"         # absent evidence


def executed_commands(script):
    """The device commands a built script would run, in order.

    This is the script evaluator the safety tests assert on: it answers "what
    would the phone actually execute?", independent of how framing is written.
    """
    commands = []
    for line in script.splitlines():
        line = line.strip()
        if line.startswith("time { "):
            body = line[len("time { "):]
        elif line.startswith("{ "):
            body = line[2:]
        else:
            continue
        commands.append(body.rsplit("; }", 1)[0].strip())
    return commands


def section(marker, index, pieces, failing=None, timed=None):
    """The output a device-side section produces, not the script that makes it."""
    timed = range(len(pieces)) if timed is None else timed
    lines = ["%s:%d:b" % (marker, index)]
    for position, piece in enumerate(pieces):
        lines.append(piece + (TIME_LINE if position in timed else ""))
        lines.append("%s:%d:c%d:%d" % (marker, index, position,
                                       1 if failing == position else 0))
    lines.append("%s:%d:e" % (marker, index))
    return "\n".join(lines)


def readiness_transaction(marker, power="awake", lock="false"):
    return "\n".join([section(marker, 0, [power_text(power), lock_text(lock)]),
                      "%s:done" % marker])


def capture_transaction(marker, *, tree=None, tree_text=None,
                        foreground=(WINDOW_OK, MISSIONS_OK, WINDOW_OK),
                        foreground_after=None, display=DISPLAY_OK, screen_after=None,
                        broken=None, extra=""):
    tree_text = tree_text if tree_text is not None else json.dumps(tree if tree is not None else TREE)
    foreground_after = foreground_after if foreground_after is not None else foreground
    screen_after = screen_after or ("awake", "false")
    parts = []
    # `cat`/`base64` payload pieces are never wrapped by the shell's `time`, so
    # the fixture must not add a timing report to them either.
    for index, (pieces, failing, timed) in enumerate((
            (list(foreground), broken - 10 if isinstance(broken, int) and broken in (10, 11, 12) else None, None),
            ([display], None, None),
            (["DumpLayout saved to:/data/local/tmp/x.json", "1700000000000000", tree_text], None, (0, 1)),
            (list(foreground_after), None, None),
            ([power_text(screen_after[0]), lock_text(screen_after[1])], None, None))):
        if broken == index:
            parts.append(section(marker, index, pieces, failing=0, timed=timed))
        else:
            parts.append(section(marker, index, pieces, failing=failing, timed=timed))
    parts.append(extra)
    parts.append("%s:done" % marker)
    return "\n".join(parts)


class GateDevice(FakeDevice):
    """A device that only answers what it is asked, and records every command.

    ``screen_after`` lets a test model a screen that becomes unready *during*
    the capture window, which is the case the post-read must still catch.
    """

    supports_foreground = True

    def __init__(self, power="awake", lock="false", screen_after=None,
                 capture_override=None, readiness_override=None):
        super().__init__("fake")
        self.power, self.lock = power, lock
        self.screen_after = screen_after
        self.capture_override = capture_override
        self.readiness_override = readiness_override
        self.probes = []
        self.executed = []

    def foreground(self):
        return {"bundle": "com.example.app", "focus_id": "42", "mission_id": "42",
                "ability_id": "7", "status": "verified"}

    def batch_probe(self, script):
        self.probes.append(script)
        self.executed.extend(executed_commands(script))
        marker = script.split("\n", 1)[0].split("=", 1)[1]
        if "uitest dumpLayout" in script:
            raw = self.capture_transaction(marker)
        else:
            raw = (self.readiness_override(marker) if self.readiness_override
                   else readiness_transaction(marker, self.power, self.lock))
        return {"raw": raw, "device_ms": 1.0}

    def capture_transaction(self, marker):
        if self.capture_override is not None:
            return self.capture_override(marker)
        after = self.screen_after or (self.power, self.lock)
        return capture_transaction(marker, screen_after=after)


class DeviceFactory:
    """A runtime factory that hands out one recorded device double."""

    supports_foreground = True

    def __init__(self, device):
        self.device = device

    def __call__(self, serial):
        return self.device


class ReadinessGateTests(unittest.TestCase):
    """Locked, dark or unknown screens must never reach a content command."""

    CONTENT = ("uitest", "snapshot_display", "base64", "cat ",
               "WindowManagerService", "AbilityManagerService", "DisplayManagerService")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def attempt(self, device, *, include_image=False):
        """Run the real runtime gate against a device double."""
        runtime = Runtime(self.tmp.name, factory=DeviceFactory(device),
                          discover=lambda: ["fake"])
        self.addCleanup(runtime.close)
        session = runtime.session("owner", "open")["session_id"]
        try:
            return runtime.observe("owner", session, include_image=include_image), runtime
        except RuntimeFault as error:
            return error, runtime

    def assert_no_content_command(self, device):
        for command in device.executed:
            self.assertFalse(
                any(token in command for token in self.CONTENT),
                "a content-bearing command reached the device: %r" % command)
        self.assertEqual(len(device.probes), 1,
                         "only the readiness probe may be sent to the device")

    def test_a_locked_screen_never_receives_a_hierarchy_command(self):
        device = GateDevice("awake", "true")
        error, _ = self.attempt(device)
        self.assertEqual(error.code, "screen_locked")
        self.assert_no_content_command(device)
        self.assertTrue(any(c.startswith("hidumper -s PowerManagerService") for c in device.executed))

    def test_a_dark_screen_never_receives_a_hierarchy_command(self):
        device = GateDevice("sleep", "false")
        error, _ = self.attempt(device)
        self.assertEqual(error.code, "screen_off")
        self.assert_no_content_command(device)

    def test_an_unknown_lock_never_receives_a_hierarchy_command(self):
        device = GateDevice("awake", "absent")
        error, _ = self.attempt(device)
        self.assertEqual(error.code, "screen_state_unknown")
        self.assert_no_content_command(device)

    def test_an_unknown_power_never_receives_a_hierarchy_command(self):
        device = GateDevice("absent", "false")
        error, _ = self.attempt(device)
        self.assertEqual(error.code, "screen_state_unknown")
        self.assert_no_content_command(device)

    def test_conflicting_power_evidence_never_receives_a_hierarchy_command(self):
        device = GateDevice("conflict", "false")
        error, _ = self.attempt(device)
        self.assertEqual(error.code, "screen_state_unknown")
        self.assert_no_content_command(device)

    def test_conflicting_lock_evidence_never_receives_a_hierarchy_command(self):
        device = GateDevice("awake", "conflict")
        error, _ = self.attempt(device)
        self.assertEqual(error.code, "screen_state_unknown")
        self.assert_no_content_command(device)

    def test_only_an_explicitly_awake_unlocked_screen_reaches_the_tree(self):
        device = GateDevice("awake", "false")
        observation, runtime = self.attempt(device)
        self.assertTrue(observation["actionable"])
        self.assertEqual(observation["snapshot_capture"]["provider"], "batched")
        self.assertEqual(len(device.probes), 2)
        trees = [c for c in device.executed if c.startswith("uitest dumpLayout")]
        self.assertEqual(len(trees), 1)
        self.assertEqual(runtime.provider_state.gate_refusals, 0)

    def test_a_full_capture_is_also_gated(self):
        device = GateDevice("awake", "true")
        error, _ = self.attempt(device, include_image=True)
        self.assertEqual(error.code, "screen_locked")
        self.assert_no_content_command(device)

    def test_the_script_builder_never_puts_content_in_the_readiness_phase(self):
        script = S.build_script(MARKER, "/tmp/example", True, S.PHASE_READINESS)
        commands = executed_commands(script)
        self.assertEqual(commands, [S.SCREEN_POWER, S.SCREEN_LOCK])
        for command in commands:
            self.assertNotIn("uitest", command)
            self.assertNotIn("snapshot_display", command)

    def test_the_capture_phase_is_the_only_one_that_carries_content(self):
        script = S.build_script(MARKER, "/tmp/example", True, S.PHASE_CAPTURE)
        commands = executed_commands(script)
        self.assertTrue(any(c.startswith("uitest dumpLayout") for c in commands))
        self.assertTrue(any(c.startswith("snapshot_display") for c in commands))
        # The capture phase carries the bracket's *end* screen read, but never
        # re-reads the readiness evidence: the first command it runs is the
        # foreground read that follows the accepted gate.
        self.assertEqual(commands[0], S.FOREGROUND_WINDOW)
        self.assertEqual(commands.count(S.SCREEN_POWER), 1)
        self.assertLess(commands.index(S.SCREEN_POWER), len(commands))

    def test_skipping_the_gate_is_refused_by_the_capture_entry_point(self):
        device = GateDevice("awake", "false")
        with self.assertRaises(RuntimeFault) as error:
            S.capture_after_readiness(device, screen_before=None, state=S.ProviderState())
        self.assertEqual(error.exception.code, "internal_error")
        self.assertEqual(device.probes, [])

    def test_the_runtime_gate_records_refusals_and_transactions(self):
        device = GateDevice("awake", "false")
        runtime = Runtime(self.tmp.name, factory=DeviceFactory(device), discover=lambda: ["fake"])
        self.addCleanup(runtime.close)
        session = runtime.session("owner", "open")["session_id"]
        observation = runtime.observe("owner", session)
        self.assertTrue(observation["actionable"])
        self.assertEqual(observation["perf"]["observe.capture_transactions"], 2)
        self.assertEqual(runtime.provider_state.gate_refusals, 0)

        locked = GateDevice("awake", "true")
        runtime.devices["fake"] = locked
        before = runtime.provider_state.transactions
        with self.assertRaises(RuntimeFault) as error:
            runtime.observe("owner", session)
        self.assertEqual(error.exception.code, "screen_locked")
        self.assertEqual(runtime.provider_state.gate_refusals, 1)
        self.assertEqual(runtime.provider_state.transactions, before + 1,
                         "a refused gate must not issue a capture transaction")
        self.assertEqual(len(locked.probes), 1)
        self.assertFalse(any(token in locked.probes[0] for token in self.CONTENT),
                         "the locked device received a content-bearing command")

    def test_a_screen_that_becomes_unready_during_the_capture_is_discarded(self):
        device = GateDevice("awake", "false", screen_after=("awake", "true"))
        state = S.ProviderState()
        evidence = S.readiness_probe(device, state=state)
        captured = S.capture_after_readiness(device, screen_before=evidence["state"], state=state)
        self.assertFalse(captured["consistent"])
        self.assertEqual(captured["consistency"], S.SCREEN_CHANGED)


class TransactionFramingTests(unittest.TestCase):
    def parse(self, raw, phase=S.PHASE_CAPTURE, marker=MARKER):
        _, counts = S._section_map(False, phase)
        return S.parse_transaction(raw, marker, counts)

    def test_a_complete_capture_transaction_parses_into_ordered_sections(self):
        results = self.parse(capture_transaction(MARKER))
        self.assertEqual(sorted(results), [0, 1, 2, 3, 4])
        self.assertEqual(results[2][2], json.dumps(TREE))

    def test_a_readiness_transaction_parses_into_one_section(self):
        results = self.parse(readiness_transaction(MARKER), S.PHASE_READINESS)
        self.assertEqual(sorted(results), [0])
        self.assertEqual(len(results[0]), 2)

    def test_a_truncated_readiness_frame_fails_closed(self):
        raw = readiness_transaction(MARKER).replace("%s:done" % MARKER, "")
        with self.assertRaises(RuntimeFault):
            self.parse(raw, S.PHASE_READINESS)

    def test_a_capture_frame_missing_the_tree_section_is_invalid(self):
        raw = "\n".join(line for line in capture_transaction(MARKER).splitlines()
                        if not line.startswith("%s:2:" % MARKER))
        with self.assertRaises(RuntimeFault):
            self.parse(raw)

    def test_truncation_is_refused(self):
        raw = capture_transaction(MARKER).replace("%s:done" % MARKER, "")
        with self.assertRaises(RuntimeFault):
            self.parse(raw)

    def test_a_duplicated_marker_is_refused(self):
        raw = capture_transaction(MARKER).replace("%s:done" % MARKER,
                                                 "%s:1:c0:0\n%s:done" % (MARKER, MARKER))
        with self.assertRaises(RuntimeFault):
            self.parse(raw)

    def test_reordered_sections_are_refused(self):
        lines = capture_transaction(MARKER).splitlines()
        with self.assertRaises(RuntimeFault):
            self.parse("\n".join(lines[5:] + lines[:5]))

    def test_a_failed_command_section_is_refused(self):
        with self.assertRaises(RuntimeFault) as error:
            self.parse(capture_transaction(MARKER, broken=0))
        self.assertEqual(error.exception.code, "device_unavailable")

    def test_a_foreign_marker_is_refused(self):
        raw = capture_transaction(MARKER).replace("%s:1:b" % MARKER, "OTHER:1:b")
        with self.assertRaises(RuntimeFault):
            self.parse(raw)

    def test_unlabelled_output_is_refused(self):
        raw = capture_transaction(MARKER).replace("%s:1:e" % MARKER,
                                                  "stray output\n%s:1:e" % MARKER)
        with self.assertRaises(RuntimeFault):
            self.parse(raw)

    def test_an_oversized_transaction_is_refused(self):
        raw = capture_transaction(MARKER) + "x" * (S.MAX_RAW_BYTES + 1)
        with self.assertRaises(RuntimeFault):
            self.parse(raw)

    def test_the_script_only_uses_the_fixed_vocabulary(self):
        for phase, include_image in ((S.PHASE_READINESS, False), (S.PHASE_CAPTURE, True)):
            for command in executed_commands(S.build_script(MARKER, "/tmp/example",
                                                            include_image, phase)):
                head = command.split(" ", 1)[0]
                if head in ("hidumper", "uitest"):
                    self.assertIn(command.split(" -p ")[0].split(" -f ")[0],
                                  {"hidumper -s PowerManagerService -a '-a'",
                                   "hidumper -s ScreenlockService -a -all",
                                   "hidumper -s WindowManagerService -a '-a'",
                                   "hidumper -s AbilityManagerService -a '-l'",
                                   'hidumper -s DisplayManagerService -a "-a"',
                                   "uitest dumpLayout"})
        script = S.build_script(MARKER, "/tmp/example", True, S.PHASE_CAPTURE)
        self.assertNotIn("$(", script)
        self.assertNotIn("`", script)

    def test_the_payload_commands_are_never_wrapped(self):
        for command in executed_commands(S.build_script(MARKER, "/tmp/example", True,
                                                        S.PHASE_CAPTURE)):
            self.assertFalse(command.startswith("time { cat"))
            self.assertFalse(command.startswith("time { base64"))


class SnapshotNormalizationTests(unittest.TestCase):
    def snapshot(self, device):
        state = S.ProviderState()
        evidence = S.readiness_probe(device, state=state)
        return S.capture_after_readiness(device, screen_before=evidence["state"], state=state)

    def test_a_consistent_capture_is_normalized(self):
        result = self.snapshot(GateDevice())
        self.assertTrue(result["consistent"])
        self.assertEqual(result["consistency"], S.CONSISTENT)
        self.assertEqual(result["round_trips"], 2)
        self.assertEqual(result["foreground_before"]["bundle"], "com.example.app")
        self.assertEqual(result["display"], (1080, 2340, 0))
        self.assertEqual(result["tree"]["children"][0]["attributes"]["text"], "Go")
        self.assertIn("tree", result["section_ms"])

    def test_a_foreground_change_inside_the_capture_is_not_consistent(self):
        device = GateDevice(capture_override=lambda marker: capture_transaction(
            marker, foreground_after=("Focus window: 43", MISSIONS_OK, "Focus window: 43")))
        result = self.snapshot(device)
        self.assertFalse(result["consistent"])
        self.assertEqual(result["consistency"], S.FOREGROUND_CHANGED)

    def test_an_unstable_foreground_is_not_consistent(self):
        device = GateDevice(capture_override=lambda marker: capture_transaction(
            marker, foreground=("Focus window: 42", MISSIONS_OK, "Focus window: 43")))
        result = self.snapshot(device)
        self.assertFalse(result["consistent"])
        self.assertEqual(result["consistency"], S.FOREGROUND_CHANGED)

    def test_malformed_tree_json_is_never_parsed_as_a_snapshot(self):
        device = GateDevice(capture_override=lambda marker: capture_transaction(
            marker, tree_text="{not json"))
        state = S.ProviderState()
        evidence = S.readiness_probe(device, state=state)
        result = S.capture_after_readiness(device, screen_before=evidence["state"], state=state)
        self.assertEqual(result["provider"], "legacy_fallback")
        self.assertIn("JSON", result["fallback_reason"])
        self.assertEqual(state.counts["legacy_fallback"], 1)

    def test_an_invalid_display_geometry_is_never_parsed_as_a_snapshot(self):
        device = GateDevice(capture_override=lambda marker: capture_transaction(
            marker, display="DisplayManagerService: display 0\n"))
        state = S.ProviderState()
        evidence = S.readiness_probe(device, state=state)
        result = S.capture_after_readiness(device, screen_before=evidence["state"], state=state)
        self.assertEqual(result["provider"], "legacy_fallback")
        self.assertIn("display", result["fallback_reason"].lower())

    def test_a_device_transport_fault_propagates(self):
        device = GateDevice()
        device.batch_probe = lambda script: (_ for _ in ()).throw(
            RuntimeFault("device_unavailable", "hdc failed"))
        with self.assertRaises(RuntimeFault):
            S.readiness_probe(device, state=S.ProviderState())


class ProviderSelectionTests(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.get(S.PROVIDER_FLAG)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._env is None:
            os.environ.pop(S.PROVIDER_FLAG, None)
        else:
            os.environ[S.PROVIDER_FLAG] = self._env

    def test_the_legacy_sequence_is_used_when_the_flag_is_off(self):
        os.environ[S.PROVIDER_FLAG] = "0"
        device = FakeDevice("fake")
        result = S.capture_snapshot(device, supports_foreground=False,
                                    state=S.ProviderState())
        self.assertEqual(result["provider"], "legacy")
        self.assertEqual(result["fallback_reason"], "flag_off")
        self.assertEqual(result["round_trips"], 4)

    def test_a_device_without_a_batched_probe_uses_the_legacy_sequence(self):
        os.environ[S.PROVIDER_FLAG] = "1"
        result = S.capture_snapshot(FakeDevice("fake"), supports_foreground=False,
                                    state=S.ProviderState())
        self.assertEqual(result["provider"], "legacy")
        self.assertEqual(result["fallback_reason"], "device_without_batched_probe")

    def test_the_batched_provider_is_selected_by_default(self):
        os.environ[S.PROVIDER_FLAG] = "1"
        name, reason = S.provider_choice(GateDevice(), True, S.ProviderState())
        self.assertEqual((name, reason), ("batched", None))

    def test_a_content_capture_that_cannot_be_parsed_falls_back_once_then_degrades(self):
        os.environ[S.PROVIDER_FLAG] = "1"
        device = GateDevice(capture_override=lambda marker: "garbage")
        state = S.ProviderState()
        results = []
        for _ in range(4):
            evidence = S.readiness_probe(device, state=state)
            results.append(S.capture_after_readiness(device, screen_before=evidence["state"],
                                                     state=state))
        self.assertEqual(results[0]["provider"], "legacy_fallback")
        self.assertTrue(results[0]["fallback_reason"])
        self.assertGreaterEqual(state.counts["legacy_fallback"],
                                S.ProviderState.MAX_CONSECUTIVE_FAILURES)
        self.assertTrue(state.degraded)
        self.assertEqual(S.provider_choice(device, True, state), ("legacy", "degraded"))

    def test_a_later_success_resets_the_failure_streak(self):
        os.environ[S.PROVIDER_FLAG] = "1"
        state = S.ProviderState()
        broken = GateDevice(capture_override=lambda marker: "garbage")
        evidence = S.readiness_probe(broken, state=state)
        S.capture_after_readiness(broken, screen_before=evidence["state"], state=state)
        good = GateDevice()
        evidence = S.readiness_probe(good, state=state)
        S.capture_after_readiness(good, screen_before=evidence["state"], state=state)
        self.assertEqual(state.consecutive_failures, 0)
        self.assertFalse(state.degraded)


class BatchedObservationTests(unittest.TestCase):
    """The batched capture must produce the same observation contract."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.device = GateDevice()
        self.runtime = Runtime(self.tmp.name, factory=DeviceFactory(self.device),
                               discover=lambda: ["fake"])
        self.sid = self.runtime.session("owner", "open")["session_id"]

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def test_fast_observation_reports_both_phases_and_stays_actionable(self):
        observation = self.runtime.observe("owner", self.sid)
        self.assertTrue(observation["actionable"])
        self.assertTrue(observation["snapshot_consistent"])
        self.assertEqual(observation["consistency_reason"], "consistent")
        self.assertEqual(observation["snapshot_capture"]["provider"], "batched")
        self.assertEqual(observation["snapshot_capture"]["round_trips"], 2)
        self.assertEqual(observation["perf"]["observe.capture_transactions"], 2)
        self.assertEqual(observation["perf"]["observe.mode"], "FAST")
        self.assertIn("observe.screen_ms", observation["perf"])
        self.assertIn("observe.tree_ms", observation["perf"])
        self.assertEqual(self.device.writes, 0)

    def test_an_inconsistent_batched_capture_is_never_actionable(self):
        self.device.capture_override = lambda marker: capture_transaction(
            marker, foreground_after=("Focus window: 99", MISSIONS_OK, "Focus window: 99"))
        observation = self.runtime.observe("owner", self.sid)
        self.assertFalse(observation["actionable"])
        self.assertNotIn(observation["observation_id"], self.runtime.sessions[self.sid].observations)

    def test_an_inconsistent_capture_cannot_authorise_an_action(self):
        observation = self.runtime.observe("owner", self.sid)
        self.device.capture_override = lambda marker: capture_transaction(
            marker, foreground_after=("Focus window: 99", MISSIONS_OK, "Focus window: 99"))
        with self.assertRaises(RuntimeFault) as error:
            self.runtime.act("owner", {
                "session_id": self.sid, "request_id": "batch-tap",
                "observation_id": observation["observation_id"],
                "action": {"kind": "tap", "target": {"action_id": "n0"}}})
        self.assertIn(error.exception.code, ("stale_observation", "target_not_found",
                                             "target_ambiguous"))
        self.assertEqual(self.device.writes, 0)

    def test_a_stale_target_is_still_refused_with_the_batched_capture(self):
        observation = self.runtime.observe("owner", self.sid)
        changed = json.loads(json.dumps(TREE))
        changed["children"][0]["attributes"]["text"] = "Gone"
        changed["children"][0]["attributes"]["bounds"] = "[500,500][600,600]"
        self.device.capture_override = lambda marker: capture_transaction(marker, tree=changed)
        with self.assertRaises(RuntimeFault) as error:
            self.runtime.act("owner", {
                "session_id": self.sid, "request_id": "batch-stale",
                "observation_id": observation["observation_id"],
                "action": {"kind": "tap", "target": {"action_id": "n0"}}})
        self.assertIn(error.exception.code, ("stale_observation", "target_not_found",
                                             "target_ambiguous"))
        self.assertEqual(self.device.writes, 0)

    def test_the_preflight_probe_is_a_fresh_live_gated_read(self):
        observation = self.runtime.observe("owner", self.sid)
        before = len(self.device.probes)
        result = self.runtime.act("owner", {
            "session_id": self.sid, "request_id": "batch-preflight",
            "observation_id": observation["observation_id"],
            "action": {"kind": "tap", "target": {"action_id": "n0"}}})
        self.assertEqual(result["execution_status"], "executed")
        self.assertGreaterEqual(len(self.device.probes) - before, 2)
        self.assertEqual(result["preflight_capture"]["provider"], "batched")
        self.assertEqual(self.device.writes, 1)

    def test_the_post_observation_is_reusable_by_its_own_id(self):
        observation = self.runtime.observe("owner", self.sid)
        result = self.runtime.act("owner", {
            "session_id": self.sid, "request_id": "batch-post",
            "observation_id": observation["observation_id"],
            "action": {"kind": "tap", "target": {"action_id": "n0"}}})
        after_id = result["after_observation_id"]
        self.assertIsNotNone(after_id)
        self.assertIsNotNone(self.runtime.cached_observation("owner", self.sid, after_id))
        second = self.runtime.act("owner", {
            "session_id": self.sid, "request_id": "batch-post-2",
            "observation_id": after_id,
            "action": {"kind": "tap", "target": {"action_id": "n0"}}})
        self.assertEqual(second["execution_status"], "executed")
        self.assertEqual(self.device.writes, 2)

    def test_action_perf_records_the_documented_metrics(self):
        observation = self.runtime.observe("owner", self.sid)
        result = self.runtime.act("owner", {
            "session_id": self.sid, "request_id": "batch-perf",
            "observation_id": observation["observation_id"],
            "action": {"kind": "tap", "target": {"action_id": "n0"}}})
        for key in ("act.preflight_ms", "act.dispatch_ms", "act.post_observe_ms", "act.total_ms"):
            self.assertIn(key, result["perf"])


if __name__ == "__main__":
    unittest.main()
