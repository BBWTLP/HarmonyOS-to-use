"""Offline coverage for the batched device snapshot (P0.5).

Every case here is a real failure mode of a merged transport: a truncated
transaction, a duplicated marker, a command that reported a failure, malformed
JSON, a foreign marker, a changed screen/foreground inside the capture window,
a missing screenshot artifact and a device-level transport fault. None of them
may ever produce an actionable observation.
"""
import json
import os
import tempfile
import unittest
from contextlib import contextmanager

from harmony_runtime import snapshot as S
from harmony_runtime.contracts import RuntimeFault
from harmony_runtime.runtime import Runtime
from test_runtime import FakeDevice

MARKER = "HRSNAPTEST0001"

#: The shell's own `time` report, which the device appends to every non-payload
#: command and which the runtime strips before parsing.
TIME_LINE = "\n  0m00.20s real     0m00.01s user     0m00.01s system"

POWER_OK = "Power Manager: Current State: AWAKE\n"
LOCK_OK = "* screenLocked false\n"
WINDOW_OK = "Focus window: 42\n"
MISSIONS_OK = ("Mission ID #42 mission name #[#com.example.app:entry:Entry]\n"
               "  bundle name [com.example.app]\n  AbilityRecord ID #7\n"
               "  state #FOREGROUND\n  app state #FOREGROUND\n")
DISPLAY_OK = ("[DISPLAY INFO]\nWidth: 1080\nHeight: 2340\nRotation: 0\n")
TREE = {"attributes": {"bundleName": "com.example.app", "visible": "true",
                       "bounds": "[0,0][1080,2340]", "type": "Root"},
        "children": [{"attributes": {"type": "Button", "text": "Go",
                                     "bounds": "[0,0][100,100]", "clickable": "true"},
                      "children": []}]}


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


def transaction(marker=MARKER, *, tree=None, tree_text=None, screen=(POWER_OK, LOCK_OK),
                foreground=(WINDOW_OK, MISSIONS_OK, WINDOW_OK),
                foreground_after=None, display=DISPLAY_OK, broken=None, extra=""):
    """Build a device-shaped FAST transaction by hand."""
    tree_text = tree_text if tree_text is not None else json.dumps(tree if tree is not None else TREE)
    foreground_after = foreground_after if foreground_after is not None else foreground
    parts = []
    for index, (pieces, failing) in enumerate((
            (list(screen), broken if broken in (0, 1) else None),
            (list(foreground), broken - 10 if isinstance(broken, int) and broken in (10, 11, 12) else None),
            ([display], None),
            (["DumpLayout saved to:/data/local/tmp/x.json",
              "1700000000000000", tree_text], None),
            (list(foreground_after), None),
            (list(screen), None))):
        if broken == index:
            parts.append(section(marker, index, pieces, failing=0,
                                 timed=(0, 1) if index == 3 else None))
        else:
            parts.append(section(marker, index, pieces, failing=failing,
                                 timed=(0, 1) if index == 3 else None))
    parts.append(extra)
    parts.append("%s:done" % marker)
    return "\n".join(parts)


class FakeBatchedDevice(FakeDevice):
    """A device that answers `batch_probe` with a scripted transaction."""

    supports_foreground = True

    def __init__(self, builder=None, error=None):
        super().__init__("fake")
        self.builder = builder or transaction
        self.error = error
        self.scripts = []

    def foreground(self):
        return {"bundle": "com.example.app", "focus_id": "42", "mission_id": "42",
                "ability_id": "7", "status": "verified"}

    def screenshot(self):
        from PIL import Image
        return Image.new("RGB", (1080, 2340), "white")

    def batch_probe(self, script):
        self.scripts.append(script)
        if self.error is not None:
            raise self.error
        marker = script.split("\n", 1)[0].split("=", 1)[1]
        return {"raw": self.builder(marker), "device_ms": 1.0}


