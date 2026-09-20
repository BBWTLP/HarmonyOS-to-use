import re
import unittest
from unittest.mock import Mock

from harmony_runtime.contracts import RuntimeFault
from harmony_runtime.device import HarmonyDevice, parse_diagnostic_sections


def framed(parts, marker="HRT_test"):
    return "".join(f"\n{marker}:{i}:begin\n{part}\n{marker}:{i}:end:0\n"
                   for i, part in enumerate(parts))


class DiagnosticBatchTests(unittest.TestCase):
    def test_complete_ordered_frames_preserve_payload(self):
        self.assertEqual(parse_diagnostic_sections(framed(["one", "two\nthree"]), "HRT_test", 2),
                         ("one", "two\nthree"))
        self.assertEqual(parse_diagnostic_sections(framed(["one"]).replace("\n", "\r\r\n"),
                                                  "HRT_test", 1), ("one",))

    def test_partial_failed_duplicated_reordered_and_spoofed_frames_rejected(self):
        valid = framed(["PRIVATE_PAYLOAD", "second"])
        cases = [valid[:-5], valid.replace(":end:0", ":end:1", 1),
                 valid.replace(":end:0", ":end:127", 1), valid + valid,
                 valid.replace(":0:", ":x:").replace(":1:", ":0:").replace(":x:", ":1:"),
                 valid + "PRIVATE_ERROR", "PRIVATE_ERROR" + valid,
                 framed(["HRT_test:1:begin", "second"]), None]
        for raw in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(RuntimeFault) as error:
                    parse_diagnostic_sections(raw, "HRT_test", 2)
                self.assertEqual(error.exception.code, "device_unavailable")
                self.assertNotIn("PRIVATE", str(error.exception))

    def make_device(self, parts):
        device = HarmonyDevice.__new__(HarmonyDevice)
        device.driver = Mock()
        def shell(command):
            marker = re.search(r"HRT_[0-9a-f]{32}", command).group()
            return framed(parts, marker)
        device.driver.shell.side_effect = shell
        return device

    def test_screen_uses_one_roundtrip_and_both_fresh_readonly_commands(self):
        device = self.make_device(["Current State: AWAKE", "screenLocked false"])
        self.assertEqual(device.screen_state(), {"screen_on": True, "screen_locked": False})
        command = device.driver.shell.call_args.args[0]
        self.assertLess(command.index("PowerManagerService"), command.index("ScreenlockService"))
        self.assertEqual(command.count("hidumper"), 2)
        self.assertEqual(device.driver.shell.call_count, 1)
        device.screen_state()
        self.assertEqual(device.driver.shell.call_count, 2)
        self.assertNotEqual(command, device.driver.shell.call_args.args[0])

    def test_foreground_keeps_bracket_and_rejects_changed_focus(self):
        device = self.make_device(["Focus window: 1", "", "Focus window: 2"])
        self.assertEqual(device.foreground()["status"], "unstable")
        command = device.driver.shell.call_args.args[0]
        self.assertEqual(command.count("hidumper"), 3)
        self.assertLess(command.index("WindowManagerService"), command.index("AbilityManagerService"))
        self.assertLess(command.index("AbilityManagerService"), command.rindex("WindowManagerService"))
        device.driver.shell.assert_called_once()

    def test_invalid_batch_does_not_retry_or_fall_back(self):
        device = self.make_device([])
        with self.assertRaises(RuntimeFault):
            device.screen_state()
        device.driver.shell.assert_called_once()
        self.assertEqual([call[0] for call in device.driver.method_calls], ["shell"])

    def test_command_selector_is_not_shell_input(self):
        device = self.make_device([])
        with self.assertRaises(KeyError):
            device._diagnostics("PRIVATE; arbitrary command")
        device.driver.shell.assert_not_called()
