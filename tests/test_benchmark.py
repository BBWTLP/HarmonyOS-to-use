import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from PIL import Image
from harmony_runtime.benchmark import distribution, run, sample_metadata, summarize
from harmony_runtime.cli import main
from harmony_runtime.runtime import Runtime
from harmony_runtime.service import Client, serve
from test_runtime import FakeDevice
from test_authentication import AuthDevice


def result(**data):
    defaults = {"status": "ok", "actionable": True, "image_tree_consistent": True,
                "som": {"available": True}}
    defaults.update(data)
    return SimpleNamespace(is_error=False, structured_content=defaults,
                           content=[SimpleNamespace(type="image")])


class BenchmarkTests(unittest.TestCase):
    def test_failure_checks_are_boolean_only_and_do_not_guess_cause(self):
        row = sample_metadata(result(actionable=False, foreground_consistent=True,
                              image_tree_consistent=False, image_dimensions_match=True,
                              image_tree_skew_ms=750, capture_consistency="PRIVATE_REASON"),
                              1, 10, "FAST", True)
        self.assertEqual(row["error_code"], "capture_unverified")
        self.assertFalse(row["checks"]["image_tree_consistent"])
        self.assertTrue(row["checks"]["foreground_consistent"])
        self.assertEqual(row["image_tree_skew_ms"], 750)
        self.assertNotIn("PRIVATE", str(row))
        row = sample_metadata(result(foreground_consistent="PRIVATE", image_dimensions_match=1,
                              image_tree_skew_ms=float("inf")), 1, 10, "FAST", False)
        self.assertNotIn("foreground_consistent", row["checks"])
        self.assertNotIn("image_dimensions_match", row["checks"])
        self.assertNotIn("image_tree_skew_ms", row)

    def test_nearest_rank_and_empty_samples(self):
        self.assertEqual(distribution(range(1, 21))["p95_ms"], 19)
        self.assertEqual(distribution(range(1, 21))["p50_ms"], 10)
        self.assertEqual(distribution([8])["p95_ms"], 8)
        self.assertIsNone(distribution([])["p50_ms"])
        self.assertEqual(distribution([True, -1, float("nan"), float("inf"), "secret"])["count"], 0)

    def test_metadata_allowlist_strips_content_and_unknown_timing(self):
        source = result(tree="PRIVATE_TREE", image="PRIVATE_BASE64", catalog=["PRIVATE_TEXT"],
                        device_id="PRIVATE_SERIAL", timing={"tree_ms": 8, "PRIVATE_ms": 10,
                        "display_ms": True, "snapshot_ms": float("nan"), "queue_ms": -1}, capture_ms=10)
        row = sample_metadata(source, 1, 20, "FAST", True)
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["timing"], {"tree_ms": 8})
        self.assertNotIn("PRIVATE", json.dumps(row))

    def test_inconsistent_missing_image_and_full_annotation_not_success(self):
        for source, mode, image in [(result(image_tree_consistent=False), "FAST", True),
                                    (result(actionable=False), "FAST", False),
                                    (result(som={"available": False}), "FULL", False)]:
            self.assertEqual(sample_metadata(source, 1, 10, mode, image)["status"], "not_ready")
        source = result()
        source.content = []
        self.assertEqual(sample_metadata(source, 1, 10, "FAST", True)["status"], "not_ready")
        self.assertEqual(sample_metadata(source, 1, 10, "FAST", False)["status"], "ok")

    def test_authentication_is_not_a_successful_sample(self):
        row = sample_metadata(result(blocking_dialog={"reason": "PRIVATE_AUTH"}), 1, 10, "FAST", False)
        self.assertEqual(row["error_code"], "authentication_required")
        self.assertNotIn("PRIVATE", str(row))

    def test_error_code_is_allowlisted(self):
        source = result(error={"code": "private_secret", "message": "PRIVATE_ERROR"})
        source.is_error = True
        row = sample_metadata(source, 1, 10, "FAST", False)
        self.assertEqual(row["error_code"], "mcp_error")
        self.assertNotIn("private", str(row))

    def test_failure_denominators_and_first_not_cold(self):
        rows = [sample_metadata(result(), 1, 10, "FAST", False),
                sample_metadata(result(actionable=False), 2, 20, "FAST", False)]
        report = summarize(rows, 3, "FAST", False)
        self.assertEqual(report["status"], "not_ready")
        self.assertEqual(report["successful_samples"], 1)
        self.assertEqual(report["failed_samples"], 1)
        self.assertEqual(report["unattempted_samples"], 1)
        self.assertEqual(report["metrics"]["all_attempts"]["client"]["count"], 2)
        self.assertEqual(report["metrics"]["successful"]["client"]["count"], 1)
        self.assertEqual(report["metrics"]["first_attempt"]["client"]["p50_ms"], 10)
        self.assertEqual(report["metrics"]["subsequent_attempts"]["client"]["p50_ms"], 20)
        self.assertEqual(report["thermal_state"], "uncontrolled")

    def test_cli_requires_opt_in_and_bounds_before_connecting(self):
        for argv in (["benchmark"], ["benchmark", "--execute", "--samples", "0"],
                     ["benchmark", "--execute", "--samples", "501"]):
            with self.subTest(argv=argv), patch("sys.argv", ["harmony-runtime", *argv]), \
                    patch("harmony_runtime.benchmark.run") as called, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main()
                self.assertEqual(error.exception.code, 2)
                called.assert_not_called()

    def test_cli_passes_options_and_failure_exit_code(self):
        with patch("sys.argv", ["harmony-runtime", "benchmark", "--execute", "--samples", "3", "--mode", "FULL", "--state-dir", "root"]), \
                patch("harmony_runtime.benchmark.run", return_value={"status": "not_ready"}) as called, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(), 1)
        called.assert_called_once_with("root", 3, "FULL", False)
        self.assertEqual(json.loads(output.getvalue())["status"], "not_ready")

    def test_python_api_validates_before_connecting(self):
        for count in (0, 501, True, 1.5):
            with self.assertRaises(ValueError):
                run("unused", samples=count)
        with self.assertRaises(ValueError):
            run("unused", mode="TEMPORAL")

    def test_missing_service_does_not_start_one(self):
        with tempfile.TemporaryDirectory() as root:
            report = run(root, samples=2)
            self.assertEqual(report["error_code"], "runtime_unavailable")
            self.assertEqual(report["attempted_samples"], 0)
            self.assertEqual(report["unattempted_samples"], 2)
            self.assertFalse((Path(root)/"endpoint.json").exists())

    def test_transport_error_is_counted_and_close_attempted(self):
        client = AsyncMock()
        client.call_tool.side_effect = [SimpleNamespace(is_error=False, structured_content={"session_id": "fake"}),
                                       OSError("PRIVATE_TOKEN"), SimpleNamespace(is_error=False)]
        @contextlib.asynccontextmanager
        async def streams(*args):
            yield (None, None)
        @contextlib.asynccontextmanager
        async def session(*args):
            yield client
        with patch("harmony_runtime.benchmark.stdio_client", streams), patch("harmony_runtime.benchmark.ClientSession", session):
            report = run("unused", samples=3)
        self.assertEqual(report["failed_samples"], 1)
        self.assertEqual(report["unattempted_samples"], 2)
        self.assertEqual(report["error_code"], "benchmark_transport_failed")
        self.assertNotIn("PRIVATE", json.dumps(report))
        self.assertEqual(client.call_tool.call_args.args, ("mobile_session", {"operation": "close", "session_id": "fake"}))

    def test_malformed_response_is_counted_and_session_closed(self):
        for malformed in (result(som=None), SimpleNamespace(is_error=False,
                          structured_content=["PRIVATE_PAYLOAD"], content=[])):
            with self.subTest(response=type(malformed.structured_content).__name__):
                client = AsyncMock()
                client.call_tool.side_effect = [
                    SimpleNamespace(is_error=False, structured_content={"session_id": "fake"}),
                    malformed, SimpleNamespace(is_error=False)]
                @contextlib.asynccontextmanager
                async def streams(*args):
                    yield (None, None)
                @contextlib.asynccontextmanager
                async def session(*args):
                    yield client
                with patch("harmony_runtime.benchmark.stdio_client", streams), patch(
                        "harmony_runtime.benchmark.ClientSession", session):
                    report = run("unused", samples=3, mode="FULL")
                self.assertEqual(report["attempted_samples"], 1)
                self.assertEqual(report["failed_samples"], 1)
                self.assertEqual(report["unattempted_samples"], 2)
                self.assertEqual(report["error_code"], "invalid_observation")
                self.assertEqual(report["error_counts"], {"invalid_observation": 1})
                self.assertNotIn("PRIVATE", json.dumps(report))
                self.assertEqual(client.call_tool.call_args.args, ("mobile_session", {
                    "operation": "close", "session_id": "fake"}))

    def test_close_failure_cannot_report_success(self):
        client = AsyncMock()
        client.call_tool.side_effect = [SimpleNamespace(is_error=False, structured_content={"session_id": "fake"}),
                                       result(), SimpleNamespace(is_error=True)]
        @contextlib.asynccontextmanager
        async def streams(*args):
            yield (None, None)
        @contextlib.asynccontextmanager
        async def session(*args):
            yield client
        with patch("harmony_runtime.benchmark.stdio_client", streams), patch("harmony_runtime.benchmark.ClientSession", session):
            report = run("unused", samples=1)
        self.assertEqual(report["successful_samples"], 1)
        self.assertEqual(report["status"], "not_ready")
        self.assertEqual(report["error_code"], "session_close_failed")


