import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from PIL import Image
from harmony_runtime.probe import run, summarize
from harmony_runtime.runtime import Runtime
from harmony_runtime.service import serve, Client
from test_runtime import FakeDevice


class ProbeTests(unittest.TestCase):
    def test_missing_service_does_not_start_one(self):
        with tempfile.TemporaryDirectory() as root:
            result = run(root)
            self.assertEqual(result["error_code"], "runtime_unavailable")
            self.assertFalse(result["phone_observation_verified"])
            self.assertFalse((Path(root) / "endpoint.json").exists())

    def test_live_stdio_probe_releases_lease_and_never_writes(self):
        with tempfile.TemporaryDirectory() as root:
            device = FakeDevice("fake")
            device.text = "PRIVATE_PHONE_CONTENT"
            device.screenshot = lambda: Image.new("RGB", (100, 100))
            ready = threading.Event()
            servers = []
            def notify(server):
                servers.append(server)
                ready.set()
            thread = threading.Thread(target=serve, args=(root,), kwargs={
                "ready": notify, "runtime_factory": lambda p: Runtime(p,
                    factory=lambda serial: device, discover=lambda: ["fake"])})
            thread.start()
            self.assertTrue(ready.wait(10))
            try:
                result = run(root)
                self.assertEqual(result["status"], "ok")
                self.assertTrue(result["phone_observation_verified"])
                self.assertFalse(result["phone_write_verified"])
                self.assertNotIn("PRIVATE_PHONE_CONTENT", str(result))
                self.assertEqual(device.writes, 0)
                client = Client(root)
                sid = client.call("session", operation="open")["session_id"]
                client.call("session", operation="close", session_id=sid)
            finally:
                servers[0].shutdown()
                thread.join(10)
            self.assertFalse(thread.is_alive())

    def test_inconsistent_or_missing_image_does_not_pass(self):
        for consistent, types in [(False, ["image"]), (True, ["text"])]:
            result = summarize(SimpleNamespace(is_error=False,
                structured_content={"status": "ok", "image_tree_consistent": consistent},
                content=[SimpleNamespace(type=t) for t in types]))
            self.assertEqual(result["status"], "not_ready")
            self.assertFalse(result["phone_observation_verified"])
