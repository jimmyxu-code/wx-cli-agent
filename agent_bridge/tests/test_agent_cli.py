import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import agent_cli as agent
import wechat_bridge as bridge


class CliTests(unittest.TestCase):
    def run_main(self, arguments, value=bridge, stdin=''):
        output = io.StringIO()
        with patch.object(agent, 'bridge', return_value=value), patch.object(sys, 'stdin', io.StringIO(stdin)), contextlib.redirect_stdout(output):
            code = agent.main(arguments)
        return code, output.getvalue()

    def test_offline_tool_discovery_and_generated_config(self):
        result = subprocess.run([str(ROOT.parent / 'wx'), 'tools', 'wechat_read_messages'], cwd='/', text=True, capture_output=True, check=True)
        data = json.loads(result.stdout)
        self.assertTrue(data['ok'])
        self.assertIn('contact', data['data']['tools'][0]['inputSchema']['required'])
        config = agent.config()
        self.assertEqual(config['mcpServers']['wx-cli-agent']['args'], ['mcp'])
        self.assertEqual(Path(config['mcpServers']['wx-cli-agent']['command']), ROOT.parent / 'wx')

    def test_explicit_read_only_environment_disables_native_write_tools(self):
        with patch.dict(agent.os.environ, {'WX_BRIDGE_READ_ONLY': '1'}):
            value = agent.bridge()
            self.assertTrue(value.READ_ONLY_MODE)
            self.assertNotIn('wechat_submit_native_text', {t['name'] for t in agent.tools(value)})

    def test_json_errors_reject_unknown_and_missing_fields_before_database_call(self):
        with patch.object(bridge, 'call') as call:
            for arguments in ({'contcat': 'oops'}, {}, {'contact': 'filehelper', 'limit': True}, {'contact': 'filehelper', 'limit': 101}):
                code, output = self.run_main(['call', 'wechat_read_messages', json.dumps(arguments)])
                data = json.loads(output)
                self.assertEqual(code, 1)
                self.assertEqual(data['error']['code'], 'invalid_argument')
                self.assertTrue(data['error']['nextActions'])
            call.assert_not_called()

    def test_stdin_and_file_preserve_literal_text(self):
        arguments = {'keyword': '中文\n🐱 `literal` $(literal)', 'mode': 'scan'}
        with patch.object(bridge, 'call', return_value={'items': []}) as call:
            code, output = self.run_main(['call', 'wechat_search', '-'], stdin=json.dumps(arguments))
            self.assertEqual(code, 0)
            self.assertEqual(call.call_args.args[1], arguments)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'request 中文.json'
                path.write_text(json.dumps(arguments))
                code, output = self.run_main(['call', 'wechat_search', '--input-file', str(path)])
                self.assertEqual(code, 0)
                self.assertEqual(call.call_args.args[1], arguments)

    def test_duplicate_and_nonfinite_json_rejected(self):
        for raw in ('{"keyword":"first","keyword":"second"}', '{"limit":NaN}'):
            with self.assertRaises(agent.AgentError):
                agent.json_input(raw)

    def test_short_search_defaults_to_scan_and_multiaccount_is_preserved(self):
        with patch.object(bridge, 'call', return_value={'items': []}) as call:
            code, output = self.run_main(['search', '2026-10-13', '--account', 'account_fixture', '--offset', '100'])
            self.assertEqual(code, 0)
            self.assertEqual(call.call_args.args[1], {'keyword': '2026-10-13', 'mode': 'scan', 'account': 'account_fixture', 'limit': 20, 'offset': 100})

    def test_mcp_notifications_do_not_call_tools_and_errors_include_recovery(self):
        requests = [{'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                    {'jsonrpc': '2.0', 'method': 'tools/call', 'params': {'name': 'wechat_contacts'}},
                    {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'},
                    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'wechat_read_messages', 'arguments': {}}}]
        with patch.object(bridge, 'call') as call:
            code, output = self.run_main(['mcp'], stdin='\n'.join(map(json.dumps, requests)) + '\n')
            data = [json.loads(line) for line in output.splitlines()]
            self.assertEqual([r['id'] for r in data], [1, 2])
            self.assertTrue(data[1]['result']['isError'])
            self.assertIn('nextActions', json.loads(data[1]['result']['content'][0]['text']))
            call.assert_not_called()

    def test_config_merge_preserves_other_servers_and_refuses_conflict(self):
        with tempfile.TemporaryDirectory(prefix='wx agent 中文 ') as directory:
            path = Path(directory) / 'mcp.json'
            path.write_text(json.dumps({'preferences': {'unchanged': True}, 'mcpServers': {'other': {'command': 'other'}}}))
            data = agent.write_config(path)
            self.assertEqual(data['mcpServers']['other'], {'command': 'other'})
            self.assertTrue(data['preferences']['unchanged'])
            data['mcpServers']['wx-cli-agent'] = {'command': 'existing'}
            path.write_text(json.dumps(data))
            original = path.read_bytes()
            with self.assertRaises(agent.AgentError):
                agent.write_config(path)
            self.assertEqual(path.read_bytes(), original)

    def test_native_send_prepares_by_default_and_authorizes_exact_draft_only_when_requested(self):
        value = SimpleNamespace(TOOLS=[
            bridge.tool('wechat_prepare_native_text', '', {'recipientId': {'type': 'string'}, 'message': {'type': 'string'}}, ['recipientId', 'message'], False),
            bridge.tool('wechat_submit_native_text', '', {'draftId': {'type': 'string'}, 'userAuthorized': {'type': 'boolean'}}, ['draftId', 'userAuthorized'], False)],
            available_tools=lambda: value.TOOLS, BridgeError=bridge.BridgeError)
        calls = []
        def call(name, arguments):
            calls.append((name, arguments))
            return {'draftId': 'a' * 32} if name == 'wechat_prepare_native_text' else {'jobId': 'a' * 32}
        value.call = call
        text = '中文\nemoji 🐱 $(literal)'
        code, output = self.run_main(['send', 'filehelper', '-'], value, stdin=text)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [('wechat_prepare_native_text', {'recipientId': 'filehelper', 'message': text})])
        calls.clear()
        code, output = self.run_main(['send', 'filehelper', '-', '--authorized'], value, stdin=text)
        self.assertEqual(code, 0)
        self.assertEqual(calls[1], ('wechat_submit_native_text', {'draftId': 'a' * 32, 'userAuthorized': True}))


