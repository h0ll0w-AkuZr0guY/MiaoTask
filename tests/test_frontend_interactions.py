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

# preview_site
"""Pages reuses the frontend without changing the desktop entry or requiring a backend."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts.build_preview_site import build_preview, ROOT


class PreviewSiteTests(unittest.TestCase):
    def test_generated_site_keeps_runtime_source_unchanged_and_explains_demo(self):
        sources = [ROOT / 'frontend/index.html', ROOT / 'frontend/js/api.js']
        before = [p.read_bytes() for p in sources]
        with tempfile.TemporaryDirectory() as folder:
            site = Path(folder)
            (site / 'activities').mkdir()
            sentinel = site / 'activities/branches.json'
            sentinel.write_text('{}')
            result = build_preview(ROOT, site)
            self.assertEqual(sentinel.read_text(), '{}')
            html = (site / 'index.html').read_text(encoding='utf-8')
            self.assertIn('不能连接手机或执行任务', html)
            self.assertIn('src="./demo/adapter.js"', html)
            self.assertNotIn('src="/', html)
            self.assertIn('href="/api/report"', html)
            self.assertEqual(json.loads((site / 'demo/catalog.json').read_text(encoding='utf-8')), result)
            self.assertEqual(len(result['catalog']), 2)
            self.assertIn('demo/phone.svg', (site / 'js/api.js').read_text(encoding='utf-8'))
        self.assertEqual(before, [p.read_bytes() for p in sources])

    def test_rejects_building_over_frontend(self):
        with self.assertRaises(ValueError):
            build_preview(ROOT, ROOT / 'frontend')

    @unittest.skipUnless(shutil.which('node'), 'Node.js required for preview adapter checks')
    def test_memory_adapter_never_sends_device_actions_to_network(self):
        result = subprocess.run(['node', str(ROOT / 'tests/preview_adapter.js')], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

# branding
import threading
import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import urlopen

from backend.core.service.branding import windows_icon_path
from backend.core.service.panel import make_handler
from scripts.check_public_files import audit_file


class BrandingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        folder = self.root / 'frontend/assets'
        folder.mkdir(parents=True)
        # Test routes with synthetic bytes, independent of maintainer artwork.
        for name in ('miaotask-32.png', 'miaotask.ico', 'miaotask-master.png', 'private.png'):
            (folder / name).write_bytes(b'synthetic asset: ' + name.encode())
        asset_patch = patch('backend.core.service.static.assets_dir', return_value=self.root)
        asset_patch.start()
        self.addCleanup(asset_patch.stop)

    def test_windows_icon_path_follows_runtime_asset_root(self):
        with patch('backend.core.service.branding.assets_dir', return_value=self.root):
            self.assertEqual(windows_icon_path(), self.root / 'frontend/assets/miaotask.ico')

    def test_public_audit_does_not_check_icon_names_or_contents(self):
        self.assertEqual(audit_file('frontend/assets/new-icon.ico', b'any replacement'), [])
        self.assertEqual(audit_file('frontend/assets/miaotask.ico', b'any replacement'), [])
        self.assertTrue(audit_file('frontend/assets/private/device.ico', b'any replacement'))

    def test_http_exposes_icons_but_not_master_or_other_local_files(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(None, 'test-only'))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            for path, mime, name in (
                ('/assets/miaotask-32.png', 'image/png', 'miaotask-32.png'),
                ('/assets/miaotask.ico', 'image/x-icon', 'miaotask.ico'),
                ('/favicon.ico', 'image/x-icon', 'miaotask.ico'),
            ):
                with urlopen(base + path, timeout=3) as response:
                    self.assertEqual(response.headers.get_content_type(), mime)
                    self.assertEqual(response.read(), b'synthetic asset: ' + name.encode())
            for path in ('/assets/miaotask-master.png', '/assets/../version.json', '/assets/private.png'):
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + path, timeout=3)
                self.assertEqual(error.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
