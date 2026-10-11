"""Explicitly acknowledged, bounded temporary-copy metadata trial.

No signing, original-process attach, expression evaluation, software breakpoint,
login automation, message body/key reads, or sending. Requires Apple LLDB Python.
The executable path is restored only after every temporary-copy process is gone.
"""
import argparse
import ctypes
import errno
import hashlib
import json
import os
import plistlib
import secrets
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

ORIGINAL = Path('/Applications/WeChat.app')
APP_NAME = 'WeChat-trial.app'
DISABLED_NAME = 'WeChat-trial.disabled'
MAIN_REL = Path('Contents/MacOS/WeChat')
MODULE_REL = Path('Contents/Resources/wechat.dylib')
SITES = {'post_start': 0x4279edc, 'callback_map': 0x427b228}
MAX_SECONDS = 120
MAX_SAMPLES = 10
SAFE_FAILURE_CODES = frozenset({
    'startup_module_not_ready', 'startup_continue_failed', 'unexpected_startup_stop',
    'startup_running_event_timeout', 'controlled_startup_pause_failed',
    'startup_pause_timeout', 'unexpected_startup_exception_or_signal',
    'module_uuid_mismatch', 'owned_launch_failed', 'local_target_required',
    'hardware_breakpoint_setup_failed', 'hardware_breakpoint_required',
    'exact_hardware_site_required', 'collector_refused_snapshot',
    'metadata_trial_interrupted', 'initial_continue_failed', 'continue_failed',
    'unexpected_exception_or_stop', 'unexpected_process_state',
    'process_event_queue_bound_exceeded',
})


class TrialError(RuntimeError):
    def __init__(self, code, stop_summary=None):
        super().__init__(code)
        self.stop_summary = stop_summary


def safe_stop_summary(process, lldb):
    """Only enum/signal metadata. Never read descriptions, frames or memory.

    Stop descriptions and exception data can contain addresses or private paths.
    In particular, exception stop data is deliberately never requested.
    """
    try:
        count = process.GetNumThreads()
        if type(count) is not int or not 0 <= count <= 4096:
            return {'available': False}
        reason_names = ('None', 'Invalid', 'Trace', 'Breakpoint', 'Watchpoint',
                        'Signal', 'Exception', 'Exec', 'PlanComplete', 'ThreadExiting',
                        'Instrumentation', 'ProcessorTrace', 'Fork', 'VFork', 'VForkDone')
        known = {getattr(lldb, 'eStopReason'+name): name for name in reason_names
                 if hasattr(lldb, 'eStopReason'+name)}
        threads = []
        for index in range(min(count, 32)):
            thread = process.GetThreadAtIndex(index)
            reason = thread.GetStopReason()
            item = {'index': index, 'reason': known.get(reason, 'Unknown')}
            if reason == getattr(lldb, 'eStopReasonSignal', object()):
                data_count = thread.GetStopReasonDataCount()
                item['signalDataCount'] = data_count if type(data_count) is int and 0 <= data_count <= 16 else None
                if data_count == 1:
                    signum = thread.GetStopReasonDataAtIndex(0)
                    if type(signum) is int and 1 <= signum <= 128:
                        item['signalNumber'] = signum
                        try:
                            item['signalName'] = signal.Signals(signum).name
                        except ValueError:
                            item['signalName'] = 'Unknown'
            threads.append(item)
        return {'available': True, 'threadCount': count, 'truncated': count > 32,
                'threads': threads}
    except Exception:
        return {'available': False}


def command(args):
    try:
        result = subprocess.run(args, capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        raise TrialError('metadata_command_failed')
    if result.returncode:
        raise TrialError('metadata_command_failed')
    return result


def owned(info, directory=False, private=False):
    if info.st_uid != os.geteuid() or (directory and not stat.S_ISDIR(info.st_mode)):
        raise TrialError('private_owner_required')
    if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
        raise TrialError('private_regular_file_required')
    if private and stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600):
        raise TrialError('private_mode_required')
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise TrialError('writable_by_others_refused')


def canonical_root(value):
    if any(c in str(value) for c in ('\r', '\n', '\0')):
        raise TrialError('control_characters_in_path_refused')
    root = Path(value)
    if not root.is_absolute() or '..' in root.parts or root != root.resolve(strict=True):
        raise TrialError('canonical_private_root_required')
    # Reject symlinks in every component, including a replaced ancestor.
    current = Path('/')
    for part in root.parts[1:]:
        current /= part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise TrialError('symlink_private_root_refused')
    owned(root.lstat(), directory=True, private=True)
    if root.parent.name != '.state':
        raise TrialError('reviewed_state_root_required')
    if root == Path('/') or root.is_relative_to('/Applications') or root.is_relative_to('/System'):
        raise TrialError('private_root_outside_original_required')
    return root


