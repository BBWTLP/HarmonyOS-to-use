import base64
import io
import tempfile
import unittest
from PIL import Image
from harmony_runtime.contracts import RuntimeFault
from harmony_runtime.runtime import Runtime
from test_runtime import FakeDevice


class ImageDevice(FakeDevice):
    def __init__(self, serial):
        super().__init__(serial)
        self.image = Image.new("RGB", (100,100), "white")
        self.change_on_image = False
        self.captures = 0

    def screenshot(self):
        self.captures += 1
        if self.change_on_image:
            self.text = "Changed"
        return self.image


class FullObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.device = ImageDevice("fake")
        self.runtime = Runtime(self.tmp.name, factory=lambda _:self.device, discover=lambda:["fake"])
        self.sid = self.runtime.session("owner", "open")["session_id"]

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def test_full_labels_match_actionable_catalog_and_preserve_original(self):
        obs = self.runtime.observe("owner", self.sid, mode="FULL")
        self.assertEqual(obs["mode"], "FULL")
        self.assertEqual(obs["tree"], self.device.tree())
        self.assertTrue(obs["som"]["available"])
        self.assertEqual(obs["som"]["observation_id"], obs["observation_id"])
        self.assertEqual(obs["som"]["labels"], [{"action_id":"n0", "hit_bounds":[0,0,100,100]}])
        original = Image.open(io.BytesIO(base64.b64decode(obs["image"]["base64"])))
        marked = Image.open(io.BytesIO(base64.b64decode(obs["annotated_image"]["base64"])))
        self.assertEqual(original.tobytes(), self.device.image.tobytes())
        self.assertNotEqual(original.tobytes(), marked.tobytes())
        result = self.runtime.act("owner", {"session_id":self.sid, "request_id":"full-tap",
            "observation_id":obs["observation_id"], "action":{"kind":"tap", "target":{"action_id":"n0"}}})
        self.assertEqual(result["execution_status"], "executed")
        self.assertEqual(self.device.writes, 1)

    def test_changed_or_wrong_dimensions_never_produce_actionable_labels(self):
        for changed, size in [(True,(100,100)), (False,(50,100))]:
            with self.subTest(changed=changed, size=size):
                self.device.change_on_image = changed
                self.device.text = "Settings"
                self.device.image = Image.new("RGB", size)
                obs = self.runtime.observe("owner", self.sid, mode="FULL")
                self.assertFalse(obs["actionable"])
                self.assertFalse(obs["som"]["available"])
                self.assertNotIn("annotated_image", obs)
                self.assertNotIn(obs["observation_id"], self.runtime.sessions[self.sid].observations)
        self.assertEqual(self.device.writes, 0)

    def test_fast_keeps_no_image_default_and_rejects_unsupported_modes(self):
        obs = self.runtime.observe("owner", self.sid)
        self.assertEqual(obs["mode"], "FAST")
        self.assertNotIn("tree", obs)
        self.assertNotIn("image", obs)
        self.assertEqual(self.device.captures, 0)
        with self.assertRaises(RuntimeFault) as error:
            self.runtime.observe("owner", self.sid, mode="INVALID")
        self.assertEqual(error.exception.code, "unsupported_capability")
        self.assertEqual(self.device.captures, 0)

    def test_disabled_targets_are_not_marked(self):
        tree = self.device.tree()
        tree["attributes"]["enabled"] = False
        self.device.tree = lambda:tree
        obs = self.runtime.observe("owner", self.sid, mode="FULL")
        self.assertEqual(obs["som"]["labels"], [])
        self.assertEqual(obs["som"]["target_count"], 0)
