import tempfile
import unittest
from harmony_runtime.runtime import Runtime
from harmony_runtime.contracts import RuntimeFault
from harmony_runtime.journal import Journal

class FakeDevice:
    def __init__(self, serial): self.writes=0; self.text="Settings"; self.fail=False
    def screen_state(self): return {"screen_on": True, "screen_locked": False}
    def tree(self): return {"attributes":{"text":self.text,"bounds":"[0,0][100,100]","clickable":"true","type":"Button"},"children":[]}
    def display(self): return (100,100,0)
    def dispatch(self, action, target):
        self.writes += 1
        if self.fail: raise OSError("Disconnected after dispatch")
        self.text="After"
    def close(self): pass

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.device=FakeDevice("fake")
        self.creations=0
        def factory(serial): self.creations+=1; return self.device
        self.runtime=Runtime(self.tmp.name,factory=factory,discover=lambda:["fake"])
        self.sid=self.runtime.session("a","open")["session_id"]
    def tearDown(self): self.runtime.close();self.tmp.cleanup()
    def request(self):
        obs=self.runtime.observe("a",self.sid)
        return dict(session_id=self.sid,request_id="r1",observation_id=obs["observation_id"],action={"kind":"tap","target":{"action_id":"n0"}})
    def test_bundle_verification_rejected_before_dispatch(self):
        request = self.request()
        request['action'] = {'kind': 'launch', 'bundle': 'com.example.app'}
        request['expected'] = {'bundle': 'com.example.app'}
        with self.assertRaises(RuntimeFault) as error:
            self.runtime.act('a', request)
        self.assertEqual(error.exception.code, 'unsupported_capability')
        self.assertEqual(self.device.writes, 0)
        self.assertEqual(self.runtime.journal.history(), [])

    def test_bundle_wait_unavailable_without_device_initialization(self):
        status = self.runtime.session('a', 'status', self.sid)
        self.assertFalse(status['capabilities']['foreground_bundle'])
        with self.assertRaises(RuntimeFault) as error:
            self.runtime.wait('a', self.sid, {'bundle': 'com.example.app'})
        self.assertEqual(error.exception.code, 'unsupported_capability')
        self.assertEqual(self.creations, 0)

    def test_lazy_and_lease(self):
        self.assertEqual(self.creations,0)
        with self.assertRaises(RuntimeFault) as ctx: self.runtime.session("b","open")
        self.assertEqual(ctx.exception.code,"lease_conflict")
    def test_duplicate_dispatches_once(self):
        req=self.request()
        first=self.runtime.act("a",req)
        second=self.runtime.act("a",req)
        self.assertEqual(self.device.writes,1)
        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["verification_status"],"inconclusive")
    def test_stale_rejected(self):
        req=self.request();self.device.text="Changed"
        with self.assertRaises(RuntimeFault) as ctx: self.runtime.act("a",req)
        self.assertEqual(ctx.exception.code,"stale_observation")
        self.assertEqual(self.device.writes,0)
    def test_unknown_not_replayed(self):
        req=self.request();self.device.fail=True
        self.assertEqual(self.runtime.act("a",req)["execution_status"],"unknown")
        self.runtime.act("a",req)
        self.assertEqual(self.device.writes,1)
    def test_conflict_rejected(self):
        req=self.request();self.runtime.act("a",req)
        req["action"]={"kind":"home"}
        with self.assertRaises(RuntimeFault) as ctx:self.runtime.act("a",req)
        self.assertEqual(ctx.exception.code,"request_conflict")
    def test_postcondition_verification(self):
        req=self.request();req["expected"]={"text":"After"}
        self.assertEqual(self.runtime.act("a",req)["verification_status"],"verified")
    def test_pause_blocks_write(self):
        req=self.request();self.runtime.session("a","pause",self.sid)
        with self.assertRaises(RuntimeFault):self.runtime.act("a",req)
        self.assertEqual(self.device.writes,0)
    def test_input_requires_observed_focus(self):
        from harmony_runtime.contracts import Action
        action=Action(kind="input_text",target={"action_id":"n0"},text="test")
        with self.assertRaises(RuntimeFault) as ctx:
            self.runtime._policy(action,{"type":"TextInput","focused":False})
        self.assertEqual(ctx.exception.code,"focus_required")
    def test_bundle_in_catalog_does_not_prove_foreground(self):
        from harmony_runtime.observation import matches
        from harmony_runtime.contracts import Expected
        self.assertFalse(matches({"catalog":[{"bundle":"com.example.app","text":"App"}]},Expected(bundle="com.example.app")))
    def test_non_catalog_state_change_invalidates_snapshot(self):
        from harmony_runtime.observation import snapshot
        tree={"attributes":{"type":"Overlay","bounds":"[0,0][100,100]","visible":True}}
        before=snapshot(tree,(100,100,0))
        tree["attributes"]["visible"]=False
        after=snapshot(tree,(100,100,0))
        self.assertNotEqual(before["fingerprint"],after["fingerprint"])
    def test_queue_wait_respects_budget(self):
        import threading
        import time
        req=self.request();req["timeout_ms"]=100
        held=threading.Event();release=threading.Event()
        def hold():
            with self.runtime.device_locks["fake"]:
                held.set();release.wait(2)
        worker=threading.Thread(target=hold);worker.start();held.wait(1)
        start=time.monotonic()
        try:
            with self.assertRaises(RuntimeFault) as ctx:self.runtime.act("a",req)
            self.assertEqual(ctx.exception.code,"timeout")
            self.assertLess(time.monotonic()-start,.7)
            self.assertEqual(self.device.writes,0)
        finally:release.set();worker.join()
    def test_image_capture_page_change_is_not_actionable(self):
        from PIL import Image
        def screenshot():
            self.device.text = "Changed during screenshot"
            return Image.new("RGB", (100, 100))
        self.device.screenshot = screenshot
        obs = self.runtime.observe("a", self.sid, include_image=True)
        self.assertFalse(obs["image_tree_consistent"])
        self.assertFalse(obs["actionable"])
        self.assertNotIn(obs["observation_id"], self.runtime.sessions[self.sid].observations)
        with self.assertRaises(RuntimeFault) as ctx:
            self.runtime.act("a", dict(session_id=self.sid, request_id="unstable",
                observation_id=obs["observation_id"], action={"kind":"home"}))
        self.assertEqual(ctx.exception.code, "stale_observation")
        self.assertEqual(self.device.writes, 0)

    def test_image_capture_stable_bracket_is_reported(self):
        from PIL import Image
        self.device.screenshot = lambda: Image.new("RGB", (100, 100))
        obs = self.runtime.observe("a", self.sid, include_image=True)
        self.assertTrue(obs["image_tree_consistent"])
        self.assertTrue(obs["actionable"])
        self.assertEqual(obs["capture_consistency"], "tree_bracket_matched")

    def test_pause_resume_cancels_action_already_reading(self):
        import threading
        req=self.request()
        entered=threading.Event();release=threading.Event();errors=[]
        original=self.device.tree
        def tree():
            entered.set()
            if not release.wait(2): raise RuntimeError("Test barrier timed out")
            return original()
        self.device.tree=tree
        def run():
            try:self.runtime.act("a",req)
            except RuntimeFault as exc:errors.append(exc.code)
        thread=threading.Thread(target=run);thread.start()
        try:
            self.assertTrue(entered.wait(1))
            self.runtime.session("a","pause",self.sid)
            self.runtime.session("a","resume",self.sid)
        finally:release.set();thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors,["cancelled"])
        self.assertEqual(self.runtime.sessions[self.sid].observations, {})
        self.assertEqual(self.device.writes,0)
        self.assertEqual(self.runtime.journal.history(),[])

    def test_close_keeps_device_reserved_until_inflight_read_finishes(self):
        import threading
        entered=threading.Event();release=threading.Event();errors=[]
        original=self.device.tree
        def tree():
            entered.set()
            if not release.wait(2):raise RuntimeError("Test barrier timed out")
            return original()
        self.device.tree=tree
        def run():
            try:self.runtime.observe("a",self.sid)
            except RuntimeFault as exc:errors.append(exc.code)
        thread=threading.Thread(target=run);thread.start()
        try:
            self.assertTrue(entered.wait(1))
            self.runtime.session("a","close",self.sid)
            with self.assertRaises(RuntimeFault) as ctx:self.runtime.session("b","open")
            self.assertEqual(ctx.exception.code,"device_busy")
        finally:release.set();thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors,["session_invalid"])
        self.assertEqual(self.runtime.session("b","open")["status"],"open")

    def test_wait_queue_uses_call_budget_not_observe_default(self):
        import threading
        import time
        held=threading.Event();release=threading.Event()
        def hold():
            with self.runtime.device_locks["fake"]:
                held.set();release.wait(2)
        thread=threading.Thread(target=hold);thread.start();held.wait(1)
        start=time.monotonic()
        try:
            result=self.runtime.wait("a",self.sid,{"text":"Before"},timeout_ms=100)
            self.assertEqual(result["status"],"timeout")
            self.assertIsNone(result["observation"])
            self.assertLess(time.monotonic()-start,.7)
            self.assertEqual(self.creations,0)
        finally:release.set();thread.join(2)

    def test_locked_observation_rejects_before_capture_and_invalidates_targets(self):
        self.request()
        self.device.screen_state = lambda: {"screen_on": False, "screen_locked": True}
        def forbidden(): raise AssertionError("Locked screen must not be captured")
        self.device.tree = forbidden
        with self.assertRaises(RuntimeFault) as ctx:
            self.runtime.observe("a", self.sid, include_image=True)
        self.assertEqual(ctx.exception.code, "screen_locked")
        self.assertEqual(self.runtime.sessions[self.sid].observations, {})

    def test_lock_during_capture_rejects_observation(self):
        original = self.device.tree
        def tree():
            self.device.screen_state = lambda: {"screen_on": True, "screen_locked": True}
            return original()
        self.device.tree = tree
        with self.assertRaises(RuntimeFault) as ctx: self.runtime.observe("a", self.sid)
        self.assertEqual(ctx.exception.code, "screen_locked")
        self.assertEqual(self.runtime.sessions[self.sid].observations, {})


    def test_observe_automatically_wakes_and_unlocks_when_driver_supports_it(self):
        class AutoUnlockDevice(FakeDevice):
            def __init__(self, serial):
                super().__init__(serial)
                self.state = {"screen_on": False, "screen_locked": True}
                self.recovery = []

            def screen_state(self):
                return dict(self.state)

            def screen_on(self):
                self.recovery.append("screen_on")
                self.state["screen_on"] = True

            def wake_up_display(self):
                self.recovery.append("wake_up_display")
                self.state["screen_on"] = True

            def unlock(self):
                self.recovery.append("unlock")
                self.state["screen_locked"] = False

        device = AutoUnlockDevice("fake")
        runtime = Runtime(self.tmp.name + "-auto-unlock", factory=lambda serial: device, discover=lambda: ["fake"])
        try:
            sid = runtime.session("a", "open")["session_id"]
            observation = runtime.observe("a", sid)
            self.assertEqual(observation["status"], "ok")
            self.assertEqual(observation["screen_state"], {"screen_on": True, "screen_locked": False})
            self.assertEqual(device.recovery, ["screen_on", "wake_up_display", "unlock"])
        finally:
            runtime.close()

    def test_unknown_screen_blocks_write_without_journaling_dispatch(self):
        req = self.request()
        self.device.screen_state = lambda: {"screen_on": True, "screen_locked": None}
        with self.assertRaises(RuntimeFault) as ctx: self.runtime.act("a", req)
        self.assertEqual(ctx.exception.code, "screen_state_unknown")
        self.assertEqual(self.device.writes, 0)
        self.assertEqual(self.runtime.journal.history(), [])

    def test_restart_marks_unfinished_unknown(self):
        path=self.runtime.root/"separate.sqlite3"
        journal=Journal(path);journal.begin("interrupted","digest");journal.close()
        journal=Journal(path)
        self.assertEqual(journal.lookup("interrupted","digest")["execution_status"],"unknown")
        journal.close()

if __name__ == "__main__": unittest.main()