def read_private(path, maximum=1024*1024):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        owned(info, private=True)
        if info.st_size > maximum:
            raise TrialError('private_metadata_too_large')
        with os.fdopen(descriptor, 'rb') as stream:
            descriptor = None
            return stream.read(maximum+1)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def digest(path, temporary=True):
    info = path.lstat()
    if temporary:
        owned(info)
    elif (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
          or info.st_uid not in (0, os.geteuid()) or stat.S_IMODE(info.st_mode) & 0o022):
        raise TrialError('original_safe_file_required')
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def sip_enabled():
    value = command(['/usr/bin/csrutil', 'status']).stdout.lower()
    if b'status: enabled' not in value:
        raise TrialError('sip_enabled_required')


def signature(path):
    result = command(['/usr/bin/codesign', '-dvv', '--entitlements', ':-', str(path)])
    try:
        permissions = plistlib.loads(result.stdout) if result.stdout else {}
    except (ValueError, plistlib.InvalidFileException):
        raise TrialError('signature_entitlements_invalid')
    import re
    match = re.search(rb'\bflags=0x([0-9a-fA-F]+)', result.stderr)
    if not match:
        raise TrialError('signature_flags_missing')
    return permissions, bool(int(match.group(1), 16) & 0x10000)


def exact_processes_under(root):
    """Only executable metadata for our UID; never process argv/environment."""
    raw = command(['/bin/ps', '-axo', 'pid=,uid=']).stdout.decode('ascii', errors='strict')
    library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    library.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    library.proc_pidpath.restype = ctypes.c_int
    found = []
    for line in raw.splitlines():
        pid, uid = map(int, line.split())
        if uid != os.geteuid():
            continue
        buffer = ctypes.create_string_buffer(4096)
        ctypes.set_errno(0)
        length = library.proc_pidpath(pid, buffer, 4096)
        if length <= 0:
            path_error = ctypes.get_errno()
            if path_error == errno.ESRCH:
                continue
            if path_error == errno.ENOENT:
                # An updated/deleted browser executable can still be alive.
                # Use only kernel-reported text vnode paths, never argv as a
                # replacement identity. Any mapping in this root counts as
                # present, even if it is a library rather than the main file.
                mapped = mapped_text_paths(pid)
                if any(path == root or path.is_relative_to(root) for path in mapped):
                    found.append(pid)
                continue
            raise TrialError('process_inventory_unavailable')
        path = Path(os.fsdecode(buffer.value))
        if path == root or path.is_relative_to(root):
            found.append(pid)
    return found


def parse_text_inventory(raw, pid, executable_name):
    """Strict lsof -F0ftn parser; all text mappings must be accounted for."""
    if (not isinstance(executable_name, str) or not executable_name
            or any(c in executable_name for c in ('\r', '\n', '\0'))
            or len(raw) > 1024*1024):
        raise TrialError('process_inventory_unavailable')
    if not raw.endswith('\0\n'):
        raise TrialError('process_inventory_unavailable')
    # lsof uses NUL fields and a separate newline after each p/f set.
    # Never split a filename on newline to manufacture another record.
    lines = []
    for field in raw.split('\0'):
        if field == '\n':
            continue
        if field.startswith('\n'):
            field = field[1:]
        if not field or any(c in field for c in ('\r', '\n')):
            raise TrialError('process_inventory_unavailable')
        lines.append(field)
    if not lines or lines[0] != 'p'+str(pid):
        raise TrialError('process_inventory_unavailable')
    entries, current = [], None
    for line in lines[1:]:
        if line == 'ftxt':
            if current is not None:
                entries.append(current)
            current = {}
        elif line[:1] in ('t', 'n') and current is not None:
            tag = line[0]
            if tag in current:
                raise TrialError('process_inventory_unavailable')
            current[tag] = line[1:]
        else:
            raise TrialError('process_inventory_unavailable')
    if current is not None:
        entries.append(current)
    if not entries or len(entries) > 256:
        raise TrialError('process_inventory_unavailable')
    paths = []
    for entry in entries:
        value = entry.get('n', '')
        path = Path(value)
        if (set(entry) != {'t', 'n'} or entry['t'] != 'REG' or not path.is_absolute()
                or '..' in path.parts or any(c in value for c in ('\r', '\n', '\0'))
                or '(deleted)' in value or ' (error' in value
                or path != path.resolve(strict=False)):
            raise TrialError('process_inventory_unavailable')
        paths.append(path)
    # Kernel BSD name is used only to correlate the main basename, not a path.
    # Missing/ambiguous correlation keeps the inventory closed.
    if sum(path.name == Path(executable_name).name for path in paths) != 1:
        raise TrialError('process_inventory_unavailable')
    return paths


