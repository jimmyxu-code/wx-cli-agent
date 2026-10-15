import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_cli as agent
import wechat_bridge as bridge
from backends.send.native_413 import agent_sender as sender


class ConnectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.binary = self.root / 'wx-cli'
        self.binary.write_bytes(b'fixture only')
        self.value = SimpleNamespace(WX=str(self.binary))
        self.accounts = [{'alias': 'account_first'}]
        self.calls = []
        self.read_error = None

    def invoke(self, value, name, args):
        self.calls.append((name, args))
        if name == 'wechat_accounts': return {'accounts': self.accounts}
        if name == 'wechat_sessions':
            if self.read_error: raise self.read_error
            return {'items': [{'preview': 'PRIVATE CHAT BODY MUST NOT BE RETURNED'}]}
        if name == 'wechat_native_sender_status':
            return {'configured': True, 'readyToStart': False, 'originalWeChatRunning': True}
        raise AssertionError('Unexpected operation: ' + name)

    def connect(self, **kwargs):
        with patch.object(agent, 'bridge', return_value=self.value), patch.object(agent, 'invoke', side_effect=self.invoke):
            return agent.connect(**kwargs)

    def test_one_command_performs_one_bounded_real_read_without_chat_output(self):
        result = self.connect()
        self.assertTrue(result['ready'])
        self.assertTrue(result['historyReadVerified'])
        self.assertEqual(self.calls, [('wechat_accounts', {}), ('wechat_sessions', {'account': 'account_first', 'limit': 1})])
        self.assertNotIn('PRIVATE CHAT', json.dumps(result))
        self.assertTrue(all(Path(argv[0]).is_absolute() for argv in result['nextCommands']))

    def test_missing_runtime_is_installed_once_and_rechecked(self):
        self.binary.unlink()
        def install(): self.binary.write_bytes(b'fixture'); return 'downloaded_verified_runtime'
        with patch.object(agent, 'install_runtime', side_effect=install) as setup:
            result = self.connect()
        setup.assert_called_once_with()
        self.assertEqual(result['runtime'], 'downloaded_verified_runtime')
        self.assertTrue(result['ready'])

    def test_multiple_accounts_require_selection_before_any_history_read(self):
        self.accounts.append({'alias': 'account_second'})
        with self.assertRaises(agent.AgentError) as error: self.connect()
        self.assertEqual(error.exception.code, 'account_selection_required')
        self.assertFalse(error.exception.details['ready'])
        self.assertEqual([name for name, _ in self.calls], ['wechat_accounts'])
        result = self.connect(account='account_second')
        self.assertEqual(result['account'], 'account_second')
        self.assertEqual(self.calls[-1][1]['account'], 'account_second')

    def test_unknown_or_missing_account_never_probes_history(self):
        for accounts, requested, code in [([], None, 'wechat_account_missing'), (self.accounts, 'missing', 'unknown_account')]:
            self.accounts = accounts
            with self.assertRaises(agent.AgentError) as error: self.connect(account=requested)
            self.assertEqual(error.exception.code, code)
        self.assertFalse(any(name == 'wechat_sessions' for name, _ in self.calls))

    def test_key_failure_is_not_reported_as_connected_and_returns_recovery(self):
        self.read_error = agent.AgentError('database_key_missing', 'No validated key is available.')
        with self.assertRaises(agent.AgentError) as error: self.connect()
        self.assertEqual(error.exception.code, 'database_key_missing')
        self.assertFalse(error.exception.details['historyReadVerified'])
        self.assertTrue(error.exception.next_actions)

    def test_send_connection_only_checks_status_and_never_prepares_or_submits(self):
        with self.assertRaises(agent.AgentError) as error: self.connect(task='send')
        self.assertEqual(error.exception.code, 'native_sender_not_ready_check_status')
        self.assertEqual(self.calls[-1][0], 'wechat_native_sender_status')
        self.assertFalse(any('prepare' in name or 'submit' in name for name, _ in self.calls))
        self.assertNotIn('--authorized', json.dumps(error.exception.details['nextCommands']))

    def test_no_argument_entry_connects_and_compact_catalog_preserves_named_schemas(self):
        stream = io.StringIO()
        with patch.object(agent, 'bridge', return_value=self.value), patch.object(agent, 'invoke', side_effect=self.invoke), contextlib.redirect_stdout(stream):
            self.assertEqual(agent.main([]), 0)
        self.assertTrue(json.loads(stream.getvalue())['data']['ready'])
        for argv, compact in [(['tools', '--compact'], True), (['tools', 'wechat_read_messages'], False)]:
            stream = io.StringIO()
            with patch.object(agent, 'bridge', return_value=bridge), contextlib.redirect_stdout(stream):
                self.assertEqual(agent.main(argv), 0)
            found = json.loads(stream.getvalue())['data']['tools']
            self.assertEqual('inputSchema' in found[0], not compact)

    def test_mcp_handshake_supplies_usage_instructions_without_reading_chats(self):
        message = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}}
        stream = io.StringIO()
        with patch.object(agent, 'bridge', return_value=bridge), patch.object(bridge, 'call') as call, patch.object(sys, 'stdin', io.StringIO(json.dumps(message)+'\n')), contextlib.redirect_stdout(stream):
            self.assertEqual(agent.main(['mcp']), 0)
        result = json.loads(stream.getvalue())['result']
        self.assertIsInstance(result['instructions'], str)
        self.assertEqual(result['protocolVersion'], '2025-06-18')
        call.assert_not_called()


class LauncherTests(unittest.TestCase):
    def test_installed_launcher_handles_spaces_quotes_and_other_working_directory(self):
        with tempfile.TemporaryDirectory(prefix="wx space ' 中文 ") as directory:
            root = Path(directory)
            executable = root / 'wx'
            executable.write_text('#!/usr/bin/env python3\nimport sys,json\nprint(json.dumps(sys.argv[1:]))\n')
            executable.chmod(0o755)
            installed = root / 'bin/wx'
            with patch.object(agent, 'REPO', root):
                agent.install_command(installed)
            args = ['literal $(command)', '中文 🐱']
            result = subprocess.run([str(installed), *args], cwd='/', capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout), args)

    def test_unrelated_command_and_symlink_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / 'wx'
            destination.write_bytes(b'\xffexisting unrelated command')
            with self.assertRaises(agent.AgentError): agent.install_command(destination)
            self.assertEqual(destination.read_bytes(), b'\xffexisting unrelated command')
            destination.unlink();destination.symlink_to(root / 'missing')
            with self.assertRaises(agent.AgentError): agent.install_command(destination)
            self.assertTrue(destination.is_symlink())


class ScreenPolicyTests(unittest.TestCase):
    def test_single_display_needs_no_spaceo_or_accessibility_gate(self):
        self.assertFalse(sender.requires_second_screen({'screens': [{'id': 1}], 'mode': 'spaceo', 'accessibility': False}))
        self.assertTrue(sender.requires_second_screen({'screens': [{'id': 1}, {'id': 2}], 'mode': 'second_physical_screen', 'accessibility': True}))
        with self.assertRaises(sender.SenderError): sender.requires_second_screen({'screens': []})


if __name__ == '__main__': unittest.main()
