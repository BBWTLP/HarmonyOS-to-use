import unittest
from unittest.mock import Mock
from harmony_runtime.observation import snapshot, resolve, matches, parse_bounds
from harmony_runtime.contracts import Action, Target, Expected, RuntimeFault
from harmony_runtime.device import HarmonyDevice


def node(bounds="[0,0][100,100]", **attrs):
    return {"attributes": {"bounds": bounds, "text": "target", **attrs}, "children": []}


class GeometryTests(unittest.TestCase):
    def test_partial_target_dispatch_uses_screen_intersection(self):
        obs = snapshot(node("[-200,-200][20,20]"), (100, 100, 0))
        target = resolve(obs, Target(text="target"))
        self.assertEqual(target["bounds"], [-200, -200, 20, 20])
        self.assertEqual(target["hit_bounds"], [0, 0, 20, 20])
        device = HarmonyDevice.__new__(HarmonyDevice)
        device.driver = Mock()
        device.dispatch(Action(kind="tap", target=Target(text="target")), target)
        device.driver.click.assert_called_once_with(10, 10)

    def test_replace_text_clears_before_writing_on_focused_field(self):
        before = node(text="old", type="TextInput", focused=True, accessibilityId="7", hostWindowId="1")
        empty = node(text="", hint="placeholder", type="TextInput", focused=True, accessibilityId="7", hostWindowId="1")
        after = node(text="鸿蒙", type="TextInput", focused=True, accessibilityId="7", hostWindowId="1")
        target = snapshot(before, (100, 100, 0))["catalog"][0]
        device = HarmonyDevice.__new__(HarmonyDevice)
        device.driver = Mock()
        device.display = Mock(return_value=(100, 100, 0))
        device.tree = Mock(side_effect=[before, empty, after])
        device.dispatch(Action(kind="replace_text", target=Target(action_id="n0"), text="鸿蒙"), target)
        self.assertEqual([call[0] for call in device.driver.method_calls],
                         ["press_combination_key", "press_keycode", "input_text_on_cursor"])
        device.driver.input_text_on_cursor.assert_called_once_with("鸿蒙")

    def test_input_hint_is_not_value_and_missing_value_is_not_observed(self):
        for attrs, observed in (({"text": "", "hint": "placeholder"}, True), ({"text": None, "hint": "placeholder"}, False)):
            item = snapshot(node(type="TextInput", **attrs), (100, 100, 0))["catalog"][0]
            self.assertEqual(item["text"], "")
            self.assertEqual(item["hint"], "placeholder")
            self.assertEqual(item["text_observed"], observed)

    def test_replace_stops_if_clear_or_identity_is_unverified(self):
        before = node(text="old", type="TextInput", focused=True, accessibilityId="7")
        for attrs in ({"text": "old"}, {"text": None}, {"text": "", "accessibilityId": "8"}, {"text": "", "focused": False}):
            with self.subTest(attrs=attrs):
                after = node(**{**before["attributes"], **attrs})
                device = HarmonyDevice.__new__(HarmonyDevice)
                device.driver = Mock()
                device.display = Mock(return_value=(100, 100, 0))
                device.tree = Mock(side_effect=[before, after])
                with self.assertRaises(RuntimeFault):
                    device.dispatch(Action(kind="replace_text", target=Target(action_id="n0"), text="new"), snapshot(before, (100, 100, 0))["catalog"][0])
                device.driver.input_text_on_cursor.assert_not_called()

    def test_empty_replace_never_calls_input(self):
        before = node(text="old", type="TextInput", focused=True, accessibilityId="7")
        after = node(text="", hint="placeholder", type="TextInput", focused=True, accessibilityId="7")
        device = HarmonyDevice.__new__(HarmonyDevice)
        device.driver = Mock()
        device.display = Mock(return_value=(100, 100, 0))
        device.tree = Mock(side_effect=[before, after, after])
        device.dispatch(Action(kind="replace_text", target=Target(action_id="n0"), text=""), snapshot(before, (100, 100, 0))["catalog"][0])
        device.driver.input_text_on_cursor.assert_not_called()

    def test_replace_does_not_report_success_for_wrong_final_value(self):
        before = node(text="old", type="TextInput", focused=True, accessibilityId="7")
        empty = node(text="", type="TextInput", focused=True, accessibilityId="7")
        device = HarmonyDevice.__new__(HarmonyDevice)
        device.driver = Mock()
        device.display = Mock(return_value=(100, 100, 0))
        device.tree = Mock(side_effect=[before, empty, before])
        with self.assertRaises(RuntimeFault) as ctx:
            device.dispatch(Action(kind="replace_text", target=Target(action_id="n0"), text="new"), snapshot(before, (100, 100, 0))["catalog"][0])
        self.assertEqual(ctx.exception.code, "input_replace_unverified")
        device.driver.input_text_on_cursor.assert_called_once_with("new")

    def test_offscreen_nodes_neither_resolve_nor_satisfy_wait(self):
        for bounds in ("[100,0][200,50]", "[-20,0][0,50]", "[0,100][50,200]", "[0,-20][50,0]"):
            with self.subTest(bounds=bounds):
                obs = snapshot(node(bounds), (100, 100, 0))
                self.assertFalse(matches(obs, Expected(text="target")))
                with self.assertRaises(RuntimeFault):
                    resolve(obs, Target(text="target"))

    def test_hidden_ancestor_suppresses_visible_child(self):
        parent = node(visible="false")
        parent["children"] = [node(visible="true")]
        self.assertEqual(snapshot(parent, (100, 100, 0))["catalog"], [])

    def test_disabled_ancestor_prevents_child_action_but_not_read(self):
        parent = node(enabled=False, text="parent")
        parent["children"] = [node(enabled=True)]
        obs = snapshot(parent, (100, 100, 0))
        self.assertTrue(matches(obs, Expected(text="target")))
        with self.assertRaises(RuntimeFault):
            resolve(obs, Target(text="target"))

    def test_container_geometry_does_not_imply_clipping(self):
        parent = node("[0,0][1,1]", text="parent")
        parent["children"] = [node("[20,20][40,40]")]
        self.assertEqual(resolve(snapshot(parent, (100, 100, 0)), Target(text="target"))["hit_bounds"], [20, 20, 40, 40])

    def test_malformed_bounds_rejected(self):
        for raw in ("junk 1 2 3 4", "[0,0][20,20]junk", [0,0,20.5,20], [False,0,20,20], "[20,0][10,10]", None):
            with self.subTest(raw=raw):
                self.assertIsNone(parse_bounds(raw))
        self.assertEqual(parse_bounds({"left":0,"top":1,"right":2,"bottom":3}), [0,1,2,3])

    def test_invalid_display_rejected(self):
        for dimensions in ((0,100,0), (100,-1,0), (True,100,0)):
            with self.assertRaises(RuntimeFault):
                snapshot(node(), dimensions)
