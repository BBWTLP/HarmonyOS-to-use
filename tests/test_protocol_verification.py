import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from harmony_runtime.cli import main
from harmony_runtime.protocol import _response_metadata, run


class ProtocolVerificationTests(unittest.TestCase):
    def setUp(self):
        self.hdc_cls = patch("devhelmkit.harmony.device.hdc.HdcDevice").start()
        self.find_hdc = patch("harmony_runtime.protocol.find_hdc", return_value="hdc").start()
        self.addCleanup(patch.stopall)
        self.hdc_cls.list_targets.return_value = ["PRIVATE_SERIAL"]
        self.device = self.hdc_cls.return_value
        self.manager = self.device.agent
        self.manager.detect_device_info.return_value = ("arm64-v8a", 2)
        self.asset = tempfile.NamedTemporaryFile(delete=False)
        self.asset.write(b"agent-test-bytes")
        self.asset.close()
        self.manager._select_agent_so.return_value = self.asset.name
        self.manager.protocol_version = 2
        self.device._sid_echo = True
        self.device._fport_established = True
        self.device.rpc_call.side_effect = [
            json.dumps({"code": 0, "result": "By#1"}),
            json.dumps({"code": 0, "result": []}),
        ]

    def tearDown(self):
        Path(self.asset.name).unlink(missing_ok=True)

    def test_success_records_actual_transport_without_response_content(self):
        data = run()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["predicted"], {"abi": "arm64-v8a", "protocol": 2})
        self.assertEqual(data["actual"]["transport"], "localabstract:uitest_socket")
        self.assertTrue(data["actual"]["session_id_echo"])
        self.assertEqual(data["agent_asset"]["name"], Path(self.asset.name).name)
        self.assertEqual(data["actual"]["rpc_probe"]["api"], "Driver.findComponents")
        self.assertEqual(data["actual"]["rpc_probe"]["result_type"], "list")
        self.assertEqual(data["actual"]["rpc_probe"]["result_length"], 0)
        self.assertEqual(self.device.rpc_call.call_count, 2)
        builder = json.loads(self.device.rpc_call.call_args_list[0].args[0])
        query = json.loads(self.device.rpc_call.call_args_list[1].args[0])
        self.assertEqual(builder["params"], {
            "api": "On.text",
            "this": "On#seed",
            "args": ["__codex_protocol_probe_absent_20260919__", 0],
            "message_type": "hypium",
        })
        self.assertEqual(query["params"], {
            "api": "Driver.findComponents",
            "this": "Driver#0",
            "args": ["By#1"],
            "message_type": "hypium",
        })
        self.assertNotIn("PRIVATE", json.dumps(data))
        self.device.close.assert_called_once_with(stop_daemon=False)

    def test_multiple_devices_is_safe_and_does_not_construct_device(self):
        self.hdc_cls.list_targets.return_value = ["PRIVATE_SERIAL", "OTHER"]
        data = run()
        self.assertEqual(data["error_code"], "multiple_devices")
        self.hdc_cls.assert_not_called()

    def test_driver_failure_is_redacted_and_cleanup_runs(self):
        self.device.rpc_call.side_effect = RuntimeError("PRIVATE_SERIAL response body")
        data = run()
        self.assertEqual(data["error_code"], "protocol_probe_failed")
        self.assertNotIn("PRIVATE", json.dumps(data))
        self.device.close.assert_called_once_with(stop_daemon=False)

    def test_response_metadata_is_shape_only(self):
        data = _response_metadata('{"result":{"private":"secret"},"success":true}')
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["top_level_keys"], ["result", "success"])
        self.assertNotIn("secret", json.dumps(data))
        self.assertEqual(data["result_type"], "dict")
        self.assertEqual(_response_metadata('{"exception":{"message":"private"},"pts":1}')["status"], "device_exception")
        self.assertEqual(_response_metadata('{"error":{"message":"private"}}')["status"], "device_error")
        self.assertEqual(_response_metadata('{"success":false}')["code"], "unsuccessful_response")
        self.assertEqual(_response_metadata("not json")["code"], "non_json_response")

    def test_transport_success_does_not_hide_device_exception(self):
        self.device.rpc_call.side_effect = [
            json.dumps({"code": 0, "result": "By#1"}),
            json.dumps({"exception": {"message": "private"}, "pts": 1}),
        ]
        data = run()
        self.assertEqual(data["status"], "partial")
        self.assertEqual(data["error_code"], "rpc_probe_failed")
        self.assertEqual(data["actual"]["transport_status"], "ok")
        self.assertEqual(data["actual"]["rpc_probe"]["status"], "device_exception")
        self.assertNotIn("private", json.dumps(data))

    def test_invalid_selector_builder_result_is_not_rpc_success(self):
        self.device.rpc_call.side_effect = [
            json.dumps({"code": 0, "result": {"private": "secret"}}),
        ]
        data = run()
        self.assertEqual(data["status"], "partial")
        self.assertEqual(data["error_code"], "rpc_probe_failed")
        self.assertEqual(
            data["actual"]["rpc_probe"]["code"],
            "invalid_selector_builder_result",
        )
        self.assertNotIn("secret", json.dumps(data))

    def test_cli_requires_explicit_execute_and_propagates_status(self):
        with patch("sys.argv", ["harmony-runtime", "protocol"]), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main()
            self.assertEqual(raised.exception.code, 2)
        with patch("sys.argv", ["harmony-runtime", "protocol", "--execute"]), contextlib.redirect_stdout(io.StringIO()), patch("harmony_runtime.protocol.report", return_value={"status": "ok"}):
            self.assertEqual(main(), 0)


if __name__ == "__main__":
    unittest.main()
