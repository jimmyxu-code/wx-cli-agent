"""Codex adapter for the locally verified, single File Helper native sender.

Read-only preflight never starts WeChat. Submission is asynchronous and requires
the agent to attest an explicit human instruction for the exact prepared text.
The existing native controller retains build, login and one-shot guards.
"""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import stat
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
STATE = ROOT / '.state'
CONFIG = STATE / 'native-agent-sender.json'
JOBS = STATE / 'native-send-jobs'
EXPERIMENT = ROOT.parent / 'experimental/native_send'


class SenderError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def save(path, data):
    temporary = path.with_name(path.name + '.' + secrets.token_hex(8))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if info.st_uid != os.geteuid() or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise SenderError('private_sender_file_required')
        return json.load(stream)


def config():
    try:
        value = load(CONFIG)
        if value.get('schema') != 1:
            raise SenderError('native_agent_config_invalid')
        root, library = Path(value['trialRoot']), Path(value['library'])
        if not root.is_absolute() or not library.is_absolute() or root != root.resolve() or library != library.resolve():
            raise SenderError('canonical_native_paths_required')
        if library.stat().st_uid != os.geteuid() or library.stat().st_mode & 0o022:
            raise SenderError('owned_native_library_required')
        if hashlib.sha256(library.read_bytes()).hexdigest() != value['librarySha256']:
            raise SenderError('verified_native_library_changed')
        return value
    except (OSError, ValueError, KeyError):
        raise SenderError('native_agent_sender_unconfigured') from None


