"""PREPARED ONLY: metadata from an authorized stopped temporary-copy process.
Never attaches, launches, sets breakpoints, evaluates expressions or sends.
Ticket schema 3; old tickets fail closed. All LLDB errors are kept private.
"""
import fcntl
import json
import math
import os
import stat
import time
from pathlib import Path

import lldb
from abi_snapshot_protocol import (packet, ALLOWED, OBSERVATION_SITES,
                                   REGISTER_NAMES, OBJECT_REGISTER, validate_marker)
from macho_code_identity import code_sha256

EXPECTED_UUID = 'CDB81058-0FAC-3518-95F5-C0CC7860F9B5'
EXPECTED_CODE_SHA256 = '5ccbdcba186265115fd52f2ef6f5dc39cca731b7f71f81601bff5e1272612df2'
TICKET_NAME = 'abi-ticket.json'
OUTPUT_NAME = 'abi-snapshots.jsonl'
TEMP_APP_NAME = 'WeChat-trial.app'
MAIN_RELATIVE = ('Contents', 'MacOS', 'WeChat')
MODULE_RELATIVE = ('Contents', 'Resources', 'wechat.dylib')
MAX_TICKET_BYTES = 16 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_TICKET_LIFETIME = 600
NOFOLLOW = os.O_NOFOLLOW
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _path(value):
    if (not isinstance(value, str) or not value or len(value.encode('utf-8')) > 4096
            or '\x00' in value or not value.startswith('/') or '..' in Path(value).parts):
        raise ValueError('absolute_canonical_path_required')
    return Path(value)


def _owned(info, uid, *, directory=False, mode=None, maximum=None):
    if info.st_uid != uid:
        raise ValueError('wrong_owner')
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError('not_directory')
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('not_private_regular_file')
    if mode is not None and stat.S_IMODE(info.st_mode) != mode:
        raise ValueError('wrong_private_mode')
    if maximum is not None and info.st_size > maximum:
        raise ValueError('private_file_too_large')


