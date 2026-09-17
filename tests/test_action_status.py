import tempfile
import unittest
from harmony_runtime.runtime import Runtime
from harmony_runtime.contracts import RuntimeFault


class ActionStatusTests(unittest.TestCase):
    def test_persistent_status_is_device_bound_read_only_and_redacted(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = Runtime(root, factory=lambda _: self.fail('query touched device'))
            sid = runtime.session('owner', 'open', device_id='device-one')['session_id']
            runtime.journal.begin('lost-response', 'digest', 'device-one')
            runtime.journal.finish('lost-response', {
                'execution_status': 'unknown', 'verification_status': 'inconclusive',
                'observation': {'secret': 'private-ui-content'}, 'text': 'private-input'})
            runtime.close()
            runtime = Runtime(root, factory=lambda _: self.fail('query touched device'))
            try:
                sid = runtime.session('owner', 'open', device_id='device-one')['session_id']
                runtime.session('owner', 'pause', session_id=sid)
                result = runtime.session('owner', 'action_status', session_id=sid, request_id='lost-response')
                self.assertEqual(result['execution_status'], 'unknown')
                self.assertNotIn('private-', str(result))
                self.assertTrue(runtime.session('owner', 'status', session_id=sid)['recovery_required'])
                with self.assertRaises(RuntimeFault) as fault:
                    runtime.session('outsider', 'action_status', session_id=sid, request_id='lost-response')
                self.assertEqual(fault.exception.code, 'session_invalid')
                other = runtime.session('owner', 'open', device_id='device-two')['session_id']
                self.assertEqual(runtime.session('owner', 'action_status', session_id=other, request_id='lost-response')['status'], 'not_found')
                missing = runtime.session('owner', 'action_status', session_id=sid, request_id='absent')
                self.assertEqual(missing['status'], 'not_found')
                self.assertIn('not proof', missing['message'])
            finally:
                runtime.close()

    def test_inflight_and_finished_records_are_distinguished(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = Runtime(root)
            try:
                sid = runtime.session('owner', 'open', device_id='device')['session_id']
                runtime.journal.begin('action', 'digest', 'device')
                current = runtime.session('owner', 'action_status', session_id=sid, request_id='action')
                self.assertTrue(current['in_flight_or_interrupted'])
                self.assertEqual(current['execution_status'], 'unknown')
                runtime.journal.finish('action', {'execution_status':'executed', 'verification_status':'passed'})
                finished = runtime.session('owner', 'action_status', session_id=sid, request_id='action')
                self.assertFalse(finished['in_flight_or_interrupted'])
                self.assertEqual(finished['verification_status'], 'passed')
                for operation, request_id in [('action_status', None), ('status', 'action')]:
                    with self.assertRaises(RuntimeFault):
                        runtime.session('owner', operation, session_id=sid, request_id=request_id)
            finally:
                runtime.close()
