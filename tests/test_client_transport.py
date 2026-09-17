import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from harmony_runtime.service import Client
from harmony_runtime.contracts import RuntimeFault


class ClientTransportTests(unittest.TestCase):
    def test_missing_endpoint_is_not_a_dispatched_action(self):
        with tempfile.TemporaryDirectory() as root:
            client = Client(root)
            with self.assertRaises(RuntimeFault) as ctx: client.call('act', arguments={})
            self.assertEqual(ctx.exception.code, 'runtime_unavailable')

    def test_lost_and_invalid_responses_never_retry_actions(self):
        for response in [None, b'not-json', b'[]', b'{"result":null}', b'{"error":{}}']:
            with self.subTest(response=response), tempfile.TemporaryDirectory() as root:
                requests = []
                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *args): pass
                    def do_POST(self):
                        requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                        if response is None:
                            self.close_connection = True
                            return
                        self.send_response(200)
                        self.send_header('Content-Length', str(len(response)))
                        self.end_headers()
                        self.wfile.write(response)
                server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':.02})
                thread.start()
                Path(root, 'endpoint.json').write_text(json.dumps({'port':server.server_port,'token':'test'}))
                client = Client(root)
                client.owner = 'test-owner'
                try:
                    with self.assertRaises(RuntimeFault) as ctx: client.call('act', arguments={'request_id':'once'})
                    self.assertEqual(ctx.exception.code, 'execution_unknown')
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(requests[0]['params']['arguments']['request_id'], 'once')
                    with self.assertRaises(RuntimeFault) as ctx: client.call('observe', session_id='test')
                    self.assertEqual(ctx.exception.code, 'runtime_transport_error')
                    self.assertEqual(len(requests), 2)
                finally:
                    server.shutdown(); server.server_close(); thread.join(2)
