import tempfile
import unittest
from pathlib import Path
from harmony_runtime.contracts import RuntimeFault
from harmony_runtime.journal import Journal
from harmony_runtime.runtime import Runtime
from test_runtime import FakeDevice


class RecoveryTests(unittest.TestCase):
    def test_restart_and_new_request_cannot_bypass_unknown_write(self):
        with tempfile.TemporaryDirectory() as root:
            device = FakeDevice('first')
            first = Runtime(root, factory=lambda serial:device, discover=lambda:['first'])
            sid = first.session('owner', 'open')['session_id']
            obs = first.observe('owner', sid)
            device.fail = True
            result = first.act('owner', dict(session_id=sid,request_id='old',observation_id=obs['observation_id'],action={'kind':'home'}))
            self.assertEqual(result['execution_status'], 'unknown')
            self.assertEqual(device.writes, 1)
            first.close()
            fresh_device = FakeDevice('first')
            second = Runtime(root, factory=lambda serial:fresh_device, discover=lambda:['first'])
            try:
                opened = second.session('new-owner', 'open')
                self.assertTrue(opened['recovery_required'])
                self.assertEqual(opened['unresolved_actions'][0]['request_id'], 'old')
                fresh = second.observe('new-owner', opened['session_id'])
                with self.assertRaises(RuntimeFault) as ctx:
                    second.act('new-owner', dict(session_id=opened['session_id'],request_id='replacement',observation_id=fresh['observation_id'],action={'kind':'home'}))
                self.assertEqual(ctx.exception.code, 'reconciliation_required')
                self.assertEqual(fresh_device.writes, 0)
                self.assertTrue(second.session('new-owner','status',opened['session_id'])['recovery_required'])
                self.assertEqual(len(second.journal.history()), 1)
            finally: second.close()

    def test_crash_boundary_preserves_device_binding(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'journal.sqlite3'
            journal = Journal(path)
            journal.begin('interrupted', 'digest', 'first')
            journal.close()
            journal = Journal(path)
            try:
                self.assertEqual(journal.unresolved('first')[0]['request_id'], 'interrupted')
                self.assertEqual(journal.unresolved('second'), [])
                with self.assertRaises(RuntimeFault): journal.begin('replacement','other','first')
                journal.begin('independent','other','second')
                self.assertEqual(journal.lookup('interrupted','digest')['execution_status'],'unknown')
            finally: journal.close()

    def test_legacy_unknown_is_not_silently_unbound(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(Path(root)/'journal.sqlite3')
            try:
                journal.begin('legacy','digest')
                self.assertEqual(journal.unresolved('any-device')[0]['request_id'],'legacy')
                with self.assertRaises(RuntimeFault): journal.require_reconciled('any-device')
            finally: journal.close()

    def test_screen_recovery_invalidates_pre_sleep_handles(self):
        class SleepyDevice(FakeDevice):
            def __init__(self, serial):
                super().__init__(serial)
                self.asleep = False
                self.wake_calls = 0
            def screen_state(self):
                if self.asleep:
                    return {"screen_on": False, "screen_locked": False}
                return {"screen_on": True, "screen_locked": False}
            def screen_on(self):
                self.wake_calls += 1
                self.asleep = False
            def wake_up_display(self):
                self.asleep = False
            def unlock(self):
                self.asleep = False

        with tempfile.TemporaryDirectory() as root:
            device = SleepyDevice('first')
            runtime = Runtime(root, factory=lambda serial: device, discover=lambda: ['first'])
            try:
                sid = runtime.session('owner', 'open')['session_id']
                pre = runtime.observe('owner', sid)
                pre_id = pre['observation_id']
                device.asleep = True
                # Recovery observe must wake/unlock and issue a fresh handle.
                post = runtime.observe('owner', sid)
                self.assertNotEqual(post['observation_id'], pre_id)
                self.assertGreaterEqual(device.wake_calls, 1)
                self.assertEqual(post['screen_state'], {"screen_on": True, "screen_locked": False})
                # The pre-sleep handle must not authorize a write.
                with self.assertRaises(RuntimeFault) as ctx:
                    runtime.act('owner', dict(
                        session_id=sid,
                        request_id='after-sleep',
                        observation_id=pre_id,
                        action={'kind': 'back'},
                        expected={'changed': True},
                    ))
                self.assertIn(ctx.exception.code, ('stale_observation', 'stale_controller_epoch'))
                self.assertEqual(device.writes, 0)
            finally:
                runtime.close()

    def test_completed_execution_does_not_leave_unknown_barrier(self):
        with tempfile.TemporaryDirectory() as root:
            journal = Journal(Path(root)/'journal.sqlite3')
            try:
                journal.begin('done','digest','device')
                journal.finish('done',{'execution_status':'executed','verification_status':'inconclusive'})
                self.assertEqual(journal.unresolved('device'),[])
                journal.begin('next','next-digest','device')
            finally: journal.close()

if __name__ == '__main__': unittest.main()