class ProcBsdInfo(ctypes.Structure):
    # Apple SDK sys/proc_info.h: PROC_PIDTBSDINFO, sizeof=136 on arm64.
    _fields_ = [(name, ctypes.c_uint32) for name in
        ('flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid',
         'rgid', 'svuid', 'svgid', 'reserved')] + [
        ('comm', ctypes.c_char*16), ('name', ctypes.c_char*32)] + [
        (name, ctypes.c_uint32) for name in
        ('nfiles', 'pgid', 'pjobc', 'tdev', 'tpgid')] + [
        ('nice', ctypes.c_int32), ('start_sec', ctypes.c_uint64),
        ('start_usec', ctypes.c_uint64)]


def process_inventory_identity(pid):
    library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                    ctypes.c_void_p, ctypes.c_int]
    library.proc_pidinfo.restype = ctypes.c_int
    info = ProcBsdInfo()
    if (ctypes.sizeof(info) != 136 or library.proc_pidinfo(pid, 3, 0,
            ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info)
            or info.pid != pid or info.uid != os.geteuid() or info.status == 5
            or info.start_sec == 0 or info.start_usec > 999999):
        raise TrialError('process_inventory_unavailable')
    name_bytes = info.name
    if not name_bytes or len(name_bytes) >= 31:
        raise TrialError('process_inventory_unavailable')
    name = name_bytes.decode('utf-8')
    if any(c in name for c in ('/', '\r', '\n', '\0')):
        raise TrialError('process_inventory_unavailable')
    return info.pid, info.uid, info.start_sec, info.start_usec, name


def mapped_text_paths(pid):
    try:
        before = process_inventory_identity(pid)
        result = command(['/usr/sbin/lsof', '-a', '-p', str(pid), '-d', 'txt', '-F0ftn'])
        if result.stderr or before != process_inventory_identity(pid):
            raise TrialError('process_inventory_unavailable')
        return parse_text_inventory(result.stdout.decode('utf-8'), pid, before[-1])
    except (TrialError, UnicodeError):
        raise TrialError('process_inventory_unavailable')


def signature_diagnostic_identity(raw):
    """Ignore only codesign's optional single-signature selection counters.

    Full executable hash and deep strict signature verification are separate
    required guards. All other diagnostic bytes must still compare exactly.
    Multi-signature, partial or duplicate counters are never normalized.
    """
    lines=raw.splitlines(keepends=True)
    counters={name:[] for name in (b'Chosen signature',b'Total signatures')}
    remaining=[]
    for line in lines:
        name=line.split(b'=',1)[0]
        if name in counters:
            counters[name].append(line.strip())
        else:
            remaining.append(line)
    if any(counters.values()) and any(counters[name]!=[name+b'=1'] for name in counters):
        raise TrialError('original_signature_changed')
    return b''.join(remaining)


def check_original(root, expected=None):
    sip_enabled()
    original_hash = digest(ORIGINAL/MAIN_REL, temporary=False)
    expected_hash = json.loads(read_private(root/'original-integrity.json'))['originalExecutableSha256']
    if original_hash != expected_hash or (expected and original_hash != expected):
        raise TrialError('original_integrity_changed')
    command(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(ORIGINAL)])
    original_signature = command(['/usr/bin/codesign', '-dvv', str(ORIGINAL)])
    recorded = read_private(root/'original-signature.txt')
    if signature_diagnostic_identity(original_signature.stdout or original_signature.stderr) != signature_diagnostic_identity(recorded):
        raise TrialError('original_signature_changed')
    return original_hash


