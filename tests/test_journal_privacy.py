import json
import tempfile
import unittest
from pathlib import Path
from harmony_runtime.journal import Journal


class JournalPrivacyTests(unittest.TestCase):
    def test_action_ui_not_persisted_and_original_reply_untouched(self):
        with tempfile.TemporaryDirectory() as root:
            j = Journal(Path(root) / 'journal.sqlite3')
            j.begin('a', 'digest', 'device')
            result = dict(status='ok', execution_status='executed', verification_status='verified',
                          observation={'catalog': [{'text': 'PRIVATE_UI_SENTINEL'}]},
                          message='PRIVATE_UI_SENTINEL', arbitrary={'input': 'PRIVATE_UI_SENTINEL'})
            j.finish('a', result)
            self.assertIn('observation', result)
            stored = j.db.execute('SELECT result FROM actions').fetchone()[0]
            self.assertNotIn('PRIVATE_UI_SENTINEL', stored)
            j.close()
            j = Journal(Path(root) / 'journal.sqlite3')
            cached = j.lookup('a', 'digest')
            self.assertEqual(cached['verification_status'], 'verified')
            self.assertFalse(cached['observation_retained'])
            self.assertNotIn('observation', cached)
            j.close()

    def test_burst_nested_results_are_minimized(self):
        with tempfile.TemporaryDirectory() as root:
            j = Journal(Path(root) / 'journal.sqlite3')
            result = dict(status='running', planned_request_ids=['child'], steps=[])
            j.begin_burst('parent', 'digest', 'device', result)
            result['steps'] = [dict(request_id='child', execution_status='executed',
                verification_status='verified', observation={'text': 'PRIVATE_UI_SENTINEL'})]
            j.save_burst('parent', result, finished=True)
            stored = j.db.execute('SELECT result FROM bursts').fetchone()[0]
            self.assertNotIn('PRIVATE_UI_SENTINEL', stored)
            self.assertIn('observation', result['steps'][0])
            self.assertEqual(j.burst_lookup('parent', 'digest')['steps'][0]['execution_status'], 'executed')
            j.close()

    def test_recovery_conditions_omit_plaintext_and_preserve_exact_match_after_restart(self):
        from test_incidents import evidence
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'journal.sqlite3'
            j = Journal(path)
            for name in ('a', 'b'):
                j.begin(name, name, 'device', expected={'text': 'PRIVATE_LABEL', 'changed': True},
                        before_fingerprint='before')
                j.finish(name, dict(execution_status='executed', verification_status='inconclusive',
                                    incident_id='incident-' + name))
            rows = [r[0] for r in j.db.execute('SELECT expected FROM recovery_conditions')]
            self.assertTrue(all('PRIVATE_LABEL' not in row for row in rows))
            self.assertNotEqual(json.loads(rows[0])['text_digest'], json.loads(rows[1])['text_digest'])
            j.close()
            j = Journal(path)
            self.assertEqual(j.reconcile_verified('device', evidence('PRIVATE_LABE')), [])
            unchanged = evidence('PRIVATE_LABEL')
            unchanged['fingerprint'] = 'before'
            self.assertEqual(j.reconcile_verified('device', unchanged), [])
            self.assertEqual(set(j.reconcile_verified('device', evidence('PRIVATE_LABEL'))),
                             {'incident-a', 'incident-b'})
            j.close()

    def test_legacy_plaintext_conditions_remain_recoverable(self):
        from test_incidents import evidence
        with tempfile.TemporaryDirectory() as root:
            j = Journal(Path(root) / 'journal.sqlite3')
            j.begin('legacy', 'digest', 'device')
            with j.lock, j.db:
                j.db.execute('UPDATE recovery_conditions SET expected=? WHERE request_id=?',
                             (json.dumps({'text': 'old-label'}), 'legacy'))
            j.finish('legacy', dict(execution_status='executed', incident_id='incident'))
            self.assertEqual(j.reconcile_verified('device', evidence('other')), [])
            self.assertEqual(j.reconcile_verified('device', evidence('old-label')), ['incident'])
            j.close()

    def test_unknown_or_malformed_digest_cannot_close_incident(self):
        from test_incidents import evidence
        valid = Journal.recovery_record({'text': 'label'})
        cases = [None, [], {**valid, 'format': 'text_digest_v2'},
                 {**valid, 'salt': None}, {**valid, 'text_digest': 'bad'},
                 {**valid, 'changed': 'false'}, {**valid, 'bundle': 'unexpected'}]
        for stored in cases:
            with self.subTest(stored=stored):
                self.assertFalse(Journal.recovery_matches(stored, evidence('label'), None))
