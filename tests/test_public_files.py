import unittest

from scripts.check_public_files import audit_file


class PublicFilesTests(unittest.TestCase):
    def test_public_source_and_docs_are_allowed(self):
        for name in ('main.py', 'modules/device_adapter.py', 'configs/double11.json',
                     '.github/workflows/release.yml', '.github/CODEOWNERS', 'docs/SAFETY.md', 'AGENTS.md'):
            self.assertEqual(audit_file(name, b'public source'), [])

    def test_local_directories_and_binary_artifacts_are_rejected(self):
        for name in ('runtime/last-run.json', '.venv/pyvenv.cfg', 'dist/MiaoTask.exe',
                     'screenshots/phone.png', 'modules/__pycache__/campaign.pyc'):
            self.assertTrue(audit_file(name, b'local data'))

    def test_device_config_and_environment_files_are_rejected(self):
        for name in ('configs/device.local.json', 'configs/devices.json', '.env', 'adbkey'):
            self.assertTrue(audit_file(name, b'private configuration'))

    def test_only_reviewed_image_paths_and_signatures_are_allowed(self):
        png = b'\x89PNG\r\n\x1a\n\0public illustration'
        webp = b'RIFF\0\0\0\0WEBPpublic screenshot'
        self.assertEqual(audit_file('frontend/assets/miaotask-128.png', png), [])
        self.assertEqual(audit_file('docs/assets/feature-wifi.webp', webp), [])
        self.assertTrue(audit_file('docs/assets/phone-private.png', png))
        self.assertTrue(audit_file('frontend/assets/miaotask-128.png', b'not a PNG'))
        self.assertTrue(audit_file('docs/assets/feature-wifi.webp', png))
        self.assertTrue(audit_file('frontend/assets/miaotask-128.png', png + b'x' * 2_000_000))

    def test_tokens_are_rejected_without_echoing_the_value(self):
        token = b'gh' + b'p_' + b'x' * 36
        reasons = audit_file('main.py', b'token = "' + token + b'"')
        self.assertTrue(reasons)
        self.assertNotIn(token.decode(), str(reasons))

    def test_private_identifiers_are_supplied_only_at_audit_time(self):
        value = 'synthetic-device-for-audit-test'
        reasons = audit_file('configs/double11.json', value.encode(), [value])
        self.assertIn('private local identifier', reasons)
        self.assertNotIn(value, str(reasons))


if __name__ == '__main__':
    unittest.main()
