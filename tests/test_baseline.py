import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from harmony_runtime.baseline import file_digest, report, source_metadata
from harmony_runtime.cli import main
from harmony_runtime.device import BASELINE_PROPERTIES, metadata_value, read_device_baseline


VALUES = {
    "model": "TEST-AL00", "software_version": "TEST-AL00 6.1.0.135(SP8)",
    "os_full_name": "OpenHarmony-6.1.1.120", "incremental_version": "6.1.1.120",
    "api_version": "24", "security_patch": "2026/07/01", "abi_list": "arm64-v8a",
}


class DeviceBaselineTests(unittest.TestCase):
    def setUp(self):
        from devhelmkit.harmony.device.hdc import HdcDevice
        self.connect = patch("devhelmkit.connect").start()
        self.addCleanup(patch.stopall)
        self.hdc = patch("devhelmkit.harmony.device.hdc.HdcDevice").start()
        self.hdc.list_targets.return_value = ["PRIVATE_SERIAL"]
        patch("harmony_runtime.device.find_hdc", return_value="hdc").start()
        self.run = patch("harmony_runtime.device.subprocess.run",
                         return_value=subprocess.CompletedProcess([], 0, "Ver: 3.2.0f", "")).start()
        self.values = dict(VALUES)
        self.commands = []
        self.hdc.return_value.shell.side_effect = self.shell

    def shell(self, command, timeout):
        self.commands.append((command, timeout))
        if command == "uitest --version":
            return "6.0.2.3\n"
        for name, key in BASELINE_PROPERTIES.items():
            if command == 'param get "' + key + '"':
                return self.values[name]
        raise AssertionError("Non-allowlisted command")

    def test_collects_current_values_without_ui_or_daemon_actions(self):
        data = read_device_baseline()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["fields"]["software_version"]["value"], VALUES["software_version"])
        self.assertEqual(data["uitest"]["driver_predicted_protocol"], 2)
        self.assertFalse(data["protocol_negotiated"])
        self.assertFalse(data["phone_operation_verified"])
        self.connect.assert_not_called()
        self.assertEqual({c[0] for c in self.hdc.return_value.method_calls}, {"shell"})
        self.assertNotIn("PRIVATE", json.dumps(data))
        self.assertTrue(all(0 < timeout <= 5 for _, timeout in self.commands))
        self.assertEqual(self.run.call_args.args[0], ["hdc", "-v"])

    def test_multiple_devices_and_no_device_do_not_choose_or_read(self):
        for targets, code in [([], "no_device"), (["PRIVATE_SERIAL", "OTHER"], "multiple_devices")]:
            self.hdc.list_targets.return_value = targets
            data = read_device_baseline()
            self.assertEqual(data["error_code"], code)
            self.hdc.assert_not_called()
            self.run.assert_not_called()
            self.assertNotIn("PRIVATE", json.dumps(data))

    def test_discovery_failure_does_not_leak_exception(self):
        self.hdc.list_targets.side_effect = RuntimeError("PRIVATE_SERIAL TOKEN")
        data = read_device_baseline()
        self.assertEqual(data["error_code"], "discovery_failed")
        self.assertNotIn("PRIVATE", str(data))

    def test_missing_required_field_fails_closed_without_old_version_fallback(self):
        self.values["software_version"] = "error: PRIVATE_SERIAL"
        data = read_device_baseline()
        self.assertEqual(data["status"], "not_ready")
        self.assertNotIn("value", data["fields"]["software_version"])
        self.assertNotIn("PRIVATE", str(data))

    def test_optional_patch_unavailable_is_explicit(self):
        self.values["security_patch"] = ""
        data = read_device_baseline()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["fields"]["security_patch"]["status"], "unavailable")

    def test_command_exception_is_redacted(self):
        self.hdc.return_value.shell.side_effect = RuntimeError("PRIVATE_ERROR")
        data = read_device_baseline()
        self.assertEqual(data["status"], "not_ready")
        self.assertNotIn("PRIVATE", str(data))

    def test_exhausted_shared_deadline_stops_issuing_commands(self):
        with patch("harmony_runtime.device.time.monotonic", side_effect=[0] + [46] * 20):
            data = read_device_baseline()
        self.hdc.return_value.shell.assert_not_called()
        self.run.assert_not_called()
        self.assertEqual(data["status"], "not_ready")

    def test_timeouts_are_decreasing_not_reset_per_field(self):
        with patch("harmony_runtime.device.time.monotonic", side_effect=[0, 42, 43, 44] + [46] * 20):
            read_device_baseline()
        self.assertEqual([timeout for _, timeout in self.commands], [3, 2, 1])

    def test_unknown_uitest_does_not_assume_protocol_v1(self):
        original = self.shell
        self.hdc.return_value.shell.side_effect = lambda c, timeout: "PRIVATE_ERROR 6.0.2.3" if c == "uitest --version" else original(c, timeout)
        data = read_device_baseline()
        self.assertEqual(data["status"], "not_ready")
        self.assertNotIn("driver_predicted_protocol", data["uitest"])
        self.assertNotIn("PRIVATE", str(data))

    def test_driver_threshold_is_strict(self):
        original = self.shell
        self.hdc.return_value.shell.side_effect = lambda c, timeout: "6.0.2.1" if c == "uitest --version" else original(c, timeout)
        self.assertEqual(read_device_baseline()["uitest"]["driver_predicted_protocol"], 1)

    def test_hdc_failed_output_is_not_exposed(self):
        self.run.side_effect = subprocess.CalledProcessError(1, "hdc", output="PRIVATE_SERIAL")
        data = read_device_baseline()
        self.assertEqual(data["hdc"]["status"], "unavailable")
        self.assertNotIn("PRIVATE", str(data))

    def test_short_deadline_is_rejected_before_any_access(self):
        with self.assertRaises(ValueError):
            read_device_baseline(timeout=1)
        self.hdc.list_targets.assert_not_called()

    def test_metadata_rejects_identity_multiline_and_invalid_api(self):
        for raw in ("PRIVATE_SERIAL", "ok\nPRIVATE_TOKEN", "permission denied", "default", "x" * 193, None):
            self.assertIsNone(metadata_value(raw, "PRIVATE_SERIAL"))
        self.assertIsNone(metadata_value("24 private", numeric=True))


