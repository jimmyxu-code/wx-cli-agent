#!/usr/bin/env python3
"""Explicit temporary-copy bootstrap. Never changes original signatures or SIP."""
import argparse
import json
import os
from pathlib import Path
import platform
import plistlib
import subprocess as sp
import sys
import time


def emit(kind, **items):
    print(json.dumps(dict(type=kind, **items)), flush=True)


def run(args):
    result = sp.run(args, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('command_failed: ' + Path(args[0]).name)
    return result.stdout or result.stderr


def copy_entitlements(original, profile):
    restricted = [name for name in original if name.startswith('com.apple.developer.') or name in
                  ('com.apple.application-identifier', 'application-identifier',
                   'keychain-access-groups', 'com.apple.security.application-groups')]
    if profile == 'preserve':
        if restricted:
            raise RuntimeError('adhoc_copy_requires_a_separately_reviewed_entitlement_profile')
        return dict(original)
    reviewed = {'com.apple.application-identifier', 'com.apple.developer.team-identifier',
                'com.apple.security.application-groups'}
    if any(name not in reviewed for name in restricted):
        raise RuntimeError('unreviewed_restricted_entitlement')
    result = {k: v for k, v in original.items() if k not in reviewed}
    # Debug permission applies only to the explicitly selected temporary copy.
    # App Sandbox and JIT stay at their original values; no group grants added.
    result['com.apple.security.get-task-allow'] = True
    if profile == 'minimal-debug-copy-unsandboxed':
        result.pop('com.apple.security.app-sandbox', None)
    return result


def prepare_copy(app, shadow, state, profile, disable_main_library_validation=False):
    import importlib.util
    module_path = Path(__file__).resolve().parents[1]/'signing_plan.py'
    spec = importlib.util.spec_from_file_location('signing_plan', module_path)
    planner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(planner)
    if profile == 'preserve':
        raise RuntimeError('explicit_reviewed_copy_profile_required')
    plan = planner.generate(app, remove_main_sandbox=profile=='minimal-debug-copy-unsandboxed',
                            disable_main_library_validation=disable_main_library_validation)
    (state/'signing-plan.json').write_text(json.dumps(plan))
    run(['/usr/bin/ditto', str(app), str(shadow)])
    copied_root = shadow.resolve(strict=True)
    for index, item in enumerate(plan['signingOrder']):
        target = (shadow/item['target']).resolve(strict=True)
        target.relative_to(copied_root)
        entitlements = item['entitlements'] or {}
        entitlement_file = state/('entitlements-'+str(index)+'.plist')
        entitlement_file.write_bytes(plistlib.dumps(entitlements))
        run(['codesign', '--force', '--sign', '-', '--preserve-metadata=identifier,flags',
             '--entitlements', str(entitlement_file), str(target)])
        actual, runtime = planner._signature(target, sp.run)
        if actual != entitlements or runtime != item['preserveRuntime']:
            raise RuntimeError('per_item_signature_metadata_changed')
    run(['codesign', '--verify', '--deep', '--strict', str(shadow)])
    emit('per_item_signatures_verified', code_items=len(plan['signingOrder']),
         hardened_runtime_preserved=True, main_sandbox_removed=profile=='minimal-debug-copy-unsandboxed')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--app', type=Path, default=Path('/Applications/WeChat.app'))
    ap.add_argument('--db-root', type=Path)
    ap.add_argument('--state-dir', type=Path, default=Path('.wechat-local'))
    ap.add_argument('--timeout', type=int, default=180)
    ap.add_argument('--acknowledge-debug-copy', action='store_true')
    ap.add_argument('--profile', choices=['preserve', 'minimal-debug-copy', 'minimal-debug-copy-unsandboxed'], default='preserve')
    ap.add_argument('--use-prepared-copy', action='store_true')
    ap.add_argument('--authorized-disable-main-library-validation', action='store_true')
    args = ap.parse_args()
    if not args.acknowledge_debug_copy:
        ap.error('Read README first; requires --acknowledge-debug-copy. Quit WeChat normally before running.')
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise RuntimeError('bootstrap supports native Apple Silicon macOS only; do not run under Rosetta')
    if os.geteuid() == 0:
        raise RuntimeError('do not run with sudo or as root')
    if sp.run(['pgrep', '-x', 'WeChat'], capture_output=True).returncode == 0:
        raise RuntimeError('quit WeChat normally first; no processes were stopped')
    app = args.app.resolve(strict=True)
    root = args.db_root
    if root is None:
        roots = list((Path.home()/'Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files').glob('*/db_storage'))
        if len(roots) != 1:
            raise RuntimeError('specify --db-root: expected exactly one account')
        root = roots[0]
    root = root.resolve(strict=True)
    if not list(root.rglob('*.db')):
        raise RuntimeError('no databases; log into original WeChat at least once first')
    state = args.state_dir.resolve()
    state.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(state, 0o700)
    keys = state/'keys.json'
    shadow = state/'WeChat-trial.app'
    disabled = state/'WeChat-trial.disabled'
    if keys.exists() or disabled.exists() or (shadow.exists() and not args.use_prepared_copy):
        raise RuntimeError('state contains a previous bootstrap; use a new --state-dir, never overwrite live keys')
    if args.use_prepared_copy:
        import hashlib
        integrity = json.loads((state/'original-integrity.json').read_text())
        if hashlib.sha256((app/'Contents/MacOS/WeChat').read_bytes()).hexdigest() != integrity['originalExecutableSha256']:
            raise RuntimeError('prepared_copy_original_changed')
        plan = json.loads((state/'signing-plan.json').read_text())
        if plan.get('mainSandboxRemovalRequested') != (args.profile=='minimal-debug-copy-unsandboxed'):
            raise RuntimeError('prepared_copy_profile_mismatch')
        if plan.get('mainLibraryValidationDisableRequested',False) != args.authorized_disable_main_library_validation:
            raise RuntimeError('prepared_copy_library_validation_mismatch')
        run(['codesign','--verify','--deep','--strict',str(shadow)])
    os.umask(0o077)
    original_signature = run(['codesign', '-dvv', str(app)])
    (state/'original-signature.txt').write_text(original_signature)
    original_exe = app/'Contents/MacOS/WeChat'
    run(['codesign', '--verify', '--deep', '--strict', str(app)])
    lldb_python = run(['/usr/bin/lldb', '-P']).strip()
    env = dict(os.environ, PYTHONPATH=lldb_python)
    rc = 2
    try:
        if not args.use_prepared_copy:
            prepare_copy(app, shadow, state, args.profile, args.authorized_disable_main_library_validation)
        emit('copy_prepared', instruction='Enter your existing account in the temporary WeChat if requested.')
        command = ['/usr/bin/python3', str(Path(__file__).with_name('capture_keys.py')),
                   '--exe', str(shadow/'Contents/MacOS/WeChat'), '--db-root', str(root),
                   '--out', str(keys), '--timeout', str(args.timeout)]
        # Capture subprocess inherits the terminal for metadata only, never secrets.
        proc = sp.Popen(command, env=env)
        try:
            rc = proc.wait()
        except KeyboardInterrupt:
            # Terminal sends SIGINT to both. Give capture its finally block before restoring.
            proc.wait(timeout=15)
            rc = 130
    finally:
        if run(['codesign', '-dvv', str(app)]) != original_signature:
            raise RuntimeError('original signature changed unexpectedly; restore manually')
        running = sp.run(['pgrep', '-x', 'WeChat'], capture_output=True, text=True).stdout.split()
        commands = [run(['ps', '-p', pid, '-o', 'comm=']).strip() for pid in running]
        if any(str(shadow) in command for command in commands):
            emit('manual_restore_required', reason='temporary process is still running; quit it normally, then open original')
        else:
            ls = '/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister'
            sp.run([ls, '-u', str(shadow)], capture_output=True)
            if shadow.exists():
                shadow.rename(disabled)
            if not commands:
                # Exact executable avoids LaunchServices confusing identical bundle IDs.
                restored = sp.Popen([str(original_exe)], stdout=sp.DEVNULL, stderr=sp.DEVNULL, start_new_session=True)
                time.sleep(1)
                emit('original_launch_requested', pid=restored.pid, exited=restored.poll() is not None)
            emit('original_signature_unchanged', temporary_bundle_disabled=True)
    if keys.exists():
        count = len(json.loads(keys.read_text())['keys'])
        emit('keys_saved_privately', verified_databases=count, permissions=oct(keys.stat().st_mode & 0o777))
    return rc


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        emit('bootstrap_error', reason=str(error))
        sys.exit(2)
