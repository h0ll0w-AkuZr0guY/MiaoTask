import json
from pathlib import Path
import re
import shutil
import tempfile
import unittest

from scripts.package_windows import copy_documentation, PORTABLE_DOCS
from scripts.release_notes import extract_notes, ROOT


class ReleaseNotesTests(unittest.TestCase):
    def test_only_requested_version_is_extracted_including_subheadings(self):
        text = '# Releases\r\n\r\n## v2\r\nNew behavior\r\n### Details\r\nKeep these\r\n\r\n## v1\r\nOld behavior\r\n'
        body = extract_notes(text, 'v2')
        self.assertIn('New behavior', body)
        self.assertIn('### Details', body)
        self.assertNotIn('Old behavior', body)
        self.assertEqual(extract_notes(text, 'v1'), 'Old behavior\n')
        branded = '## MiaoTask-v0.0.90\nOther release\n## MiaoTask-v0.0.9\nRequested release\n'
        self.assertEqual(extract_notes(branded, 'MiaoTask-v0.0.9'), 'Requested release\n')

    def test_missing_duplicate_empty_and_invalid_versions_are_rejected(self):
        cases = [
            ('## v2\nNew\n', 'v1'),
            ('## v1\nFirst\n## v1\nSecond\n', 'v1'),
            ('## v1\n\n## v2\nOther\n', 'v1'),
            ('## v1\nBody\n', '../v1'),
        ]
        for text, version in cases:
            with self.subTest(version=version), self.assertRaises(ValueError):
                extract_notes(text, version)

    def test_configured_version_has_reviewed_notes_or_can_prepare_pending_contributions(self):
        metadata = json.loads((ROOT / 'version.json').read_text(encoding='utf-8'))
        version = metadata['version']
        self.assertRegex(version, r'^MiaoTask-v\d+\.\d+\.\d+$')
        self.assertEqual(version, metadata['app_version'])
        history = (ROOT / 'docs/RELEASES.md').read_text(encoding='utf-8')
        if not re.search(r'^## ' + re.escape(version) + r'\s*$', history, re.M):
            from scripts.prepare_release import prepare
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root / 'docs').mkdir()
                for name in ('RELEASES.md', 'UNRELEASED.md'):
                    shutil.copy2(ROOT / 'docs' / name, root / 'docs' / name)
                shutil.copy2(ROOT / 'version.json', root / 'version.json')
                prepare(root, version, 'MiaoTask 待审阅更新。')
                history = (root / 'docs/RELEASES.md').read_text(encoding='utf-8')
        notes = extract_notes(history, version)
        self.assertIn('MiaoTask', notes)


class PortableDocsTests(unittest.TestCase):
    def test_packaged_help_uses_current_docs_and_all_local_links_resolve(self):
        with tempfile.TemporaryDirectory() as work:
            destination = Path(work)
            copy_documentation(destination)
            self.assertEqual({p.name for p in (destination / 'docs').iterdir()}, set(PORTABLE_DOCS))
            for name in PORTABLE_DOCS:
                self.assertEqual((destination / 'docs' / name).read_bytes(), (ROOT / 'docs' / name).read_bytes())
            for file in destination.rglob('*.md'):
                for link in re.findall(r'\]\(([^\s)]+)\)', file.read_text(encoding='utf-8')):
                    if '://' not in link and not link.startswith('#'):
                        self.assertTrue((file.parent / link.split('#')[0]).is_file(), (file.name, link))

# release_preparation
"""Review preparation is separate from building and publishing."""
import json
from pathlib import Path
import tempfile
import unittest
import yaml
from scripts.prepare_release import prepare, pending_entries, release_ready
from scripts.release_notes import extract_notes


class ReleasePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'docs').mkdir()
        (self.root / 'version.json').write_text(json.dumps({'version':'MiaoTask-v0.0.9','app_version':'MiaoTask-v0.0.9'}))
        (self.root / 'docs/UNRELEASED.md').write_text('# 待发布贡献\n\n## 待发布\n\n* 【修复】合成问题 (#10)\n* 【新增】合成能力 (#11)\n', encoding='utf-8')
        (self.root / 'docs/RELEASES.md').write_text('# 发布记录\n\n说明。\n\n## MiaoTask-v0.0.9\n\n旧说明。\n', encoding='utf-8')

    def test_rollup_keeps_history_resets_accumulator_and_updates_both_versions(self):
        prepare(self.root, 'MiaoTask-v0.1.0', '合成更新。')
        history = (self.root / 'docs/RELEASES.md').read_text(encoding='utf-8')
        self.assertIn('合成能力 (#11)', extract_notes(history, 'MiaoTask-v0.1.0'))
        self.assertEqual(extract_notes(history, 'MiaoTask-v0.0.9'), '旧说明。\n')
        self.assertEqual(pending_entries((self.root / 'docs/UNRELEASED.md').read_text(encoding='utf-8')), [])
        metadata = json.loads((self.root / 'version.json').read_text())
        self.assertEqual(metadata['version'], metadata['app_version'])
        self.assertEqual(metadata['version'], 'MiaoTask-v0.1.0')
        plan = json.loads((self.root / '.github/release-plan.json').read_text())
        self.assertEqual(plan['previous_version'], 'MiaoTask-v0.0.9')
        self.assertEqual(plan['contributions'], 2)
        self.assertEqual(release_ready(self.root, 'MiaoTask-v0.0.9'), 'MiaoTask-v0.1.0')
        with self.assertRaises(ValueError):
            release_ready(self.root, 'MiaoTask-v0.0.8')

    def test_invalid_existing_and_backwards_version_do_not_change_files(self):
        paths = list(self.root.rglob('*.md')) + [self.root / 'version.json']
        before = {path:path.read_bytes() for path in paths}
        for version in ('MiaoTask-v0.0.9', 'MiaoTask-v0.0.8', 'v0.1.0', 'MiaoTask-v1.0.0'):
            with self.assertRaises(ValueError):
                prepare(self.root, version, '更新')
            self.assertEqual(before, {path:path.read_bytes() for path in paths})

    def test_invalid_pending_entries_and_empty_release_fail(self):
        with self.assertRaises(ValueError):
            pending_entries('## 待发布\n* unrelated text\n')
        (self.root / 'docs/UNRELEASED.md').write_text('## 待发布\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            prepare(self.root, 'MiaoTask-v0.1.0', '更新')

    def test_only_prepared_main_versions_can_automatically_build_and_publish(self):
        folder = Path(__file__).resolve().parents[1] / '.github/workflows'
        # BaseLoader preserves YAML's `on` key instead of YAML 1.1 booleans.
        for name in ('prepare-release.yml', 'release.yml', 'publish-release.yml'):
            workflow = yaml.load((folder / name).read_text(encoding='utf-8'), Loader=yaml.BaseLoader)
            self.assertNotIn('pull_request', workflow['on'])
            self.assertNotIn('push', workflow['on'])
        pipeline = yaml.load((folder / 'release-pipeline.yml').read_text(encoding='utf-8'), Loader=yaml.BaseLoader)
        self.assertEqual(pipeline['on']['push']['branches'], ['main'])
        self.assertEqual(pipeline['on']['push']['paths'], ['version.json'])
        self.assertNotIn('tags', pipeline['on']['push'])
        self.assertIn('needs', pipeline['jobs']['publish'])
        publish = yaml.load((folder / 'publish-release.yml').read_text(encoding='utf-8'), Loader=yaml.BaseLoader)
        self.assertEqual(publish['jobs']['publish']['environment'], 'release')
        ci = (folder / 'ci.yml').read_text(encoding='utf-8')
        self.assertNotIn('package_windows.py', ci)
        self.assertNotIn('gh release', ci)
        self.assertNotIn('contents: write', ci)
        self.assertNotIn('pull_request_target', ci)
        self.assertIn('persist-credentials: false', ci)
        self.assertIn('check_contribution.py', ci)

    def test_missing_stale_marker_or_uncollected_entries_cannot_release(self):
        with self.assertRaises(FileNotFoundError):
            release_ready(self.root)
        prepare(self.root, 'MiaoTask-v0.1.0', '合成更新。')
        pending = self.root / 'docs/UNRELEASED.md'
        pending.write_text('## 待发布\n\n* 【新增】后来贡献 (#20)\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            release_ready(self.root)

# preinstalled_selection
import unittest
from scripts.package_windows import selected_packages


class PreinstalledSelectionTests(unittest.TestCase):
    def test_release_selection_excludes_other_source_activities(self):
        catalog = {'packages': [dict(app='sample', activity=name) for name in ('daily', 'seasonal')]}
        self.assertEqual(selected_packages(catalog, ['sample/seasonal']), [catalog['packages'][1]])
        self.assertEqual(selected_packages(catalog, []), [])

    def test_unknown_and_repeated_selections_are_rejected(self):
        catalog = {'packages': [dict(app='sample', activity='daily')]}
        for choices in (['missing/daily'], ['sample/daily', 'sample/daily']):
            with self.assertRaises(ValueError):
                selected_packages(catalog, choices)

class AllPreinstalledSelectionTests(unittest.TestCase):
    def test_every_catalog_item_and_exclusive_choices(self):
        catalog={'packages':[{'app':'sample','activity':'a','bundle_by_default':True},{'app':'sample','activity':'b','bundle_by_default':True}]}
        self.assertEqual(selected_packages(catalog,[],True),catalog['packages'])
        with self.assertRaises(ValueError):
            selected_packages(catalog,['sample/a'],True)

# preview_build
"""Local candidates may use dirty code but cannot bypass publication hygiene."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import package_windows as build


class PreviewBuildTests(unittest.TestCase):
    def test_normal_build_refuses_dirty_source_before_running_tools(self):
        with patch.object(build.subprocess, 'check_output', side_effect=['a'*40, ' M backend/main.py\n']), \
                patch.object(build.subprocess, 'run') as command:
            with self.assertRaisesRegex(RuntimeError, 'clean source'):
                build.source_commit()
        command.assert_not_called()

    def test_preview_snapshot_includes_new_source_and_rejects_private_nonignored_files(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(build, 'ROOT', Path(folder)):
            source = Path(folder) / 'docs/new.md'
            source.parent.mkdir()
            source.write_text('first', encoding='utf-8')
            with patch.object(build.subprocess, 'check_output', return_value=b'docs/new.md\0'):
                initial = build.worktree_snapshot()
                source.write_text('changed', encoding='utf-8')
                self.assertNotEqual(initial, build.worktree_snapshot())
            private = Path(folder) / '.env'
            private.write_text('local settings', encoding='utf-8')
            with patch.object(build.subprocess, 'check_output', return_value=b'.env\0'):
                with self.assertRaisesRegex(RuntimeError, 'Public source audit failed'):
                    build.worktree_snapshot()

    def test_local_source_snapshot_never_collects_downloaded_activities(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(build, 'ROOT', Path(folder)):
            source = Path(folder) / 'main.py'
            source.write_text('print("fixture")')
            expected = build.worktree_snapshot(local_source=True)
            downloaded = Path(folder) / 'activities/apps/sample/secret.py'
            downloaded.parent.mkdir(parents=True)
            downloaded.write_text('raise AssertionError("must not package installed code")')
            self.assertEqual(expected, build.worktree_snapshot(local_source=True))

# package_licenses
"""Distribution notices must survive packaging regardless of wheel layout."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.package_windows import copy_documentation, licenses, ROOT


class PackageLicenseTests(unittest.TestCase):
    def distribution(self, root, name, files, version='1.0'):
        for relative in files:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('Original notice: ' + relative, encoding='utf-8')
        return SimpleNamespace(metadata={'Name': name}, files=files,
                               version=version, locate_file=lambda path: root / path)

    def test_package_local_and_british_spelled_notices_are_retained(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            dists = [
                self.distribution(root, 'onnxruntime',
                                  ['onnxruntime/LICENSE', 'onnxruntime/ThirdPartyNotices.txt']),
                self.distribution(root, 'tqdm', ['tqdm-1.0.dist-info/licenses/LICENCE']),
            ]
            (root / 'NOTICE.txt').write_text('ADB notice', encoding='utf-8')
            with patch('scripts.package_windows.metadata.distributions', return_value=dists):
                licenses(root / 'output', root)
            for dist in dists:
                for relative in dist.files:
                    self.assertEqual((root / 'output' / dist.metadata['Name'] / relative).read_bytes(),
                                     (root / relative).read_bytes())
            self.assertEqual((root / 'output/Android-platform-tools-NOTICE.txt').read_text(), 'ADB notice')

    def test_project_license_scope_is_present_in_help_and_licenses(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            copy_documentation(root)
            (root / 'NOTICE.txt').write_text('ADB notice', encoding='utf-8')
            with patch('scripts.package_windows.metadata.distributions', return_value=[]):
                licenses(root / 'licenses', root)
            for name in ('LICENSE', 'NOTICE'):
                for target in (root / name, root / 'licenses/MiaoTask' / name):
                    self.assertEqual(target.read_bytes(), (ROOT / name).read_bytes())
            self.assertEqual((root / 'docs/LICENSING.md').read_bytes(),
                             (ROOT / 'docs/LICENSING.md').read_bytes())

    def test_flatbuffers_notice_is_version_bound_and_keeps_copyright(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            (root / 'NOTICE.txt').write_text('ADB notice', encoding='utf-8')
            dist = self.distribution(root, 'flatbuffers', [], version='25.12.19')
            with patch('scripts.package_windows.metadata.distributions', return_value=[dist]):
                licenses(root / 'output', root)
            self.assertEqual((root / 'output/flatbuffers/LICENSE').read_bytes(), (ROOT / 'LICENSE').read_bytes())
            notice = (root / 'output/flatbuffers/NOTICE.txt').read_text(encoding='utf-8')
            self.assertIn('Copyright 2014 Google Inc.', notice)
            self.assertIn('/v25.12.19/', notice)

    def test_new_flatbuffers_without_a_license_requires_review(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            dist = self.distribution(root, 'flatbuffers', [], version='99.0.0')
            with patch('scripts.package_windows.metadata.distributions', return_value=[dist]):
                with self.assertRaisesRegex(RuntimeError, 'Review the FlatBuffers license'):
                    licenses(root / 'output', root)

    def test_license_directory_does_not_copy_code_bytecode_or_binaries(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            declaration_files = [
                'packaging-1.0.dist-info/licenses/LICENSE',
                'packaging-1.0.dist-info/licenses/LICENSE.APACHE',
                'packaging-1.0.dist-info/licenses/AUTHORS.txt',
                'rapidocr-1.0.dist-info/licenses/MODEL_LICENSES.md',
                'runtime/COPYING', 'runtime/ThirdPartyNotices.txt',
            ]
            excluded = [
                'packaging/licenses/__init__.py',
                'packaging/licenses/_spdx.py',
                'packaging/licenses/__pycache__/__init__.cpython-313.pyc',
                'pip/_vendor/packaging/licenses/_spdx.py',
                'packaging-1.0.dist-info/licenses/helper.py',
                'packaging-1.0.dist-info/licenses/README.md',
                'runtime/LICENSE.py', 'runtime/LICENSE.pyd',
                'runtime/LICENSE.dll', 'runtime/NOTICE.exe',
                'runtime/COPYING.pyc', 'runtime/LICENSE.txt',
            ]
            dist = self.distribution(root, 'sample', declaration_files + excluded)
            (root / 'runtime/LICENSE.txt').write_bytes(b'MZ\0Binary payload')
            (root / 'NOTICE.txt').write_text('ADB notice', encoding='utf-8')
            with patch('scripts.package_windows.metadata.distributions', return_value=[dist]):
                licenses(root / 'output', root)
            copied = {p.relative_to(root / 'output/sample').as_posix()
                      for p in (root / 'output/sample').rglob('*') if p.is_file()}
            self.assertEqual(copied, set(declaration_files))

# bundle_policy
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



class DefaultBundleFlags(unittest.TestCase):
    def test_packaging_uses_activity_bool(self):
        from scripts.package_windows import selected_packages
        yes = dict(app='sample',activity='yes',bundle_by_default=True)
        no = dict(app='sample',activity='no',bundle_by_default=False)
        self.assertEqual(selected_packages({'packages':[yes,no]},[],True),[yes])
