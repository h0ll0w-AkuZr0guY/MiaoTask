"""Preview activity builds include new source and ignore developer installations."""
import importlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts.package_activities import build_packages
from test_activity_distribution import package_fixture


class ActivityPackageBuildTests(unittest.TestCase):
    def test_preview_includes_untracked_adapter_without_loading_installed_plugins(self):
        package = importlib.import_module('backend.app')
        previous_path = list(package.__path__)
        previous_marker = getattr(package, '_miaotask_plugin_paths', ())
        try:
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                subprocess.run(['git', 'init', '-q'], cwd=root, check=True, capture_output=True)
                subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                                'commit', '--allow-empty', '-qm', 'Fixture'], cwd=root, check=True, capture_output=True)
                fixture, _ = package_fixture(root)
                with zipfile.ZipFile(fixture) as source:
                    for name in source.namelist():
                        if name.startswith('backend/app/'):
                            source.extract(name, root)
                poison = root / 'private-data/plugins/apps/foreign'
                poison.mkdir(parents=True)
                (poison / 'manifest.py').write_text('raise AssertionError("must not load installed code")')
                with patch('backend.core.registry.activities_dir', return_value=root / 'private-data/plugins'):
                    catalog = build_packages(root, root/'dist', 'preview', '0.1.1', require_clean=False)
                self.assertEqual(len(catalog['packages']), 1)
                self.assertEqual(catalog['packages'][0]['app'], 'sample')
                self.assertEqual(catalog['packages'][0]['source_commit'], '')
                archive = root / 'dist' / catalog['packages'][0]['url'].rsplit('/', 1)[-1]
                with zipfile.ZipFile(archive) as result:
                    self.assertIn('backend/app/sample/utils/value.py', result.namelist())
                    self.assertFalse(any('foreign' in name for name in result.namelist()))
                with self.assertRaisesRegex(ValueError, '干净源码'):
                    build_packages(root, root/'dist', 'official', '0.1.1')
        finally:
            package.__path__ = previous_path
            package._miaotask_plugin_paths = previous_marker
            importlib.invalidate_caches()

# activity_site
"""A Pages artifact and a future mirrored copy use the same pinned contract."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile

from scripts.build_activity_site import build_site
from backend.core.plugins.resource_mirror import ResourceMirror
from test_activity_distribution import package_fixture


class ActivitySiteTests(unittest.TestCase):
    def test_generated_static_site_can_supply_selected_activity(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source';source.mkdir()
            (source/'.gitignore').write_text('__pycache__/\n',encoding='utf-8')
            archive,_=package_fixture(root)
            with zipfile.ZipFile(archive) as bundle:
                for name in bundle.namelist():
                    if name=='manifest.json':
                        continue
                    target=source/name;target.parent.mkdir(parents=True,exist_ok=True)
                    target.write_bytes(bundle.read(name))
            (source/'version.json').write_text(json.dumps({'version':'MiaoTask-v0.1.4'}),encoding='utf-8')
            for args in (['init','-q'],['add','.'],['-c','user.name=Synthetic','-c','user.email=test@example.invalid','commit','-qm','fixture']):
                subprocess.run(['git',*args],cwd=source,check=True,capture_output=True)
            output=root/'site';catalog=build_site(source,output)
            mirror=ResourceMirror(fetch=lambda url,limit:(output/url.split('/activities/',1)[1]).read_bytes())
            listed=mirror.catalog()
            self.assertEqual(listed['source_commit'],catalog['source_commit'])
            entry=listed['packages'][0]
            self.assertEqual(mirror.build_archive(entry,root/'selected.zip'),entry['sha256'])
            self.assertTrue((output/'index.html').is_file())
            build_site(source,output,'feature/开发')
            other=ResourceMirror(source_ref='feature/开发',fetch=lambda url,limit:(output/url.split('/activities/',1)[1]).read_bytes())
            self.assertEqual(other.catalog()['source_ref'],'feature/开发')
            self.assertEqual(other.catalog(commit=catalog['source_commit'])['source_ref'],'feature/开发')
