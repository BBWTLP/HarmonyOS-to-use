import tempfile
import unittest
from harmony_runtime.foreground import parse_foreground
from harmony_runtime.runtime import Runtime
from harmony_runtime.contracts import RuntimeFault
from tests.test_runtime import FakeDevice

FOCUS = "Focus window: 1958\r\r\n"
MISSION = """Mission ID #1958  mission name #[#com.example.first:entry:EntryAbility] lockedState #0
  AbilityRecord ID #88
    bundle name [com.example.first]
    state #FOREGROUND start time [123]
    app state #FOREGROUND
"""

class ParserTests(unittest.TestCase):
    def test_explicit_matched_foreground(self):
        result = parse_foreground(FOCUS, MISSION, FOCUS)
        self.assertEqual(result['bundle'], 'com.example.first')
        self.assertEqual(result['status'], 'verified')

    def test_unknown_ambiguous_background_and_mismatch(self):
        for mission in ('', MISSION + MISSION,
                        MISSION.replace('state #FOREGROUND', 'state #BACKGROUND', 1),
                        MISSION.replace('app state #FOREGROUND', 'app state #BACKGROUND'),
                        MISSION.replace('bundle name [com.example.first]', 'bundle name [com.example.other]'),
                        MISSION.replace('AbilityRecord ID #88', 'AbilityRecord ID #88\nAbilityRecord ID #89')):
            with self.subTest(mission=mission):
                self.assertIsNone(parse_foreground(FOCUS, mission, FOCUS)['bundle'])
        for before, after in [(FOCUS, 'Focus window: 1959'), (FOCUS + FOCUS, FOCUS), ('', FOCUS)]:
            self.assertEqual(parse_foreground(before, MISSION, after)['status'], 'unstable')

class AppDevice(FakeDevice):
    supports_foreground = True
    def __init__(self, serial):
        super().__init__(serial)
        self.bundle = 'com.example.first'
        self.reads = 0
        self.transition = False
    def foreground(self):
        self.reads += 1
        if self.transition:
            self.bundle = 'com.example.first' if self.reads % 2 else 'com.example.second'
        return {'bundle': self.bundle, 'status': 'verified' if self.bundle else 'unknown',
                'focus_id': self.bundle, 'mission_id': self.bundle, 'ability_id': self.bundle}
    def dispatch(self, action, target):
        self.writes += 1
        if action.kind == 'launch':
            self.bundle = action.bundle

class ForegroundRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Runtime(self.tmp.name, factory=AppDevice, discover=lambda: ['fake'])
        self.session = self.runtime.session('a', 'open')
        self.sid = self.session['session_id']
    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()
    def observe(self):
        return self.runtime.observe('a', self.sid)
    def test_lazy_capability_and_launch_bundle_postcondition(self):
        self.assertTrue(self.session['capabilities']['foreground_bundle'])
        self.assertEqual(self.runtime.devices, {})
        obs = self.observe()
        result = self.runtime.act('a', dict(session_id=self.sid, request_id='launch',
            observation_id=obs['observation_id'], action={'kind':'launch','bundle':'com.example.second'},
            expected={'bundle':'com.example.second'}))
        self.assertEqual(result['verification_status'], 'verified')
    def test_identical_tree_in_different_app_is_stale(self):
        obs = self.observe()
        self.runtime.devices['fake'].bundle = 'com.example.second'
        with self.assertRaises(RuntimeFault) as error:
            self.runtime.act('a', dict(session_id=self.sid, request_id='stale',
                observation_id=obs['observation_id'], action={'kind':'back'}))
        self.assertEqual(error.exception.code, 'stale_observation')
        self.assertEqual(self.runtime.devices['fake'].writes, 0)
    def test_capture_transition_cannot_be_used(self):
        self.observe()
        self.runtime.devices['fake'].transition = True
        obs = self.observe()
        self.assertFalse(obs['actionable'])
        self.assertIsNone(obs['foreground_bundle'])
        self.assertNotIn(obs['observation_id'], self.runtime.sessions[self.sid].observations)
    def test_wait_requires_known_baseline_and_known_new_app(self):
        obs = self.observe()
        device = self.runtime.devices['fake']
        device.bundle = None
        condition = dict(type='app_changed', observation_id=obs['observation_id'])
        self.assertEqual(self.runtime.wait('a', self.sid, condition=condition, timeout_ms=100)['status'], 'timeout')
        unknown = self.observe()
        with self.assertRaises(RuntimeFault) as error:
            self.runtime.wait('a', self.sid, condition=dict(type='app_changed', observation_id=unknown['observation_id']))
        self.assertEqual(error.exception.code, 'foreground_unknown')
        device.bundle = 'com.example.second'
        result = self.runtime.wait('a', self.sid, condition=condition)
        self.assertEqual(result['status'], 'matched')
        self.assertEqual(result['evidence']['source'], 'system_foreground')
    def test_unknown_identity_never_satisfies_bundle(self):
        self.observe()
        self.runtime.devices['fake'].bundle = None
        self.assertEqual(self.runtime.wait('a', self.sid, expected={'bundle':'com.example.first'}, timeout_ms=100)['status'], 'timeout')

if __name__ == '__main__':
    unittest.main()
