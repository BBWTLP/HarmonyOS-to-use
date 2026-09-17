import tempfile
import threading
import unittest
from harmony_runtime.runtime import Runtime
from harmony_runtime.journal import Journal
from harmony_runtime.contracts import RuntimeFault
from test_runtime import FakeDevice


class ShutdownTests(unittest.TestCase):
    def test_shutdown_drains_write_before_closing_journal_and_rejects_queue(self):
        with tempfile.TemporaryDirectory() as root:
            entered, release = threading.Event(), threading.Event()
            device = FakeDevice('fake')
            closes = []
            def dispatch(action, target):
                device.writes += 1
                entered.set()
                if not release.wait(5): raise AssertionError('test release missing')
                raise OSError('uncertain device outcome')
            device.dispatch = dispatch
            device.close = lambda: closes.append(True)
            runtime = Runtime(root, factory=lambda serial: device, discover=lambda: ['fake'])
            sid = runtime.session('a', 'open')['session_id']
            obs = runtime.observe('a', sid)
            req = dict(session_id=sid, request_id='shutdown-write', observation_id=obs['observation_id'], action={'kind':'home'})
            results, errors = [], []
            def invoke(fn):
                try: results.append(fn())
                except Exception as exc: errors.append(exc)
            action = threading.Thread(target=invoke, args=(lambda: runtime.act('a', req),))
            action.start()
            self.assertTrue(entered.wait(2))
            queued = threading.Thread(target=invoke, args=(lambda: runtime.observe('a', sid),))
            queued.start()
            closing = [threading.Thread(target=invoke, args=(runtime.close,)) for _ in range(2)]
            for thread in closing: thread.start()
            # Acquiring the guard establishes whether shutdown has begun without
            # sleeping; wait_for yields it to the closing thread when necessary.
            with runtime.shutdown:
                self.assertTrue(runtime.shutdown.wait_for(lambda: runtime.stopped, timeout=2))
            self.assertFalse(runtime.resources_closed)
            self.assertEqual(closes, [])
            with self.assertRaises(RuntimeFault) as ctx: runtime.session('b', 'open')
            self.assertEqual(ctx.exception.code, 'runtime_stopped')
            release.set()
            for thread in [action, queued, *closing]:
                thread.join(3)
                self.assertFalse(thread.is_alive())
            runtime.close()
            self.assertEqual(closes, [True])
            self.assertEqual(device.writes, 1)
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0].code, 'runtime_stopped')
            self.assertTrue(any(isinstance(r, dict) and r.get('execution_status') == 'unknown' for r in results))
            journal = Journal(runtime.root / 'journal.sqlite3')
            try: self.assertEqual(journal.unresolved('fake')[0]['request_id'], 'shutdown-write')
            finally: journal.close()

    def test_shutdown_without_device_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = Runtime(root, discover=lambda: [])
            runtime.close()
            runtime.close()
            self.assertTrue(runtime.resources_closed)
