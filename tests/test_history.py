import json
import tempfile
import unittest
from harmony_runtime.runtime import Runtime
from harmony_runtime.contracts import RuntimeFault


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Runtime(self.tmp.name, factory=lambda _: self.fail("History initialized device"), discover=lambda: ['fake'])
        self.sid = self.runtime.session('owner', 'open')['session_id']

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def record(self, name, device='fake'):
        j = self.runtime.journal
        j.begin(name, name, device, expected={'text': 'PRIVATE_VALUE'})
        j.finish(name, dict(execution_status='executed', verification_status='inconclusive',
                            incident_id='incident-' + name, observation={'text': 'PRIVATE_VALUE'}))

    def test_paging_is_device_bound_and_does_not_duplicate_new_insert(self):
        for name in ('a', 'b', 'c'):
            self.record(name)
        self.record('other', 'other-device')
        first = self.runtime.history('owner', self.sid, limit=2)
        self.assertEqual([x['request_id'] for x in first['items']], ['c', 'b'])
        self.record('new')
        second = self.runtime.history('owner', self.sid, limit=2, before=first['next_before'])
        self.assertEqual([x['request_id'] for x in second['items']], ['a'])
        self.assertIsNone(second['next_before'])
        self.assertNotIn('PRIVATE_VALUE', json.dumps(first))
        self.assertEqual(self.runtime.devices, {})

    def test_paused_owner_can_read_but_other_owner_cannot(self):
        self.record('a')
        self.runtime.session('owner', 'pause', self.sid)
        self.assertEqual(len(self.runtime.history('owner', self.sid)['items']), 1)
        with self.assertRaises(RuntimeFault):
            self.runtime.history('other', self.sid)
        for kwargs in ({'limit': 0}, {'limit': True}, {'before': -1}, {'before': 2**64}):
            with self.assertRaises(RuntimeFault):
                self.runtime.history('owner', self.sid, **kwargs)

    def test_unknown_record_is_not_replayed_or_cleared(self):
        self.runtime.journal.begin('unknown', 'digest', 'fake')
        item = self.runtime.history('owner', self.sid)['items'][0]
        self.assertEqual(item['execution_status'], 'unknown')
        self.assertEqual(len(self.runtime.journal.unresolved('fake')), 1)
        self.assertEqual(self.runtime.devices, {})

    def test_incident_closure_does_not_rewrite_original_verification(self):
        self.record('a')
        with self.runtime.journal.lock, self.runtime.journal.db:
            self.runtime.journal.db.execute("UPDATE incidents SET status='closed',closed=123")
        item = self.runtime.history('owner', self.sid)['items'][0]
        self.assertEqual(item['verification_status'], 'inconclusive')
        self.assertEqual(item['incident']['status'], 'closed')
