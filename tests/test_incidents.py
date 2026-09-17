import tempfile
import unittest
from pathlib import Path
from harmony_runtime.journal import Journal
from harmony_runtime.observation import snapshot
from harmony_runtime.runtime import Runtime
from test_runtime import FakeDevice


def evidence(text):
    return snapshot({"attributes": {"text": text, "bounds": "[0,0][20,20]"}}, (100,100,0))


class IncidentTests(unittest.TestCase):
    def test_original_condition_only_closes_after_fresh_matching_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'journal.db'
            j = Journal(path)
            stale = evidence('done')
            stale['captured_at'] = 0  # Explicitly old evidence; Windows clock ticks can coincide.
            j.begin('r', 'digest', 'phone', expected={'text':'done'}, before_fingerprint='before')
            result = {'execution_status':'executed', 'verification_status':'inconclusive', 'incident_id':'incident'}
            j.finish('r', result)
            j.close()
            j = Journal(path)
            try:
                self.assertEqual(j.reconcile_verified('phone', stale), [])
                self.assertEqual(j.reconcile_verified('other', evidence('done')), [])
                self.assertEqual(j.reconcile_verified('phone', evidence('unrelated')), [])
                self.assertEqual(j.reconcile_verified('phone', evidence('done')), ['incident'])
                self.assertEqual(j.reconcile_verified('phone', evidence('done')), [])
                self.assertEqual(j.lookup('r', 'digest'), {**result, 'observation_retained': False})
                self.assertEqual(j.incidents('phone')[0]['status'], 'closed')
                self.assertNotIn('done', str(j.incidents('phone')))
            finally: j.close()

    def test_unknown_and_changed_only_are_not_closed(self):
        for state, expected in [('unknown', {'text':'done'}), ('executed', {'changed':True}), ('executed', None)]:
            with self.subTest(state=state, expected=expected), tempfile.TemporaryDirectory() as root:
                j = Journal(Path(root)/'journal.db')
                try:
                    j.begin('r', 'd', 'phone', expected=expected, before_fingerprint='before')
                    j.finish('r', {'execution_status':state, 'incident_id':'incident'})
                    self.assertEqual(j.reconcile_verified('phone', evidence('done')), [])
                    self.assertEqual(j.incidents('phone')[0]['status'], 'open')
                    if state == 'unknown': self.assertTrue(j.unresolved('phone'))
                finally: j.close()

    def test_interrupted_dispatch_gets_persistent_incident(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'journal.db'
            j = Journal(path)
            j.begin('r', 'd', 'phone')
            j.close()
            j = Journal(path)
            incident = j.incidents('phone')[0]
            j.close()
            j = Journal(path)
            try:
                self.assertEqual(j.incidents('phone'), [incident])
                self.assertTrue(j.unresolved('phone'))
            finally: j.close()

    def test_runtime_recover_closes_verified_failure_without_replaying_action(self):
        with tempfile.TemporaryDirectory() as root:
            device = FakeDevice('phone')
            r = Runtime(root, factory=lambda _: device, discover=lambda: ['phone'])
            try:
                sid = r.session('owner','open')['session_id']
                obs = r.observe('owner',sid)
                result = r.act('owner', {'session_id':sid, 'request_id':'r', 'observation_id':obs['observation_id'],
                    'action':{'kind':'tap','target':{'text':'Settings'}}, 'expected':{'text':'done'}, 'timeout_ms':100})
                self.assertEqual(result['status'], 'timeout')
                incident = result['incident_id']
                self.assertEqual(r.session('owner','status',sid)['incidents'][0]['status'], 'open')
                device.text = 'done'
                recovered = r.session('owner','recover',sid)
                self.assertEqual(recovered['closed_incidents'], [incident])
                self.assertEqual(device.writes, 1)
            finally: r.close()
