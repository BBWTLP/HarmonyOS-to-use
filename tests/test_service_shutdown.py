import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from harmony_runtime.service import serve, Client
from harmony_runtime.runtime import Runtime
from test_runtime import FakeDevice


class ServiceShutdownTests(unittest.TestCase):
    def start(self, root, factory, timeout=.4):
        ready = threading.Event()
        servers, errors = [], []
        def notify(server): servers.append(server); ready.set()
        def run():
            try: serve(root, runtime_factory=factory, ready=notify, request_read_timeout=timeout)
            except Exception as exc: errors.append(exc)
        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(ready.wait(10))
        return servers[0], thread, errors

    def test_partial_headers_and_body_expire_without_device_creation(self):
        with tempfile.TemporaryDirectory() as root:
            devices = []
            def device(serial): devices.append(serial); return FakeDevice(serial)
            server, thread, errors = self.start(root, lambda p: Runtime(p, factory=device))
            endpoint = json.loads((Path(root)/'endpoint.json').read_text())
            try:
                for payload in [b'POST /rpc HTTP/1.0\r\n',
                    (f'POST /rpc HTTP/1.0\r\nAuthorization: Bearer {endpoint["token"]}\r\nContent-Length: 100\r\n\r\n{{').encode()]:
                    with socket.create_connection(('127.0.0.1', server.server_port), timeout=2) as conn:
                        conn.sendall(payload)
                        started = time.monotonic()
                        try: data = conn.recv(1024)
                        except (ConnectionResetError, ConnectionAbortedError): data = b''
                        self.assertEqual(data, b'')
                        self.assertLess(time.monotonic()-started, 1.5)
                self.assertEqual(devices, [])
            finally:
                server.shutdown(); thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertFalse((Path(root)/'endpoint.json').exists())

    def test_trickling_headers_cannot_extend_total_read_deadline(self):
        with tempfile.TemporaryDirectory() as root:
            server, thread, errors = self.start(root, lambda p: Runtime(p))
            try:
                with socket.create_connection(('127.0.0.1', server.server_port), timeout=2) as conn:
                    conn.sendall(b'POST /rpc HTTP/1.0\r\nX-Slow: ')
                    stop = threading.Event()
                    sent = []
                    def trickle():
                        while not stop.wait(.05):
                            try: conn.sendall(b'x'); sent.append(True)
                            except OSError: return
                    sender = threading.Thread(target=trickle)
                    sender.start()
                    try:
                        start = time.monotonic()
                        try: data = conn.recv(1024)
                        except (ConnectionResetError, ConnectionAbortedError): data = b''
                        self.assertEqual(data, b'')
                        self.assertLess(time.monotonic()-start, 1.5)
                        self.assertGreaterEqual(len(sent), 2)
                    finally:
                        stop.set(); sender.join(2)
            finally:
                server.shutdown(); thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

    def test_shutdown_cancels_active_http_request_before_joining_handlers(self):
        with tempfile.TemporaryDirectory() as root:
            entered = threading.Event()
            runtimes = []
            def factory(path):
                runtime = Runtime(path, factory=FakeDevice, discover=lambda: ['fake'])
                original = runtime._observe
                def observe(session, *args, **kwargs):
                    entered.set()
                    if not session.paused.wait(3):
                        raise AssertionError('shutdown did not cancel active worker')
                    return original(session, *args, **kwargs)
                runtime._observe = observe
                runtimes.append(runtime)
                return runtime
            server, thread, errors = self.start(root, factory)
            client = Client(root)
            sid = client.call('session', operation='open')['session_id']
            outcomes = []
            def request():
                try: outcomes.append(client.call('observe', session_id=sid))
                except Exception as exc: outcomes.append(exc)
            caller = threading.Thread(target=request)
            caller.start()
            try:
                self.assertTrue(entered.wait(2))
                start = time.monotonic()
                server.shutdown(); thread.join(2); caller.join(2)
                self.assertFalse(thread.is_alive())
                self.assertFalse(caller.is_alive())
                self.assertLess(time.monotonic()-start, 2)
                self.assertEqual(outcomes[0].code, 'runtime_stopped')
                self.assertTrue(runtimes[0].resources_closed)
                self.assertEqual(errors, [])
            finally:
                server.shutdown(); thread.join(5); caller.join(5)
