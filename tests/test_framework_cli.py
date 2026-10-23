"""Local activity tools are explicit and keep ordinary launch defaults intact."""
from contextlib import redirect_stderr
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.main import main


class FrameworkCliTests(unittest.TestCase):
    def invalid(self, *arguments):
        with patch('sys.argv', ['main.py', *arguments]), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                main()
            self.assertEqual(caught.exception.code, 2)

    def test_local_package_requires_explicit_digest_and_self_test(self):
        self.invalid('--self-test', '--activity-package', 'trusted.zip')
        self.invalid('--self-test', '--activity-package-sha256', 'a' * 64)
        self.invalid('--activity-package', 'trusted.zip', '--activity-package-sha256', 'a' * 64)

    def test_test_source_rejects_missing_catalog_and_non_web_modes(self):
        with tempfile.TemporaryDirectory() as work:
            self.invalid('--test-activity-source-dir', work)
            (Path(work) / 'activity-catalog.json').write_text('{}', encoding='utf-8')
            self.invalid('--cli', '--test-activity-source-dir', work)
            self.invalid('--self-test', '--test-activity-source-dir', work)

    def test_ordinary_launch_uses_web_service_without_a_local_source(self):
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / 'source'
            source.mkdir()
            (source / 'activity-catalog.json').write_text('{}', encoding='utf-8')
            with patch('sys.argv', ['main.py', '--no-browser']), \
                    patch.dict(os.environ, {'MIAO_DATA_DIR': str(Path(work) / 'runtime')}), \
                    patch('backend.main.RotatingFileHandler'), patch('backend.main.logging.basicConfig'), \
                    patch('backend.core.registry.default_registry', return_value=SimpleNamespace(has_activities=False)), \
                    patch('backend.core.service.panel.serve') as serve:
                main()
            self.assertNotIn('test_activity_source_dir', serve.call_args.kwargs)
            self.assertFalse(serve.call_args.args[1])


if __name__ == '__main__':
    unittest.main()
