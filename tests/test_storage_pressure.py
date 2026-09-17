import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from harmony_runtime.journal import Journal
from harmony_runtime.contracts import RuntimeFault
from harmony_runtime.runtime import Runtime
from test_runtime import FakeDevice


class StoragePressureTests(unittest.TestCase):
    def test_low_space_stops_before_phone_dispatch(self):
        with tempfile.TemporaryDirectory() as root:
            device = FakeDevice('fake')
            runtime = Runtime(root, factory=lambda serial: device, discover=lambda: ['fake'])
            try:
                sid = runtime.session('owner', 'open')['session_id']
                obs = runtime.observe('owner', sid)
                request = dict(session_id=sid, request_id='request', observation_id=obs['observation_id'], action={'kind': 'home'})
                with patch('harmony_runtime.journal.shutil.disk_usage', return_value=SimpleNamespace(free=0)):
                    with self.assertRaises(RuntimeFault) as error:
                        runtime.act('owner', request)
                self.assertEqual(error.exception.code, 'storage_pressure')
                self.assertEqual(device.writes, 0)
                self.assertEqual(runtime.journal.history(), [])
            finally:
                runtime.close()

    def test_burst_admission_fails_but_existing_result_can_finish(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(root + '/journal.db')
            try:
                journal.begin('existing', 'digest', 'phone')
                with patch('harmony_runtime.journal.shutil.disk_usage', return_value=SimpleNamespace(free=0)):
                    journal.finish('existing', {'execution_status': 'executed'})
                    with self.assertRaises(RuntimeFault) as error:
                        journal.begin_burst('burst', 'digest', 'phone', {'steps': []})
                    self.assertEqual(error.exception.code, 'storage_pressure')
                    self.assertEqual(journal.action_status('phone', 'existing')['execution_status'], 'executed')
                    self.assertIsNone(journal.burst_lookup('burst', 'digest'))
            finally:
                journal.close()

    def test_unreadable_volume_rejects_admission(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(root + '/journal.db')
            try:
                with patch('harmony_runtime.journal.shutil.disk_usage', side_effect=OSError('unavailable')):
                    with self.assertRaises(RuntimeFault) as error:
                        journal.begin('new', 'digest')
                self.assertEqual(error.exception.code, 'storage_unavailable')
                self.assertEqual(journal.history(), [])
            finally:
                journal.close()

    def test_sqlite_full_after_dispatch_keeps_unknown_barrier_after_restart(self):
        with tempfile.TemporaryDirectory() as root:
            device = FakeDevice('fake')
            runtime = Runtime(root, factory=lambda serial: device, discover=lambda: ['fake'])
            try:
                sid = runtime.session('owner', 'open')['session_id']
                obs = runtime.observe('owner', sid)
                db = runtime.journal.db
                db.execute('CREATE TABLE fault_fill (payload BLOB)')
                db.execute("CREATE TRIGGER fail_completion BEFORE UPDATE ON actions BEGIN INSERT INTO fault_fill VALUES (zeroblob(1048576)); END")
                db.commit()
                pages = db.execute('PRAGMA page_count').fetchone()[0]
                db.execute('PRAGMA max_page_count=' + str(pages + 8))
                request = dict(session_id=sid, request_id='full', observation_id=obs['observation_id'], action={'kind': 'home'}, expected={'text': 'After'})
                result = runtime.act('owner', request)
                self.assertEqual(result['error_code'], 'journal_write_failed')
                self.assertFalse(result['completion_persisted'])
                self.assertEqual(result['execution_status'], 'unknown')
                self.assertEqual(device.writes, 1)
                duplicate = runtime.act('owner', request)
                self.assertEqual(duplicate['execution_status'], 'unknown')
                self.assertEqual(device.writes, 1)
                self.assertTrue(runtime.journal.unresolved('fake'))
                # Remove only the fault harness before restarting; no action records change.
                db.execute('DROP TRIGGER fail_completion')
                db.commit()
            finally:
                runtime.close()
            journal = Journal(root + '/journal.sqlite3')
            try:
                self.assertTrue(journal.unresolved('fake'))
                with self.assertRaises(RuntimeFault):
                    journal.require_reconciled('fake')
            finally:
                journal.close()
