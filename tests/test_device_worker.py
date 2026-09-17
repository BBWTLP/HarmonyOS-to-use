import functools
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock
import multiprocessing.context
from harmony_runtime.contracts import RuntimeFault, Action
from harmony_runtime.device_worker import ProcessDevice
from harmony_runtime.runtime import Runtime


class InjectedDevice:
    def __init__(self, serial, path, hang=None):
        self.path = Path(path)
        self.hang = hang
        self.tree_calls = 0
        if hang == "init": time.sleep(20)
    def screen_state(self): return {"screen_on": True, "screen_locked": False}
    def tree(self):
        self.tree_calls += 1
        if self.hang == "watch" and self.tree_calls > 1: time.sleep(20)
        if self.hang == "tree": time.sleep(20)
        return {"attributes": {"text": "Ready", "bounds": "[0,0][100,100]", "type": "Button", "clickable": "true"}, "children": []}
    def display(self): return (100, 100, 0)
    def screenshot(self):
        from PIL import Image
        return Image.new("RGB", (100, 100), "blue")
    def dispatch(self, action, target):
        with self.path.open("a", encoding="utf-8") as output:
            output.write("write\n")
        if self.hang == "dispatch": time.sleep(20)
        if self.hang == "crash": os._exit(7)
    def close(self):
        if self.hang == "close": time.sleep(20)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "writes.txt"
        self.devices = []
    def tearDown(self):
        for device in self.devices: device.close()
        self.tmp.cleanup()
    def worker(self, hang=None):
        worker = ProcessDevice("fake", factory=functools.partial(InjectedDevice, path=str(self.path), hang=hang))
        self.devices.append(worker)
        return worker
    def warm(self, device):
        with device.budget(time.monotonic()+5, None): device.screen_state()

    def test_watch_short_deadline_interrupts_real_worker_read(self):
        device = self.worker("watch")
        runtime = Runtime(self.tmp.name, factory=lambda _: device, discover=lambda: ["fake"])
        try:
            sid = runtime.session("owner", "open")["session_id"]
            obs = runtime.observe("owner", sid)
            started = time.monotonic()
            result = runtime.burst("owner", dict(session_id=sid, request_id="short-watch",
                observation_id=obs["observation_id"], timeout_ms=3000,
                steps=[dict(action=dict(kind="tap", target=dict(text="Ready")),
                            expected=dict(text="Ready"), watch_timeout_ms=150)]))
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(result["steps"][0]["execution_status"], "not_dispatched")
            self.assertEqual(result["stop_reason"], "timeout")
            self.assertTrue(device.quarantined)
            self.assertFalse(self.path.exists())
        finally:
            runtime.close()

    def test_nested_budget_restores_outer_and_never_extends_deadline(self):
        device = self.worker()
        outer = time.monotonic() + 5
        check = lambda: None
        with device.budget(outer, check):
            with device.budget(outer + 10, check):
                self.assertEqual(device.deadline, outer)
            with device.budget(outer - 1, check):
                self.assertEqual(device.deadline, outer - 1)
            self.assertEqual(device.deadline, outer)
            self.assertIs(device.check, check)
        self.assertIsNone(device.deadline)

    def test_resident_worker_reused_and_image_roundtrips(self):
        device = self.worker()
        self.assertIsNone(device.process)
        self.warm(device)
        pid = device.process.pid
        with device.budget(time.monotonic()+5, None):
            self.assertEqual(device.tree()["attributes"]["text"], "Ready")
            self.assertEqual(device.screenshot().getpixel((0,0)), (0,0,255))
        self.assertEqual(device.process.pid, pid)
        self.assertNotEqual(pid, os.getpid())

    def test_hung_initialization_is_bounded_and_quarantined(self):
        device = self.worker("init")
        start = time.monotonic()
        with self.assertRaises(RuntimeFault) as ctx:
            with device.budget(start+.3, None): device.tree()
        self.assertEqual(ctx.exception.code, "timeout")
        self.assertLess(time.monotonic()-start, 1.5)
        self.assertFalse(device.process.is_alive())
        with self.assertRaises(RuntimeFault) as ctx:
            with device.budget(time.monotonic()+5, None): device.tree()
        self.assertEqual(ctx.exception.code, "device_quarantined")

    def test_read_hang_cancels_without_waiting_for_driver(self):
        device = self.worker("tree")
        self.warm(device)
        cancel = threading.Event()
        timer = threading.Timer(.1, cancel.set)
        def check():
            if cancel.is_set(): raise RuntimeFault("cancelled", "Test cancellation")
        timer.start()
        start = time.monotonic()
        try:
            with self.assertRaises(RuntimeFault) as ctx:
                with device.budget(start+5, check): device.tree()
            self.assertEqual(ctx.exception.code, "cancelled")
            self.assertLess(time.monotonic()-start, 1.5)
            self.assertFalse(device.process.is_alive())
        finally: timer.join()

    def test_hung_write_is_unknown_and_duplicate_never_replayed(self):
        device = self.worker("dispatch")
        runtime = Runtime(Path(self.tmp.name)/"runtime", factory=lambda serial:device, discover=lambda:["fake"])
        try:
            sid = runtime.session("owner", "open")["session_id"]
            obs = runtime.observe("owner", sid)
            request = {"session_id":sid,"request_id":"hang","observation_id":obs["observation_id"],"action":{"kind":"home"},"timeout_ms":200}
            start = time.monotonic()
            result = runtime.act("owner", request)
            self.assertLess(time.monotonic()-start, 1.5)
            self.assertEqual(result["execution_status"], "unknown")
            self.assertEqual(self.path.read_text(), "write\n")
            self.assertFalse(device.process.is_alive())
            self.assertEqual(runtime.sessions[sid].observations, {})
            retry = runtime.act("owner", request)
            self.assertTrue(retry["deduplicated"])
            self.assertEqual(retry["execution_status"], "unknown")
            self.assertEqual(self.path.read_text(), "write\n")
        finally: runtime.close()

    def test_explicit_recovery_reconnects_for_reads_without_clearing_unknown(self):
        devices = [self.worker("dispatch"), self.worker()]
        pending = iter(devices)
        runtime = Runtime(Path(self.tmp.name)/"recovery", factory=lambda serial:next(pending), discover=lambda:["fake"])
        try:
            sid = runtime.session("owner", "open")["session_id"]
            obs = runtime.observe("owner", sid)
            result = runtime.act("owner", {"session_id":sid,"request_id":"unknown","observation_id":obs["observation_id"],"action":{"kind":"home"},"timeout_ms":200})
            self.assertEqual(result["execution_status"], "unknown")
            recovered = runtime.session("owner", "recover", sid)
            self.assertEqual(recovered["status"], "recovered_read_only")
            self.assertTrue(recovered["recovery_required"])
            fresh = recovered["observation"]
            self.assertIn(fresh["observation_id"], runtime.sessions[sid].observations)
            self.assertFalse(devices[0].process.is_alive())
            self.assertTrue(devices[1].process.is_alive())
            with self.assertRaises(RuntimeFault) as ctx:
                runtime.act("owner", {"session_id":sid,"request_id":"new","observation_id":fresh["observation_id"],"action":{"kind":"home"}})
            self.assertEqual(ctx.exception.code,"reconciliation_required")
            self.assertEqual(self.path.read_text(), "write\n")
        finally: runtime.close()

    def test_recovery_after_read_failure_can_resume_normal_actions(self):
        first, second = self.worker("tree"), self.worker()
        self.warm(first)
        with self.assertRaises(RuntimeFault):
            with first.budget(time.monotonic()+.1, None): first.tree()
        pending = iter([first, second])
        runtime = Runtime(Path(self.tmp.name)/"read-recovery", factory=lambda serial:next(pending), discover=lambda:["fake"])
        try:
            sid = runtime.session("owner", "open")["session_id"]
            recovered = runtime.session("owner", "recover", sid)
            self.assertEqual(recovered["status"], "recovered")
            self.assertFalse(recovered["recovery_required"])
            self.assertFalse(self.path.exists())
            result = runtime.act("owner", {"session_id":sid,"request_id":"safe","observation_id":recovered["observation"]["observation_id"],"action":{"kind":"home"}})
            self.assertEqual(result["execution_status"], "executed")
            self.assertEqual(self.path.read_text(), "write\n")
        finally: runtime.close()

    def test_worker_crash_does_not_replay(self):
        device = self.worker("crash")
        self.warm(device)
        with self.assertRaises(RuntimeFault) as ctx:
            with device.budget(time.monotonic()+5, None): device.dispatch(Action(kind="home"), None)
        self.assertEqual(ctx.exception.code, "device_worker_lost")
        self.assertTrue(device.quarantined)
        self.assertEqual(self.path.read_text(), "write\n")

    def test_budget_expiring_during_spawn_never_sends_action(self):
        device = self.worker()
        original = multiprocessing.context.SpawnProcess.start
        def delayed_start(process):
            original(process)
            device.connection.send = Mock(wraps=device.connection.send)
            device.deadline = time.monotonic() - 1
        with patch.object(multiprocessing.context.SpawnProcess, "start", delayed_start):
            with self.assertRaises(RuntimeFault) as ctx:
                with device.budget(time.monotonic()+5, None):
                    device.dispatch(Action(kind="home"), None)
        self.assertEqual(ctx.exception.code, "timeout")
        self.assertTrue(device.quarantined)
        self.assertFalse(device.process.is_alive())
        self.assertFalse(self.path.exists())
        device.connection.send.assert_not_called()

    def test_pause_during_spawn_never_sends_action(self):
        device = self.worker()
        cancelled = threading.Event()
        original = multiprocessing.context.SpawnProcess.start
        def paused_start(process):
            original(process)
            device.connection.send = Mock(wraps=device.connection.send)
            cancelled.set()
        def check():
            if cancelled.is_set(): raise RuntimeFault("cancelled", "Session paused during startup")
        with patch.object(multiprocessing.context.SpawnProcess, "start", paused_start):
            with self.assertRaises(RuntimeFault) as ctx:
                with device.budget(time.monotonic()+5, check):
                    device.dispatch(Action(kind="home"), None)
        self.assertEqual(ctx.exception.code, "cancelled")
        self.assertTrue(device.quarantined)
        self.assertFalse(device.process.is_alive())
        self.assertFalse(self.path.exists())
        device.connection.send.assert_not_called()

    def test_hung_close_is_bounded(self):
        device = self.worker("close")
        self.warm(device)
        start = time.monotonic()
        device.close()
        self.assertLess(time.monotonic()-start, 1.5)
        self.assertFalse(device.process.is_alive())
        device.close()

if __name__ == "__main__": unittest.main()