class StdioBenchmarkTests(unittest.TestCase):
    @contextlib.contextmanager
    def service(self, device):
        with tempfile.TemporaryDirectory() as root:
            ready = threading.Event()
            servers = []
            def notify(server):
                servers.append(server)
                ready.set()
            thread = threading.Thread(target=serve, args=(root,), kwargs={"ready": notify,
                "runtime_factory": lambda p: Runtime(p, factory=lambda serial: device, discover=lambda: ["fake"])})
            thread.start()
            self.assertTrue(ready.wait(10))
            try:
                yield root
                self.assertEqual(device.writes, 0)
                client = Client(root)
                sid = client.call("session", operation="open")["session_id"]
                client.call("session", operation="close", session_id=sid)
            finally:
                servers[0].shutdown()
                thread.join(10)
            self.assertFalse(thread.is_alive())

    def test_fast_and_full_over_real_stdio_release_lease(self):
        device = FakeDevice("fake")
        device.text = "PRIVATE_PHONE_TEXT"
        device.screenshot = lambda: Image.new("RGB", (100, 100))
        with self.service(device) as root:
            for mode in ("FAST", "FULL"):
                report = run(root, samples=2, mode=mode)
                self.assertEqual(report["status"], "ok")
                self.assertEqual(report["successful_samples"], 2)
                self.assertEqual(report["metrics"]["all_attempts"]["phases"]["tree_ms"]["count"], 2)
                self.assertNotIn("PRIVATE", json.dumps(report))

    def test_unstable_capture_is_counted_on_each_attempt(self):
        device = FakeDevice("fake")
        def screenshot():
            device.text += " changed"
            return Image.new("RGB", (100, 100))
        device.screenshot = screenshot
        with self.service(device) as root:
            report = run(root, samples=2, include_image=True)
        self.assertEqual(report["failed_samples"], 2)
        self.assertEqual(report["attempted_samples"], 2)
        self.assertEqual(report["error_counts"], {"capture_unverified": 2})
        self.assertEqual(report["metrics"]["successful"]["client"]["count"], 0)

    def test_authentication_stops_further_samples_and_releases_lease(self):
        with self.service(AuthDevice("fake")) as root:
            report = run(root, samples=3)
        self.assertEqual(report["error_code"], "authentication_required")
        self.assertEqual(report["attempted_samples"], 1)
        self.assertEqual(report["unattempted_samples"], 2)
