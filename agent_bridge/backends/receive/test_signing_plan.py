import plistlib
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from signing_plan import (APP_SANDBOX, GET_TASK_ALLOW, INHERIT, PlanError,
                          REMOVED_IDENTITY_KEYS, generate)


def binary(path, kind=2):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'\xcf\xfa\xed\xfe'+struct.pack('<III', 0x100000c, 0, kind)+b'\0'*16)


def bundle(path, name, framework=False):
    info = path/'Resources/Info.plist' if framework else path/'Contents/Info.plist'
    info.parent.mkdir(parents=True, exist_ok=True)
    info.write_bytes(plistlib.dumps({'CFBundleExecutable': name}))
    exe = path/name if framework else path/'Contents/MacOS'/name
    binary(exe, kind=6 if framework else 2)
    return exe


class SigningPlanTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.app = Path(self.folder.name).resolve()/'WeChat.app'
        bundle(self.app, 'WeChat')
        self.original = {APP_SANDBOX: True, 'com.apple.security.cs.allow-jit': True,
                         'com.apple.application-identifier': 'private-id',
                         'com.apple.developer.team-identifier': 'private-team',
                         'com.apple.security.application-groups': ['private-group']}
        self.profiles = {self.app: self.original}
        self.calls = []

    def runner(self, args, **kwargs):
        self.calls.append(args)
        self.assertEqual(args[:4], ['/usr/bin/codesign', '-dvv', '--entitlements', ':-'])
        target = Path(args[-1])
        profile = self.profiles.get(target, {})
        return SimpleNamespace(returncode=0, stdout=plistlib.dumps(profile) if profile else b'',
                               stderr=b'CodeDirectory v=20500 flags=0x10000(runtime)')

    def test_only_main_adds_debug_and_no_identity_values_escape(self):
        result = generate(self.app, self.runner)
        main = result['signingOrder'][-1]
        self.assertTrue(main['entitlements'][GET_TASK_ALLOW])
        self.assertTrue(main['entitlements'][APP_SANDBOX])
        self.assertEqual(set(main['removedIdentityKeys']), REMOVED_IDENTITY_KEYS)
        self.assertTrue(main['preserveRuntime'])
        self.assertNotIn('private-', str(result))
        self.assertEqual(result['modifications'], [])
        self.assertFalse(result['signatureApplied'])
        self.assertFalse(result['launchabilityVerified'])

    def test_each_helper_preserves_own_profile_inside_out(self):
        helper = self.app/'Contents/MacOS/Helper.app'
        nested = helper/'Contents/Frameworks/Nested.app'
        bundle(helper, 'Helper')
        bundle(nested, 'Nested')
        self.profiles[helper] = {APP_SANDBOX: True, INHERIT: True}
        self.profiles[nested] = {APP_SANDBOX: True, INHERIT: True, 'com.apple.security.cs.allow-jit': True}
        result = generate(self.app, self.runner)['signingOrder']
        self.assertEqual([item['target'] for item in result],
                         ['Contents/MacOS/Helper.app/Contents/Frameworks/Nested.app',
                          'Contents/MacOS/Helper.app', '.'])
        for item in result[:-1]:
            self.assertTrue(item['entitlements'][INHERIT])
            self.assertNotIn(GET_TASK_ALLOW, item['entitlements'])
            self.assertEqual(item['addedDebugKeys'], [])

    def test_framework_and_raw_dylib_get_no_entitlement_profile(self):
        framework = self.app/'Contents/Frameworks/Example.framework'
        bundle(framework, 'Example', framework=True)
        library = self.app/'Contents/Resources/extra.dylib'
        binary(library, kind=6)
        items = generate(self.app, self.runner)['signingOrder']
        self.assertEqual({item['target'] for item in items if item['kind'] == 'library'},
                         {'Contents/Frameworks/Example.framework', 'Contents/Resources/extra.dylib'})
        self.assertTrue(all(item['entitlements'] is None for item in items if item['kind'] == 'library'))

    def test_unknown_entitlement_stops_instead_of_silent_strip(self):
        self.original['com.apple.private.unreviewed'] = 'private-value'
        with self.assertRaisesRegex(PlanError, 'unreviewed_entitlement'):
            generate(self.app, self.runner)

    def test_helper_inherit_get_task_allow_conflict_stops(self):
        helper = self.app/'Contents/MacOS/Helper.app'
        bundle(helper, 'Helper')
        self.profiles[helper] = {APP_SANDBOX: True, INHERIT: True, GET_TASK_ALLOW: True}
        with self.assertRaisesRegex(PlanError, 'helper_inherit_conflicts'):
            generate(self.app, self.runner)

    def test_invalid_boolean_stops(self):
        self.original[APP_SANDBOX] = 'true'
        with self.assertRaisesRegex(PlanError, 'type_invalid'):
            generate(self.app, self.runner)

    def test_explicit_sandbox_removal_changes_main_only(self):
        helper = self.app/'Contents/MacOS/Helper.app'
        bundle(helper, 'Helper')
        self.profiles[helper] = {APP_SANDBOX: True, INHERIT: True}
        before = generate(self.app, self.runner)['signingOrder']
        after = generate(self.app, self.runner, remove_main_sandbox=True)['signingOrder']
        self.assertEqual(before[0], after[0])
        self.assertTrue(before[-1]['entitlements'][APP_SANDBOX])
        self.assertNotIn(APP_SANDBOX, after[-1]['entitlements'])
        self.assertEqual(after[-1]['removedSandboxKeys'], [APP_SANDBOX])
        self.assertEqual(after[-1]['addedDebugKeys'], [GET_TASK_ALLOW])
        self.assertEqual(after[-1]['preserveRuntime'], before[-1]['preserveRuntime'])

    def test_signature_failure_keeps_private_diagnostics_out_of_exception(self):
        def failure(*args, **kwargs):
            return SimpleNamespace(returncode=1, stdout=b'private-secret', stderr=b'private-secret')
        with self.assertRaises(PlanError) as error:
            generate(self.app, failure)
        self.assertEqual(str(error.exception), 'signature_metadata_unreadable')

    def test_symlink_alias_not_signed_twice_and_external_link_rejected(self):
        alias = self.app/'Contents/MacOS/Alias'
        alias.symlink_to('WeChat')
        self.assertEqual(len(generate(self.app, self.runner)['signingOrder']), 1)
        outside = Path(self.folder.name)/'Outside'
        binary(outside)
        (self.app/'Contents/MacOS/Escape').symlink_to(outside)
        with self.assertRaisesRegex(PlanError, 'outside_root'):
            generate(self.app, self.runner)

    def test_fat_binary_all_slices_classified(self):
        dylib = self.app/'Contents/Resources/Fat.dylib'
        dylib.parent.mkdir(parents=True)
        thin = b'\xcf\xfa\xed\xfe'+struct.pack('<III', 0x100000c, 0, 6)+b'\0'*16
        fat = b'\xca\xfe\xba\xbe'+struct.pack('>I', 2)
        fat += struct.pack('>IIIII', 0x100000c, 0, 48, len(thin), 0)
        fat += struct.pack('>IIIII', 0x1000007, 0, 80, len(thin), 0)
        dylib.write_bytes(fat+thin+thin)
        items = generate(self.app, self.runner)['signingOrder']
        self.assertEqual(next(item['kind'] for item in items if 'Fat' in item['target']), 'library')


if __name__ == '__main__':
    unittest.main()
