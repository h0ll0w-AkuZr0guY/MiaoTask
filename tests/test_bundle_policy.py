"""Framework releases must not accidentally freeze optional app code or data."""
import hashlib
import json
import importlib
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
import zipfile

from scripts.bundle_policy import application_data, bundle_policy, include_module, write_policy
from scripts.smoke_packaged import synthetic_activity_package


class BundlePolicyTests(unittest.TestCase):
    def source_tree(self, root):
        for name in ('backend/app/common/models/task.py', 'backend/app/sample/manifest.py',
                     'backend/app/sample/configs/base.json',
                     'backend/app/sample/activities/daily/manifest.py',
                     'backend/app/sample/activities/daily/config.json',
                     'backend/app/sample/activities/daily/activity.py'):
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('{}' if target.suffix == '.json' else '# fixture', encoding='utf-8')

    def test_framework_module_filter_preserves_common_and_core_only(self):
        for module in ('backend', 'backend.app', 'backend.core.execution.activity',
                       'backend.app.common', 'backend.app.common.models.task'):
            self.assertTrue(include_module(module), module)
        for module in ('backend.app.sample', 'backend.app.sample.activity', 'backend.app.commonish'):
            self.assertFalse(include_module(module), module)
            self.assertTrue(include_module(module, True), module)

    def test_framework_data_excludes_app_manifests_and_configuration(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            self.source_tree(root)
            self.assertEqual(application_data(root), [])
            full = application_data(root, True)
            self.assertEqual(len(full), 4)
            self.assertFalse(any(source.endswith('activity.py') for source, _ in full))

    def test_policy_records_mode_and_excluded_names_without_importing_apps(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            self.source_tree(root)
            (root / 'backend/app/sample/manifest.py').write_text('raise RuntimeError("must not import")')
            policy = bundle_policy(root)
            self.assertEqual(policy['bundle_mode'], 'framework')
            self.assertEqual(policy['excluded_app_modules'], ['backend.app.sample'])
            self.assertEqual(policy['embedded_activity_modules'], [])
            full = bundle_policy(root, True)
            self.assertEqual(full['excluded_app_modules'], [])
            self.assertEqual(full['embedded_activity_modules'], [{'app_module': 'sample', 'activity_module': 'daily'}])
            written = write_policy(root / 'build/bundle-policy.json', root)
            self.assertEqual(json.loads(written.read_text(encoding='utf-8')), policy)

    def test_empty_source_tree_has_a_valid_framework_policy(self):
        with tempfile.TemporaryDirectory() as work:
            policy = bundle_policy(Path(work))
            self.assertEqual(policy['embedded_activity_modules'], [])
            self.assertEqual(policy['excluded_app_modules'], [])

    def test_frozen_import_fixture_has_complete_member_digests(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / 'fixture.zip'
            digest = synthetic_activity_package(path)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
            with zipfile.ZipFile(path) as archive:
                manifest = json.loads(archive.read('manifest.json'))
                self.assertEqual(set(archive.namelist()), {'manifest.json', *manifest['files']})
                for name, expected in manifest['files'].items():
                    self.assertEqual(hashlib.sha256(archive.read(name)).hexdigest(), expected)
                activity = archive.read('backend/app/smokefixture/activities/daily/activity.py').decode()
                self.assertIn('backend.app.common.models.task', activity)
                self.assertIn('must not execute', activity)

    def test_smoke_fixture_installs_and_constructs_through_real_public_contracts(self):
        from backend.core.config.loader import load_resolved
        from backend.core.plugins.manager import PluginManager
        from backend.core.registry import Registry
        package = importlib.import_module('backend.app')
        old_paths = list(package.__path__)
        old_plugin_paths = getattr(package, '_miaotask_plugin_paths', ())
        try:
            with tempfile.TemporaryDirectory() as work:
                root = Path(work)
                archive = root / 'fixture.zip'
                digest = synthetic_activity_package(archive)
                manager = PluginManager(root / 'plugins')
                manager.install_local(archive, digest)
                registry = Registry(root / 'empty', plugin_dir=manager.apps_dir).discover()
                registration = registry.get('smokefixture', 'daily')
                config = load_resolved(registration).mutable()
                runner = registry.instantiate(SimpleNamespace(device_type='uiautomator2', selected_instance=None),
                                              config, app='smokefixture', activity='daily')
                self.assertEqual(type(runner).__name__, 'OfflineActivity')
                self.assertEqual(runner.config['name'], 'Offline import check')
        finally:
            for name in list(sys.modules):
                if name.startswith('backend.app.smokefixture'):
                    del sys.modules[name]
            package.__path__ = old_paths
            package._miaotask_plugin_paths = old_plugin_paths
            importlib.invalidate_caches()


if __name__ == '__main__':
    unittest.main()