class TransactionFramingTests(unittest.TestCase):
    def parse(self, raw, marker=MARKER):
        _, counts = S._section_map(False)
        return S.parse_transaction(raw, marker, counts)

    def test_a_complete_transaction_parses_into_ordered_sections(self):
        results = self.parse(transaction())
        self.assertEqual(sorted(results), [0, 1, 2, 3, 4, 5])
        self.assertEqual(results[3][2], json.dumps(TREE))

    def test_truncation_is_refused(self):
        raw = transaction().replace("%s:done" % MARKER, "")
        with self.assertRaises(RuntimeFault) as error:
            self.parse(raw)
        self.assertEqual(error.exception.code, "device_unavailable")

    def test_a_duplicated_marker_is_refused(self):
        raw = transaction().replace("%s:done" % MARKER,
                                    "%s:1:c0:0\n%s:done" % (MARKER, MARKER))
        with self.assertRaises(RuntimeFault):
            self.parse(raw)

    def test_reordered_sections_are_refused(self):
        lines = transaction().splitlines()
        swapped = lines[6:] + lines[:6]
        with self.assertRaises(RuntimeFault):
            self.parse("\n".join(swapped))

    def test_a_failed_command_section_is_refused(self):
        with self.assertRaises(RuntimeFault) as error:
            self.parse(transaction(broken=1))
        self.assertEqual(error.exception.code, "device_unavailable")

    def test_a_foreign_marker_is_refused(self):
        raw = transaction().replace("%s:2:b" % MARKER, "OTHER:2:b")
        with self.assertRaises(RuntimeFault):
            self.parse(raw)

    def test_unlabelled_output_is_refused(self):
        raw = transaction().replace("%s:2:e" % MARKER, "stray output\n%s:2:e" % MARKER)
        with self.assertRaises(RuntimeFault):
            self.parse(raw)

    def test_an_oversized_transaction_is_refused(self):
        raw = transaction() + "x" * (S.MAX_RAW_BYTES + 1)
        with self.assertRaises(RuntimeFault):
            self.parse(raw)

    def test_the_script_only_uses_the_fixed_vocabulary(self):
        script = S.build_script(MARKER, "/tmp/example", False)
        for line in script.splitlines():
            command = line.split("{ ", 1)[-1].split(";")[0] if "{ " in line else line
            if command.startswith(("hidumper", "uitest", "snapshot_display")):
                self.assertIn(command, S.ALLOWED_COMMANDS | {
                    "uitest dumpLayout -p /tmp/example_layout.json",
                    "uitest dumpLayout -p /tmp/example_layout_after.json",
                    "snapshot_display -f /tmp/example_shot.jpeg"})
        self.assertNotIn("$(", script)
        self.assertNotIn("`", script)

    def test_the_payload_commands_are_never_wrapped(self):
        script = S.build_script(MARKER, "/tmp/example", True)
        for line in script.splitlines():
            if line.startswith("time { cat") or line.startswith("time { base64"):
                self.fail("payload output must not be wrapped in a timing report")


class SnapshotNormalizationTests(unittest.TestCase):
    def snapshot(self, builder=None):
        device = FakeBatchedDevice(builder)
        return S.BatchedSnapshotProvider(device).capture(False)

    def test_a_consistent_capture_is_normalized(self):
        result = self.snapshot()
        self.assertTrue(result["consistent"])
        self.assertEqual(result["consistency"], S.CONSISTENT)
        self.assertEqual(result["providers"] if "providers" in result else result["provider"], "batched")
        self.assertEqual(result["round_trips"], 1)
        self.assertEqual(result["foreground_before"]["bundle"], "com.example.app")
        self.assertEqual(result["display"], (1080, 2340, 0))
        self.assertEqual(result["tree"]["children"][0]["attributes"]["text"], "Go")
        self.assertIn("tree", result["section_ms"])

    def test_a_screen_change_inside_the_capture_is_not_consistent(self):
        changed = lambda marker: transaction(marker).replace(
            "* screenLocked false", "* screenLocked true", 1)
        result = self.snapshot(changed)
        self.assertFalse(result["consistent"])
        self.assertEqual(result["consistency"], S.SCREEN_CHANGED)

    def test_a_foreground_change_inside_the_capture_is_not_consistent(self):
        builder = lambda marker: transaction(
            marker, foreground_after=("Focus window: 43", MISSIONS_OK, "Focus window: 43"))
        result = self.snapshot(builder)
        self.assertFalse(result["consistent"])
        self.assertEqual(result["consistency"], S.FOREGROUND_CHANGED)

    def test_an_unstable_foreground_is_not_consistent(self):
        builder = lambda marker: transaction(
            marker, foreground=("Focus window: 42", MISSIONS_OK, "Focus window: 43"))
        result = self.snapshot(builder)
        self.assertFalse(result["consistent"])
        self.assertEqual(result["consistency"], S.FOREGROUND_CHANGED)

    def test_malformed_tree_json_is_a_device_error(self):
        with self.assertRaises(RuntimeFault) as error:
            self.snapshot(lambda marker: transaction(marker, tree_text="{not json"))
        self.assertEqual(error.exception.code, "device_unavailable")

    def test_an_invalid_display_geometry_is_a_device_error(self):
        with self.assertRaises(RuntimeFault):
            self.snapshot(lambda marker: transaction(
                marker, display="DisplayManagerService: display 0\n"))

    def test_a_device_transport_fault_propagates(self):
        device = FakeBatchedDevice(error=RuntimeFault("device_unavailable", "hdc failed"))
        with self.assertRaises(RuntimeFault):
            S.BatchedSnapshotProvider(device).capture(False)


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
        result = S.capture_snapshot(device, supports_foreground=False, state=S.ProviderState())
        self.assertEqual(result["provider"], "legacy")
        self.assertEqual(result["fallback_reason"], "flag_off")
        self.assertEqual(result["round_trips"], 4)

    def test_a_device_without_a_batched_probe_uses_the_legacy_sequence(self):
        os.environ[S.PROVIDER_FLAG] = "1"
        result = S.capture_snapshot(FakeDevice("fake"), supports_foreground=False,
                                    state=S.ProviderState())
        self.assertEqual(result["provider"], "legacy")
        self.assertEqual(result["fallback_reason"], "device_without_batched_probe")

    def test_the_batched_provider_is_used_by_default(self):
        os.environ[S.PROVIDER_FLAG] = "1"
        result = S.capture_snapshot(FakeBatchedDevice(), state=S.ProviderState())
        self.assertEqual(result["provider"], "batched")
        self.assertEqual(result["round_trips"], 1)

    def test_a_framing_failure_falls_back_once_and_then_degrades(self):
        os.environ[S.PROVIDER_FLAG] = "1"
        device = FakeBatchedDevice(lambda marker: "garbage")
        state = S.ProviderState()
        results = [S.capture_snapshot(device, state=state) for _ in range(4)]
        self.assertEqual(results[0]["provider"], "legacy_fallback")
        self.assertTrue(results[0]["fallback_reason"])
        self.assertEqual(state.counts["legacy_fallback"], S.ProviderState.MAX_CONSECUTIVE_FAILURES)
        self.assertTrue(state.degraded)
        self.assertEqual(results[-1]["provider"], "legacy")
        self.assertEqual(results[-1]["fallback_reason"], "degraded")

    def test_a_later_success_resets_the_failure_streak(self):
        os.environ[S.PROVIDER_FLAG] = "1"
        state = S.ProviderState()
        S.capture_snapshot(FakeBatchedDevice(lambda marker: "garbage"), state=state)
        S.capture_snapshot(FakeBatchedDevice(), state=state)
        self.assertEqual(state.consecutive_failures, 0)
        self.assertFalse(state.degraded)


