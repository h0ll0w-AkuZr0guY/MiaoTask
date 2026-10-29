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


if __name__ == '__main__':
    unittest.main()