class BaselineReportTests(unittest.TestCase):
    def test_source_digest_changes_on_uncommitted_code_but_not_private_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "src/harmony_runtime").mkdir(parents=True)
            (root / "pyproject.toml").write_text("[project]", encoding="utf-8")
            source = root / "src/harmony_runtime/test.py"
            source.write_text("a=1", encoding="utf-8")
            with patch("harmony_runtime.baseline.subprocess.run", side_effect=OSError()):
                first = source_metadata(root)
                (root / ".runtime").mkdir()
                (root / ".runtime/private.py").write_text("PRIVATE", encoding="utf-8")
                self.assertEqual(first["content_sha256"], source_metadata(root)["content_sha256"])
                source.write_text("a=2", encoding="utf-8")
                self.assertNotEqual(first["content_sha256"], source_metadata(root)["content_sha256"])
            self.assertEqual(first["file_count"], 2)
            self.assertNotIn("PRIVATE", str(first))

    def test_source_missing_is_not_fabricated(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(source_metadata(temp)["status"], "unavailable")

    def test_report_does_not_claim_acceptance_or_hide_failed_reads(self):
        host = {"packages": {"devhelmkit": {"status": "ok"}}}
        with patch("harmony_runtime.baseline.host_metadata", return_value=host), patch("harmony_runtime.baseline.read_device_baseline", return_value={"status": "ok"}):
            data = report()
            self.assertEqual(data["status"], "ok")
            self.assertFalse(data["acceptance_verified"])
            self.assertFalse(data["phone_operation_verified"])
            self.assertEqual(len(data["baseline_id"]), 64)
        with patch("harmony_runtime.baseline.host_metadata", return_value=host), patch("harmony_runtime.baseline.read_device_baseline", side_effect=RuntimeError("PRIVATE")):
            data = report()
            self.assertEqual(data["status"], "not_ready")
            self.assertNotIn("PRIVATE", str(data))

    def test_cli_requires_explicit_read_opt_in(self):
        with patch("sys.argv", ["harmony-runtime", "baseline"]), contextlib.redirect_stderr(io.StringIO()), patch("harmony_runtime.baseline.report") as collect:
            with self.assertRaises(SystemExit) as raised:
                main()
            self.assertEqual(raised.exception.code, 2)
            collect.assert_not_called()

    def test_cli_status_exit_codes(self):
        for status, expected in (("ok", 0), ("not_ready", 1)):
            with patch("sys.argv", ["harmony-runtime", "baseline", "--execute"]), contextlib.redirect_stdout(io.StringIO()), patch("harmony_runtime.baseline.report", return_value={"status": status}):
                self.assertEqual(main(), expected)
