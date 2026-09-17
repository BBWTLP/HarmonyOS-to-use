import tempfile
import time
import unittest
from pydantic import ValidationError
from harmony_runtime.runtime import Runtime
from harmony_runtime.contracts import RuntimeFault, WaitCondition
from test_runtime import FakeDevice


class WaitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.device = FakeDevice('fake')
        self.runtime = Runtime(self.tmp.name, factory=lambda _: self.device, discover=lambda: ['fake'])
        self.sid = self.runtime.session('a', 'open')['session_id']

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def wait(self, condition, **kwargs):
        return self.runtime.wait('a', self.sid, condition=condition, poll_ms=100, **kwargs)

    def test_presence_absence_are_read_only_and_exact(self):
        for condition in [dict(type='text_present', value='Settings'),
                          dict(type='text_absent', value='Setting'),
                          dict(type='element_present', target=dict(text='Settings')),
                          dict(type='element_absent', target=dict(text='Other'))]:
            self.assertEqual(self.wait(condition)['status'], 'matched')
        self.assertEqual(self.device.writes, 0)

    def test_resource_only_nodes_are_present_without_becoming_enabled(self):
        self.device.tree = lambda: {"attributes": {}, "children": [
            {"attributes": {"id": "loading", "bounds": "[10,10][40,40]",
                            "enabled": "false", "type": "Progress"}, "children": []}
        ]}
        result = self.wait(dict(type='element_present', target=dict(resource_id='loading')))
        self.assertEqual(result['status'], 'matched')
        self.assertEqual(self.wait(dict(type='element_absent', target=dict(resource_id='loading')),
                                   timeout_ms=100)['status'], 'timeout')
        obs = self.runtime.observe('a', self.sid)
        from harmony_runtime.observation import resolve
        from harmony_runtime.contracts import Target
        with self.assertRaises(RuntimeFault) as caught:
            resolve(obs, Target(resource_id='loading'))
        self.assertEqual(caught.exception.code, 'target_not_found')
        self.assertEqual(self.device.writes, 0)

    def test_resource_only_hidden_and_offscreen_nodes_remain_excluded(self):
        self.device.tree = lambda: {"attributes": {}, "children": [
            {"attributes": {"visible": "false"}, "children": [
                {"attributes": {"id": "hidden", "bounds": "[0,0][50,50]"}}]},
            {"attributes": {"id": "offscreen", "bounds": "[101,0][150,50]"}}
        ]}
        for resource_id in ('hidden', 'offscreen'):
            self.assertEqual(self.wait(dict(type='element_absent',
                                           target=dict(resource_id=resource_id)))['status'], 'matched')
        self.assertEqual(self.device.writes, 0)

    def test_absence_waits_until_tree_changes(self):
        original = self.device.tree
        calls = []
        def tree():
            calls.append(1)
            if len(calls) > 1:
                self.device.text = 'Done'
            return original()
        self.device.tree = tree
        result = self.wait(dict(type='text_absent', value='Settings'))
        self.assertEqual(result['status'], 'matched')
        self.assertEqual(result['evidence']['samples'], 2)

    def test_change_requires_session_bound_observation(self):
        obs = self.runtime.observe('a', self.sid)
        condition = dict(type='fingerprint_changed', observation_id=obs['observation_id'])
        self.assertEqual(self.wait(condition, timeout_ms=100)['status'], 'timeout')
        self.device.text = 'Changed'
        self.assertEqual(self.wait(condition)['status'], 'matched')
        with self.assertRaises(RuntimeFault) as caught:
            self.wait(dict(type='change', observation_id='not-in-session'))
        self.assertEqual(caught.exception.code, 'stale_observation')

    def test_stability_resets_when_tree_changes(self):
        original = self.device.tree
        calls = []
        def tree():
            calls.append(1)
            self.device.text = 'A' if len(calls) == 1 else 'B'
            return original()
        self.device.tree = tree
        started = time.monotonic()
        result = self.wait(dict(type='stable', stable_ms=200))
        self.assertEqual(result['status'], 'matched')
        self.assertGreaterEqual(result['evidence']['samples'], 4)
        self.assertGreaterEqual(time.monotonic() - started, .3)
        self.assertFalse(result['evidence']['continuous_stability_proven'])
        self.assertFalse(result['evidence']['pixel_stability_proven'])

    def test_app_change_rejected_without_initializing_device(self):
        with self.assertRaises(RuntimeFault) as caught:
            self.wait(dict(type='app_changed', observation_id='baseline'))
        self.assertEqual(caught.exception.code, 'unsupported_capability')
        self.assertEqual(self.runtime.devices, {})

    def test_invalid_and_conflicting_conditions(self):
        for value in [dict(type='change'), dict(type='text_absent'),
                      dict(type='stable', value='unexpected'),
                      dict(type='element_present', target=dict(action_id='n0'))]:
            with self.assertRaises(ValidationError):
                WaitCondition.model_validate(value)
        with self.assertRaises(RuntimeFault):
            self.runtime.wait('a', self.sid, expected=dict(text='Settings'), condition=dict(type='stable'))


if __name__ == '__main__':
    unittest.main()
