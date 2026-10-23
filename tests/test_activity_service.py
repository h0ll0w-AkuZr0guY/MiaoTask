"""Loopback installation lifecycle, authentication and all-device update gate."""
import hashlib
import importlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from backend.core.plugins.manager import PluginManager
from backend.core.registry import Registry, default_registry
from backend.core.service.devices import DeviceHub
from backend.core.service.panel import make_handler
from test_activity_distribution import package_fixture


class ActivityServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.source = self.folder / 'source'
        self.source.mkdir()
        archive, digest = package_fixture(self.source)
        self.digest = digest
        catalog = dict(schema=1, framework_api=1, repository='h0ll0w-AkuZr0guY/MiaoTask', packages=[dict(
            app='sample', activity='daily', app_module='sample', activity_module='daily',
            app_label='Sample', label='Daily', version='0.1.1', framework_api=1,
            python=f'{sys.version_info.major}.{sys.version_info.minor}',
            source_commit='a'*40, url='https://example.invalid/daily.zip', sha256=digest,
            size=archive.stat().st_size, device_types=['uiautomator2'])])
        (self.source / 'activity-catalog.json').write_text(json.dumps(catalog), encoding='utf-8')
        self.app_package = importlib.import_module('backend.app')
        self.old_path = list(self.app_package.__path__)
        self.old_marker = getattr(self.app_package, '_miaotask_plugin_paths', ())
        self.asset_patch = patch('backend.core.registry.assets_dir', return_value=self.folder / 'empty')
        self.asset_patch.start()
        registry = Registry(plugin_dir=self.folder / 'plugins/apps').discover()
        self.hub = DeviceHub(registry=registry)
        base_handler = make_handler(self.hub, 'test-token')
        fixture = self
        class FixtureHandler(base_handler):
            def do_GET(self):
                if self.path == '/fixture/catalog':
                    self.reply(200, fixture.catalog)
                elif self.path == '/fixture/daily.zip':
                    self.reply(200, (fixture.source / 'daily.zip').read_bytes(), 'application/zip')
                else:
                    super().do_GET()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.catalog = catalog
        self.catalog['packages'][0]['url'] = self.base + '/fixture/daily.zip'
        self.hub.plugin_manager = PluginManager(self.folder / 'plugins',
            catalog_url=self.base+'/fixture/catalog', allow_local=True)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def tearDown(self):
        if self.hub.plugin_thread:
            self.hub.plugin_thread.join(5)
        self.server.shutdown()
        self.server.server_close()
        self.asset_patch.stop()
        default_registry.cache_clear()
        for name in list(sys.modules):
            if name == 'backend.app.sample' or name.startswith('backend.app.sample.'):
                sys.modules.pop(name, None)
        self.app_package.__path__ = self.old_path
        self.app_package._miaotask_plugin_paths = self.old_marker
        importlib.invalidate_caches()
        self.temp.cleanup()

    def get(self, path):
        with urlopen(self.base+path, timeout=4) as response:
            return json.load(response)

    def post(self, path, payload, token='test-token'):
        request = Request(self.base+path, data=json.dumps(payload).encode(),
                          headers={'Content-Type':'application/json', 'X-Miao-Token':token})
        with urlopen(request, timeout=4) as response:
            return response.status, json.load(response)

    def test_empty_framework_installs_and_uninstalls_through_authenticated_http(self):
        self.assertEqual(self.get('/api/apps')['apps'], [])
        snapshot = self.get('/api/plugins?refresh=1')
        self.assertTrue(snapshot['test_source'])
        entry = self.get('/fixture/catalog')['packages'][0]
        self.assertTrue(entry['url'].startswith(self.base+'/fixture/'))
        with urlopen(entry['url']) as response:
            self.assertEqual(hashlib.sha256(response.read()).hexdigest(), self.digest)
        for action in ('install', 'uninstall'):
            self.assertEqual(self.post('/api/plugins/'+action, dict(app='sample', activity='daily'))[0], 202)
            self.hub.plugin_thread.join(5)
            self.assertFalse(self.hub.plugin_busy)
            self.assertEqual(self.hub.plugin_operation['status'], 'finished')
            state = self.get('/api/state')
            self.assertEqual(bool(state['catalog']), action == 'install')
            self.assertEqual(state['activity_plan'], [dict(app='sample', activity='daily')] if action == 'install' else [])
        self.assertFalse(self.hub.resolve().registry.has_activities)

    def test_update_requires_token_and_every_device_idle(self):
        with self.assertRaises(HTTPError) as denied:
            self.post('/api/plugins/install', dict(app='sample', activity='daily'), token='bad')
        self.assertEqual(denied.exception.code, 403)
        second = self.hub.add('Second')
        self.hub.resolve(second).thread = Mock(is_alive=lambda: True)
        with self.assertRaises(HTTPError) as busy:
            self.post('/api/plugins/install', dict(app='sample', activity='daily'))
        self.assertEqual(busy.exception.code, 400)
        self.assertFalse(self.hub.plugin_busy)
        self.hub.resolve(second).thread = None
        self.hub.plugin_busy = True
        with self.assertRaisesRegex(ValueError, '正在更新'):
            self.hub.resolve().dispatch('configure', {})
        with self.assertRaisesRegex(ValueError, '正在更新'):
            self.hub.add('Blocked')
        self.hub.plugin_busy = False

    def test_activity_folder_is_visible_and_opening_requires_token_and_ignores_client_path(self):
        snapshot = self.get('/api/plugins')
        self.assertEqual(Path(snapshot['activities_dir']), self.hub.plugin_manager.root)
        with patch('os.startfile', create=True) as opener:
            with self.assertRaises(HTTPError) as denied:
                self.post('/api/plugins/open-folder', {}, token='bad')
            self.assertEqual(denied.exception.code, 403)
            opener.assert_not_called()
            status, result = self.post('/api/plugins/open-folder', {'path': 'C:/unrelated'})
            self.assertEqual(status, 200)
            opener.assert_called_once_with(str(self.hub.plugin_manager.root))
            self.assertEqual(result['path'], str(self.hub.plugin_manager.root))

    def test_normal_polling_never_requests_catalog_and_test_server_cannot_read_other_files(self):
        with patch.object(self.hub.plugin_manager, 'snapshot', side_effect=AssertionError('unexpected network')):
            self.get('/api/state')
            self.get('/api/apps')
            self.get('/api/workspaces')
            self.post('/api/configure', dict(config={'max_tasks': 3}))
        for path in ('/api/plugins/test/files/activity-catalog.json', '/api/plugins/test/files/..%2Fsecret.zip'):
            with self.assertRaises(HTTPError) as missing:
                self.get(path)
            self.assertEqual(missing.exception.code, 404)


if __name__ == '__main__':
    unittest.main()
