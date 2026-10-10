import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('bridge', ROOT / 'wechat_bridge.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class ProtocolTests(unittest.TestCase):
    def test_database_doctor_does_not_require_accessibility_helper(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(bridge, 'WX', str(Path(folder)/'missing-cli')), \
                patch.object(bridge, 'NATIVE', str(Path(folder)/'missing-helper')), \
                patch.object(bridge, 'accounts', return_value={}), \
                patch('backends.receive.preflight.inspect', return_value={'modifications': []}), \
                patch.object(bridge, 'native') as native, \
                patch.object(bridge, 'run_wx') as cli:
            result = bridge.call('wechat_doctor', {})
            self.assertFalse(result['accessibilityHelperInstalled'])
            self.assertFalse(result['wxCliExists'])
            self.assertIsNone(result['databaseKeyFileExists'])
            native.assert_not_called()
            cli.assert_not_called()

    def test_missing_explicit_cli_is_not_replaced_with_another_executable(self):
        with patch.object(bridge, 'WX', '/nonexistent/explicit-wx-cli'), \
                patch.object(bridge.subprocess, 'run') as run:
            with self.assertRaises(bridge.BridgeError) as error:
                bridge.run_wx_process(['paths', '--json'])
            self.assertEqual(error.exception.code, 'cli_missing')
            run.assert_not_called()

    def test_read_only_stdio_excludes_and_rejects_send_tools(self):
        with patch.object(bridge,'READ_ONLY_MODE',True):
            tools=bridge.handle_rpc({'id':1,'method':'tools/list'})['result']['tools']
            names={x['name'] for x in tools}
            self.assertIn('wechat_contacts',names);self.assertIn('wechat_read_blob',names)
            self.assertNotIn('wechat_send_message',names);self.assertNotIn('wechat_prepare_message',names)
            result=bridge.handle_rpc({'id':2,'method':'tools/call','params':{'name':'wechat_send_message','arguments':{}}})['result']
            self.assertTrue(result['isError']);self.assertEqual(json.loads(result['content'][0]['text'])['error'],'read_only_mode')

    def test_paginated_query_preserves_time_type_and_context_options(self):
        with patch.object(bridge,'account_args',return_value=['--account','fixture']),patch.object(bridge,'run_wx',return_value={'items':[{}],'paging':{'returned':1,'has_more':True}}) as run:
            result=bridge.call('wechat_read_messages',{'contact':'fixture','limit':1,'offset':100,'order':'asc','since':10,'until':20,'type':'image','showHidden':True})
            argv=run.call_args[0][0]
            for option in ('--offset','--since','--until','--type','--show-hidden'):self.assertIn(option,argv)
            self.assertEqual(result['paging']['nextOffset'],101)
            with self.assertRaises(bridge.BridgeError):bridge.call('wechat_read_messages',{'contact':'fixture','aroundServerId':1,'aroundSortSeq':2})
            with self.assertRaises(bridge.BridgeError):bridge.call('wechat_read_messages',{'contact':'fixture','aroundServerId':1,'since':10})
            bridge.call('wechat_search',{'keyword':'20261010','mode':'scan'})
            self.assertIn('--scan',run.call_args[0][0])

    def test_explicit_cache_location_is_respected(self):
        with patch.dict(bridge.os.environ, {'WX_CLI_CACHE_ROOT': '/fixture/private-cache'}), patch.object(bridge, 'private_state') as state:
            self.assertEqual(bridge.wx_environment()['WX_CLI_CACHE_ROOT'], '/fixture/private-cache')
            state.assert_not_called()

    def test_single_account_is_explicit_and_multiple_requires_selection(self):
        with patch.object(bridge, 'accounts', return_value={'account_a': Path('/fixture/account-directory')}):
            self.assertEqual(bridge.account_args(), ['--account', 'account-directory'])
        with patch.object(bridge, 'accounts', return_value={'account_a': Path('/a'), 'account_b': Path('/b')}):
            with self.assertRaises(bridge.BridgeError) as error:
                bridge.account_args()
            self.assertEqual(error.exception.code, 'account_selection_required')

    def test_default_receive_cursor_cannot_follow_changed_account(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(bridge, 'STATE', Path(directory)), patch.object(bridge, 'run_wx', return_value={'items': []}), patch.object(bridge, 'accounts', return_value={'account_a': Path('/a')}) as accounts:
            start = bridge.call('wechat_receive_start', {'contact': 'fixture-contact'})
            accounts.return_value = {'account_b': Path('/b')}
            with self.assertRaises(bridge.BridgeError) as error:
                bridge.call('wechat_receive_pull', {'contact': 'fixture-contact', 'cursor': start['cursor']})
            self.assertEqual(error.exception.code, 'cursor_scope_mismatch')

    def test_watch_missing_key_does_not_start_monitor(self):
        with patch.object(bridge, 'run_wx', side_effect=bridge.BridgeError('database_key_missing', 'Missing key')), patch.object(bridge.subprocess, 'Popen') as monitor:
            with self.assertRaises(bridge.BridgeError):
                bridge.watch_sessions({'seconds': 1})
            monitor.assert_not_called()

    def test_watch_timeout_terminates_and_labels_preview_events(self):
        with patch.object(bridge, 'run_wx', return_value=[]), patch.object(bridge.subprocess, 'Popen') as monitor:
            monitor.return_value.communicate.side_effect = [subprocess.TimeoutExpired('watch', 1), ('{"kind":"update"}\n', '')]
            result = bridge.watch_sessions({'seconds': 1})
            monitor.return_value.terminate.assert_called_once()
            self.assertFalse(result['completeMessageStream'])
            self.assertEqual(len(result['events']), 1)

    def test_stdio_initialize_and_notifications(self):
        messages = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'},
                    {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}]
        process = subprocess.run(['/usr/bin/python3', str(ROOT / 'wechat_bridge.py'), 'mcp'],
                                 input='\n'.join(map(json.dumps, messages)) + '\n',
                                 text=True, capture_output=True, check=True)
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual([r['id'] for r in responses], [1, 2])
        self.assertIn('tools', responses[1]['result'])

    def test_database_error_withholds_private_stderr(self):
        result = subprocess.CompletedProcess([], 1, '', 'no key for account PRIVATE_ACCOUNT')
        with patch.object(bridge.Path, 'is_file', return_value=True), patch.object(bridge.subprocess, 'run', return_value=result):
            with self.assertRaises(bridge.BridgeError) as caught:
                bridge.run_wx(['sessions'])
        self.assertEqual(caught.exception.code, 'database_key_missing')
        self.assertNotIn('PRIVATE_ACCOUNT', str(caught.exception))

    def test_successful_cli_with_failed_wal_does_not_create_receive_cursor(self):
        result = subprocess.CompletedProcess([], 0, '{"items": []}',
                                             '  WAL skipped (previously failed) PRIVATE_DATABASE')
        with tempfile.TemporaryDirectory() as directory, patch.object(bridge, 'STATE', Path(directory)), patch.object(bridge, 'accounts', return_value={'account_a': Path('/a')}), patch.object(bridge.Path, 'is_file', return_value=True), patch.object(bridge.subprocess, 'run', return_value=result):
            with self.assertRaises(bridge.BridgeError) as caught:
                bridge.call('wechat_receive_start', {'contact': 'fixture-contact'})
            self.assertEqual(caught.exception.code, 'database_cache_incomplete')
            self.assertNotIn('PRIVATE_DATABASE', str(caught.exception))
            self.assertEqual(list(Path(directory).glob('receive-*.json')), [])

    def test_partial_shards_and_decode_skips_are_rejected(self):
        for diagnostics, envelope in [('warning: shard PRIVATE_SHARD: unavailable', {'items': []}),
                                      ('', {'items': [], 'stats': {'skipped': 1}})]:
            with self.subTest(diagnostics=bool(diagnostics)), self.assertRaises(bridge.BridgeError) as caught:
                bridge.validate_database_output(diagnostics, envelope)
            self.assertEqual(caught.exception.code, 'message_query_incomplete')
            self.assertNotIn('PRIVATE_SHARD', str(caught.exception))

    def test_normal_cache_stats_and_sidecar_warning_do_not_block_reads(self):
        bridge.validate_database_output('Cache: 2 decrypted, 1 cached, 0 errors, 1 WAL patched\n'
                                        'warning: failed to write shard metadata sidecar: unavailable',
                                        {'items': [], 'stats': {'skipped': 0}})

    def test_attachments_outside_account_are_rejected(self):
        with patch.object(bridge, 'accounts', return_value={}):
            with self.assertRaises(bridge.BridgeError) as caught:
                bridge.attachment('/etc/hosts')
        self.assertEqual(caught.exception.code, 'attachment_scope')

    def test_send_token_and_uncertain_result_cannot_replay(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(bridge, 'STATE', Path(directory)):
            draft_id = 'a' * 32
            path = bridge.draft_file(draft_id)
            bridge.write_private(path, {'contact': 'Test', 'message': 'Exact text',
                                       'expires': time.time() + 600, 'approval': 'token', 'used': False})
            with patch.object(bridge, 'native') as native:
                with self.assertRaises(bridge.BridgeError):
                    bridge.call('wechat_send_message', {'draftId': draft_id, 'approvalToken': 'wrong'})
                native.assert_not_called()
                native.side_effect = bridge.BridgeError('native_timeout', 'Uncertain result')
                with self.assertRaises(bridge.BridgeError):
                    bridge.call('wechat_send_message', {'draftId': draft_id, 'approvalToken': 'token'})
                with self.assertRaises(bridge.BridgeError) as caught:
                    bridge.call('wechat_send_message', {'draftId': draft_id, 'approvalToken': 'token'})
                self.assertEqual(caught.exception.code, 'draft_expired')
                self.assertEqual(native.call_count, 1)


if __name__ == '__main__':
    unittest.main()
