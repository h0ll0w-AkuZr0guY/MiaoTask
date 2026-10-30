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
