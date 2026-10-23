"""Offline package integrity, namespace and independent activity lifecycle tests."""
import copy
import hashlib
import importlib
import json
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from backend.core.config.loader import load_config, load_framework_config, load_resolved
from backend.core.plugins.contracts import FRAMEWORK_API, OFFICIAL_REPOSITORY
from backend.core.plugins.manager import PluginManager, official_asset_url, reload_plugin_registry, validate_archive
from backend.core.registry import Registry
from backend.core.runtime import ROOT


def package_fixture(folder, activity='daily', *, extra=None, shared='original', version='0.1.1', mutate=None):
    base = 'backend/app/sample/'
    active = base + f'activities/{activity}/'
    files = {
        base + '__init__.py': b'',
        base + 'manifest.py': ("from backend.core.registry import AppMeta\nfrom backend.core.device.profile import ApplicationProfile\n"
                              "APP=AppMeta('sample','Sample',ApplicationProfile('Sample','org.example.sample',r'org\\.example\\.sample'))\n").encode(),
        base + 'configs/base.json': b'{}',
        base + 'utils/__init__.py': b'',
        base + 'utils/value.py': ('VALUE=' + repr(shared) + '\n').encode(),
        base + 'activities/__init__.py': b'',
        active + '__init__.py': b'',
        active + 'manifest.py': ("from backend.core.registry import ActivityMeta\n"
                                f"META=ActivityMeta('sample','{activity}','Daily','backend.app.sample.activities.{activity}.activity:Runner')\n").encode(),
        active + 'config.json': b'{"search_keyword":"sample","entry_labels":["Sample"],"task_entry_labels":["Tasks"]}',
        active + 'activity.py': ("from backend.app.sample.utils.value import VALUE\n"
                                  "class Runner:\n    def __init__(self,device,config,*args,**kwargs):\n        self.marker=VALUE\n").encode(),
    }
    files.update(extra or {})
    manifest = dict(schema=1, framework_api=FRAMEWORK_API,
                    python=f'{sys.version_info.major}.{sys.version_info.minor}', app='sample', activity=activity,
                    app_module='sample', activity_module=activity, version=version, label='Daily', app_label='Sample',
                    files={name: hashlib.sha256(content).hexdigest() for name, content in files.items()})
    if mutate:
        mutate(manifest, files)
    path = Path(folder) / (activity + '.zip')
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json', json.dumps(manifest).encode())
        for name, content in files.items():
            archive.writestr(name, content)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


class ActivityDistributionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manager = PluginManager(self.root / 'plugins')
        self.package = importlib.import_module('backend.app')
        self.original_path = list(self.package.__path__)
        self.original_marker = getattr(self.package, '_miaotask_plugin_paths', ())

    def tearDown(self):
        for name in list(sys.modules):
            if name == 'backend.app.sample' or name.startswith('backend.app.sample.'):
                sys.modules.pop(name, None)
        self.package.__path__ = self.original_path
        self.package._miaotask_plugin_paths = self.original_marker
        importlib.invalidate_caches()
        self.temporary.cleanup()

    def make(self, *args, **kwargs):
        return package_fixture(self.root, *args, **kwargs)

    def install(self, *args, **kwargs):
        path, digest = self.make(*args, **kwargs)
        return self.manager.install_local(path, digest)

    def test_package_installs_and_factory_imports_absolute_app_utils(self):
        self.install()
        registry = Registry(self.root / 'empty', plugin_dir=self.manager.apps_dir).discover()
        registration = registry.get('sample', 'daily')
        self.assertEqual(registration.factory()(None, {}).marker, 'original')
        config = load_resolved(registration)
        self.assertEqual(config.sources['max_tasks'], 'core')
        self.assertEqual(self.manager.installed()[0]['activity'], 'daily')

    def test_framework_only_config_works_without_fabricating_an_activity(self):
        registry = Registry(self.root / 'empty').discover()
        config = load_config(registry=registry, overrides={'max_tasks': 3})
        self.assertEqual(config['max_tasks'], 3)
        self.assertEqual(config['entry_labels'], ['活动'])
        self.assertEqual(config['search_keyword'], '')
        self.assertFalse(registry.has_activities)
        self.assertEqual(load_framework_config()['default_browse_seconds'], 15)
        with self.assertRaises(ValueError):
            load_config(app='missing', registry=registry)

    def test_two_selected_activities_share_app_helpers_and_uninstall_independently(self):
        self.install('daily')
        self.install('weekly')
        registry = Registry(self.root / 'empty', plugin_dir=self.manager.apps_dir).discover()
        self.assertEqual({a['id'] for a in registry.catalog()[0]['activities']}, {'daily', 'weekly'})
        self.manager.uninstall('sample', 'daily')
        self.assertTrue((self.manager.apps_dir / 'sample/utils/value.py').exists())
        registry.discover()
        self.assertEqual(registry.catalog()[0]['activities'][0]['id'], 'weekly')
        self.manager.uninstall('sample', 'weekly')
        self.assertFalse(registry.discover().has_activities)
        self.assertEqual(self.manager.installed(), [])

    def test_shared_helper_change_cannot_silently_break_other_activity(self):
        self.install('daily')
        self.install('weekly')
        with self.assertRaisesRegex(ValueError, '共享组件'):
            self.install('daily', shared='changed', version='0.1.2')
        self.assertEqual((self.manager.apps_dir / 'sample/utils/value.py').read_text(), "VALUE='original'\n")
        self.assertEqual(len(self.manager.installed()), 2)

    def test_upgrade_replaces_activity_without_erasing_other_activity(self):
        self.install('daily')
        self.install('weekly')
        self.install('daily', version='0.1.2')
        self.assertEqual({p['activity']: p['version'] for p in self.manager.installed()}, {'weekly': '0.1.1', 'daily': '0.1.2'})

    def test_upgrade_drops_removed_shared_modules_instead_of_leaving_old_files(self):
        self.install(extra={'backend/app/sample/utils/obsolete.py': b'VALUE=1\n'})
        self.install(version='0.1.2')
        self.assertFalse((self.manager.apps_dir / 'sample/utils/obsolete.py').exists())

    def test_committed_upgrade_succeeds_when_old_windows_backup_cannot_be_removed(self):
        self.install()
        original = shutil.rmtree

        def temporarily_locked(path, *args, **kwargs):
            if Path(path).name.startswith('rollback-'):
                if kwargs.get('ignore_errors'):
                    return
                raise PermissionError('synthetic old imported file is locked')
            return original(path, *args, **kwargs)

        with patch('backend.core.plugins.manager.shutil.rmtree', side_effect=temporarily_locked):
            result = self.install(version='0.1.2')
        self.assertEqual(result['version'], '0.1.2')
        self.assertEqual(self.manager.installed()[0]['version'], '0.1.2')

    def test_reload_only_preserves_exact_common_namespace_and_drops_app_attributes(self):
        previous_modules = {name: module for name, module in list(sys.modules.items()) if name.startswith('backend.app.')}
        previous_attributes = {name.split('.')[2]: getattr(self.package, name.split('.')[2])
                               for name in previous_modules if hasattr(self.package, name.split('.')[2])}
        common_name = 'backend.app.common.synthetic_distribution_marker'
        app_name = 'backend.app.common_tools'
        common = types.ModuleType(common_name)
        stale = types.ModuleType(app_name)
        sys.modules[common_name] = common
        sys.modules[app_name] = stale
        self.package.common_tools = stale
        try:
            with patch('backend.core.registry.default_registry') as default:
                reload_plugin_registry()
            self.assertIs(sys.modules.get(common_name), common)
            self.assertNotIn(app_name, sys.modules)
            self.assertFalse(hasattr(self.package, 'common_tools'))
            default.cache_clear.assert_called_once()
        finally:
            sys.modules.pop(common_name, None)
            sys.modules.pop(app_name, None)
            if hasattr(self.package, 'common_tools'):
                delattr(self.package, 'common_tools')
            sys.modules.update(previous_modules)
            for name, module in previous_attributes.items():
                setattr(self.package, name, module)

    def test_failed_state_write_rolls_back_original_app_and_install_record(self):
        self.install()
        before = self.manager.state_path.read_bytes()
        path, digest = self.make(shared='new', version='0.1.2')
        with patch('backend.core.plugins.manager._atomic_json', side_effect=OSError('simulated full disk')):
            with self.assertRaises(OSError):
                self.manager.install_local(path, digest)
        self.assertEqual(before, self.manager.state_path.read_bytes())
        self.assertEqual((self.manager.apps_dir / 'sample/utils/value.py').read_text(), "VALUE='original'\n")

    def test_broken_factory_import_is_rejected_without_losing_existing_install(self):
        self.install()
        before = self.manager.state_path.read_bytes()
        path, digest = self.make(version='0.1.2', extra={
            'backend/app/sample/activities/daily/activity.py': b'raise RuntimeError("dependency missing")\n'})
        with self.assertRaisesRegex(RuntimeError, 'dependency missing'):
            self.manager.install_local(path, digest)
        self.assertEqual(before, self.manager.state_path.read_bytes())
        registry = Registry(self.root / 'empty', plugin_dir=self.manager.apps_dir).discover()
        self.assertEqual(registry.get().factory()(None, {}).marker, 'original')

    def test_digest_mismatch_never_writes_or_imports(self):
        path, _ = self.make()
        with self.assertRaisesRegex(ValueError, '校验失败'):
            self.manager.install_local(path, '0' * 64)
        self.assertFalse(self.manager.state_path.exists())
        self.assertNotIn('backend.app.sample', sys.modules)

    def test_archive_cannot_include_other_activity_or_core(self):
        for name in ('../escape.py', '/absolute.py', 'backend/core/registry.py',
                     'backend/app/sample/activities/other/manifest.py', 'backend/app/sample/CON.txt',
                     'backend/app/sample/unsafe.exe', 'backend\\app\\sample\\escape.py'):
            with self.subTest(name=name):
                path, digest = self.make(extra={name: b'bad'})
                with self.assertRaises(ValueError):
                    validate_archive(path, digest)

    def test_zip_symlink_is_rejected(self):
        path, _ = self.make()
        with zipfile.ZipFile(path, 'a') as archive:
            info = zipfile.ZipInfo('backend/app/sample/link.py')
            info.external_attr = ((stat.S_IFLNK | 0o777) << 16)
            archive.writestr(info, '../core')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, '不安全'):
            validate_archive(path, digest)

    def test_case_collisions_and_zip_bombs_are_rejected(self):
        for extra in ({'backend/app/sample/MANIFEST.py': b'x'}, {'backend/app/sample/data.txt': b'0' * 2_000_000}):
            path, digest = self.make(extra=extra)
            with self.assertRaises(ValueError):
                validate_archive(path, digest)

    def test_manifest_tampering_or_wrong_identity_is_rejected(self):
        mutations = [lambda m, f: m.update(framework_api=999), lambda m, f: m.update(python='0.0'),
                     lambda m, f: m.update(app='other'), lambda m, f: m.update(activity='other'),
                     lambda m, f: m['files'].clear(),
                     lambda m, f: f.update({'backend/app/sample/manifest.py': b'raise RuntimeError("must not run")'})]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                path, digest = self.make(mutate=mutate)
                with self.assertRaises(ValueError):
                    validate_archive(path, digest)

    def test_python_manifest_identity_and_factory_are_checked_before_import(self):
        for replacement in ("META=ActivityMeta('other','daily','Daily','backend.app.sample.activities.daily.activity:Runner')",
                            "META=ActivityMeta('sample','daily','Daily','os:system')"):
            name = 'backend/app/sample/activities/daily/manifest.py'
            path, digest = self.make(extra={name: ('from backend.core.registry import ActivityMeta\n' + replacement).encode()})
            with self.assertRaisesRegex(ValueError, '工厂'):
                validate_archive(path, digest)

    def test_official_url_rejects_other_repositories_schemes_and_encoded_traversal(self):
        good = f'https://github.com/{OFFICIAL_REPOSITORY}/releases/download/activities-one/sample.zip'
        self.assertEqual(official_asset_url(good), good)
        for bad in (good.replace('https:', 'http:'), good.replace('github.com', 'github.com.evil.invalid'),
                    good.replace('MiaoTask/', 'other/'), good + '?token=abc', good.replace('sample.zip', '%2e%2e')):
            with self.subTest(url=bad), self.assertRaises(ValueError):
                official_asset_url(bad)

    def test_catalog_compatibility_and_cached_offline_feedback(self):
        self.manager = PluginManager(self.root / 'plugins', catalog_url='http://127.0.0.1:12345/catalog', allow_local=True)
        self.install()
        path, digest = self.make()
        entry = dict(app='sample', activity='daily', app_module='sample', activity_module='daily',
                     app_label='Sample', label='Daily', version='0.1.1', framework_api=FRAMEWORK_API,
                     python=f'{sys.version_info.major}.{sys.version_info.minor}', size=path.stat().st_size,
                     sha256=digest, url='http://127.0.0.1:12345/sample.zip')
        catalog = dict(schema=1, repository=OFFICIAL_REPOSITORY, packages=[entry])
        self.manager.cache_path.write_text(json.dumps(catalog), encoding='utf-8')
        result = self.manager.snapshot()
        self.assertTrue(result['packages'][0]['installed'])
        self.assertTrue(result['packages'][0]['compatible'])
        with patch.object(self.manager, '_fetch', side_effect=OSError('offline')):
            result = self.manager.snapshot(refresh=True)
        self.assertTrue(result['cached'])
        self.assertIn('offline', result['error'])
        self.assertEqual(result['catalog'][0]['activities'][0]['id'], 'daily')

    def test_download_install_only_uses_catalog_digest_and_explicit_ids(self):
        self.manager = PluginManager(self.root / 'plugins', catalog_url='http://127.0.0.1:12345/catalog', allow_local=True)
        path, digest = self.make()
        content = path.read_bytes()
        entry = dict(app='sample', activity='daily', app_module='sample', activity_module='daily', version='0.1.1',
                     framework_api=1, python=f'{sys.version_info.major}.{sys.version_info.minor}',
                     sha256=digest, size=len(content), url='http://127.0.0.1:12345/sample.zip')
        catalog = dict(schema=1, repository=OFFICIAL_REPOSITORY, packages=[entry])
        with patch.object(self.manager, '_fetch', side_effect=[json.dumps(catalog).encode(), content]):
            result = self.manager.install('sample', 'daily')
        self.assertEqual(result['sha256'], digest)

    def test_installed_app_overrides_same_builtin_module_without_loading_unselected_activity(self):
        self.install()
        builtin = self.root / 'builtin/sample'
        builtin.mkdir(parents=True)
        (builtin / 'manifest.py').write_text('raise RuntimeError("source version should not load")')
        registry = Registry(builtin.parent, plugin_dir=self.manager.apps_dir).discover()
        self.assertEqual(registry.get().app.id, 'sample')
        self.assertEqual(len(registry.catalog()[0]['activities']), 1)

    def test_local_test_source_requires_explicit_opt_in_and_same_loopback_origin(self):
        source = 'http://127.0.0.1:12345/catalog'
        with self.assertRaises(ValueError):
            PluginManager(self.root / 'normal', catalog_url=source)
        for value in ('http://localhost:12345/catalog', 'http://192.0.2.1:12345/catalog',
                      'https://127.0.0.1:12345/catalog', 'http://127.0.0.1/catalog',
                      'http://user@127.0.0.1:12345/catalog'):
            with self.subTest(source=value), self.assertRaises(ValueError):
                PluginManager(self.root / 'invalid', catalog_url=value, allow_local=True)
        manager = PluginManager(self.root / 'local', catalog_url=source, allow_local=True)
        with patch.object(manager, '_fetch', side_effect=OSError('offline')):
            self.assertTrue(manager.snapshot()['test_source'])

    def test_local_test_source_downloads_same_integrity_pipeline_and_rejects_redirect(self):
        path, digest = self.make()
        content = path.read_bytes()
        catalog = dict(schema=1, repository=OFFICIAL_REPOSITORY, packages=[])

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/redirect':
                    self.send_response(302)
                    self.send_header('Location', 'https://example.invalid/not-allowed')
                    self.end_headers()
                    return
                body = json.dumps(catalog).encode() if self.path == '/catalog' else content
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            origin = f'http://127.0.0.1:{server.server_port}'
            entry = dict(app='sample', activity='daily', app_module='sample', activity_module='daily', version='0.1.1',
                         framework_api=1, python=f'{sys.version_info.major}.{sys.version_info.minor}',
                         sha256=digest, size=len(content), url=origin + '/package.zip')
            catalog['packages'].append(entry)
            manager = PluginManager(self.root / 'local', catalog_url=origin + '/catalog', allow_local=True)
            self.assertEqual(manager.install('sample', 'daily')['sha256'], digest)
            self.assertTrue(manager.snapshot()['packages'][0]['installed'])
            with self.assertRaisesRegex(ValueError, '重定向'):
                manager._fetch(origin + '/redirect', 100)
            catalog['packages'][0]['url'] = origin.replace('127.0.0.1', 'localhost') + '/package.zip'
            self.assertIn('同一', manager.snapshot(refresh=True)['error'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


if __name__ == '__main__':
    unittest.main()