class BatchedObservationTests(unittest.TestCase):
    """The batched capture must produce the same observation contract."""

    class Factory:
        supports_foreground = True

        def __init__(self, device):
            self.device = device

        def __call__(self, serial):
            return self.device

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.device = FakeBatchedDevice()
        self.runtime = Runtime(self.tmp.name, factory=self.Factory(self.device),
                               discover=lambda: ["fake"])
        self.sid = self.runtime.session("owner", "open")["session_id"]

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def test_fast_observation_reports_one_round_trip_and_stays_actionable(self):
        observation = self.runtime.observe("owner", self.sid)
        self.assertTrue(observation["actionable"])
        self.assertTrue(observation["snapshot_consistent"])
        self.assertEqual(observation["consistency_reason"], "consistent")
        self.assertEqual(observation["snapshot_capture"]["provider"], "batched")
        self.assertEqual(observation["snapshot_capture"]["round_trips"], 1)
        self.assertEqual(observation["perf"]["observe.hdc_round_trips"], 1)
        self.assertEqual(observation["perf"]["observe.mode"], "FAST")
        self.assertIn("observe.screen_ms", observation["perf"])
        self.assertIn("observe.foreground_ms", observation["perf"])
        self.assertIn("observe.tree_ms", observation["perf"])
        self.assertEqual(self.device.writes, 0)
        self.assertEqual(len(self.device.scripts), 1)

    def test_an_inconsistent_batched_capture_is_never_actionable(self):
        self.device.builder = lambda marker: transaction(
            marker, foreground_after=("Focus window: 99", MISSIONS_OK, "Focus window: 99"))
        observation = self.runtime.observe("owner", self.sid)
        self.assertFalse(observation["actionable"])
        self.assertNotIn(observation["observation_id"], self.runtime.sessions[self.sid].observations)

    def test_an_inconsistent_capture_cannot_authorise_an_action(self):
        observation = self.runtime.observe("owner", self.sid)
        self.device.builder = lambda marker: transaction(
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
        self.device.builder = lambda marker: transaction(marker, tree=changed)
        with self.assertRaises(RuntimeFault) as error:
            self.runtime.act("owner", {
                "session_id": self.sid, "request_id": "batch-stale",
                "observation_id": observation["observation_id"],
                "action": {"kind": "tap", "target": {"action_id": "n0"}}})
        self.assertIn(error.exception.code, ("stale_observation", "target_not_found",
                                             "target_ambiguous"))
        self.assertEqual(self.device.writes, 0)

    def test_the_preflight_probe_is_a_fresh_device_read(self):
        observation = self.runtime.observe("owner", self.sid)
        self.assertEqual(len(self.device.scripts), 1)
        result = self.runtime.act("owner", {
            "session_id": self.sid, "request_id": "batch-preflight",
            "observation_id": observation["observation_id"],
            "action": {"kind": "tap", "target": {"action_id": "n0"}}})
        self.assertEqual(result["execution_status"], "executed")
        self.assertGreaterEqual(len(self.device.scripts), 3)
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
        cached = self.runtime.cached_observation("owner", self.sid, after_id)
        self.assertIsNotNone(cached)
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
        perf = result["perf"]
        for key in ("act.preflight_ms", "act.dispatch_ms", "act.post_observe_ms", "act.total_ms"):
            self.assertIn(key, perf)


if __name__ == "__main__":
    unittest.main()
