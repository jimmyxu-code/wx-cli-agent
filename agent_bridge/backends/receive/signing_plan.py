"""Read-only, per-code-item signing plan. Never copy, sign, launch or attach.

The plan preserves each executable's own reviewed runtime entitlement values.
It removes exactly three old-identity claims, and permits one explicit addition:
get-task-allow on the main executable. It is not an assertion of launchability.
"""
import argparse
import json
import plistlib
import re
import struct
import subprocess
from pathlib import Path


REMOVED_IDENTITY_KEYS = frozenset({
    'com.apple.application-identifier',
    'com.apple.developer.team-identifier',
    'com.apple.security.application-groups',
})
GET_TASK_ALLOW = 'com.apple.security.get-task-allow'
APP_SANDBOX = 'com.apple.security.app-sandbox'
INHERIT = 'com.apple.security.inherit'
REVIEWED_BOOLEAN_KEYS = frozenset({
    APP_SANDBOX, INHERIT, GET_TASK_ALLOW,
    'com.apple.security.cs.allow-jit',
    'com.apple.security.device.audio-input',
    'com.apple.security.device.camera',
    'com.apple.security.files.bookmarks.app-scope',
    'com.apple.security.files.downloads.read-write',
    'com.apple.security.files.user-selected.read-write',
    'com.apple.security.network.client',
    'com.apple.security.network.server',
    'com.apple.security.personal-information.location',
    'com.apple.security.personal-information.photos-library',
    'com.apple.security.print',
})
BUNDLE_SUFFIXES = frozenset({'.app', '.appex', '.xpc', '.framework', '.bundle'})
MACH_EXECUTE = 2
MACH_DYLIB = 6
MACH_BUNDLE = 8
MACH_KEXT_BUNDLE = 11
THIN_MAGIC = {
    b'\xce\xfa\xed\xfe': '<', b'\xcf\xfa\xed\xfe': '<',
    b'\xfe\xed\xfa\xce': '>', b'\xfe\xed\xfa\xcf': '>',
}
FAT_MAGIC = {
    b'\xca\xfe\xba\xbe': ('>', 20, False),
    b'\xbe\xba\xfe\xca': ('<', 20, False),
    b'\xca\xfe\xba\xbf': ('>', 32, True),
    b'\xbf\xba\xfe\xca': ('<', 32, True),
}


class PlanError(RuntimeError):
    """Safe error category. Do not attach captured signing stdout/stderr."""


def _inside(root, path):
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        raise PlanError('bundle_path_outside_root_or_missing')
    return resolved


