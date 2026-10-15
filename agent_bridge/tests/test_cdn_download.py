import hashlib
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backends.receive import cdn_download as cdn
from backends.receive import merged_attachments as merged

PLAINTEXT = b'%PDF-1.7\nsynthetic CDN original fixture\n%%EOF\n'
# Independent OpenSSL AES-128-ECB/PKCS7 fixture, public synthetic key.
KEY = '000102030405060708090a0b0c0d0e0f'
CIPHER = bytes.fromhex('c174a35202af067dfdec361a74fbd37b215899c7ca1343b8cdc8575329c0cb6087256991ffc344793b9b1627add36ad4')
MD5 = hashlib.md5(PLAINTEXT).hexdigest()


def encoded_integer(value):
    output = bytearray()
    while value > 127:
        output.append((value & 127) | 128); value >>= 7
    return bytes(output) + bytes([value])


def pb_field(field, value):
    if type(value) is int:
        return encoded_integer(field << 3) + encoded_integer(value)
    return encoded_integer(field << 3 | 2) + encoded_integer(len(value)) + value


class CdnDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.target = self.root / 'guide.pdf'
        self.route = {'ip': '1.1.1.1', 'uin': 0xf1234567, 'version': 1, 'auth': b'private-token'}
        self.reference = {'fileId': '30' * 97, 'aesKey': KEY, 'fileType': 5}

    def tearDown(self):
        self.temp.cleanup()

    def response(self, route, file_id, file_type, start, end, sequence, timeout):
        end = min(end, start + 15, len(CIPHER) - 1)
        return {'retcode': b'0', 'seq': str(sequence).encode(), 'totalsize': str(len(CIPHER)).encode(),
                'rangestart': str(start).encode(), 'rangeend': str(end).encode(), 'filedata': CIPHER[start:end + 1]}

    def run_download(self, fetch=None, md5=MD5):
        with patch.object(cdn, 'routes', return_value=[self.route]), patch.object(cdn, 'fetch_range', side_effect=fetch or self.response):
            return cdn.download(self.root / 'cache', self.reference, self.target, len(PLAINTEXT), md5)

    def test_signed_int32_uin_is_normalized_without_truncating_arbitrary_uint64(self):
        self.assertEqual(cdn.unsigned_uin(0xfffffffff1234567), 0xf1234567)
        self.assertEqual(cdn.unsigned_uin(0x71234567), 0x71234567)
        for invalid in (True, 0, 0x1234567812345678):
            with self.assertRaises(cdn.DownloadError): cdn.unsigned_uin(invalid)

    def test_route_cache_keeps_auth_private_and_rejects_local_addresses(self):
        def route(ip):
            return b''.join(pb_field(k, v) for k, v in [(1, 1), (3, int.from_bytes(bytes(ip), 'little')), (7, 443), (8, b'synthetic-secret-auth-key')])
        config = pb_field(1, 1) + pb_field(2, 0xfffffffff1234567) + pb_field(7, route([127, 0, 0, 1])) + pb_field(7, route([1, 1, 1, 1]))
        cache = self.root / 'cache'
        cache.write_bytes(pb_field(1, pb_field(7, config)))
        found = cdn.routes(cache)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['uin'], 0xf1234567)
        self.assertEqual(found[0]['ip'], '1.1.1.1')

    def test_malformed_wire_and_duplicate_response_fields_are_rejected(self):
        for payload in (b'\x08\x80', b'\x0a\x40short', b'\x00'):
            with self.assertRaises(cdn.DownloadError): cdn.protobuf(payload)
        for data in (b'\x00\x00\x10\x00short', cdn.pack({'retcode': 0}) * 2):
            with self.assertRaises(cdn.DownloadError): cdn.unpack(data)

    @unittest.skipUnless(sys.platform == 'darwin', 'CommonCrypto backend')
    def test_fresh_network_download_handles_inclusive_partial_ranges_and_decrypts(self):
        result = self.run_download()
        self.assertEqual(self.target.read_bytes(), PLAINTEXT)
        self.assertEqual(result['downloadRequests'], 3)
        self.assertEqual(result['encryptedBytesDownloaded'], len(CIPHER))
        self.assertTrue(result['networkDownloadPerformed'])
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['guide.pdf'])

    @unittest.skipUnless(sys.platform == 'darwin', 'CommonCrypto backend')
    def test_integrity_failure_preserves_existing_original_and_removes_temporary_files(self):
        self.target.write_bytes(b'previous verified original')
        with self.assertRaises(cdn.DownloadError) as error:
            self.run_download(md5='0' * 32)
        self.assertEqual(error.exception.code, 'cdn_file_integrity_mismatch')
        self.assertEqual(self.target.read_bytes(), b'previous verified original')
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['guide.pdf'])

    def test_wrong_thumbnail_size_is_rejected_before_decrypting(self):
        def wrong(*args):
            response = self.response(*args); response['totalsize'] = b'16'; return response
        with self.assertRaises(cdn.DownloadError) as error: self.run_download(fetch=wrong)
        self.assertEqual(error.exception.code, 'cdn_range_invalid')
        self.assertFalse(self.target.exists())

    @unittest.skipUnless(sys.platform == 'darwin', 'CommonCrypto backend')
    def test_auth_rejection_tries_next_cached_route_without_login_or_gui(self):
        first = dict(self.route, ip='8.8.8.8')
        def response(route, *args):
            if route is first: raise cdn.DownloadError('cdn_auth_rejected', -5103011)
            return self.response(route, *args)
        with patch.object(cdn, 'routes', return_value=[first, self.route]), patch.object(cdn, 'fetch_range', side_effect=response):
            result = cdn.download(self.root / 'cache', self.reference, self.target, len(PLAINTEXT), MD5)
        self.assertTrue(result['md5Verified'])

    def test_force_download_never_uses_existing_local_file_as_download_proof(self):
        item = {'itemPath': [0], 'dataId': 'entry', 'fileName': 'guide.pdf', 'fileFormat': 'pdf',
                'expectedBytes': len(PLAINTEXT), 'expectedMd5': MD5}
        message = {'server_id': 9007199254742007, 'sort_seq': 1700000000000}
        with patch.object(merged, 'export', side_effect=AssertionError('must bypass local copies')), patch.object(merged, 'download_reference', return_value=self.reference), patch.object(cdn, 'download', side_effect=cdn.DownloadError('cdn_auth_rejected', -5103011)):
            result = merged.acquire(self.root / 'account', self.root, message, 'group', item, force_download=True)
        self.assertFalse(result['originalFileObtained'])
        self.assertEqual(result['cdnRetcode'], -5103011)
        self.assertNotIn('private-token', str(result))


if __name__ == '__main__': unittest.main()
