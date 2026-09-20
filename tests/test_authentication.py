import copy
import tempfile
import unittest
from harmony_runtime.observation import snapshot, matches
from harmony_runtime.contracts import Expected, RuntimeFault
from harmony_runtime.runtime import Runtime


def auth_tree(bundle="com.ohos.sceneboard"):
    def node(kind, identifier, text=""):
        return {"attributes": {"type": kind, "id": identifier, "text": text,
                "bounds": "[0,0][100,100]", "clickable": "true", "focused": "true"}}
    return {"attributes": {"bundleName": bundle}, "children": [
        node("UIExtensionComponent", "userauthuiextensionability-test"),
        node("Column", "NumberPasswordTitleGroup"),
        node("TextInput", "pinSix"), node("Button", "key1", "1")]}


class AuthDevice:
    def __init__(self, serial):
        self.blocked = True
        self.writes = 0
        self.reads = 0
    def tree(self):
        self.reads += 1
        return auth_tree() if self.blocked else {"attributes": {"text": "Home", "bounds": "[0,0][100,100]"}}
    def display(self): return (100, 100, 0)
    def screen_state(self): return {"screen_on": True, "screen_locked": False}
    def dispatch(self, action, target):
        self.writes += 1
        self.blocked = action.kind == "launch"
    def close(self): pass


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Runtime(self.tmp.name, factory=AuthDevice, discover=lambda: ["fake"])
        self.sid = self.runtime.session("a", "open")["session_id"]
    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()
    def observe(self): return self.runtime.observe("a", self.sid)
    def request(self, obs, action, expected=None):
        return dict(session_id=self.sid, request_id="request", observation_id=obs["observation_id"],
                    action=action, expected=expected, timeout_ms=15000)
    def test_requires_system_provenance_and_full_signature(self):
        self.assertIsNotNone(snapshot(auth_tree(), (100,100,0))["blocking_dialog"])
        self.assertIsNone(snapshot(auth_tree("com.example.app"), (100,100,0))["blocking_dialog"])
        for index in range(3):
            tree = copy.deepcopy(auth_tree())
            del tree["children"][index]
            self.assertIsNone(snapshot(tree, (100,100,0))["blocking_dialog"])
    def test_keypad_tap_is_rejected_before_dispatch(self):
        obs = self.observe()
        with self.assertRaises(RuntimeFault) as error:
            self.runtime.act("a", self.request(obs, {"kind":"tap", "target":{"text":"1"}}))
        self.assertEqual(error.exception.code, "authentication_required")
        self.assertEqual(self.runtime.devices["fake"].writes, 0)
        self.assertEqual(self.runtime.journal.history(), [])
    def test_credential_input_and_swipe_are_rejected(self):
        for action in ({"kind":"input_text", "target":{"resource_id":"pinSix"}, "text":"test"},
                       {"kind":"swipe", "direction":"up"}):
            with self.subTest(action=action):
                obs = self.observe()
                with self.assertRaises(RuntimeFault) as error:
                    self.runtime.act("a", self.request(obs, action))
                self.assertEqual(error.exception.code, "authentication_required")
                self.assertEqual(self.runtime.devices["fake"].writes, 0)
    def test_back_can_escape_and_verify(self):
        obs = self.observe()
        result = self.runtime.act("a", self.request(obs, {"kind":"back"}, {"text":"Home"}))
        self.assertEqual(result["verification_status"], "verified")
        self.assertEqual(self.runtime.devices["fake"].writes, 1)
    def test_post_launch_blocker_stops_before_changed_success_and_is_deduplicated(self):
        self.observe()
        device = self.runtime.devices["fake"]
        device.blocked = False
        obs = self.observe()
        reads = device.reads
        request = self.request(obs, {"kind":"launch", "bundle":"com.example.app"}, {"changed":True})
        result = self.runtime.act("a", request)
        self.assertEqual(result["status"], "authentication_required")
        self.assertEqual(result["execution_status"], "executed")
        self.assertEqual(result["verification_status"], "inconclusive")
        self.assertTrue(result["incident_id"])
        self.assertEqual(device.reads - reads, 2)
        self.assertTrue(self.runtime.act("a", request)["deduplicated"])
        self.assertEqual(device.writes, 1)
    def test_wait_never_matches_auth_dialog(self):
        for options in ({"expected":{"text":"1"}}, {"condition":{"type":"text_present","value":"1"}}):
            result = self.runtime.wait("a", self.sid, **options)
            self.assertEqual(result["status"], "authentication_required")
        self.assertFalse(matches(self.observe(), Expected(text="1")))


if __name__ == "__main__": unittest.main()