def digest(proposal):
    return hashlib.sha256(json.dumps(proposal, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def screen_call(command, *args):
    try:
        result = subprocess.run([command, *map(str, args)], capture_output=True, text=True, timeout=15, check=True)
        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        raise SenderError('agent_screen_unavailable') from None


def status():
    try:
        value = config()
        original = Path('/Applications/WeChat.app')
        info = plistlib.loads((original / 'Contents/Info.plist').read_bytes())
        if info.get('CFBundleShortVersionString') != '4.1.13' or str(info.get('CFBundleVersion')) != '269602':
            raise SenderError('verified_wechat_build_changed')
        sys.path.insert(0, str(EXPERIMENT))
        import metadata_trial as trial
        root = trial.canonical_root(Path(value['trialRoot']))
        original_running = bool(trial.exact_processes_under(original))
        disabled = (root / trial.DISABLED_NAME).is_dir()
        return {'backend': 'wechat_native_413_unix_socket', 'configured': True,
                'runtimePrerequisitesChecked': True, 'localDeliveryTestPerformed': False,
                'supportedRecipientIds': ['filehelper'], 'supportedMessageTypes': ['text'],
                'build': 269602, 'originalWeChatRunning': original_running, 'copyDisabled': disabled,
                'readyToStart': disabled and not original_running,
                'nextAction': 'quit_original_wechat_manually' if original_running else 'submit_authorized_text_then_login_temporary_client',
                'requiresTemporaryClientLogin': True, 'deliveryVerifiedForNewMessages': False,
                'guiMessageInputUsed': False, 'automaticRetry': False}
    except Exception as error:
        return {'backend': 'wechat_native_413_unix_socket', 'configured': False, 'readyToStart': False,
                'reason': getattr(error, 'code', 'native_sender_prerequisites_unavailable'), 'automaticRetry': False}


def job_path(identifier):
    if not isinstance(identifier, str) or not re.fullmatch('[0-9a-f]{32}', identifier):
        raise SenderError('invalid_native_draft_id')
    path = JOBS / identifier
    if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.geteuid() or stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise SenderError('native_draft_not_found')
    return path


def prepare(recipient, message):
    config()
    if recipient != 'filehelper':
        raise SenderError('native_sender_only_verified_for_filehelper')
    if not isinstance(message, str) or not message or '\0' in message or len(message.encode('utf8')) > 16384:
        raise SenderError('bounded_native_text_required')
    JOBS.mkdir(mode=0o700, exist_ok=True)
    if JOBS.is_symlink() or stat.S_IMODE(JOBS.stat().st_mode) != 0o700:
        raise SenderError('private_native_jobs_directory_required')
    identifier = secrets.token_hex(16)
    path = JOBS / identifier
    path.mkdir(mode=0o700)
    proposal = {'schema': 1, 'kind': 'send_text', 'recipient': recipient, 'text': message,
                'timestamp': int(time.time()), 'client_message_id': secrets.randbelow(2**31 - 1) + 1,
                'maximum_sends': 1, 'gui_input': False}
    save(path / 'proposal.json', proposal)
    save(path / 'draft.json', {'expires': time.time() + 600, 'proposalSha256': digest(proposal),
                              'configSha256': hashlib.sha256(CONFIG.read_bytes()).hexdigest()})
    return {'draftId': identifier, 'recipientId': recipient, 'message': message,
            'proposalSha256': digest(proposal), 'submitted': False, 'guiMessageInputUsed': False,
            'authorization': 'An explicit human request for this exact recipient and text is required to submit.'}


def submit(identifier, user_authorized):
    if user_authorized is not True:
        raise SenderError('explicit_human_send_instruction_required')
    path = job_path(identifier)
    draft, proposal = load(path / 'draft.json'), load(path / 'proposal.json')
    if draft['expires'] < time.time() or digest(proposal) != draft['proposalSha256']:
        raise SenderError('native_draft_expired_or_changed')
    config()
    if hashlib.sha256(CONFIG.read_bytes()).hexdigest() != draft['configSha256']:
        raise SenderError('native_sender_configuration_changed')
    if not status()['readyToStart']:
        raise SenderError('native_sender_not_ready_check_status')
    value = config()
    if value.get('windowCommand'):
        screen = screen_call(value['windowCommand'], 'prepare')
        if screen.get('mode') != 'second_physical_screen' or screen.get('accessibility') is not True:
            raise SenderError('native_launch_requires_spaceo_or_second_screen')
    try:
        fd = os.open(path / 'submitted.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise SenderError('native_draft_already_submitted_no_retry') from None
    with os.fdopen(fd, 'w') as stream:
        json.dump({'proposalSha256': digest(proposal), 'automaticRetry': False}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    save(path / 'approval.json', {'schema': 1, 'human_approved': True, 'proposal_sha256': digest(proposal),
                                 'expires_at': min(draft['expires'], time.time() + 600)})
    save(path / 'state.json', {'phase': 'starting', 'submitted': False, 'automaticRetry': False})
    try:
        child = subprocess.Popen(['/usr/bin/python3', str(Path(__file__).resolve()), '--worker', identifier],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True, close_fds=True)
        save(path / 'worker.json', {'pid': child.pid})
    except OSError:
        save(path / 'state.json', {'phase': 'failed_before_native_launch', 'submitted': False, 'automaticRetry': False})
        raise SenderError('native_worker_start_failed_draft_consumed') from None
    return {'jobId': identifier, 'phase': 'starting', 'nativeEnqueueConfirmed': False,
            'automaticRetry': False, 'nextTool': 'wechat_native_text_job'}


def job_status(identifier):
    path = job_path(identifier)
    if not (path / 'submitted.json').exists():
        return {'jobId': identifier, 'phase': 'prepared', 'submitted': False}
    state = load(path / 'state.json')
    if state.get('phase') in ('starting', 'native_process_started', 'native_login_needed', 'native_message_queued') and (path / 'worker.json').exists():
        try:
            os.kill(load(path / 'worker.json')['pid'], 0)
        except ProcessLookupError:
            state = {'phase': 'worker_exited_outcome_unknown', 'uncertain': True, 'automaticRetry': False}
    return {'jobId': identifier, **state}


def worker(identifier):
    path = job_path(identifier)
    fd = os.open(STATE / 'native-agent-sender.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        save(path / 'state.json', {'phase': 'native_sender_busy', 'submitted': False, 'automaticRetry': False})
        return
    try:
        value = config()
        draft = load(path / 'draft.json')
        if hashlib.sha256(CONFIG.read_bytes()).hexdigest() != draft['configSha256']:
            raise SenderError('native_sender_configuration_changed')
        python_path = subprocess.run(['xcrun', 'lldb', '-P'], capture_output=True, text=True, check=True, timeout=10).stdout.strip()
        sys.path.insert(0, python_path)
        sys.path.insert(0, str(EXPERIMENT / 'native_413'))
        import runtime_sender as sender

        def place_window(pid):
            if not value.get('windowCommand'):
                progress = load(path / 'state.json')
                if progress.get('phase') == 'starting':progress['phase'] = 'native_process_started'
                progress['nativePid'] = pid
                save(path / 'state.json', progress)
                return
            current = screen_call(value['windowCommand'], 'status', pid)
            if current.get('mode') != 'second_physical_screen' or current.get('accessibility') is not True:
                raise sender.Guard('second_screen_placement_required')
            target = next(s['bounds'] for s in current['screens'] if s['id'] == current['target_display_id'])
            for window in current.get('windows', []):
                bounds = window['bounds']
                if bounds['x'] >= target['x'] and bounds['y'] >= target['y'] and bounds['x'] + bounds['width'] <= target['x'] + target['width'] and bounds['y'] + bounds['height'] <= target['y'] + target['height']:
                    continue
                screen_call(value['windowCommand'], 'move', pid, window['index'])
                checked = screen_call(value['windowCommand'], 'status', pid)
                placed = next(w['bounds'] for w in checked['windows'] if w['index'] == window['index'])
                if not (placed['x'] >= target['x'] and placed['y'] >= target['y'] and placed['x'] + placed['width'] <= target['x'] + target['width'] and placed['y'] + placed['height'] <= target['y'] + target['height']):
                    raise sender.Guard('work_window_placement_unconfirmed')

        class Progress:
            buffer = ''
            def write(self, text):
                self.buffer += text
                while '\n' in self.buffer:
                    line, self.buffer = self.buffer.split('\n', 1)
                    try:
                        event = json.loads(line)
                        if event.get('type') in ('native_login_needed', 'native_message_queued'):
                            progress = load(path / 'state.json')
                            progress.update(phase=event['type'], submitted=event['type'] == 'native_message_queued', automaticRetry=False)
                            save(path / 'state.json', progress)
                    except ValueError:
                        pass
            def flush(self):
                pass

        with contextlib.redirect_stdout(Progress()):
            result = sender.execute(Path(value['trialRoot']), Path(value['library']), path / 'proposal.json',
                                    path / 'approval.json', path / 'claim.json',
                                    report_name='native-agent-send-result-' + identifier + '.json', place_window=place_window)
        save(path / 'state.json', {'phase': 'finished', 'result': result, 'automaticRetry': False})
    except Exception as error:
        # No private error strings, proposal text, paths or debugger data in tool errors.
        code = getattr(error, 'code', None)
        if type(error).__name__ in ('Guard', 'TrialError'):
            code = str(error)
        save(path / 'state.json', {'phase': 'stopped', 'reason': code or 'native_worker_failed',
                                  'uncertain': (path / 'claim.json').exists(), 'automaticRetry': False})
    finally:
        os.close(fd)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--worker':
        worker(sys.argv[2])
    else:
        raise SystemExit('This adapter is called through wechat_bridge.py.')
