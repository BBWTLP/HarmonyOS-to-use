import copy
import tempfile
import unittest
from harmony_runtime.observation import snapshot
from harmony_runtime.runtime import Runtime
from harmony_runtime.contracts import RuntimeFault


def tree(progress='1.000000'):
    return {'attributes': {'bounds': '[0,0][100,100]', 'type': 'Column'}, 'children': [
        {'attributes': {'bounds': '[0,0][100,10]', 'type': 'Slider', 'text': progress, 'originalText': progress}},
        {'attributes': {'bounds': '[0,20][100,40]', 'type': 'Text', 'text': 'Clip A'}},
        {'attributes': {'bounds': '[0,50][100,70]', 'type': 'Button', 'text': 'Open', 'clickable': 'true'}},
        {'attributes': {'id': 'ClockStatusView', 'bounds': '[0,90][20,100]', 'text': '08:01'},
         'children': [{'attributes': {'type': 'Text', 'text': '01', 'bounds': '[0,90][20,100]'}}]}]}


class MovingVideo:
    def __init__(self): self.progress = '1.000000'; self.writes = 0; self.title = 'Clip A'
    def tree(self):
        value = tree(self.progress)
        value['children'][1]['attributes']['text'] = self.title
        return value
    def screen_state(self): return {'screen_on': True, 'screen_locked': False}
    def display(self): return (100, 100, 0)
    def dispatch(self, action, target): self.writes += 1; self.title = 'Clip B'
    def close(self): pass


class NavigationTests(unittest.TestCase):
    def test_projection_only_normalizes_numeric_progress_and_clock_text(self):
        original = tree(); changed = copy.deepcopy(original)
        changed['children'][0]['attributes']['text'] = '20.000000'
        changed['children'][0]['attributes']['originalText'] = '20.000000'
        changed['children'][3]['children'][0]['attributes']['text'] = '02'
        before, after = [snapshot(t, (100, 100, 0)) for t in (original, changed)]
        self.assertNotEqual(before['fingerprint'], after['fingerprint'])
        self.assertEqual(before['navigation_fingerprint'], after['navigation_fingerprint'])
        self.assertEqual(original, tree())

    def test_layout_focus_title_and_non_numeric_slider_changes_remain_stale(self):
        baseline = snapshot(tree(), (100, 100, 0))['navigation_fingerprint']
        for index, key, value in [(0, 'bounds', '[1,0][100,10]'), (0, 'text', 'Danger'),
                                  (0, 'focused', 'true'), (1, 'text', 'Clip B'),
                                  (3, 'enabled', 'false')]:
            changed = tree(); changed['children'][index]['attributes'][key] = value
            self.assertNotEqual(baseline, snapshot(changed, (100, 100, 0))['navigation_fingerprint'])
        self.assertNotEqual(baseline, snapshot(tree(), (100, 100, 1))['navigation_fingerprint'])

    def test_numeric_progress_nodes_include_slider_and_progress(self):
        value = tree()
        value['children'][0]['attributes']['type'] = 'Progress'
        projected = snapshot(value, (100, 100, 0))
        changed = copy.deepcopy(value)
        changed['children'][0]['attributes']['text'] = '2.000000'
        changed['children'][0]['attributes']['originalText'] = '2.000000'
        self.assertEqual(projected['navigation_fingerprint'], snapshot(changed, (100, 100, 0))['navigation_fingerprint'])
        changed['children'][0]['attributes']['text'] = 'buffering'
        self.assertNotEqual(projected['navigation_fingerprint'], snapshot(changed, (100, 100, 0))['navigation_fingerprint'])

    def test_navigation_dispatches_once_and_targeted_tap_survives_known_progress_change(self):
        for action in ({'kind': 'swipe', 'direction': 'up'},
                       {'kind': 'tap', 'target': {'text': 'Open'}},
                       {'kind': 'launch', 'bundle': 'com.example.app'}):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                device = MovingVideo()
                runtime = Runtime(tmp, factory=lambda _: device, discover=lambda: ['fake'])
                try:
                    sid = runtime.session('owner', 'open')['session_id']
                    obs = runtime.observe('owner', sid)
                    device.progress = '2.000000'
                    req = dict(session_id=sid, request_id='nav', observation_id=obs['observation_id'],
                               action=action, expected={'text': 'Clip B'})
                    if action['kind'] == 'swipe':
                        result = runtime.act('owner', req)
                        self.assertEqual(result['verification_status'], 'verified')
                        runtime.act('owner', req)
                        self.assertEqual(device.writes, 1)
                    else:
                        result = runtime.act('owner', req)
                        self.assertEqual(result['verification_status'], 'verified')
                        self.assertEqual(device.writes, 1)
                finally:
                    runtime.close()

    def test_back_remains_available_during_feed_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            device = MovingVideo()
            runtime = Runtime(tmp, factory=lambda _: device, discover=lambda: ['fake'])
            try:
                sid = runtime.session('owner', 'open')['session_id']
                obs = runtime.observe('owner', sid)
                device.progress = '2.000000'
                result = runtime.act('owner', dict(session_id=sid, request_id='back', observation_id=obs['observation_id'],
                                                   action={'kind': 'back'}, expected={'changed': True}))
                self.assertEqual(result['verification_status'], 'verified')
                self.assertEqual(device.writes, 1)
            finally:
                runtime.close()


class StaleTargetTests(unittest.TestCase):
    def test_changed_pages_block_all_navigation_before_dispatch(self):
        for kind in ("back", "home", "swipe", "tap", "long_press", "replace_text", "launch"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                device = MovingVideo()
                runtime = Runtime(tmp, factory=lambda _: device, discover=lambda: ["fake"])
                try:
                    sid = runtime.session("owner", "open")["session_id"]
                    obs = runtime.observe("owner", sid)
                    device.title = "Different screen"
                    action = {"kind": kind}
                    if kind == "swipe": action["direction"] = "up"
                    if kind == "launch": action["bundle"] = "com.example.app"
                    if kind in ("tap", "long_press", "replace_text"): action["target"] = {"text": "Open"}
                    if kind == "replace_text": action["text"] = "replacement"
                    with self.assertRaises(RuntimeFault) as caught:
                        runtime.act("owner", dict(session_id=sid, request_id="stale",
                            observation_id=obs["observation_id"], action=action))
                    self.assertEqual(caught.exception.code, "stale_observation")
                    self.assertEqual(device.writes, 0)
                finally: runtime.close()

    def test_changed_progress_target_and_missing_projection_are_rejected(self):
        for missing, target in ((False, "n0"), (True, "n2")):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as tmp:
                device = MovingVideo()
                runtime = Runtime(tmp, factory=lambda _: device, discover=lambda: ["fake"])
                try:
                    sid = runtime.session("owner", "open")["session_id"]
                    obs = runtime.observe("owner", sid)
                    if missing: obs.pop("navigation_fingerprint")
                    device.progress = "2.000000"
                    with self.assertRaises(RuntimeFault) as caught:
                        runtime.act("owner", dict(session_id=sid, request_id="stale",
                            observation_id=obs["observation_id"], action={"kind":"tap", "target":{"action_id":target}}))
                    self.assertEqual(caught.exception.code, "stale_observation")
                    self.assertEqual(device.writes, 0)
                finally: runtime.close()
