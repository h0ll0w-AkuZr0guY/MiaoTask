import importlib.metadata
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.core.models import TextNode
from backend.core.service import diagnostics


class DiagnosticsSelfTestTests(unittest.TestCase):
    def test_distribution_version_delegates_to_importlib_metadata(self):
        with patch.object(importlib.metadata, 'version', return_value='3.5.0') as lookup:
            self.assertEqual(diagnostics._distribution_version('uiautomator2'), '3.5.0')
        lookup.assert_called_once_with('uiautomator2')

    def test_self_test_succeeds_when_apple_silicon_vision_ocr_and_adb_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / 'report.json'
            fake_engine = SimpleNamespace(
                engine=SimpleNamespace(
                    miao_backend='vision',
                    miao_providers={'det': ['AppleVision'], 'rec': ['AppleVision']},
                )
            )

            def fake_ocr(reader, _image):
                reader._engine = fake_engine
                return [TextNode('BROWSE 15 SECONDS', (10, 10, 200, 60), 'ocr')]

            with patch.object(diagnostics, 'is_apple_silicon_macos', return_value=True), \
                    patch.object(diagnostics, '_distribution_version', return_value='10.3'), \
                    patch.object(diagnostics.shutil, 'which', return_value='/opt/homebrew/bin/adb'), \
                    patch.object(diagnostics.subprocess, 'run',
                                 return_value=subprocess.CompletedProcess([], 0, 'Android Debug Bridge version 1.0.41\n', '')), \
                    patch('backend.core.device.screen.ScreenReader._ocr', autospec=True, side_effect=fake_ocr):
                report = diagnostics.self_test(out)

            self.assertTrue(report['ok'], report['errors'])
            self.assertEqual(report['checks']['ocr_backend'], 'vision')
            self.assertEqual(report['checks']['ocr_runtime_source'], 'macos-vision')
            self.assertIn('pyobjc-framework-Vision', report['checks'])
            self.assertIn('15', report['checks']['ocr_text'])

    def test_self_test_reports_non_apple_silicon_host_clearly(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / 'report.json'
            with patch.object(diagnostics, 'is_apple_silicon_macos', return_value=False), \
                    patch.object(diagnostics, '_distribution_version', return_value='3.5.0'), \
                    patch.object(diagnostics.shutil, 'which', return_value='/usr/bin/adb'), \
                    patch.object(diagnostics.subprocess, 'run',
                                 return_value=subprocess.CompletedProcess([], 0, 'Android Debug Bridge version 1.0.41\n', '')):
                report = diagnostics.self_test(out)
            self.assertFalse(report['ok'])
            self.assertTrue(any('Apple Silicon' in err for err in report['errors']))


if __name__ == '__main__':
    unittest.main()
