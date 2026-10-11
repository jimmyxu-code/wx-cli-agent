"""Real local socket with an offline C++ fixture; no WeChat or network send."""
import os
import json
from pathlib import Path
import select
import struct
import subprocess
import tempfile
import time
import unittest

from client import Client, RpcError


class OfflineRpcTests(unittest.TestCase):
    def start(self, authorized=False):
        self.temp = tempfile.TemporaryDirectory(prefix='wxn-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name).resolve() / 'rpc.sock'
        fixture = Path(os.environ['WX_NATIVE_RPC_FIXTURE']).resolve(strict=True)
        arguments = [str(fixture), str(self.path)]
        if authorized:
            arguments.append('authority-fixture')
        self.process = subprocess.Popen(arguments, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.cleanup_process)
        until = time.monotonic() + 3
        while time.monotonic() < until:
            if self.process.poll() is not None:
                self.fail('offline fixture exited before socket readiness')
            readable, _, _ = select.select([self.process.stdout], [], [], .05)
            if readable:
                ready = json.loads(self.process.stdout.readline())
                self.assertTrue(ready['offlineRpcFixtureReady'])
                self.assertFalse(ready['realWeChatSend'])
                break
        else:
            self.fail('offline fixture readiness timeout')
        # bind creates the inode before chmod/listen. The fixture announces
        # readiness only after rpc_start returns; existence alone races setup.
        self.client = Client(self.path)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def cleanup_process(self):
        if self.process.poll() is None:
            self.process.terminate()
        self.process.communicate(timeout=5)

    def finish(self):
        self.client.request(7)
        output, errors = self.process.communicate(timeout=5)
        self.assertEqual(self.process.returncode, 0, errors)
        self.assertFalse(errors)
        self.assertIn('"privateRpcStopped":true', output)
        self.assertFalse(self.path.exists())

    def rejected(self, operation, code):
        with self.assertRaises(RpcError) as caught:
            operation()
        self.assertEqual(caught.exception.native_code, code)

    def test_unarmed_capacity_and_cancel(self):
        self.start()
        handles = [self.client.prepare('filehelper', 'offline 中文 🧪', 1, i + 1)['handle']
                   for i in range(128)]
        self.rejected(lambda: self.client.prepare('filehelper', 'overflow', 1, 129), -11)
        self.rejected(lambda: self.client.enqueue(handles[0]), -8)
        for handle in handles:
            status = self.client.status(handle)
            self.assertEqual(status['phase'], 1)
            self.assertFalse(status['serialized'])
            self.client.cancel(handle)
        self.finish()

    def test_private_authority_gates_and_mock_callback(self):
        self.start(authorized=True)
        handle = self.client.prepare('filehelper', 'offline fixture only', 1, 1)['handle']
        authority = bytes([1]) * 32
        for op in (12, 13):
            self.rejected(lambda op=op: self.client.request(op, struct.pack('<Q', handle) + bytes(32)), -8)
        self.rejected(lambda: self.client.request(12, struct.pack('<Q', handle) + authority), -14)
        self.client.request(13, struct.pack('<Q', handle) + authority)
        for _ in range(100):
            phase, result = struct.unpack('<Ii', self.client.request(14))
            if phase == 2:
                break
            time.sleep(.01)
        self.assertEqual((phase, result), (2, -14))
        self.assertEqual(self.client.status(handle)['phase'], 1)
        self.client.arm_from_controller(handle, authority)
        self.client.enqueue(handle)
        for _ in range(100):
            status = self.client.status(handle)
            if status['destroyed']:
                break
            time.sleep(.01)
        self.assertEqual(status['phase'], 3)
        self.assertTrue(status['destroyed'])
        self.assertFalse(status['nativeAcknowledgementSucceeded'])
        self.rejected(lambda: self.client.enqueue(handle), -8)
        other = self.client.prepare('filehelper', 'second offline handle', 1, 2)['handle']
        self.rejected(lambda: self.client.arm_from_controller(other, authority), -8)
        self.client.cancel(other)
        self.client.release(handle)
        self.finish()


if __name__ == '__main__':
    unittest.main()
