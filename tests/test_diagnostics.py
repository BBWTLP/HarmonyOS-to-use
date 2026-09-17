import subprocess
import unittest
from unittest.mock import patch
from harmony_runtime.diagnostics import report
from harmony_runtime.contracts import RuntimeFault


class DiagnosticTests(unittest.TestCase):
    @patch("harmony_runtime.diagnostics.version", return_value="test")
    @patch("harmony_runtime.diagnostics.find_hdc", return_value="hdc")
    def test_discovery_timeout_is_structured(self, hdc, version):
        with patch("harmony_runtime.diagnostics.subprocess.run", side_effect=subprocess.TimeoutExpired("child", 1)):
            result = report(timeout=1)
        self.assertEqual(result["status"], "not_ready")
        self.assertEqual(result["checks"][-1]["code"], "discovery_timeout")
        self.assertFalse(result["phone_operation_verified"])

    @patch("harmony_runtime.diagnostics.version", return_value="test")
    def test_missing_hdc_skips_discovery(self, version):
        with patch("harmony_runtime.diagnostics.find_hdc", side_effect=RuntimeFault("device_unavailable", "missing")), patch("harmony_runtime.diagnostics.subprocess.run") as run:
            result = report()
        run.assert_not_called()
        self.assertEqual(result["checks"][-1]["status"], "skipped")

    @patch("harmony_runtime.diagnostics.version", return_value="test")
    @patch("harmony_runtime.diagnostics.find_hdc", return_value="hdc")
    def test_failed_child_output_is_not_exposed(self, hdc, version):
        child = subprocess.CompletedProcess([], 1, "PRIVATE_SERIAL", "PRIVATE_SERIAL")
        with patch("harmony_runtime.diagnostics.subprocess.run", return_value=child):
            result = report()
        self.assertNotIn("PRIVATE_SERIAL", str(result))
        self.assertEqual(result["checks"][-1]["code"], "discovery_failed")