def preflight(root, collector):
    if os.geteuid() == 0 or os.getuid() != os.geteuid():
        raise TrialError('ordinary_user_required')
    if exact_processes_under(ORIGINAL):
        raise TrialError('original_running_quit_manually_first')
    if (root/APP_NAME).exists() or not (root/DISABLED_NAME).is_dir():
        raise TrialError('disabled_copy_required')
    if exact_processes_under(root/DISABLED_NAME) or exact_processes_under(root/APP_NAME):
        raise TrialError('temporary_process_already_running')
    original_hash = check_original(root)
    plan = json.loads(read_private(root/'signing-plan.json'))
    if not isinstance(plan.get('signingOrder'), list):
        raise TrialError('signing_plan_invalid')
    main = [item for item in plan['signingOrder'] if item.get('kind') == 'main']
    if (len(main) != 1 or main[0].get('target') != '.' or main[0].get('binary') != str(MAIN_REL)
            or main[0].get('entitlements', {}).get('com.apple.security.get-task-allow') is not True):
        raise TrialError('previous_debug_profile_required')
    if collector.ALLOWED != SITES:
        raise TrialError('collector_observation_sites_changed')
    for path in (root/'abi-ticket.json', root/'abi-snapshots.jsonl'):
        if path.exists() or path.is_symlink():
            raise TrialError('fresh_observation_files_required')
    return original_hash, plan


def validate_enabled_copy(root, plan, collector):
    app = root/APP_NAME
    owned(app.lstat(), directory=True)
    info = plistlib.loads((app/'Contents/Info.plist').read_bytes())
    if (info.get('CFBundleIdentifier') != 'com.tencent.xinWeChat'
            or info.get('CFBundleExecutable') != 'WeChat'
            or info.get('CFBundleShortVersionString') != '4.1.13'
            or str(info.get('CFBundleVersion')) != '269602'):
        raise TrialError('exact_413_build_required')
    original_info = plistlib.loads((ORIGINAL/'Contents/Info.plist').read_bytes())
    if any(info.get(k) != original_info.get(k) for k in
           ('CFBundleIdentifier', 'CFBundleExecutable', 'CFBundleShortVersionString', 'CFBundleVersion')):
        raise TrialError('original_copy_version_mismatch')
    command(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)])
    for item in plan['signingOrder']:
        target = app/item['target']
        resolved = target.resolve(strict=True)
        # Framework Versions/Current symlinks are legitimate, provided the
        # resolved signing target stays entirely inside the exact owned copy.
        if not resolved.is_relative_to(app):
            raise TrialError('signing_target_not_exact_copy')
        permissions, runtime = signature(target)
        if permissions != (item['entitlements'] or {}) or runtime != item['preserveRuntime']:
            raise TrialError('previous_signature_profile_changed')
    from macho_code_identity import code_sha256
    if code_sha256(app/MODULE_REL) != collector.EXPECTED_CODE_SHA256:
        raise TrialError('module_code_identity_changed')
    return app/MAIN_REL, app/MODULE_REL