def _open_root(path, uid):
    # Follow no symbolic links, including intermediate components. System
    # ancestors may be 0755/1777; the final private root must be owned and 0700.
    descriptor = os.open('/', DIR_FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, DIR_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        _owned(os.fstat(descriptor), uid, directory=True, mode=0o700)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_subdirectory(root_fd, parts, uid):
    descriptor = os.dup(root_fd)
    try:
        for part in parts:
            child = os.open(part, DIR_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            info = os.fstat(descriptor)
            _owned(info, uid, directory=True)
            if stat.S_IMODE(info.st_mode) & 0o022:
                raise ValueError('temporary_directory_writable_by_others')
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_temporary_file(root_fd, relative, uid):
    parent = _open_subdirectory(root_fd, relative[:-1], uid)
    try:
        descriptor = os.open(relative[-1], FILE_FLAGS, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        info = os.fstat(descriptor)
        _owned(info, uid)
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise ValueError('temporary_file_writable_by_others')
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _unique_object(pairs):
    value = {}
    for name, item in pairs:
        if name in value:
            raise ValueError('duplicate_ticket_field')
        value[name] = item
    return value


def _read_ticket(root_fd, uid):
    descriptor = os.open(TICKET_NAME, FILE_FLAGS, dir_fd=root_fd)
    try:
        _owned(os.fstat(descriptor), uid, mode=0o600, maximum=MAX_TICKET_BYTES)
        with os.fdopen(descriptor, 'rb') as stream:
            descriptor = None
            raw = stream.read(MAX_TICKET_BYTES + 1)
        if len(raw) > MAX_TICKET_BYTES:
            raise ValueError('ticket_too_large')
        return json.loads(raw, object_pairs_hook=_unique_object)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validate_ticket(ticket, private_root, now):
    required = {'schema', 'privateRoot', 'temporaryAppRoot', 'pid', 'processUniqueId',
                'expiresAt', 'consentScope', 'moduleUuid', 'codeSectionVerified',
                'modulePath', 'moduleCodeSha256', 'privateSaltHex', 'outputPath', 'observationSites'}
    if (not isinstance(ticket, dict) or not required.issubset(ticket)
            or set(ticket) - required - {'externalMarker'}
            or type(ticket['schema']) is not int or ticket['schema'] != 3):
        raise ValueError('ticket_schema')
    if ticket['observationSites'] != OBSERVATION_SITES:
        raise ValueError('observation_scope')
    expires = ticket['expiresAt']
    if (type(expires) not in (int, float) or not math.isfinite(expires)
            or expires <= now or expires > now + MAX_TICKET_LIFETIME):
        raise ValueError('ticket_expired_or_unbounded')
    for field in ('pid', 'processUniqueId'):
        if type(ticket[field]) is not int or not 0 < ticket[field] < 2**32:
            raise ValueError('process_binding_required')
    if ticket['consentScope'] != 'temporary-copy-task-metadata-only':
        raise ValueError('scope')
    if (ticket['codeSectionVerified'] is not True or ticket['moduleUuid'] != EXPECTED_UUID
            or ticket['moduleCodeSha256'] != EXPECTED_CODE_SHA256):
        raise ValueError('code_profile')
    if _path(ticket['privateRoot']) != private_root:
        raise ValueError('private_root_binding')
    temporary_root = private_root / TEMP_APP_NAME
    if _path(ticket['temporaryAppRoot']) != temporary_root:
        raise ValueError('temporary_root_binding')
    expected_main = temporary_root.joinpath(*MAIN_RELATIVE)
    expected_module = temporary_root.joinpath(*MODULE_RELATIVE)
    if _path(ticket['modulePath']) != expected_module:
        raise ValueError('module_path_binding')
    if _path(ticket['outputPath']) != private_root / OUTPUT_NAME:
        raise ValueError('output_path_binding')
    salt_hex = ticket['privateSaltHex']
    if not isinstance(salt_hex, str) or len(salt_hex) != 64:
        raise ValueError('private_salt_required')
    salt = bytes.fromhex(salt_hex)
    if len(salt) != 32:
        raise ValueError('private_salt_required')
    marker = validate_marker(ticket.get('externalMarker', ''))
    return expected_main, expected_module, salt, marker


def _file_spec_path(spec):
    if not spec.IsValid():
        raise ValueError('executable_metadata_unavailable')
    directory, name = spec.GetDirectory(), spec.GetFilename()
    if not isinstance(name, str) or not name or '/' in name or name in ('.', '..'):
        raise ValueError('executable_metadata_unavailable')
    return _path(directory) / name


def _bind_process(target, process, ticket, expected_main, uid):
    # Installed Apple LLDB exposes GetCoreFile, but not the newer upstream
    # IsLiveDebugSession. Require a non-core session and valid live process info.
    if not target.IsValid() or not process.IsValid() or process.GetCoreFile().IsValid():
        raise ValueError('live_local_target_required')
    if target.GetPlatform().GetName() != 'host':
        raise ValueError('local_platform_required')
    if process.GetProcessID() != ticket['pid'] or process.GetUniqueID() != ticket['processUniqueId']:
        raise ValueError('process_binding')
    if process.GetState() != lldb.eStateStopped:
        raise ValueError('not_stopped')
    if _file_spec_path(target.GetExecutable()) != expected_main:
        raise ValueError('target_not_temporary_main')
    # This is the process's executable, not merely LLDB's chosen symbol target.
    info = process.GetProcessInfo()
    if (not info.IsValid() or info.GetProcessID() != ticket['pid']
            or not info.EffectiveUserIDIsValid() or info.GetEffectiveUserID() != uid
            or not info.UserIDIsValid() or info.GetUserID() != uid):
        raise ValueError('process_identity_unavailable')
    if _file_spec_path(info.GetExecutableFile()) != expected_main:
        raise ValueError('process_not_temporary_main')


def _append(root_fd, uid, data):
    raw = (json.dumps(data, separators=(',', ':')) + '\n').encode('utf-8')
    if len(raw) > 4096:
        raise ValueError('snapshot_too_large')
    descriptor = os.open(OUTPUT_NAME, os.O_WRONLY | os.O_CREAT | os.O_APPEND |
                         NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=root_fd)
    try:
        _owned(os.fstat(descriptor), uid, mode=0o600, maximum=MAX_OUTPUT_BYTES-len(raw))
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _owned(os.fstat(descriptor), uid, mode=0o600, maximum=MAX_OUTPUT_BYTES-len(raw))
        if os.write(descriptor, raw) != len(raw):
            raise ValueError('snapshot_write_incomplete')
    finally:
        os.close(descriptor)


def _validate_output(root_fd, uid):
    try:
        info = os.stat(OUTPUT_NAME, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    _owned(info, uid, mode=0o600, maximum=MAX_OUTPUT_BYTES)


def _hardware_stop(target, thread, pc):
    if thread.GetStopReason() != lldb.eStopReasonBreakpoint:
        raise ValueError('hardware_breakpoint_stop_required')
    count = thread.GetStopReasonDataCount()
    if count < 2 or count % 2:
        raise ValueError('hardware_breakpoint_stop_required')
    for index in range(0, count, 2):
        breakpoint = target.FindBreakpointByID(thread.GetStopReasonDataAtIndex(index))
        location = breakpoint.FindLocationByID(thread.GetStopReasonDataAtIndex(index + 1))
        if (not breakpoint.IsValid() or not breakpoint.IsHardware() or not location.IsValid()
                or location.GetAddress().GetLoadAddress(target) != pc):
            raise ValueError('hardware_breakpoint_stop_required')


def snapshot(debugger, command, result, _dict):
    root_fd = main_fd = module_fd = None
    try:
        if command.strip():
            raise ValueError('command_arguments_not_supported')
        uid = os.geteuid()
        if uid == 0 or uid != os.getuid():
            raise ValueError('ordinary_user_required')
        ticket_path = _path(os.environ['WX_ABI_PRIVATE_TICKET'])
        if ticket_path.name != TICKET_NAME:
            raise ValueError('ticket_name')
        private_root = ticket_path.parent
        if private_root == Path('/') or private_root == Path('/Applications'):
            raise ValueError('private_root_required')
        root_fd = _open_root(private_root, uid)
        ticket = _read_ticket(root_fd, uid)
        expected_main, expected_module, salt, marker = _validate_ticket(ticket, private_root, time.time())
        _validate_output(root_fd, uid)
        main_fd = _open_temporary_file(root_fd, (TEMP_APP_NAME,) + MAIN_RELATIVE, uid)
        module_fd = _open_temporary_file(root_fd, (TEMP_APP_NAME,) + MODULE_RELATIVE, uid)
        target = debugger.GetSelectedTarget()
        process = target.GetProcess()
        _bind_process(target, process, ticket, expected_main, uid)
        thread = process.GetSelectedThread()
        if not thread.IsValid():
            raise ValueError('thread_unavailable')
        frame = thread.GetFrameAtIndex(0)
        if not frame.IsValid():
            raise ValueError('frame_unavailable')
        _hardware_stop(target, thread, frame.GetPC())
        module = frame.GetModule()
        if (not module.IsValid() or module.GetUUIDString().upper() != EXPECTED_UUID
                or _file_spec_path(module.GetFileSpec()) != expected_module):
            raise ValueError('module_binding')
        # The path here is a descriptor owned by this function, never a ticket
        # path. /dev/fd duplicates the already-guarded file; replacement of the
        # bundle's pathname cannot redirect the code hash to another file.
        if code_sha256(Path('/dev/fd') / str(module_fd)) != EXPECTED_CODE_SHA256:
            raise ValueError('code_section_hash')
        section = module.FindSection('__TEXT')
        if not section.IsValid():
            raise ValueError('section_unavailable')
        base = section.GetLoadAddress(target) - section.GetFileAddress()
        offset = frame.GetPC() - base
        event = next((name for name, address in ALLOWED.items() if address == offset), None)
        if event is None:
            raise ValueError('stop_site')
        registers = {}
        for name in REGISTER_NAMES:
            register = frame.FindRegister(name)
            if not register.IsValid():
                raise ValueError('register_unavailable')
            registers[name] = register.GetValueAsUnsigned()
        object_address = registers[OBJECT_REGISTER[event]]
        if type(object_address) is not int or not 0 < object_address <= 2**64-16 or object_address % 4:
            raise ValueError('object_identity')
        fields = {}
        for name, field_offset in (('transport_id', 8), ('command_id', 0xc)):
            error = lldb.SBError()
            fields[name] = process.ReadUnsignedFromMemory(object_address + field_offset, 4, error)
            if not error.Success():
                raise ValueError('typed_field_unreadable')
        data = packet(event, registers, salt, external_marker=marker, **fields)
        _bind_process(target, process, ticket, expected_main, uid)
        if ticket['expiresAt'] <= time.time():
            raise ValueError('ticket_expired')
        _append(root_fd, uid, data)
        result.AppendMessage('metadata snapshot saved; no raw addresses or body captured')
    except Exception:
        result.SetError('metadata collection refused; validate private ticket and authorized stop')
    finally:
        for descriptor in (module_fd, main_fd, root_fd):
            if descriptor is not None:
                os.close(descriptor)


def __lldb_init_module(debugger, _dict):
    debugger.HandleCommand('command script add -f lldb_metadata_collector.snapshot wx-abi-snapshot')
