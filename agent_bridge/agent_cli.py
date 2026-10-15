#!/usr/bin/env python3
"""Portable agent entry point: JSON discovery, guided setup and MCP stdio."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
SETTINGS = ROOT / '.state/agent-settings.json'


class AgentError(Exception):
    def __init__(self, code, message, next_actions=None):
        super().__init__(message)
        self.code, self.next_actions = code, next_actions or []


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise AgentError('invalid_command', message, ['./wx --help'])


def output(data):
    print(json.dumps({'schemaVersion': 1, 'ok': True, 'data': data}, ensure_ascii=False))


def recovery(code):
    return {
        'cli_missing': ['./wx setup'],
        'account_selection_required': ['./wx accounts', './wx tools wechat_read_messages'],
        'unknown_account': ['./wx accounts'],
        'database_key_missing': ['./wx status', './wx setup --help'],
        'wechat_data_permission': ['Enable WeChat container access for the calling Terminal/app in macOS Files and Folders.', './wx status'],
        'filesystem_permission': ['Enable WeChat container access for the calling Terminal/app in macOS Files and Folders.', './wx status'],
        'invalid_argument': ['./wx tools'],
        'merged_parent_selector_required': ['./wx tools wechat_list_merged_attachments'],
        'merged_attachment_selector_required': ['./wx tools wechat_list_merged_attachments', './wx tools wechat_read_merged_attachment'],
        'unknown_tool': ['./wx tools'],
        'native_sender_unconfigured': ['./wx status', 'Read experimental/native_send/README.md for native sender prerequisites.'],
        'native_sender_not_ready_check_status': ['./wx call wechat_native_sender_status'],
        'native_sender_only_verified_for_filehelper': ['Only filehelper text is currently verified.'],
    }.get(code, ['./wx status'])


def error_output(error):
    code = getattr(error, 'code', 'internal_error')
    message = str(error) if isinstance(error, AgentError) else 'Agent entry point failed; private details withheld.'
    print(json.dumps({'schemaVersion': 1, 'ok': False, 'error': {
        'code': code, 'message': message, 'nextActions': getattr(error, 'next_actions', None) or recovery(code),
        'automaticRetry': False}}, ensure_ascii=False))


def bridge():
    settings = {}
    if SETTINGS.is_file():
        try:
            settings = json.loads(SETTINGS.read_text())
        except (OSError, ValueError):
            raise AgentError('invalid_local_settings', 'Local agent settings are invalid.') from None
    script = Path(os.environ.get('WX_BRIDGE_SCRIPT') or settings.get('bridgeScript') or ROOT / 'wechat_bridge.py').expanduser().resolve()
    if not script.is_file():
        raise AgentError('bridge_missing', 'The configured bridge script does not exist.')
    sys.path.insert(0, str(script.parent))
    spec = importlib.util.spec_from_file_location('wx_agent_bridge', script)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    # A native-only bridge can expose its configured sender without GUI tools.
    native = (hasattr(value, 'NATIVE_SEND_MODE') and (script.parent / '.state/native-agent-sender.json').is_file()
              and os.environ.get('WX_BRIDGE_READ_ONLY') != '1')
    value.READ_ONLY_MODE = not native
    if hasattr(value, 'NATIVE_SEND_MODE'):
        value.NATIVE_SEND_MODE = native
    return value


def tools(value):
    return value.available_tools() if hasattr(value, 'available_tools') else [t for t in value.TOOLS if t['annotations']['readOnlyHint']]


def validate(arguments, schema, path='arguments'):
    if 'oneOf' in schema:
        accepted = 0
        for option in schema['oneOf']:
            try:
                validate(arguments, option, path)
                accepted += 1
            except AgentError:
                pass
        if accepted != 1:
            raise AgentError('invalid_argument', path + ' does not match its declared schema.')
    expected = schema.get('type')
    types = {'object': dict, 'array': list, 'string': str, 'integer': int, 'boolean': bool, 'number': (int, float)}
    if expected in types and (not isinstance(arguments, types[expected]) or expected in ('integer', 'number') and isinstance(arguments, bool)):
        raise AgentError('invalid_argument', path + ' must be ' + expected + '.')
    if 'enum' in schema and arguments not in schema['enum']:
        raise AgentError('invalid_argument', path + ' has an unsupported value.')
    if 'const' in schema and arguments != schema['const']:
        raise AgentError('invalid_argument', path + ' requires the declared value.')
    if isinstance(arguments, dict):
        missing = set(schema.get('required', [])) - set(arguments)
        if missing:
            raise AgentError('invalid_argument', 'Missing fields: ' + ', '.join(sorted(missing)))
        props = schema.get('properties', {})
        if schema.get('additionalProperties') is False and set(arguments) - set(props):
            raise AgentError('invalid_argument', 'Unknown fields: ' + ', '.join(sorted(set(arguments) - set(props))))
        for name, item in arguments.items():
            if name in props:
                validate(item, props[name], path + '.' + name)
    if isinstance(arguments, str):
        if len(arguments) < schema.get('minLength', 0) or len(arguments) > schema.get('maxLength', float('inf')):
            raise AgentError('invalid_argument', path + ' has an invalid length.')
        if schema.get('pattern') and not re.search(schema['pattern'], arguments):
            raise AgentError('invalid_argument', path + ' does not match the required format.')
    if isinstance(arguments, (int, float)) and not isinstance(arguments, bool):
        if arguments < schema.get('minimum', -float('inf')) or arguments > schema.get('maximum', float('inf')):
            raise AgentError('invalid_argument', path + ' is outside the supported range.')


def invoke(value, name, arguments):
    tool = next((t for t in tools(value) if t['name'] == name), None)
    if tool is None:
        raise AgentError('unknown_tool', 'Tool is unavailable in the current mode: ' + name, ['./wx tools'])
    validate(arguments, tool['inputSchema'])
    try:
        return value.call(name, arguments)
    except value.BridgeError as error:
        raise AgentError(error.code, str(error), recovery(error.code)) from None


def unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError('duplicate field')
        result[name] = value
    return result


def reject_constant(value):
    raise ValueError('Non-finite JSON number')


def json_input(argument=None, input_file=None):
    try:
        if input_file:
            with Path(input_file).open() as stream:
                raw = stream.read(262145)
        elif argument == '-':
            raw = sys.stdin.read(262145)
        else:
            raw = argument or '{}'
        if len(raw.encode('utf8')) > 262144:
            raise ValueError('bounded input required')
        return json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (OSError, ValueError, UnicodeError):
        raise AgentError('invalid_json', 'Provide one JSON object, an input file, or - for stdin.', ['./wx tools']) from None


def status(value, verify=False):
    from backends.receive.preflight import inspect
    info = inspect()
    installed = Path(value.WX).is_file()
    accessible, account_list = True, []
    try:
        account_list = invoke(value, 'wechat_accounts', {})['accounts']
    except AgentError:
        accessible = False
    key_present = False
    if installed:
        try:
            key_present = Path(value.run_wx(['paths', '--json'])['keys_file']).is_file()
        except Exception:
            pass
    checks = {'platformSupported': platform.system() == 'Darwin', 'architecture': platform.machine(),
              'cliInstalled': installed, 'wechatInstalled': info['wechatVersion'] is not None,
              'accountAccess': accessible, 'accountCount': len(account_list), 'databaseKeyFileExists': key_present,
              'historyReadVerified': False}
    actions = []
    if not checks['platformSupported']:
        actions.append('Run on macOS with local WeChat data; tool discovery/config generation work on other platforms.')
    if not installed:
        actions.append('./wx setup')
    if not accessible:
        actions.extend(recovery('wechat_data_permission'))
    if not account_list:
        actions.append('Log in to WeChat on this Mac and save the required data locally.')
    elif len(account_list) > 1:
        actions.append('./wx accounts; select an account alias in JSON arguments')
    if not key_present:
        actions.append('Import your authorized database keys with ./wx setup --capture-file FILE --db-root DIR; see ./wx setup --help.')
    if verify and installed and accessible and len(account_list) == 1 and key_present:
        try:
            invoke(value, 'wechat_sessions', {'limit': 1})
            checks['historyReadVerified'] = True
        except AgentError as error:
            checks['verificationError'] = error.code
            actions.extend(error.next_actions)
    ready = all(checks[k] for k in ('platformSupported', 'cliInstalled', 'wechatInstalled', 'accountAccess', 'databaseKeyFileExists')) and len(account_list) > 0
    native = any(t['name'] == 'wechat_submit_native_text' for t in tools(value))
    native_state = invoke(value, 'wechat_native_sender_status', {}) if native else {'configured': False}
    return {'readyForReadAttempt': ready, 'checks': checks, 'accounts': account_list,
            'wechat': {'version': info['wechatVersion'], 'build': info['wechatBuild']},
            'interfaces': {'jsonCli': True, 'mcpStdio': True, 'toolSchemaDiscovery': True, 'nativeTextSenderConfigured': native},
            'nativeTextSender': native_state, 'nextActions': list(dict.fromkeys(actions)), 'automaticKeyExtraction': False,
            'note': 'Key existence is not successful decryption. Use status --verify for a bounded read check.'}


def install_runtime(build=False):
    if platform.system() != 'Darwin':
        raise AgentError('unsupported_platform', 'Runtime setup requires macOS.')
    binary = ROOT / 'bin/wx-cli'
    if binary.is_file() and not build:
        return 'already_installed'
    if build:
        if not (REPO / 'Cargo.toml').is_file():
            raise AgentError('source_checkout_required', 'Rust builds require a full source checkout; the release already includes a runtime.', ['./wx setup'])
        if not shutil.which('cargo'):
            raise AgentError('rust_missing', 'Source builds require Rust stable in PATH.', ['./wx setup'])
        result = subprocess.run(['sh', str(ROOT / 'build-agent.sh')], cwd=REPO, stdout=sys.stderr, stderr=sys.stderr)
        if result.returncode:
            raise AgentError('build_failed', 'Rust build failed; see stderr.')
        return 'built_from_source'
    arch = {'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine(), platform.machine())
    manifest = json.loads((ROOT / 'runtime-release.json').read_text())
    release = manifest.get('darwin-' + arch)
    if not release:
        raise AgentError('binary_unavailable', 'No pinned runtime exists for this architecture.', ['./wx setup --build'])
    with tempfile.TemporaryDirectory(prefix='wx-agent-setup-') as directory:
        archive = Path(directory) / 'runtime.tar.gz'
        try:
            with urllib.request.urlopen(release['url'], timeout=30) as incoming, archive.open('wb') as stream:
                total = 0
                while True:
                    block = incoming.read(1024 * 1024)
                    if not block:
                        break
                    total += len(block)
                    if total > 64 * 1024 * 1024:
                        raise AgentError('download_too_large', 'Runtime archive exceeded its size limit.')
                    stream.write(block)
        except OSError:
            raise AgentError('download_failed', 'Pinned runtime download failed.', ['./wx setup --build']) from None
        if hashlib.sha256(archive.read_bytes()).hexdigest() != release['sha256']:
            raise AgentError('checksum_mismatch', 'Runtime checksum did not match; nothing was installed.')
        with tarfile.open(archive) as package:
            member = package.getmember(release['binaryMember'])
            if not member.isfile() or member.size > 64 * 1024 * 1024:
                raise AgentError('invalid_runtime_archive', 'Runtime archive has no valid regular binary.')
            binary.parent.mkdir(parents=True, exist_ok=True)
            descriptor, filename = tempfile.mkstemp(prefix='.wx-cli-', dir=binary.parent)
            temporary = Path(filename)
            try:
                with package.extractfile(member) as source, os.fdopen(descriptor, 'wb') as target:
                    shutil.copyfileobj(source, target)
                temporary.chmod(0o755)
                os.replace(temporary, binary)
            finally:
                temporary.unlink(missing_ok=True)
    return 'downloaded_verified_runtime'


def config(format_name='json', name='wx-cli-agent'):
    command = str(REPO / 'wx')
    if format_name == 'toml':
        return '[mcp_servers.' + json.dumps(name) + ']\ncommand = ' + json.dumps(command, ensure_ascii=False) + '\nargs = ["mcp"]\n'
    return {'mcpServers': {name: {'command': command, 'args': ['mcp']}}}


def configure_native(trial_root, library=None, window_command=None):
    experiment = REPO / 'experimental/native_send'
    sys.path.insert(0, str(experiment))
    import metadata_trial as trial
    try:
        root = trial.canonical_root(Path(trial_root).expanduser().absolute())
        for name in ('original-integrity.json', 'signing-plan.json', 'original-signature.txt'):
            trial.read_private(root / name)
        if not (root / trial.DISABLED_NAME).is_dir():
            raise AgentError('native_trial_unprepared', 'An already prepared, disabled private trial copy is required.')
    except trial.TrialError:
        raise AgentError('native_trial_unprepared', 'Private trial records are missing or invalid.', ['Read experimental/native_send/README.md']) from None
    if library:
        native_library = Path(library).expanduser().resolve(strict=True)
    else:
        directory = ROOT / '.state/native-runtime'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        native_library = directory / 'libwxnative413.dylib'
        source = experiment / 'native_413'
        build = subprocess.run(['xcrun', 'clang++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-dynamiclib',
                                str(source / 'core.cpp'), str(source / 'rpc.cpp'), '-o', str(native_library)],
                               stdout=sys.stderr, stderr=sys.stderr)
        if build.returncode:
            raise AgentError('native_build_failed', 'Native adapter build failed; see stderr.')
        signed = subprocess.run(['codesign', '--force', '--sign', '-', str(native_library)], stdout=sys.stderr, stderr=sys.stderr)
        if signed.returncode:
            raise AgentError('native_library_sign_failed', 'Native adapter signing failed; WeChat itself was not changed.')
    if native_library.is_symlink() or native_library.stat().st_uid != os.geteuid() or native_library.stat().st_mode & 0o022:
        raise AgentError('native_library_invalid', 'Use an owned, non-writable-by-others native library.')
    sys.path.insert(0, str(ROOT))
    from backends.send.native_413 import agent_sender as sender
    value = {'schema': 1, 'trialRoot': str(root), 'library': str(native_library),
             'librarySha256': hashlib.sha256(native_library.read_bytes()).hexdigest()}
    if window_command:
        command = Path(window_command).expanduser().resolve(strict=True)
        if not command.is_file() or not os.access(command, os.X_OK):
            raise AgentError('invalid_window_command', 'Window-placement command must be an executable file.')
        value['windowCommand'] = str(command)
    sender.CONFIG.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    sender.save(sender.CONFIG, value)


def write_config(path, format_name='json', name='wx-cli-agent'):
    path = Path(path).expanduser()
    generated = config(format_name, name)
    if path.exists():
        if format_name != 'json':
            raise AgentError('config_exists', 'Existing TOML files are preserved; generate TOML for a manual merge.')
        try:
            existing = json.loads(path.read_text())
            servers = existing.setdefault('mcpServers', {})
            if name in servers and servers[name] != generated['mcpServers'][name]:
                raise AgentError('mcp_name_conflict', 'Another server uses this name. Choose --name or a separate output file.')
            servers.update(generated['mcpServers'])
            generated = existing
        except (OSError, ValueError, AttributeError, TypeError):
            raise AgentError('invalid_mcp_config', 'Existing configuration is invalid; it was not changed.') from None
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix='.wx-config-', dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(generated if format_name == 'toml' else json.dumps(generated, ensure_ascii=False, indent=2) + '\n')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return generated


def parser():
    result = Parser(prog='./wx', description='WeChat agent entry point. JSON stdout; MCP uses JSON-RPC. Start with ./wx status or ./wx tools.')
    commands = result.add_subparsers(dest='command')
    p = commands.add_parser('status', help='Check readiness without reading message bodies')
    p.add_argument('--verify', action='store_true', help='Attempt one bounded read and report only success/failure')
    p = commands.add_parser('tools', help='Discover available tools and exact JSON input schemas')
    p.add_argument('name', nargs='?')
    p = commands.add_parser('call', help='Call any discovered tool using JSON, stdin (-), or an input file')
    p.add_argument('name')
    p.add_argument('arguments', nargs='?')
    p.add_argument('--input-file')
    commands.add_parser('mcp', help='Run portable MCP stdio; no client-specific dependency')
    p = commands.add_parser('config', help='Generate MCP configuration with this installation path')
    p.add_argument('--format', choices=['json', 'toml'], default='json')
    p.add_argument('--output', help='Write configuration; JSON merges unrelated servers safely')
    p.add_argument('--name', default='wx-cli-agent')
    p = commands.add_parser('setup', help='Install pinned runtime; never extracts keys, restarts WeChat, or changes SIP')
    p.add_argument('--build', action='store_true', help='Build Rust locally instead of downloading the pinned runtime')
    p.add_argument('--capture-file', help='Import an already authorized key capture using HMAC validation')
    p.add_argument('--db-root', help='Exact db_storage directory for the authorized capture')
    p.add_argument('--bridge-script', help='Use another trusted local bridge, including a configured native sender')
    p.add_argument('--native-root', help='Register an already authorized private native trial directory; no WeChat launch')
    p.add_argument('--native-library', help='Reuse a compatible owned native library instead of building one')
    p.add_argument('--window-command', help='Optional agent-screen executable to enforce second-screen placement')
    commands.add_parser('accounts', help='List anonymous local account aliases')
    p = commands.add_parser('send', help='Send exact text through a configured native bridge')
    p.add_argument('recipient', help='Currently verified: filehelper')
    p.add_argument('message', nargs='?', help='Exact text, or - to read text from stdin')
    p.add_argument('--message-file')
    p.add_argument('--authorized', action='store_true', help='Attest the human explicitly requested this exact recipient and text')
    p = commands.add_parser('job', help='Read a native send job without replaying it')
    p.add_argument('id')
    for name in ('sessions', 'contacts', 'messages', 'search'):
        p = commands.add_parser(name, help='Convenient JSON ' + name + ' query')
        if name in ('contacts', 'messages', 'search'):
            p.add_argument('target', nargs='?' if name == 'contacts' else None)
        p.add_argument('--account')
        p.add_argument('--limit', type=int, default=20)
        p.add_argument('--offset', type=int, default=0)
        if name == 'search':
            p.add_argument('--mode', choices=['native', 'scan'], default='scan')
    p = commands.add_parser('raw', help='Pass through to the underlying Rust CLI')
    p.add_argument('arguments', nargs=argparse.REMAINDER)
    return result


def main(argv=None):
    try:
        args = parser().parse_args(argv)
        if args.command == 'config':
            generated = write_config(args.output, args.format, args.name) if args.output else config(args.format, args.name)
            if args.format == 'toml':
                print(generated, end='')
            else:
                print(json.dumps(generated, ensure_ascii=False, indent=2))
            return 0
        if args.command == 'setup':
            if bool(args.capture_file) != bool(args.db_root):
                raise AgentError('invalid_argument', '--capture-file and --db-root must be supplied together.')
            if (args.native_library or args.window_command) and not args.native_root:
                raise AgentError('invalid_argument', '--native-library/--window-command require --native-root.')
            if args.native_root and args.bridge_script:
                raise AgentError('invalid_argument', 'Choose the bundled native adapter or an external trusted bridge.')
            method = install_runtime(args.build)
            if args.native_root:
                configure_native(args.native_root, args.native_library, args.window_command)
            if args.bridge_script:
                script = Path(args.bridge_script).expanduser().resolve()
                if not script.is_file():
                    raise AgentError('bridge_missing', 'The trusted local bridge script does not exist.')
                SETTINGS.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(SETTINGS, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'w') as stream:
                    json.dump({'bridgeScript': str(script)}, stream)
            value = bridge()
            write_config(REPO / '.mcp.json')
            imported = False
            if args.capture_file:
                try:
                    value.run_wx_process(['key', 'import-capture', '--capture-file', str(Path(args.capture_file).resolve()), '--db-root', str(Path(args.db_root).resolve())])
                    imported = True
                except value.BridgeError as error:
                    raise AgentError(error.code, str(error), recovery(error.code)) from None
            output({'runtime': method, 'keysImported': imported, 'mcpConfig': str(REPO / '.mcp.json'), 'status': status(value),
                    'nextActions': ['./wx config', './wx tools', './wx status --verify']})
            return 0
        value = bridge()
        if args.command is None or args.command == 'status':
            output(status(value, getattr(args, 'verify', False)))
        elif args.command == 'tools':
            available = tools(value)
            if args.name:
                available = [t for t in available if t['name'] == args.name]
                if not available:
                    raise AgentError('unknown_tool', 'No available tool has that name.', ['./wx tools'])
            output({'tools': available, 'callSyntax': './wx call TOOL JSON', 'stdinSyntax': './wx call TOOL -'})
        elif args.command == 'mcp':
            for line in sys.stdin:
                try:
                    request = json.loads(line, object_pairs_hook=unique_object, parse_constant=reject_constant)
                    if request.get('id') is not None and request.get('method') == 'tools/call':
                        params = request.get('params', {})
                        response = {'jsonrpc': '2.0', 'id': request.get('id'), 'result': {'content': [
                            {'type': 'text', 'text': json.dumps(invoke(value, params.get('name', ''), params.get('arguments', {})), ensure_ascii=False)}], 'isError': False}}
                    else:
                        response = value.handle_rpc(request)
                except AgentError as error:
                    response = {'jsonrpc': '2.0', 'id': request.get('id'), 'result': {'content': [{'type': 'text', 'text': json.dumps({
                        'error': error.code, 'message': str(error), 'nextActions': error.next_actions or recovery(error.code)}, ensure_ascii=False)}], 'isError': True}}
                except Exception:
                    response = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Invalid JSON-RPC request'}}
                if response is not None:
                    print(json.dumps(response, ensure_ascii=False), flush=True)
        elif args.command == 'call':
            if args.arguments is not None and args.input_file:
                raise AgentError('invalid_argument', 'Choose inline JSON, stdin, or an input file.')
            output(invoke(value, args.name, json_input(args.arguments, args.input_file)))
        elif args.command == 'accounts':
            output(invoke(value, 'wechat_accounts', {}))
        elif args.command == 'send':
            if not any(t['name'] == 'wechat_submit_native_text' for t in tools(value)):
                raise AgentError('native_sender_unconfigured', 'No configured native text bridge is available.',
                                 ['./wx setup --native-root PRIVATE_TRIAL_DIR', 'Read experimental/native_send/README.md; native setup is build-specific.'])
            if bool(args.message is not None) == bool(args.message_file):
                raise AgentError('invalid_argument', 'Provide exact text, stdin (-), or --message-file.')
            if args.message_file:
                with Path(args.message_file).open() as stream:
                    message = stream.read(16385)
            elif args.message == '-':
                message = sys.stdin.read(16385)
            else:
                message = args.message
            if not message or len(message.encode('utf8')) > 16384:
                raise AgentError('invalid_argument', 'Text must be 1 to 16384 UTF-8 bytes.')
            draft = invoke(value, 'wechat_prepare_native_text', {'recipientId': args.recipient, 'message': message})
            if not args.authorized:
                output(draft)
            else:
                output(invoke(value, 'wechat_submit_native_text', {'draftId': draft['draftId'], 'userAuthorized': True}))
        elif args.command == 'job':
            output(invoke(value, 'wechat_native_text_job', {'jobId': args.id}))
        elif args.command in ('sessions', 'contacts', 'messages', 'search'):
            arguments = {'limit': args.limit, 'offset': args.offset}
            if args.account:
                arguments['account'] = args.account
            names = {'sessions': 'wechat_sessions', 'contacts': 'wechat_contacts', 'messages': 'wechat_read_messages', 'search': 'wechat_search'}
            if args.command == 'contacts' and args.target:
                arguments['search'] = args.target
            if args.command == 'messages':
                arguments['contact'] = args.target
            if args.command == 'search':
                arguments.update(keyword=args.target, mode=args.mode)
            output(invoke(value, names[args.command], arguments))
        elif args.command == 'raw':
            if not Path(value.WX).is_file():
                raise AgentError('cli_missing', 'Runtime is not installed.', ['./wx setup'])
            return subprocess.call([value.WX] + args.arguments, env=value.wx_environment())
        return 0
    except AgentError as error:
        error_output(error)
        return 1
    except Exception as error:
        error_output(error)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