def write_ticket(root, process, module, collector):
    ticket = {'schema': 3, 'privateRoot': str(root), 'temporaryAppRoot': str(root/APP_NAME),
              'pid': process.GetProcessID(), 'processUniqueId': process.GetUniqueID(),
              'expiresAt': time.time()+180, 'consentScope': 'temporary-copy-task-metadata-only',
              'moduleUuid': collector.EXPECTED_UUID, 'moduleCodeSha256': collector.EXPECTED_CODE_SHA256,
              'codeSectionVerified': True, 'modulePath': str(module),
              'privateSaltHex': secrets.token_hex(32), 'outputPath': str(root/collector.OUTPUT_NAME),
              'observationSites': list(SITES)}
    collector._validate_ticket(ticket, root, time.time())
    fd = os.open(root/collector.TICKET_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                 os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(ticket, stream)
        stream.flush()
        os.fsync(stream.fileno())
    return ticket


def hardware_sites(debugger, target, module_path, lldb):
    if any(c in str(module_path) for c in ('\r', '\n', '\0')):
        raise TrialError('control_characters_in_path_refused')
    created = {}
    for event, offset in SITES.items():
        before = target.GetNumBreakpoints()
        # LLDB interpreter quoting only; path cannot alter the command.
        quoted = '"'+str(module_path).replace('\\', '\\\\').replace('"', '\\"')+'"'
        result = lldb.SBCommandReturnObject()
        debugger.GetCommandInterpreter().HandleCommand(
            'breakpoint set --hardware --shlib '+quoted+' --address '+hex(offset), result)
        if not result.Succeeded() or target.GetNumBreakpoints() != before+1:
            raise TrialError('hardware_breakpoint_setup_failed')
        breakpoint = target.GetBreakpointAtIndex(before)
        if not breakpoint.IsValid() or not breakpoint.IsHardware() or breakpoint.GetNumLocations() != 1:
            raise TrialError('hardware_breakpoint_required')
        location = breakpoint.GetLocationAtIndex(0)
        address = location.GetAddress()
        if (not location.IsValid() or not location.IsResolved() or address.GetFileAddress() != offset
                or Path(address.GetModule().GetFileSpec().fullpath) != module_path):
            raise TrialError('exact_hardware_site_required')
        created[event] = breakpoint
    return created


def process_event(listener, process, lldb, timeout=1):
    event = lldb.SBEvent()
    if not listener.WaitForEvent(timeout, event) or not lldb.SBProcess.EventIsProcessEvent(event):
        return None
    observed = lldb.SBProcess.GetProcessFromEvent(event)
    if (observed.GetProcessID(), observed.GetUniqueID()) != (process.GetProcessID(), process.GetUniqueID()):
        raise TrialError('foreign_process_event')
    state = lldb.SBProcess.GetStateFromEvent(event)
    # LLDB also broadcasts internal stops it has already resumed (e.g. dyld
    # notification breakpoints). They are not public stops to inspect/resume.
    if state == lldb.eStateStopped and lldb.SBProcess.GetRestartedFromEvent(event):
        return None
    return state


def drain_process_events(listener, process, lldb):
    event = lldb.SBEvent()
    for _ in range(128):
        if not listener.GetNextEvent(event):
            return
        if lldb.SBProcess.EventIsProcessEvent(event):
            observed = lldb.SBProcess.GetProcessFromEvent(event)
            if (observed.GetProcessID(), observed.GetUniqueID()) != (process.GetProcessID(), process.GetUniqueID()):
                raise TrialError('foreign_process_event')
            lldb.SBProcess.GetStateFromEvent(event)
    raise TrialError('process_event_queue_bound_exceeded')


def controlled_startup_stop(process, lldb):
    """Accept our pause, including one verified dyld notification step.

    A Stop() interrupt can expose LLDB's internal loader-breakpoint step. This
    is deliberately bound to the observed system dyld UUID and static PC;
    arbitrary Trace stops, user breakpoints, exceptions and SIGTRAP are refused.
    No application arguments or data memory are inspected.
    """
    traces = 0
    for index in range(process.GetNumThreads()):
        thread = process.GetThreadAtIndex(index)
        reason = thread.GetStopReason()
        if reason in (lldb.eStopReasonNone, lldb.eStopReasonInvalid):
            continue
        if (reason == lldb.eStopReasonSignal
                and thread.GetStopReasonDataCount() == 1
                and thread.GetStopReasonDataAtIndex(0) == signal.SIGSTOP):
            continue
        if reason != getattr(lldb, 'eStopReasonTrace', None):
            return False
        if thread.GetStopReasonDataCount() != 0:
            return False
        frame = thread.GetFrameAtIndex(0)
        if not frame.IsValid():
            return False
        address = frame.GetPCAddress()
        module = address.GetModule()
        if (not address.IsValid() or not module.IsValid()
                or module.GetFileSpec().GetFilename() != 'dyld'
                or module.GetUUIDString() != 'D341DDCB-7969-3A9C-8E03-0CF4751398A9'
                or address.GetFileAddress() != 0x1804d5688
                or frame.GetFunctionName() !=
                'dyld4::ExternallyViewableState::triggerNotifications(dyld_image_mode, unsigned int, dyld_image_info const*)'):
            return False
        traces += 1
        if traces > 1:
            return False
    return True


def startup_module(debugger, target, process, module_path, lldb, deadline, listener=None):
    """Bounded own-child startup pause, never a metadata collection stop."""
    listener = debugger.GetListener() if listener is None else listener
    until = min(deadline, time.monotonic()+5)
    for attempt in range(10):
        if time.monotonic() >= until:
            raise TrialError('startup_module_not_ready', safe_stop_summary(process, lldb))
        module = target.FindModule(lldb.SBFileSpec(str(module_path)))
        if (module.IsValid() and module.FindSection('__TEXT').IsValid()
                and module.FindSection('__TEXT').GetLoadAddress(target) != lldb.LLDB_INVALID_ADDRESS):
            return module
        if time.monotonic() >= until or process.GetState() != lldb.eStateStopped:
            raise TrialError('startup_module_not_ready', safe_stop_summary(process, lldb))
        drain_process_events(listener, process, lldb)
        if not process.Continue().Success():
            raise TrialError('startup_continue_failed')
        # Apple LLDB requires consuming process events; GetState polling alone
        # can remain on the stale entry stop and never populate loaded modules.
        while time.monotonic() < until:
            state = process_event(listener, process, lldb)
            if state == lldb.eStateRunning:
                break
            if state is not None:
                raise TrialError('unexpected_startup_stop', safe_stop_summary(process, lldb))
        else:
            raise TrialError('startup_running_event_timeout')
        # Spend the actual remaining budget across remaining pauses. A fixed
        # 0.1-second cadence exhausted all ten attempts around two seconds,
        # before a legitimate delayed module had loaded within five seconds.
        time.sleep(min(0.5, max(0, until-time.monotonic())/(10-attempt)))
        if not process.Stop().Success():
            # A natural exception can already have stopped the child while its
            # event is still queued. Acknowledge metadata only, never continue.
            drain_process_events(listener, process, lldb)
            raise TrialError('controlled_startup_pause_failed', safe_stop_summary(process, lldb))
        while time.monotonic() < until:
            state = process_event(listener, process, lldb)
            if state == lldb.eStateStopped:
                break
            if state not in (None, lldb.eStateRunning):
                raise TrialError('unexpected_startup_stop', safe_stop_summary(process, lldb))
        else:
            raise TrialError('startup_pause_timeout', safe_stop_summary(process, lldb))
        if not controlled_startup_stop(process, lldb):
            raise TrialError('unexpected_startup_exception_or_signal', safe_stop_summary(process, lldb))
    raise TrialError('startup_module_not_ready')


def selected_site(target, process, breakpoints, lldb):
    events_by_id = {bp.GetID(): name for name, bp in breakpoints.items()}
    selected = []
    for index in range(process.GetNumThreads()):
        thread = process.GetThreadAtIndex(index)
        reason = thread.GetStopReason()
        if reason in (lldb.eStopReasonNone, lldb.eStopReasonInvalid):
            continue
        if reason != lldb.eStopReasonBreakpoint:
            raise TrialError('unexpected_exception_or_stop')
        count = thread.GetStopReasonDataCount()
        if count != 2:
            raise TrialError('ambiguous_breakpoint_stop')
        bp = target.FindBreakpointByID(thread.GetStopReasonDataAtIndex(0))
        if not bp.IsValid() or not bp.IsHardware() or bp.GetID() not in events_by_id:
            raise TrialError('unexpected_breakpoint_stop')
        selected.append((thread, events_by_id[bp.GetID()]))
    if len(selected) != 1:
        raise TrialError('one_authorized_stopped_thread_required')
    process.SetSelectedThread(selected[0][0])
    return selected[0][1]


def observe(debugger, target, process, breakpoints, collector, lldb, deadline, clock=time.monotonic, counts=None, listener=None):
    counts = {name: 0 for name in SITES} if counts is None else counts
    last_report = clock()
    while clock() < deadline and any(value < MAX_SAMPLES for value in counts.values()):
        if clock()-last_report >= 15:
            print(json.dumps({'type': 'metadata_counts', 'counts': counts}), flush=True)
            last_report = clock()
        state = process_event(listener, process, lldb) if listener is not None else process.GetState()
        if state is None:
            continue
        if state == lldb.eStateStopped:
            event = selected_site(target, process, breakpoints, lldb)
            if counts[event] >= MAX_SAMPLES:
                raise TrialError('site_sample_bound_exceeded')
            result = lldb.SBCommandReturnObject()
            collector.snapshot(debugger, '', result, {})
            if result.GetError() or 'metadata snapshot saved' not in (result.GetOutput() or ''):
                raise TrialError('collector_refused_snapshot')
            counts[event] += 1
            if counts[event] == MAX_SAMPLES:
                breakpoints[event].SetEnabled(False)
                if breakpoints[event].IsEnabled():
                    raise TrialError('sample_bound_disable_failed')
            error = process.Continue()
            if not error.Success():
                raise TrialError('continue_failed')
        elif state in (lldb.eStateRunning, lldb.eStateStepping, lldb.eStateLaunching):
            time.sleep(0.1)
        elif state == lldb.eStateExited:
            return counts
        else:
            raise TrialError('unexpected_process_state')
    return counts


def unregister_copy(app):
    try:
        result = subprocess.run([
            '/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister',
            '-u', str(app)], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        raise TrialError('metadata_command_failed')
    if not result.returncode:
        return
    # The child was launched by exact executable, so LaunchServices may
    # never have registered this app. Apple SDK: kLSApplicationNotFoundErr.
    # Accept only the exact local diagnostic; every other failure blocks.
    absent = b'failed to scan '+os.fsencode(app)+b': -10814\n from spotlight'
    accepted = (absent, absent+b'\n')
    exact_absent = ((not result.stdout and result.stderr in accepted)
                    or (not result.stderr and result.stdout in accepted))
    if result.returncode != 1 or not exact_absent:
        raise TrialError('metadata_command_failed')


def cleanup(process, owner, root, original_hash, lldb, renamed, inventory=exact_processes_under,
            original_check=check_original, restore=subprocess.Popen, restore_original=True):
    if not renamed:
        return False
    if process is not None and process.IsValid() and owner is not None:
        if (process.GetProcessID(), process.GetUniqueID()) != owner:
            raise TrialError('manual_restore_required')
        if process.GetState() != lldb.eStateExited:
            error = process.Kill()  # SBProcess owner only, never killall/pgrep-selected PID.
            if not error.Success():
                raise TrialError('manual_restore_required')
            until = time.monotonic()+5
            while process.GetState() != lldb.eStateExited and time.monotonic() < until:
                time.sleep(0.1)
            if process.GetState() != lldb.eStateExited:
                raise TrialError('manual_restore_required')
    if inventory(root/APP_NAME) or inventory(root/DISABLED_NAME):
        raise TrialError('manual_restore_required')
    original_check(root, original_hash)
    enabled, disabled = root/APP_NAME, root/DISABLED_NAME
    if not enabled.is_dir() or disabled.exists():
        raise TrialError('manual_restore_required')
    unregister_copy(enabled)  # Never unregister /Applications/WeChat.app.
    enabled.rename(disabled)
    # Ticket no longer authorizes any process; retain private snapshots only.
    try:
        (root/'abi-ticket.json').unlink()
    except FileNotFoundError:
        pass
    if restore_original and not inventory(ORIGINAL):
        restore([str(ORIGINAL/MAIN_REL)], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                env={k: os.environ[k] for k in ('HOME', 'USER', 'LOGNAME', 'TMPDIR', 'PATH', 'LANG') if k in os.environ})
    return True


def execute(root, seconds=MAX_SECONDS, startup_gate=None, restore_original=True):
    import lldb
    import lldb_metadata_collector as collector
    root = canonical_root(root)
    original_hash, plan = preflight(root, collector)
    if not 1 <= seconds <= MAX_SECONDS:
        raise TrialError('bounded_seconds_required')
    debugger = process = owner = None
    renamed = False
    ticket_created = False
    result = {'type': 'metadata_trial_result', 'counts': {name: 0 for name in SITES},
              'originalRestored': False, 'originalLaunchRequested': restore_original,
              'sendingEnabled': False}
    old_ticket_env = os.environ.get('WX_ABI_PRIVATE_TICKET')
    saved_handlers = {}
    stage = 'enable_reviewed_copy'
    def interrupted(_signal, _frame):
        raise TrialError('metadata_trial_interrupted')
    try:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM):
            saved_handlers[signum] = signal.signal(signum, interrupted)
        (root/DISABLED_NAME).rename(root/APP_NAME)
        renamed = True
        stage = 'validate_reviewed_signature'
        main, module = validate_enabled_copy(root, plan, collector)
        # Collector uses fixed constants, guarded descriptors and selected live process.
        lldb.SBDebugger.Initialize()
        debugger = lldb.SBDebugger.Create()
        debugger.SkipLLDBInitFiles(True)
        if hasattr(debugger, 'SkipAppInitFiles'):
            debugger.SkipAppInitFiles(True)
        debugger.SetAsync(True)
        target = debugger.CreateTarget(str(main))
        if not target.IsValid() or target.GetPlatform().GetName() != 'host':
            raise TrialError('local_target_required')
        launch = lldb.SBLaunchInfo([])
        launch.SetLaunchFlags(lldb.eLaunchFlagStopAtEntry)
        # Explicit per-launch event ownership avoids relying on the debugger's
        # default listener and its asynchronous stop-state bookkeeping.
        listener = lldb.SBListener('wx-owned-metadata-events')
        launch.SetListener(listener)
        launch.SetWorkingDirectory(str(root))
        launch.SetEnvironmentEntries([k+'='+os.environ[k] for k in
            ('HOME', 'USER', 'LOGNAME', 'TMPDIR', 'PATH', 'LANG') if k in os.environ], False)
        for fd in (0, 1, 2):
            if not launch.AddOpenFileAction(fd, '/dev/null', fd == 0, fd != 0):
                raise TrialError('private_inferior_io_required')
        error = lldb.SBError()
        stage = 'launch_owned_child'
        started = time.monotonic()
        # The observer already reserves six seconds. The watchdog must expire
        # later, otherwise a normal event wait at the deadline is misreported
        # as an interrupted trial before cleanup can run.
        signal.alarm(seconds)
        process = target.Launch(launch, error)
        if process.IsValid() and process.GetProcessID() > 0:
            owner = (process.GetProcessID(), process.GetUniqueID())
        if not error.Success() or owner is None or process.GetState() != lldb.eStateStopped:
            raise TrialError('owned_launch_failed')
        ticket = write_ticket(root, process, module, collector)
        ticket_created = True
        os.environ['WX_ABI_PRIVATE_TICKET'] = str(root/collector.TICKET_NAME)
        stage = 'bind_process_identity'
        collector._bind_process(target, process, ticket, main, os.geteuid())
        if startup_gate is not None:
            stage = 'bind_isolated_display'
            startup_gate(process)
        stage = 'load_observation_module'
        module_object = startup_module(debugger, target, process, module, lldb,
                                      started+max(1, seconds-6), listener=listener)
        if not module_object.IsValid() or module_object.GetUUIDString().upper() != collector.EXPECTED_UUID:
            raise TrialError('module_uuid_mismatch')
        collector._bind_process(target, process, ticket, main, os.geteuid())
        stage = 'install_exact_hardware_sites'
        sites = hardware_sites(debugger, target, module, lldb)
        drain_process_events(listener, process, lldb)
        if not process.Continue().Success():
            raise TrialError('initial_continue_failed')
        stage = 'observe_task_metadata'
        print(json.dumps({'type': 'metadata_trial_started', 'sendingEnabled': False}), flush=True)
        # Reserve cleanup time inside the nominal 120-second observation budget.
        result['counts'] = observe(debugger, target, process, sites, collector, lldb,
                                   started+max(1, seconds-6), counts=result['counts'], listener=listener)
        result['observationSeconds'] = round(time.monotonic()-started, 3)
        result['processExited'] = process.GetState() == lldb.eStateExited
        if result['processExited']:
            # Exit code only: descriptions, stdout and stderr can carry data.
            result['processExitStatus'] = process.GetExitStatus()
    except Exception as error:
        result['error'] = 'metadata_trial_stopped_safely'
        result['failureStage'] = stage
        result['failureType'] = type(error).__name__
        if isinstance(error, TrialError) and str(error) in SAFE_FAILURE_CODES:
            result['failureCode'] = str(error)
            if error.stop_summary is not None:
                result['startupStop'] = error.stop_summary
    finally:
        signal.alarm(0)
        if ticket_created:
            # Revoke observation even when a surviving child requires manual restoration.
            try:
                (root/collector.TICKET_NAME).unlink()
            except FileNotFoundError:
                pass
            except Exception:
                result['error'] = 'manual_restore_required'
        try:
            result['originalRestored'] = cleanup(process, owner, root, original_hash, lldb, renamed,
                                                 restore_original=restore_original)
        except Exception:
            result['error'] = 'manual_restore_required'
        if debugger is not None:
            lldb.SBDebugger.Destroy(debugger)
        if old_ticket_env is None:
            os.environ.pop('WX_ABI_PRIVATE_TICKET', None)
        else:
            os.environ['WX_ABI_PRIVATE_TICKET'] = old_ticket_env
        for signum, handler in saved_handlers.items():
            signal.signal(signum, handler)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--private-root', type=Path, required=True)
    parser.add_argument('--acknowledge-metadata-observation', action='store_true')
    parser.add_argument('--seconds', type=int, default=MAX_SECONDS)
    args = parser.parse_args()
    if not args.acknowledge_metadata_observation:
        parser.error('Explicit authorization for the two-site metadata observation is required.')
    try:
        result = execute(args.private_root, args.seconds)
    except Exception:
        result = {'type': 'metadata_trial_result', 'error': 'preflight_refused_no_process_launched',
                  'originalRestored': False, 'sendingEnabled': False}
    print(json.dumps(result), flush=True)
    return 0 if 'error' not in result and result['originalRestored'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
