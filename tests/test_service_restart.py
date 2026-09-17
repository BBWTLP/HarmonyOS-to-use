import tempfile
import threading
import unittest
from harmony_runtime.service import Client, serve
from harmony_runtime.runtime import Runtime
from harmony_runtime.contracts import RuntimeFault
from test_runtime import FakeDevice


class ServiceRestartTests(unittest.TestCase):
    def test_existing_frontend_reopens_after_restart_without_replaying_action(self):
        with tempfile.TemporaryDirectory() as root:
            devices = []
            runtimes = []
            def factory(serial):
                device = FakeDevice(serial)
                devices.append(device)
                return device
            def runtime_factory(path):
                runtime = Runtime(path, factory=factory, discover=lambda: ['fake'])
                runtimes.append(runtime)
                return runtime
            def start():
                ready = threading.Event()
                servers = []
                def notify(server):
                    servers.append(server)
                    ready.set()
                thread = threading.Thread(target=serve, args=(root,), kwargs={'runtime_factory':runtime_factory,'ready':notify})
                thread.start()
                self.assertTrue(ready.wait(10))
                return servers[0], thread
            def stop(server, thread):
                server.shutdown()
                thread.join(10)
                self.assertFalse(thread.is_alive())

            client = Client(root)
            server, thread = start()
            try:
                old_session = client.call('session', operation='open')['session_id']
                old_owner = client.owner
                runtimes[-1].journal.begin('interrupted', 'digest', 'fake')
            finally:
                stop(server, thread)
            server, thread = start()
            try:
                # Old identity is rejected before action validation or dispatch.
                with self.assertRaises(RuntimeFault) as error:
                    client.call('act', arguments={'session_id':old_session,'request_id':'must-not-replay'})
                self.assertEqual(error.exception.code, 'session_reopen_required')
                self.assertIsNone(client.owner)
                self.assertEqual(devices, [])
                new = client.call('session', operation='open')
                self.assertNotEqual(client.owner, old_owner)
                self.assertNotEqual(new['session_id'], old_session)
                self.assertTrue(new['recovery_required'])
                status = client.call('session', operation='action_status', session_id=new['session_id'], request_id='interrupted')
                self.assertEqual(status['execution_status'], 'unknown')
                rejected = client.call('session', operation='action_status', session_id=new['session_id'], request_id='must-not-replay')
                self.assertEqual(rejected['status'], 'not_found')
                with self.assertRaises(RuntimeFault) as error:
                    client.call('session', operation='status', session_id=old_session)
                self.assertEqual(error.exception.code, 'session_invalid')
                self.assertEqual(devices, [])
            finally:
                stop(server, thread)
