import unittest
import tempfile
import plistlib
from pathlib import Path
from types import SimpleNamespace
from incremental import initialize, pull, ReceiveError
from preflight import inspect


class ReceiveTests(unittest.TestCase):
    def test_preflight_checks_presence_without_exposing_key_or_account(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app = root/'WeChat.app'
            (app/'Contents').mkdir(parents=True)
            (app/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleShortVersionString': '4.1.13',
                                                                'CFBundleVersion': '269602'}))
            config = root/'Library/Application Support/wx-cli/config'
            config.mkdir(parents=True)
            (config/'keys.toml').write_text('synthetic_private_marker')
            result = inspect(app, root, lambda *args, **kwargs:
                             SimpleNamespace(stdout='System Integrity Protection status: enabled.'))
            self.assertTrue(result['keyFilePresent'])
            self.assertFalse(result['keyValidationPerformed'])
            self.assertEqual(result['routes']['traceMemo413'], 'blocked_sip')
            self.assertNotIn('synthetic_private_marker', str(result))
            self.assertEqual(result['modifications'], [])

    def test_initialize_discards_history(self):
        result = initialize(lambda args: {'items': [{'sort_seq': 123, 'content': 'old'}]}, 'filehelper')
        self.assertEqual(result['sortSeq'], 123)
        self.assertEqual(result['messages'], [])

    def test_real_query_anchor_and_repeatable_cursor(self):
        calls = []
        def run(args):
            calls.append(args)
            return {'items': [{'sort_seq': 4}, {'sort_seq': 5}], 'paging': {'has_more': False}}
        one = pull(run, 'filehelper', 3)
        two = pull(run, 'filehelper', 3)
        self.assertEqual(one, two)
        self.assertEqual(one['sortSeq'], 5)
        self.assertIn('--after-sort-seq', calls[0])

    def test_truncated_same_sequence_bucket_not_skipped(self):
        result = pull(lambda args: {'items': [{'sort_seq': 4}, {'sort_seq': 5}, {'sort_seq': 5}]},
                      'filehelper', 3, limit=2)
        self.assertEqual(result['messages'], [{'sort_seq': 4}])
        self.assertEqual(result['sortSeq'], 4)
        self.assertTrue(result['hasMore'])

    def test_oversized_bucket_fails_without_cursor_advance(self):
        with self.assertRaises(ReceiveError):
            pull(lambda args: {'items': [{'sort_seq': 4}]*3}, 'filehelper', 3, limit=2)

    def test_runner_key_failure_propagates_no_fake_empty_success(self):
        def blocked(args):
            raise ReceiveError('key_missing')
        with self.assertRaisesRegex(ReceiveError, 'key_missing'):
            pull(blocked, 'filehelper', 1)

    def test_invalid_upstream_cursor_rejected(self):
        with self.assertRaises(ReceiveError):
            pull(lambda args: {'items': [{'sort_seq': 1}]}, 'filehelper', 1)


if __name__ == '__main__':
    unittest.main()
