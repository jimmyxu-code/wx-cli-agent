import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backends.send.native_413 import agent_sender as sender

spec = importlib.util.spec_from_file_location('native_bridge_test', ROOT / 'wechat_bridge.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class NativeRoutingTests(unittest.TestCase):
    def test_native_mode_exposes_structured_sender_without_gui_write_tools(self):
        messages = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'},
                    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
                    {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                     'params': {'name': 'wechat_prepare_message', 'arguments': {}}}]
        result = subprocess.run(['/usr/bin/python3', str(ROOT / 'wechat_bridge.py'), 'mcp', '--native-send'],
                                input='\n'.join(map(json.dumps, messages)) + '\n', text=True,
                                capture_output=True, check=True)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        tools = {t['name']: t for t in responses[1]['result']['tools']}
        self.assertIn('wechat_contacts', tools)
        self.assertIn('wechat_prepare_native_text', tools)
        self.assertIn('wechat_submit_native_text', tools)
        self.assertIn('wechat_native_text_job', tools)
        self.assertNotIn('wechat_send_message', tools)
        self.assertNotIn('wechat_open_chat', tools)
        self.assertFalse(tools['wechat_submit_native_text']['annotations']['readOnlyHint'])
        self.assertTrue(tools['wechat_submit_native_text']['annotations']['openWorldHint'])
        self.assertTrue(responses[2]['result']['isError'])

    def test_read_only_mode_still_refuses_native_submission(self):
        with patch.object(bridge, 'READ_ONLY_MODE', True):
            names = {t['name'] for t in bridge.available_tools()}
            self.assertNotIn('wechat_prepare_native_text', names)
            self.assertNotIn('wechat_submit_native_text', names)
            with self.assertRaises(bridge.BridgeError) as error:
                bridge.call('wechat_submit_native_text', {'draftId': 'a' * 32, 'userAuthorized': True})
            self.assertEqual(error.exception.code, 'read_only_mode')


class NativeSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / 'config.json'
        self.config.write_text('{}')
        self.config.chmod(0o600)
        self.patches = [patch.object(sender, 'CONFIG', self.config),
                        patch.object(sender, 'JOBS', self.root / 'jobs'),
                        patch.object(sender, 'config', return_value={'schema': 1, 'windowCommand': '/fixture/agent-screen'}),
                        patch.object(sender, 'status', return_value={'readyToStart': True}),
                        patch.object(sender, 'screen_call', return_value={'mode': 'second_physical_screen', 'accessibility': True}),
                        patch.object(sender.subprocess, 'Popen')]
        for p in self.patches:
            p.start()
        sender.subprocess.Popen.return_value.pid = 123456789

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def draft(self):
        return sender.prepare('filehelper', '中文\nemoji 🐱\n`literal` $(literal)')

    def test_prepare_preserves_exact_text_and_never_starts_process(self):
        draft = self.draft()
        saved = sender.load(sender.job_path(draft['draftId']) / 'proposal.json')
        self.assertEqual(saved['text'], draft['message'])
        self.assertFalse(draft['submitted'])
        sender.subprocess.Popen.assert_not_called()

    def test_unverified_recipient_refused(self):
        with self.assertRaises(sender.SenderError):
            sender.prepare('some_contact', 'text')
        sender.subprocess.Popen.assert_not_called()

    def test_missing_authorization_does_not_issue_approval_or_spawn(self):
        draft = self.draft()
        with self.assertRaises(sender.SenderError):
            sender.submit(draft['draftId'], False)
        self.assertFalse((sender.job_path(draft['draftId']) / 'approval.json').exists())
        sender.subprocess.Popen.assert_not_called()

    def test_changed_text_and_configuration_refused(self):
        for kind in ('text', 'config'):
            with self.subTest(kind=kind):
                draft = self.draft()
                path = sender.job_path(draft['draftId']) / 'proposal.json'
                if kind == 'text':
                    data = sender.load(path)
                    data['text'] = 'changed'
                    sender.save(path, data)
                else:
                    self.config.write_text('{"changed":true}')
                with self.assertRaises(sender.SenderError):
                    sender.submit(draft['draftId'], True)
        sender.subprocess.Popen.assert_not_called()

    def test_unknown_or_failed_launch_cannot_replay_submission(self):
        draft = self.draft()
        sender.submit(draft['draftId'], True)
        with self.assertRaises(sender.SenderError) as error:
            sender.submit(draft['draftId'], True)
        self.assertEqual(error.exception.code, 'native_draft_already_submitted_no_retry')
        self.assertEqual(sender.subprocess.Popen.call_count, 1)
        command = sender.subprocess.Popen.call_args.args[0]
        self.assertNotIn(draft['message'], command)
        result = sender.job_status(draft['draftId'])
        self.assertEqual(result['phase'], 'worker_exited_outcome_unknown')
        self.assertFalse(result['automaticRetry'])

    def test_original_running_and_single_screen_do_not_launch(self):
        draft = self.draft()
        with patch.object(sender, 'status', return_value={'readyToStart': False}), self.assertRaises(sender.SenderError):
            sender.submit(draft['draftId'], True)
        with patch.object(sender, 'screen_call', return_value={'mode': 'spaceo', 'accessibility': True}), self.assertRaises(sender.SenderError):
            sender.submit(draft['draftId'], True)
        sender.subprocess.Popen.assert_not_called()

    def test_worker_connects_exact_job_to_native_controller_and_retains_receipt_boundary(self):
        draft = self.draft()
        sender.submit(draft['draftId'], True)
        calls = []
        def execute(*args, **kwargs):
            calls.append((args, kwargs))
            kwargs['place_window'](12345)
            print(json.dumps({'type': 'native_login_needed'}))
            kwargs['place_window'](12345)
            progress = sender.job_status(draft['draftId'])
            self.assertEqual(progress['phase'], 'native_login_needed')
            self.assertEqual(progress['nativePid'], 12345)
            return {'nativeAcknowledgementSucceeded': True, 'recipientDeliveryVerified': False}
        native = SimpleNamespace(execute=execute)
        with patch.object(sender, 'STATE', self.root), patch.object(sender, 'config', return_value={
                'trialRoot': str(self.root / 'trial'), 'library': str(self.root / 'library')}), patch.dict(sys.modules, {'runtime_sender': native}), patch.object(sender.os, 'kill'), patch.object(sender.subprocess, 'run', return_value=SimpleNamespace(stdout='/fixture/lldb-python')):
            sender.worker(draft['draftId'])
        args, kwargs = calls[0]
        self.assertEqual(args[2], sender.job_path(draft['draftId']) / 'proposal.json')
        self.assertEqual(args[3], sender.job_path(draft['draftId']) / 'approval.json')
        self.assertTrue(callable(kwargs['place_window']))
        result = sender.job_status(draft['draftId'])
        self.assertEqual(result['phase'], 'finished')
        self.assertTrue(result['result']['nativeAcknowledgementSucceeded'])
        self.assertFalse(result['result']['recipientDeliveryVerified'])

    def test_worker_refuses_configuration_changed_after_submission(self):
        draft = self.draft()
        sender.submit(draft['draftId'], True)
        self.config.write_text('{"changedAfterSubmit":true}')
        with patch.object(sender, 'STATE', self.root):
            sender.worker(draft['draftId'])
        result = sender.job_status(draft['draftId'])
        self.assertEqual(result['phase'], 'stopped')
        self.assertEqual(result['reason'], 'native_sender_configuration_changed')
        self.assertFalse(result['uncertain'])


if __name__ == '__main__':
    unittest.main()
