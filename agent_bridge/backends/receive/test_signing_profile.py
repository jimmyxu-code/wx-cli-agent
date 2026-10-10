import importlib.util
from pathlib import Path
import unittest

s = importlib.util.spec_from_file_location('bootstrap', Path(__file__).parent / 'debug_copy_source/bootstrap_keys.py')
m = importlib.util.module_from_spec(s)
s.loader.exec_module(m)


class SigningProfileTests(unittest.TestCase):
    def test_default_refuses_restricted_profile(self):
        with self.assertRaises(RuntimeError):
            m.copy_entitlements({'com.apple.application-identifier': 'example'}, 'preserve')

    def test_minimal_keeps_sandbox_and_jit_without_groups(self):
        original = {'com.apple.application-identifier': 'example',
                    'com.apple.developer.team-identifier': 'example',
                    'com.apple.security.application-groups': ['example'],
                    'com.apple.security.app-sandbox': True,
                    'com.apple.security.cs.allow-jit': True}
        selected = m.copy_entitlements(original, 'minimal-debug-copy')
        self.assertTrue(selected['com.apple.security.app-sandbox'])
        self.assertTrue(selected['com.apple.security.cs.allow-jit'])
        self.assertTrue(selected['com.apple.security.get-task-allow'])
        self.assertNotIn('com.apple.security.application-groups', selected)
        self.assertEqual(len(original), 5)

    def test_new_restricted_entitlement_requires_review(self):
        with self.assertRaises(RuntimeError):
            m.copy_entitlements({'com.apple.developer.unknown': True}, 'minimal-debug-copy')

    def test_explicit_unsandboxed_profile_keeps_jit_and_debug(self):
        selected = m.copy_entitlements({'com.apple.security.app-sandbox': True,
                                        'com.apple.security.cs.allow-jit': True}, 'minimal-debug-copy-unsandboxed')
        self.assertNotIn('com.apple.security.app-sandbox', selected)
        self.assertTrue(selected['com.apple.security.cs.allow-jit'])
        self.assertTrue(selected['com.apple.security.get-task-allow'])


if __name__ == '__main__':
    unittest.main()