def _plist(path):
    try:
        value = plistlib.loads(path.read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        raise PlanError('bundle_plist_invalid')
    if not isinstance(value, dict):
        raise PlanError('bundle_plist_invalid')
    return value


def _macho_types(path):
    """Parse only Mach-O headers, including all fat slices; no code execution."""
    try:
        with path.open('rb') as stream:
            header = stream.read(8)
            magic = header[:4]
            if magic in THIN_MAGIC:
                stream.seek(12)
                raw = stream.read(4)
                if len(raw) != 4:
                    raise PlanError('macho_header_invalid')
                return {struct.unpack(THIN_MAGIC[magic]+'I', raw)[0]}
            if magic not in FAT_MAGIC:
                return set()
            endian, stride, wide = FAT_MAGIC[magic]
            if len(header) != 8:
                raise PlanError('macho_header_invalid')
            count = struct.unpack(endian+'I', header[4:])[0]
            if not 1 <= count <= 32:
                raise PlanError('macho_header_invalid')
            kinds = set()
            for index in range(count):
                stream.seek(8+index*stride+8)
                offset_raw = stream.read(8 if wide else 4)
                if len(offset_raw) != (8 if wide else 4):
                    raise PlanError('macho_header_invalid')
                offset = struct.unpack(endian+('Q' if wide else 'I'), offset_raw)[0]
                stream.seek(offset)
                slice_header = stream.read(16)
                slice_endian = THIN_MAGIC.get(slice_header[:4])
                if slice_endian is None or len(slice_header) != 16:
                    raise PlanError('macho_header_invalid')
                kinds.add(struct.unpack(slice_endian+'I', slice_header[12:])[0])
            return kinds
    except OSError:
        raise PlanError('bundle_file_unreadable')


def _bundle_target(root, binary):
    """Map a bundle's designated executable to its enclosing signing target."""
    for directory in binary.parents:
        if directory == root.parent:
            break
        if directory.suffix not in BUNDLE_SUFFIXES:
            continue
        infos = [directory/'Contents/Info.plist', directory/'Resources/Info.plist']
        if directory.suffix == '.framework':
            infos += sorted(directory.glob('Versions/*/Resources/Info.plist'))
        for info in infos:
            if not info.is_file():
                continue
            info_path = _inside(root, info)
            executable = _plist(info_path).get('CFBundleExecutable')
            if not isinstance(executable, str) or not executable or '/' in executable:
                continue
            candidates = [directory/'Contents/MacOS'/executable,
                          directory/executable]
            if directory.suffix == '.framework':
                candidates += sorted(directory.glob('Versions/*/'+executable))
            for candidate in candidates:
                if candidate.is_file() and _inside(root, candidate) == binary:
                    return directory
    return binary


def _signature(target, runner):
    try:
        result = runner(['/usr/bin/codesign', '-dvv', '--entitlements', ':-', str(target)],
                        capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        raise PlanError('signature_metadata_unreadable')
    if result.returncode:
        raise PlanError('signature_metadata_unreadable')
    raw = result.stdout.encode() if isinstance(result.stdout, str) else result.stdout
    try:
        entitlements = plistlib.loads(raw) if raw else {}
    except (ValueError, plistlib.InvalidFileException):
        raise PlanError('signature_entitlements_invalid')
    if not isinstance(entitlements, dict):
        raise PlanError('signature_entitlements_invalid')
    diagnostic = result.stderr.decode(errors='replace') if isinstance(result.stderr, bytes) else result.stderr
    match = re.search(r'\bflags=0x([0-9a-fA-F]+)', diagnostic or '')
    if match is None:
        raise PlanError('signature_runtime_flags_unreadable')
    return entitlements, bool(int(match.group(1), 16) & 0x10000)


def _profile(original, main, library, remove_main_sandbox=False, disable_main_library_validation=False):
    unknown = set(original) - REMOVED_IDENTITY_KEYS - REVIEWED_BOOLEAN_KEYS
    if unknown:
        # Names alone are safe; never include raw values or codesign diagnostics.
        raise PlanError('unreviewed_entitlement:'+','.join(sorted(unknown)))
    if any(type(original[name]) is not bool for name in set(original) & REVIEWED_BOOLEAN_KEYS):
        raise PlanError('runtime_entitlement_type_invalid')
    removed = sorted(set(original) & REMOVED_IDENTITY_KEYS)
    effective = {name: value for name, value in original.items() if name not in REMOVED_IDENTITY_KEYS}
    added = []
    warnings = []
    if library:
        # Apple: libraries inherit the host's entitlements. Do not inject profiles.
        if effective:
            warnings.append('library_runtime_entitlements_not_applied')
        return None, removed, added, warnings
    if main:
        if effective.get(APP_SANDBOX) is not True:
            raise PlanError('main_sandbox_not_enabled')
        if effective.get(INHERIT) is True:
            raise PlanError('main_inherit_conflicts_with_debug_profile')
        if effective.get(GET_TASK_ALLOW) is not True:
            effective[GET_TASK_ALLOW] = True
            added.append(GET_TASK_ALLOW)
        if remove_main_sandbox:
            effective.pop(APP_SANDBOX)
            warnings.append('main_sandbox_removed_requires_explicit_authorization')
        if disable_main_library_validation:
            effective['com.apple.security.cs.disable-library-validation'] = True
            warnings.append('main_library_validation_disabled_requires_explicit_authorization')
    elif effective.get(INHERIT) is True and effective.get(GET_TASK_ALLOW) is True:
        raise PlanError('helper_inherit_conflicts_with_debug_profile')
    return effective, removed, added, warnings


def generate(app=Path('/Applications/WeChat.app'), runner=subprocess.run,
             remove_main_sandbox=False, disable_main_library_validation=False):
    root = Path(app).resolve(strict=True)
    if not root.is_dir() or root.suffix != '.app':
        raise PlanError('app_bundle_required')
    main_name = _plist(_inside(root, root/'Contents/Info.plist')).get('CFBundleExecutable')
    if not isinstance(main_name, str) or not main_name or '/' in main_name:
        raise PlanError('main_executable_missing')
    main = _inside(root, root/'Contents/MacOS'/main_name)
    items = []
    binaries_seen = set()
    targets_seen = set()
    for entry in sorted(root.rglob('*')):
        if not entry.is_file():
            continue
        binary = _inside(root, entry)
        if binary in binaries_seen:
            continue
        kinds = _macho_types(binary)
        if not kinds:
            continue
        binaries_seen.add(binary)
        if not kinds.issubset({MACH_EXECUTE, MACH_DYLIB, MACH_BUNDLE}) or len(kinds) != 1:
            raise PlanError('unsupported_macho_type')
        kind = next(iter(kinds))
        target = _bundle_target(root, binary)
        if target in targets_seen:
            raise PlanError('ambiguous_bundle_signing_target')
        targets_seen.add(target)
        original, runtime = _signature(target, runner)
        is_main = binary == main
        is_library = kind in {MACH_DYLIB, MACH_BUNDLE}
        if is_main and kind != MACH_EXECUTE:
            raise PlanError('main_not_macho_executable')
        effective, removed, added, warnings = _profile(
            original, is_main, is_library, remove_main_sandbox=remove_main_sandbox,
            disable_main_library_validation=disable_main_library_validation)
        items.append({
            'target': str(target.relative_to(root)),
            'binary': str(binary.relative_to(root)),
            'kind': 'main' if is_main else 'library' if is_library else 'helper',
            'originalEntitlementKeys': sorted(original),
            'entitlements': effective,
            'removedIdentityKeys': removed,
            'addedDebugKeys': added,
            'removedSandboxKeys': [APP_SANDBOX] if is_main and remove_main_sandbox else [],
            'preserveRuntime': runtime,
            'warnings': warnings,
        })
    if main not in binaries_seen:
        raise PlanError('main_not_macho_executable')
    # Every enclosing bundle comes after its nested code; the root signs last.
    items.sort(key=lambda item: (-len(Path(item['target']).parts), item['target']))
    return {'readOnly': True, 'launchabilityVerified': False,
            'containerAccessVerified': False, 'signatureApplied': False,
            'signingOrder': items, 'modifications': [],
            'mainSandboxRemovalRequested': remove_main_sandbox,
            'mainLibraryValidationDisableRequested': disable_main_library_validation,
            'warnings': ['runtime_library_validation_requires_separate_verification',
                         'old_team_group_access_removed',
                         'container_signature_identity_changes',
                         'do_not_use_deep_for_signing']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, default=Path('/Applications/WeChat.app'))
    parser.add_argument('--authorized-remove-main-sandbox', action='store_true',
                        help='Plan only; use only after explicit user authorization for the temporary copy.')
    args = parser.parse_args()
    try:
        print(json.dumps(generate(args.app, remove_main_sandbox=args.authorized_remove_main_sandbox),
                         ensure_ascii=False, indent=2))
    except (PlanError, OSError):
        # Avoid emitting unreviewed signing output or source identifiers.
        print(json.dumps({'readOnly': True, 'error': 'signing_plan_requires_review',
                          'signatureApplied': False, 'modifications': []}))
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
