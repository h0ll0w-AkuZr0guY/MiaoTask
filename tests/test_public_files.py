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

    def test_public_images_and_readme_are_outside_content_audit(self):
        # Direct maintainer edits must not block a subsequent unrelated PR.
        content = b'\0' * 4_000_001 + b'-----BEGIN ' + b'PRIVATE KEY-----'
        for name in ('README.md', 'docs/assets/new-name.png', 'docs/assets/nested/image.JPG',
                     'docs/assets/feature-wifi.webp', 'frontend/assets/new-icon.ico',
                     'frontend/assets/new-illustration.svg'):
            with self.subTest(name=name):
                self.assertEqual(audit_file(name, content, forbidden=['PRIVATE']), [])

    def test_asset_exemption_keeps_path_and_source_protections(self):
        for name in ('docs/assets/../../outside.png', 'docs/assets/private/image.png',
                     'docs/assets/file.exe', 'frontend/assets/unknown.bin', 'screenshots/phone.png'):
            with self.subTest(name=name):
                self.assertTrue(audit_file(name, b'\0binary'))
        self.assertTrue(audit_file('docs/example.md', b'x' * 2_000_001))
        self.assertTrue(audit_file('scripts/example.py', b'\0binary'))

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
