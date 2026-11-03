"""Console-wide network/OCR preferences: storage, wiring and HTTP routes."""
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError

# Preload OpenCV: ``patch.dict(sys.modules, ...)`` restores the mapping and
# would otherwise drop a module first imported inside the patched block.
import cv2  # noqa: F401
from urllib.request import Request, urlopen

from backend.core import preferences
from backend.core.device import screen
from backend.core.service.activity_hub import ActivityCatalogHub
from backend.core.service.panel import make_handler


class _Hub(ActivityCatalogHub):
    def __init__(self):
        self.lock = threading.RLock()
        self._init_activity_catalog()


class PreferencesStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'MIAO_DATA_DIR': self.tmp.name}, clear=False)
        self.env.start()
        preferences.reset_preferences_cache()
        self.addCleanup(preferences.reset_preferences_cache)

    def test_defaults(self):
        self.assertEqual(preferences.load_preferences(),
                         {'download_proxy': '', 'ocr_device': 'auto'})

    def test_partial_save_persists_and_reloads(self):
        first = preferences.save_preferences({'ocr_device': 'cpu'})
        self.assertEqual(first['ocr_device'], 'cpu')
        self.assertEqual(first['download_proxy'], '')
        preferences.reset_preferences_cache()
        again = preferences.load_preferences()
        self.assertEqual(again['ocr_device'], 'cpu')
        on_disk = json.loads((Path(self.tmp.name) / 'preferences.json').read_text())
        self.assertEqual(on_disk['ocr_device'], 'cpu')

    def test_proxy_validation_accepts_url_and_off(self):
        self.assertEqual(preferences.save_preferences(
            {'download_proxy': '  http://127.0.0.1:7890 '})['download_proxy'],
            'http://127.0.0.1:7890')
        self.assertEqual(preferences.save_preferences(
            {'download_proxy': 'off'})['download_proxy'], 'off')

    def test_bad_proxy_rejected_before_write(self):
        with self.assertRaises(ValueError):
            preferences.save_preferences({'download_proxy': 'socks5://127.0.0.1:1080'})
        self.assertFalse((Path(self.tmp.name) / 'preferences.json').exists())

    def test_bad_ocr_backend_rejected(self):
        with self.assertRaises(ValueError):
            preferences.save_preferences({'ocr_device': 'tpu'})

    def test_unknown_keys_ignored(self):
        saved = preferences.save_preferences({'ocr_device': 'cuda', 'evil': True})
        self.assertNotIn('evil', saved)

    def test_corrupt_file_falls_back_to_defaults(self):
        (Path(self.tmp.name) / 'preferences.json').write_text('{broken', encoding='utf-8')
        preferences.reset_preferences_cache()
        self.assertEqual(preferences.load_preferences()['ocr_device'], 'auto')


class PreferenceWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'MIAO_DATA_DIR': self.tmp.name}, clear=False)
        self.env.start()
        preferences.reset_preferences_cache()
        screen.reset_ocr_engines()
        self.addCleanup(preferences.reset_preferences_cache)
        self.addCleanup(screen.reset_ocr_engines)

    def test_download_proxy_preference_reaches_opener(self):
        from backend.core.plugins import networking
        preferences.save_preferences({'download_proxy': '127.0.0.1:7890'})
        with patch.dict(os.environ, {'MIAO_DATA_DIR':self.tmp.name}, clear=True):
            os.environ.pop(networking.PROXY_ENV, None)
            proxies = networking.resolve_proxies()
        self.assertEqual(proxies, {'http': 'http://127.0.0.1:7890',
                                   'https': 'http://127.0.0.1:7890'})

    def test_env_proxy_overrides_saved_preference(self):
        from backend.core.plugins import networking
        preferences.save_preferences({'download_proxy': '127.0.0.1:7890'})
        with patch.dict(os.environ, {networking.PROXY_ENV: 'off'}, clear=False):
            self.assertEqual(networking.resolve_proxies(), {})

    def _engine_params(self, providers):
        screen.reset_ocr_engines()
        engine = Mock()
        engine.text_det.session.session.get_providers.return_value = providers
        rapid = SimpleNamespace(RapidOCR=Mock(return_value=engine))
        ort = SimpleNamespace(get_available_providers=Mock(return_value=list(providers)))
        with patch.dict(sys.modules, {'rapidocr': rapid, 'onnxruntime': ort}):
            screen.local_ocr_engine()
        return rapid.RapidOCR.call_args.kwargs['params']

    def test_saved_cuda_preference_enables_flag(self):
        providers = ['CUDAExecutionProvider', 'CPUExecutionProvider']
        preferences.save_preferences({'ocr_device': 'cuda'})
        params = self._engine_params(providers)
        self.assertIs(params['EngineConfig.onnxruntime.use_cuda'], True)

    def test_env_ocr_backend_overrides_preference(self):
        providers = ['CUDAExecutionProvider', 'CPUExecutionProvider']
        preferences.save_preferences({'ocr_device': 'cpu'})
        with patch.dict(os.environ, {'MIAO_OCR_DEVICE': 'auto'}, clear=False):
            params = self._engine_params(providers)
        self.assertIs(params['EngineConfig.onnxruntime.use_cuda'], True)

    def test_reset_bumps_generation_and_reader_rebuilds_engine(self):
        before = screen.engine_generation()
        screen.reset_ocr_engines()
        self.assertEqual(screen.engine_generation(), before + 1)
        reader = screen.ScreenReader(Mock())
        reader._engine = 'stale-engine'
        reader._engine_generation = screen.engine_generation() - 1
        with patch.object(screen, 'local_ocr_engine', return_value='new-engine') as renewed:
            # Mirror the lazy resolution in ScreenReader._ocr_tile.
            if reader._engine is None or reader._engine_generation != screen.engine_generation():
                reader._engine = screen.local_ocr_engine()
                reader._engine_generation = screen.engine_generation()
        renewed.assert_called_once()
        self.assertEqual(reader._engine, 'new-engine')


class PreferencesHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'MIAO_DATA_DIR': self.tmp.name}, clear=False)
        self.env.start()
        preferences.reset_preferences_cache()
        self.addCleanup(preferences.reset_preferences_cache)
        screen.reset_ocr_engines()
        self.addCleanup(screen.reset_ocr_engines)
        self.hub = _Hub()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.hub, 'test-token'))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def get(self, path):
        with urlopen(self.base + path, timeout=5) as response:
            return response.status, json.load(response)

    def post(self, path, payload, token='test-token'):
        request = Request(self.base + path, data=json.dumps(payload).encode(),
                          headers={'Content-Type': 'application/json', 'X-Miao-Token': token})
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_get_returns_defaults_and_capability_flags(self):
        status, body = self.get('/api/preferences')
        self.assertEqual(status, 200)
        self.assertEqual(body['download_proxy'], '')
        self.assertEqual(body['ocr_device'], 'auto')
        self.assertIn('cuda_available', body)
        self.assertIsInstance(body['ocr_providers'], list)

    def test_post_persists_and_returns_updated_values(self):
        status, body = self.post('/api/preferences',
                                 {'download_proxy': 'off', 'ocr_device': 'cpu'})
        self.assertEqual(status, 200)
        self.assertEqual(body['download_proxy'], 'off')
        self.assertEqual(body['ocr_device'], 'cpu')
        _, again = self.get('/api/preferences')
        self.assertEqual(again['ocr_device'], 'cpu')

    def test_invalid_proxy_returns_400(self):
        status, body = self.post('/api/preferences', {'download_proxy': 'socks5://x:1'})
        self.assertEqual(status, 400)
        self.assertIn('代理', body['error'])

    def test_post_requires_token(self):
        status, _ = self.post('/api/preferences', {'ocr_device': 'cpu'}, token='wrong')
        self.assertEqual(status, 403)


if __name__ == '__main__':
    unittest.main()


class SharedPreferenceRegressionTests(unittest.TestCase):
    def test_network_updates_preserve_task_words_branch_and_other_settings(self):
        from backend.core.storage.preferences import save_preferences as save_task,read_preferences
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ,{'MIAO_DATA_DIR':folder}):
            save_task(config={'skip_task_keywords':['custom'],'default_browse_seconds':17},activity_branch='dev')
            preferences.save_preferences({'ocr_device':'cuda'})
            saved=read_preferences()
            self.assertEqual(saved['config']['skip_task_keywords'],['custom'])
            self.assertEqual(saved['activity_branch'],'dev')
            save_task(config={'skip_task_keywords':[]})
            self.assertEqual(preferences.load_preferences()['ocr_device'],'cuda')
            self.assertEqual(read_preferences()['config']['skip_task_keywords'],[])