class RuntimeInstallTests(unittest.TestCase):
    def archive(self, symbolic=False):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w:gz') as archive:
            item = tarfile.TarInfo('package/bin/wx-cli')
            if symbolic:
                item.type, item.linkname = tarfile.SYMTYPE, '/outside'
                archive.addfile(item)
            else:
                body = b'fixture runtime, not executable code'
                item.size = len(body)
                archive.addfile(item, io.BytesIO(body))
            other = tarfile.TarInfo('../../outside')
            other.size = 1
            archive.addfile(other, io.BytesIO(b'x'))
        return output.getvalue()

    def install(self, data, digest=None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        manifest = {'darwin-arm64': {'url': 'https://example.invalid/fixture', 'sha256': digest or hashlib.sha256(data).hexdigest(), 'binaryMember': 'package/bin/wx-cli'}}
        (root / 'runtime-release.json').write_text(json.dumps(manifest))
        with patch.object(agent, 'ROOT', root), patch.object(agent.platform, 'system', return_value='Darwin'), patch.object(agent.platform, 'machine', return_value='arm64'), patch.object(agent.urllib.request, 'urlopen', return_value=io.BytesIO(data)):
            result = agent.install_runtime()
        return root, result

    def test_verified_archive_extracts_only_expected_binary(self):
        root, result = self.install(self.archive())
        self.assertEqual(result, 'downloaded_verified_runtime')
        self.assertEqual((root / 'bin/wx-cli').read_bytes(), b'fixture runtime, not executable code')
        self.assertFalse((root.parent / 'outside').exists())

    def test_checksum_mismatch_and_symlink_binary_are_rejected(self):
        for data, digest in ((self.archive(), '0' * 64), (self.archive(symbolic=True), None)):
            with self.assertRaises(agent.AgentError):
                self.install(data, digest)


class PackageTests(unittest.TestCase):
    def test_private_state_and_local_config_are_not_packaged(self):
        spec = importlib.util.spec_from_file_location('package_agent_test', ROOT.parent / 'scripts/package-agent.py')
        packager = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(packager)
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / 'repo'
            repo.mkdir()
            for name in ('wx', 'README.md', 'SKILL.md', 'AGENTS.md', 'LICENSE', 'docs/UPSTREAM_README.md', 'scripts/package-agent.py',
                         'agent_bridge/agent_cli.py', 'agent_bridge/.state/secret.py', 'agent_bridge/.state/keys.json',
                         'agent_bridge/.state/private.log', '.mcp.json', 'agent_bridge/bin/wx-cli'):
                path = repo / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('fixture')
            output = Path(directory) / 'release.tar.gz'
            result = packager.package(repo, repo / 'agent_bridge/bin/wx-cli', output, 'v-fixture')
            with tarfile.open(output) as archive:
                names = [m.name for m in archive.getmembers()]
            self.assertIn('wx-cli-agent/wx', names)
            self.assertIn('wx-cli-agent/agent_bridge/bin/wx-cli', names)
            self.assertFalse(any('.state' in n or '.mcp.json' in n or 'keys.json' in n for n in names))
            self.assertEqual(result['sha256'], hashlib.sha256(output.read_bytes()).hexdigest())
            self.assertTrue(output.with_name(output.name + '.sha256').is_file())


if __name__ == '__main__':
    unittest.main()
