"""Presentation behavior without adding browser dependencies to PR CI."""
from pathlib import Path
import shutil
import subprocess
import unittest

from backend.core.service.static import static_response


class FrontendInteractions(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for JavaScript checks')
    def test_dialog_preview_and_catalog_interactions(self):
        result = subprocess.run(['node', str(Path(__file__).with_name('frontend_interactions.js'))],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_new_components_are_available_through_static_allowlist(self):
        for name in ('dialogs.js', 'preview-controls.js', 'rankings.js', 'themes.js'):
            with self.subTest(component=name):
                body, mime = static_response('/js/components/' + name)
                self.assertIn('text/javascript', mime)
                self.assertTrue(body.startswith(b"'use strict'"))
        body, mime = static_response('/css/themes.css')
        self.assertIn('text/css', mime)
        self.assertIn(b'--canvas', body)
