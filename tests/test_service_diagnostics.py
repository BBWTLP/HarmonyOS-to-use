"""Operational diagnosis of the resident loopback service (P7-02).

Two real incidents motivated this: a device batch started with no service, and
another started against a stale `endpoint.json` whose process had exited. Both
looked like silent stalls, because the stdio frontend's failure was reported
without naming the endpoint or the port.
"""
from __future__ import annotations

import json
import pathlib
import socket
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from harmony_runtime.diagnostics import report, service_report  # noqa: E402


def write_endpoint(root: pathlib.Path, **overrides) -> pathlib.Path:
    payload = {"port": 65000, "token": "token-value", "pid": 4242, "protocol": 1}
    payload.update(overrides)
    path = root / "endpoint.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class ServiceReportTests(unittest.TestCase):
    def test_a_missing_endpoint_names_the_start_command(self):
        with tempfile.TemporaryDirectory() as root:
            result = service_report(root)
        self.assertEqual(result["status"], "not_ready")
        self.assertEqual(result["code"], "service_endpoint_missing")
        self.assertIn("serve", result["message"])
        self.assertEqual(result["endpoint_file"], str(pathlib.Path(root) / "endpoint.json"))

    def test_a_malformed_endpoint_is_reported_as_invalid(self):
        with tempfile.TemporaryDirectory() as root:
            (pathlib.Path(root) / "endpoint.json").write_text("{not json", encoding="utf-8")
            result = service_report(root)
        self.assertEqual(result["code"], "service_endpoint_invalid")
        for payload in ({"port": 0, "token": "t", "pid": 1},
                        {"port": 70000, "token": "t", "pid": 1},
                        {"port": 1234, "token": "", "pid": 1}):
            with tempfile.TemporaryDirectory() as root:
                write_endpoint(pathlib.Path(root), **payload)
                self.assertEqual(service_report(root)["code"],
                                 "service_endpoint_invalid")
        with tempfile.TemporaryDirectory() as root:
            # A missing port is malformed, not stale.
            (pathlib.Path(root) / "endpoint.json").write_text(
                json.dumps({"token": "t", "pid": 1}), encoding="utf-8")
            self.assertEqual(service_report(root)["code"], "service_endpoint_invalid")
        with tempfile.TemporaryDirectory() as root:
            # A JSON array is not an endpoint object.
            (pathlib.Path(root) / "endpoint.json").write_text("[1, 2]", encoding="utf-8")
            self.assertEqual(service_report(root)["code"], "service_endpoint_invalid")

    def test_a_stale_endpoint_reports_the_port_and_no_listener(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        with tempfile.TemporaryDirectory() as root:
            write_endpoint(pathlib.Path(root), port=port, pid=999999)
            result = service_report(root)
        self.assertEqual(result["status"], "not_ready")
        self.assertEqual(result["code"], "service_endpoint_stale")
        self.assertEqual(result["port"], port)
        self.assertFalse(result["port_listening"])
        self.assertIn(str(port), result["message"])
        self.assertIn("Restart", result["message"])

    def test_a_listening_endpoint_is_reachable(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        try:
            with tempfile.TemporaryDirectory() as root:
                write_endpoint(pathlib.Path(root), port=port)
                result = service_report(root)
        finally:
            server.close()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["code"], "service_reachable")
        self.assertTrue(result["port_listening"])
        self.assertEqual(result["port"], port)
        # The check must not claim anything about the device.
        self.assertIn("does not open a session", result["message"])

    def test_the_doctor_report_carries_the_service_check(self):
        result = report(timeout=20)
        names = [item["name"] for item in result["checks"]]
        self.assertIn("service", names)
        check = next(item for item in result["checks"] if item["name"] == "service")
        self.assertIn(check["code"],
                      ("service_reachable", "service_endpoint_missing",
                       "service_endpoint_invalid", "service_endpoint_stale"))
        self.assertIn(result["service"]["status"], ("ok", "not_ready"))
        self.assertFalse(result["phone_operation_verified"])


if __name__ == "__main__":
    unittest.main()
