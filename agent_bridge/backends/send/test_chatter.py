import hashlib
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from chatter import ChatterBackend, SendError

class Handler(BaseHTTPRequestHandler):
    calls = []
    reply = b'{"status":"ok"}'
    def log_message(self, *args): pass
    def do_GET(self):
        self.send_response(405); self.end_headers()
    def do_POST(self):
        type(self).calls.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
        self.send_response(200); self.end_headers(); self.wfile.write(type(self).reply)

class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()
    def setUp(self): Handler.calls = []; Handler.reply = b'{"status":"ok"}'
    def backend(self, verified=True):
        return ChatterBackend('http://127.0.0.1:' + str(self.server.server_port), 'fake-test-token', '4.1.13', '0'*64 if verified else None)
    def test_default_blocks_send(self):
        with self.assertRaises(SendError) as caught: self.backend(False).send('test_id', text='test')
        self.assertEqual(caught.exception.code, 'version_build_not_verified'); self.assertEqual(Handler.calls, [])
    def test_probe_no_send(self):
        result = self.backend().capabilities(probe=True)
        self.assertTrue(result['routePresent']); self.assertFalse(result['runtimeReadinessVerified']); self.assertEqual(Handler.calls, [])
    def test_native_request_and_honest_receipt(self):
        result = self.backend().send('test_id', text='hello')
        self.assertEqual(Handler.calls[0]['message'][0]['data']['text'], 'hello'); self.assertFalse(result['deliveryConfirmed'])
    def test_uncertain_no_retry(self):
        Handler.reply = b'bad json'
        with self.assertRaises(SendError) as caught: self.backend().send('test_id', text='hello')
        self.assertTrue(caught.exception.uncertain); self.assertEqual(len(Handler.calls), 1)
    def test_file_name_and_hash(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'test.zip'; path.write_bytes(b'payload')
            with self.assertRaises(SendError): self.backend().send('test_id', file_path=path)
            with self.assertRaises(SendError): self.backend().send('test_id', file_path=path, expected_sha256='bad', allow_filename_change=True)
            result = self.backend().send('test_id', file_path=path, expected_sha256=hashlib.sha256(b'payload').hexdigest(), allow_filename_change=True)
            self.assertFalse(result['filenamePreserved']); self.assertEqual(len(Handler.calls), 1)
    def test_invalid_version_rejected(self):
        with self.assertRaises(SendError): ChatterBackend('http://127.0.0.1', 'fake', '')
        with self.assertRaises(SendError): ChatterBackend('http://127.0.0.1', 'fake', '4.1.13', 'bad')
    def test_remote_endpoint_rejected(self):
        with self.assertRaises(SendError): ChatterBackend('http://example.com', 'fake', '4.1.13')

if __name__ == '__main__': unittest.main()
