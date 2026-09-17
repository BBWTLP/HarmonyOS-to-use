import copy
import tempfile
import unittest
import sqlite3
from unittest.mock import patch
from harmony_runtime.journal import Journal
from pydantic import ValidationError
from harmony_runtime.contracts import BurstRequest, RuntimeFault
from harmony_runtime.runtime import Runtime
from test_runtime import FakeDevice


class Pages(FakeDevice):
    def __init__(self, serial):
        super().__init__(serial)
        self.text = 'Page0'
        self.targets = []

    def dispatch(self, action, target):
        self.targets.append(target['text'])
        self.writes += 1
        if self.fail:
            raise OSError('Connection lost during dispatch')
        self.text = f'Page{self.writes}'


class BurstTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.device = Pages('fake')
        self.runtime = Runtime(self.tmp.name, factory=lambda serial: self.device,
                               discover=lambda: ['fake'])
        self.sid = self.runtime.session('owner', 'open')['session_id']

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def request(self, count=3):
        obs = self.runtime.observe('owner', self.sid)
        return dict(session_id=self.sid, request_id='sequence',
                    observation_id=obs['observation_id'], steps=[
                        dict(action=dict(kind='tap', target=dict(text=f'Page{i}')),
                             expected=dict(text=f'Page{i+1}')) for i in range(count)])

    def test_pages_are_resolved_again_and_duplicate_never_replays(self):
        req = self.request(5)
        result = self.runtime.burst('owner', req)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['verified_steps'], 5)
        self.assertEqual(self.device.targets, [f'Page{i}' for i in range(5)])
        self.assertTrue(self.runtime.burst('owner', req)['deduplicated'])
        self.assertEqual(self.device.writes, 5)
        req['steps'].pop()
        with self.assertRaises(RuntimeFault) as ctx:
            self.runtime.burst('owner', req)
        self.assertEqual(ctx.exception.code, 'request_conflict')

    def test_missing_next_target_stops_without_later_writes(self):
        req = self.request()
        req['steps'][1]['action']['target']['text'] = 'Missing'
        result = self.runtime.burst('owner', req)
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(result['verified_steps'], 1)
        self.assertEqual(result['steps'][-1]['execution_status'], 'not_dispatched')
        self.assertEqual(self.device.writes, 1)
        self.runtime.burst('owner', req)
        self.assertEqual(self.device.writes, 1)

    def test_unknown_dispatch_stops_and_blocks_new_sequence(self):
        req = self.request()
        self.device.fail = True
        result = self.runtime.burst('owner', req)
        self.assertEqual(result['steps'][0]['execution_status'], 'unknown')
        self.assertEqual(len(result['steps']), 1)
        self.assertEqual(self.device.writes, 1)
        req['request_id'] = 'different'
        with self.assertRaises(RuntimeFault):
            self.runtime.burst('owner', req)
        self.assertEqual(self.device.writes, 1)

    def test_failed_postcondition_stops_before_second_dispatch(self):
        req = self.request()
        req['timeout_ms'] = 100
        req['steps'][0]['expected']['text'] = 'Never appears'
        result = self.runtime.burst('owner', req)
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(result['verified_steps'], 0)
        self.assertEqual(self.device.writes, 1)
        self.assertEqual(result['steps'][0]['execution_status'], 'executed')

    def test_contract_requires_bounded_semantic_verified_steps(self):
        req = self.request()
        invalid = []
        for key, value in [('timeout_ms', 3001), ('steps', []), ('steps', req['steps'] * 2)]:
            candidate = copy.deepcopy(req)
            candidate[key] = value
            invalid.append(candidate)
        candidate = copy.deepcopy(req)
        candidate['steps'][0]['action']['target'] = {'action_id': 'n0'}
        invalid.append(candidate)
        candidate = copy.deepcopy(req)
        del candidate['steps'][0]['expected']
        invalid.append(candidate)
        for candidate in invalid:
            with self.assertRaises(ValidationError):
                BurstRequest.model_validate(candidate)
        self.assertEqual(self.device.writes, 0)

    def test_watch_detects_then_regrounds_without_model_roundtrip(self):
        req = self.request(1)
        req['steps'][0]['watch_timeout_ms'] = 500
        original = self.device.tree
        calls = []
        def tree():
            calls.append(1)
            if not self.device.writes:
                self.device.text = 'Waiting' if len(calls) < 3 else 'Page0'
            return original()
        self.device.tree = tree
        result = self.runtime.burst('owner', req)
        self.assertEqual(result['status'], 'completed')
        self.assertGreaterEqual(len(calls), 5)
        self.assertEqual(self.device.writes, 1)
        self.runtime.burst('owner', req)
        self.assertEqual(self.device.writes, 1)

    def test_pause_during_watch_prevents_dispatch_and_rearm(self):
        req = self.request(1)
        req['steps'][0]['watch_timeout_ms'] = 500
        original = self.device.tree
        def tree():
            self.runtime.session('owner', 'pause', self.sid)
            return original()
        self.device.tree = tree
        result = self.runtime.burst('owner', req)
        self.assertEqual(result['steps'][0]['execution_status'], 'not_dispatched')
        self.assertEqual(self.device.writes, 0)
        self.device.tree = original
        self.runtime.session('owner', 'resume', self.sid)
        repeated = self.runtime.burst('owner', req)
        self.assertTrue(repeated['deduplicated'])
        self.assertEqual(self.device.writes, 0)

    def test_expired_watch_is_durable_and_never_rearmed(self):
        req = self.request(1)
        req['steps'][0]['watch_timeout_ms'] = 100
        self.device.text = 'Waiting'
        result = self.runtime.burst('owner', req)
        self.assertEqual(result['stop_reason'], 'watch_expired')
        self.device.text = 'Page0'
        result = self.runtime.burst('owner', req)
        self.assertTrue(result['deduplicated'])
        self.assertEqual(self.device.writes, 0)

    def test_target_disappears_during_regrounding_and_is_not_clicked(self):
        req = self.request(1)
        req['steps'][0]['watch_timeout_ms'] = 500
        original = self.device.tree
        calls = []
        def tree():
            calls.append(1)
            self.device.text = 'Page0' if len(calls) == 1 else 'Gone'
            return original()
        self.device.tree = tree
        result = self.runtime.burst('owner', req)
        self.assertEqual(result['stop_reason'], 'stale_observation')
        self.assertEqual(self.device.writes, 0)

    def test_status_has_child_evidence_without_page_content(self):
        result = self.runtime.burst('owner', self.request())
        status = self.runtime.session('owner', 'burst_status', self.sid,
                                      request_id='sequence')
        self.assertEqual(status['state'], 'finished')
        self.assertEqual(len(status['children']), 3)
        self.assertNotIn('Page', str(status))
        self.assertEqual([c['request_id'] for c in status['children']], result['planned_request_ids'])
        self.assertEqual(self.runtime.journal.burst_status('other-device', 'sequence')['status'], 'not_found')

    def test_restart_retains_child_completed_before_parent_progress(self):
        from harmony_runtime.journal import Journal
        path = self.runtime.root / 'restart-test.sqlite3'
        journal = Journal(path)
        initial = dict(status='running', planned_request_ids=['child-a', 'child-b'],
                       steps=[], verified_steps=0, stop_reason=None)
        journal.begin_burst('parent', 'digest', 'fake', initial)
        journal.begin('child-a', 'child-digest', 'fake')
        journal.finish('child-a', dict(status='ok', execution_status='executed', verification_status='verified'))
        journal.close()
        journal = Journal(path)
        try:
            cached = journal.burst_lookup('parent', 'digest')
            self.assertEqual(cached['status'], 'interrupted')
            status = journal.burst_status('fake', 'parent')
            self.assertEqual(status['state'], 'interrupted')
            self.assertEqual(status['children'][0]['execution_status'], 'executed')
            self.assertEqual(status['children'][1]['status'], 'not_found')
            self.assertEqual(self.device.writes, 0)
        finally:
            journal.close()

    def test_durable_admission_rechecks_global_request_namespace(self):
        journal = self.runtime.journal
        initial = dict(planned_request_ids=[], steps=[])
        # Simulate two device callers that both observed an unused ID.
        self.assertIsNone(journal.lookup('race', 'a'))
        self.assertIsNone(journal.burst_lookup('race', 'b'))
        journal.begin_burst('race', 'b', 'other-device', initial)
        with self.assertRaises(RuntimeFault) as ctx:
            journal.begin('race', 'a', 'fake')
        self.assertEqual(ctx.exception.code, 'request_conflict')
        journal.begin('reverse', 'a', 'fake')
        with self.assertRaises(RuntimeFault) as ctx:
            journal.begin_burst('reverse', 'b', 'other-device', initial)
        self.assertEqual(ctx.exception.code, 'request_conflict')



    def test_parent_database_full_stops_after_durable_child(self):
        req = self.request()
        db = self.runtime.journal.db
        db.execute('CREATE TABLE fault_fill(payload BLOB)')
        db.execute("CREATE TRIGGER fail_parent BEFORE UPDATE ON bursts BEGIN INSERT INTO fault_fill VALUES (zeroblob(1048576)); END")
        db.commit()
        pages = db.execute('PRAGMA page_count').fetchone()[0]
        db.execute('PRAGMA max_page_count=' + str(pages + 8))
        result = self.runtime.burst('owner', req)
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(result['stop_reason'], 'journal_write_failed')
        self.assertFalse(result['completion_persisted'])
        self.assertEqual(result['verified_steps'], 1)
        self.assertEqual(result['steps'][0]['execution_status'], 'executed')
        self.assertEqual(self.device.writes, 1)
        duplicate = self.runtime.burst('owner', req)
        self.assertEqual(duplicate['status'], 'interrupted')
        self.assertEqual(duplicate['planned_request_ids'], result['planned_request_ids'])
        self.assertEqual(self.device.writes, 1)
        db.execute('DROP TRIGGER fail_parent')
        db.commit()
        # A separate connection models a process reading the durable state.
        journal = Journal(self.tmp.name + '/journal.sqlite3')
        try:
            status = journal.burst_status('fake', req['request_id'])
            self.assertEqual(status['state'], 'interrupted')
            self.assertEqual(status['children'][0]['execution_status'], 'executed')
            self.assertEqual(status['children'][0]['verification_status'], 'verified')
            self.assertTrue(all(c['status'] == 'not_found' for c in status['children'][1:]))
        finally:
            journal.close()

    def test_final_parent_save_failure_never_reports_completed(self):
        req = self.request(1)
        original = self.runtime.journal.save_burst
        def save(request_id, result, finished=False):
            if finished:
                raise sqlite3.OperationalError('database or disk is full')
            return original(request_id, result, finished=finished)
        with patch.object(self.runtime.journal, 'save_burst', side_effect=save):
            result = self.runtime.burst('owner', req)
        self.assertEqual(result['status'], 'interrupted')
        self.assertFalse(result['completion_persisted'])
        self.assertEqual(result['verified_steps'], 1)
        self.assertEqual(self.runtime.burst('owner', req)['status'], 'interrupted')
        self.assertEqual(self.device.writes, 1)
