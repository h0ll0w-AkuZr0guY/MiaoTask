"""A source ZIP need not be a Git checkout to produce reviewable release files."""
import json
import hashlib
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts.local_release import (REQUIRED, build, copy_source, extract_source,
                                  github_archive_url, rollup_local, plan_version,
                                  record_success, builder_lock)
from scripts import package_windows
from scripts import local_release
from scripts.smoke_packaged import synthetic_activity_package


def source_tree(root):
    root.mkdir(parents=True)
    for name in REQUIRED:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# source\n', encoding='utf-8')
    (root / 'scripts/package_windows.py').write_text('# supports --local-source\n', encoding='utf-8')
    (root / 'version.json').write_text(json.dumps({'version': 'MiaoTask-v0.1.1', 'app_version': 'MiaoTask-v0.1.1'}), encoding='utf-8')
    (root / 'docs').mkdir()
    (root / 'docs/UNRELEASED.md').write_text('# 贡献\n\n## 待发布\n\n* 【修复】合成问题 (#4)\n* 【新增】合成功能\n', encoding='utf-8')
    (root / 'docs/RELEASES.md').write_text('# 发布记录\n\n## MiaoTask-v0.1.0\n\n旧记录。\n', encoding='utf-8')


class LocalReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / 'input'
        source_tree(self.source)

    def test_directory_copy_excludes_device_state_environment_and_old_outputs(self):
        for name in ('.venv/private.py', '.git/config', 'runtime/state.json', 'dist/old.zip',
                     'configs/device.local.json', 'try.py', 'activities/private.py'):
            path = self.source / name
            path.parent.mkdir(exist_ok=True)
            path.write_text('local only', encoding='utf-8')
        destination = self.root / 'copied'
        copy_source(self.source, destination)
        self.assertTrue((destination / 'backend/main.py').is_file())
        self.assertFalse((destination / '.git').exists())
        self.assertFalse((destination / '.venv').exists())
        self.assertFalse((destination / 'runtime').exists())
        self.assertFalse((destination / 'configs/device.local.json').exists())
        self.assertFalse((destination / 'try.py').exists())
        self.assertFalse((destination / 'activities').exists())
        with self.assertRaisesRegex(ValueError, '分离'):
            copy_source(self.source, self.source / 'build-copy')

    def test_wrapped_source_zip_needs_no_git_and_unsafe_paths_are_rejected(self):
        archive = self.root / 'source.zip'
        with zipfile.ZipFile(archive, 'w') as bundle:
            for path in self.source.rglob('*'):
                if path.is_file():
                    bundle.write(path, 'MiaoTask-main/' + path.relative_to(self.source).as_posix())
            bundle.writestr('MiaoTask-main/runtime/private.json', '{}')
        destination = self.root / 'extracted'
        extract_source(archive, destination)
        self.assertTrue((destination / 'main.py').is_file())
        self.assertFalse((destination / 'runtime').exists())
        bad = self.root / 'bad.zip'
        with zipfile.ZipFile(bad, 'w') as bundle:
            bundle.writestr('../outside.py', 'bad')
        with self.assertRaisesRegex(ValueError, '不安全'):
            extract_source(bad, self.root / 'bad-output')
        self.assertFalse((self.root / 'outside.py').exists())

    def test_directly_maintained_readme_and_new_images_survive_copy_and_zip(self):
        files = {
            'README.md': b'maintainer documentation\0' + b'x' * 2_000_001,
            'docs/assets/new-name.PNG': b'\0image' + b'x' * 4_000_001,
            'frontend/assets/nested/new-icon.svg': b'<svg/>',
        }
        for name, content in files.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        copied = self.root / 'asset-copy'
        copy_source(self.source, copied)
        archive = self.root / 'assets.zip'
        with zipfile.ZipFile(archive, 'w') as bundle:
            for path in self.source.rglob('*'):
                if path.is_file():
                    bundle.write(path, 'MiaoTask-main/' + path.relative_to(self.source).as_posix())
        extracted = self.root / 'asset-extract'
        extract_source(archive, extracted)
        for destination in (copied, extracted):
            for name, content in files.items():
                with self.subTest(destination=destination.name, name=name):
                    self.assertEqual((destination / name).read_bytes(), content)

    def test_same_version_collects_contributions_without_bump_or_release_plan(self):
        metadata = (self.source / 'version.json').read_bytes()
        self.assertEqual(rollup_local(self.source, '更新概述。'), ('MiaoTask-v0.1.1', 2))
        history = (self.source / 'docs/RELEASES.md').read_text(encoding='utf-8')
        self.assertIn('旧记录', history)
        self.assertEqual(history.count('## MiaoTask-v0.1.1'), 1)
        pending = self.source / 'docs/UNRELEASED.md'
        self.assertNotIn('* 【', pending.read_text(encoding='utf-8'))
        pending.write_text(pending.read_text(encoding='utf-8') + '\n* 【优化】再合成一项\n', encoding='utf-8')
        rollup_local(self.source)
        history = (self.source / 'docs/RELEASES.md').read_text(encoding='utf-8')
        self.assertEqual(history.count('## MiaoTask-v0.1.1'), 1)
        self.assertIn('再合成一项', history)
        self.assertEqual((self.source / 'version.json').read_bytes(), metadata)
        self.assertFalse((self.source / '.github/release-plan.json').exists())

    def test_malformed_contribution_does_not_clear_or_change_any_document(self):
        pending = self.source / 'docs/UNRELEASED.md'
        pending.write_text('## 待发布\n\nnot a contribution\n', encoding='utf-8')
        before = {name: (self.source / name).read_bytes() for name in ('docs/UNRELEASED.md', 'docs/RELEASES.md', 'version.json')}
        with self.assertRaises(ValueError):
            rollup_local(self.source)
        for name, content in before.items():
            self.assertEqual((self.source / name).read_bytes(), content)

    def test_older_source_is_rejected_without_overlaying_host_business_code(self):
        (self.source / 'scripts/package_windows.py').write_text('# older builder', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '不会替换业务代码'):
            copy_source(self.source, self.root / 'old-copy')
        self.assertEqual((self.source / 'backend/main.py').read_text(encoding='utf-8'), '# source\n')

    def test_legacy_adapter_requires_both_reviewed_version_and_exact_build_entry(self):
        entry = self.source / 'scripts/package_windows.py'
        entry.write_text('# reviewed old packaging entry\n', encoding='utf-8')
        version = self.source / 'version.json'
        version.write_text(json.dumps({'version':'MiaoTask-v0.1.0','app_version':'MiaoTask-v0.1.0'}), encoding='utf-8')
        digest = hashlib.sha256(entry.read_bytes()).hexdigest()
        with patch.dict(local_release.LEGACY_BUILDERS, {'MiaoTask-v0.1.0':digest}, clear=True):
            self.assertTrue(local_release.legacy_supported(self.source))
            local_release.validate_source(self.source)
            entry.write_text('# unreviewed packaging change\n', encoding='utf-8')
            self.assertFalse(local_release.legacy_supported(self.source))
            with self.assertRaisesRegex(ValueError, '已核验'):
                local_release.legacy_build_worker(self.source, self.root/'tools')

    def test_legacy_adapter_preserves_input_business_and_marks_local_provenance(self):
        version = self.source / 'version.json'
        version.write_text(json.dumps({'version':'MiaoTask-v0.1.0','app_version':'MiaoTask-v0.1.0'}), encoding='utf-8')
        entry = self.source / 'scripts/package_windows.py'
        entry.write_text('''import json
from pathlib import Path
import zipfile
ROOT=Path(__file__).resolve().parents[1]
def source_commit():
    raise AssertionError('a source ZIP has no Git checkout')
def main():
    output=ROOT/'dist'
    output.mkdir()
    with zipfile.ZipFile(output/'MiaoTask-v0.1.0-windows-x64.zip','w') as archive:
        archive.writestr('MiaoTask/build-manifest.json',json.dumps({'version':'MiaoTask-v0.1.0','source_commit':source_commit()}))
        archive.writestr('MiaoTask/business-marker.txt',(ROOT/'backend/main.py').read_bytes())
''', encoding='utf-8')
        digest = hashlib.sha256(entry.read_bytes()).hexdigest()
        original = (self.source/'backend/main.py').read_bytes()
        with patch.dict(local_release.LEGACY_BUILDERS, {'MiaoTask-v0.1.0':digest}, clear=True), \
                patch('sys.path', list(sys.path)), patch('sys.argv', ['fixture']):
            local_release.legacy_build_worker(self.source, self.root/'tools')
        self.assertEqual((self.source/'backend/main.py').read_bytes(), original)
        with zipfile.ZipFile(self.source/'dist/MiaoTask-v0.1.0-windows-x64.zip') as result:
            self.assertEqual(result.read('MiaoTask/business-marker.txt'), original)
            manifest = json.loads(result.read('MiaoTask/build-manifest.json'))
        self.assertTrue(manifest['local_source'])
        self.assertEqual(manifest['source_commit'], '')
        self.assertEqual(manifest['bundle_mode'], 'full')

    def test_only_github_source_links_are_accepted(self):
        self.assertEqual(github_archive_url('https://github.com/example/project'), 'https://api.github.com/repos/example/project/zipball')
        self.assertTrue(github_archive_url('https://github.com/example/project/tree/feature/new').endswith('feature%2Fnew'))
        self.assertEqual(github_archive_url('https://github.com/example/project/archive/refs/heads/main.zip'),
                         'https://github.com/example/project/archive/refs/heads/main.zip')
        for value in ('http://github.com/example/project', 'https://github.com.evil.invalid/example/project',
                      'https://user:password@github.com/example/project', 'https://github.com/example/project/releases/download/latest/app.zip'):
            with self.assertRaises(ValueError):
                github_archive_url(value)

    def test_local_snapshot_is_audited_without_git_commands(self):
        with patch.object(package_windows, 'ROOT', self.source), \
                patch.object(package_windows.subprocess, 'check_output') as git:
            self.assertEqual(len(package_windows.worktree_snapshot(local_source=True)), 64)
            git.assert_not_called()

    def test_success_exports_notes_and_clean_source_but_never_mutates_input_by_default(self):
        before = (self.source / 'docs/UNRELEASED.md').read_bytes()
        calls = []
        def fake_run(command, cwd, env, log):
            calls.append([str(item) for item in command])
            if 'scripts/package_windows.py' in [str(item) for item in command]:
                folder = Path(cwd) / 'dist'
                folder.mkdir()
                version = json.loads((Path(cwd) / 'version.json').read_text(encoding='utf-8'))['version']
                (folder / f'{version}-windows-x64.zip').write_bytes(b'synthetic archive')
            self.assertNotIn('MIAO_ACTIVITIES_DIR', env)
        with patch('scripts.local_release.run_logged', side_effect=fake_run), patch('builtins.print'), \
                patch.dict(os.environ, MIAO_ACTIVITIES_DIR='not-the-users-installed-activities'):
            result = build(self.source, self.root / 'builder', adb_dir=self.root / 'tools')
        self.assertEqual(result['version'], 'MiaoTask-v0.1.2')
        self.assertEqual(json.loads((self.source / 'version.json').read_text())['version'], 'MiaoTask-v0.1.1')
        self.assertTrue(Path(result['archive']).is_file())
        self.assertNotIn('preview', Path(result['archive']).name)
        self.assertTrue(Path(result['source_archive']).is_file())
        self.assertTrue(Path(result['notes']).is_file())
        self.assertIn('合成问题', Path(result['notes']).read_text(encoding='utf-8'))
        self.assertEqual((self.source / 'docs/UNRELEASED.md').read_bytes(), before)
        self.assertTrue(any('--local-source' in command for command in calls))
        self.assertFalse(any('git' in command or 'gh' in command for command in calls))

    def test_version_plan_does_not_reserve_and_success_counter_never_moves_back(self):
        home = self.root / 'builder'
        home.mkdir()
        self.assertEqual(plan_version('0.1.1', home), 'MiaoTask-v0.1.2')
        self.assertEqual(plan_version('0.1.1', home), 'MiaoTask-v0.1.2')
        self.assertFalse((home / 'builder-state.json').exists())
        record_success(home, 'MiaoTask-v0.1.5', 'synthetic.zip')
        self.assertEqual(plan_version('0.1.1', home), 'MiaoTask-v0.1.6')
        self.assertEqual(plan_version('0.2.1', home), 'MiaoTask-v0.2.2')
        self.assertEqual(plan_version('0.1.1', home, 'minor'), 'MiaoTask-v0.2.0')
        self.assertEqual(plan_version('0.1.1', home, 'keep'), 'MiaoTask-v0.1.1')
        self.assertEqual(plan_version('0.1.1', home, target_version='0.3.0'), 'MiaoTask-v0.3.0')
        record_success(home, 'MiaoTask-v0.1.2', 'older.zip')
        self.assertEqual(plan_version('0.1.1', home), 'MiaoTask-v0.1.6')
        with self.assertRaises(ValueError):
            plan_version('0.1.1', home, target_version='1.0.0')

    def test_preview_and_failed_build_do_not_consume_a_version(self):
        home = self.root / 'builder'
        def fake_run(command, cwd, env, log):
            if 'scripts/package_windows.py' in [str(item) for item in command]:
                version = json.loads((Path(cwd) / 'version.json').read_text())['version']
                (Path(cwd) / 'dist').mkdir()
                (Path(cwd) / 'dist' / f'{version}-windows-x64.zip').write_bytes(b'synthetic archive')
        with patch('scripts.local_release.run_logged', side_effect=RuntimeError('synthetic failure')), patch('builtins.print'):
            with self.assertRaisesRegex(RuntimeError, 'synthetic failure'):
                build(self.source, home, adb_dir=self.root / 'tools')
        self.assertFalse((home / 'builder-state.json').exists())
        with patch('scripts.local_release.run_logged', side_effect=fake_run), patch('builtins.print'):
            preview = build(self.source, home, adb_dir=self.root / 'tools', preview=True)
            self.assertTrue(preview['preview'])
            self.assertFalse((home / 'builder-state.json').exists())
            released = build(self.source, home, adb_dir=self.root / 'tools')
        self.assertEqual(preview['version'], released['version'])
        self.assertEqual(plan_version('0.1.1', home), 'MiaoTask-v0.1.3')
        self.assertEqual(json.loads((self.source / 'version.json').read_text())['version'], 'MiaoTask-v0.1.1')

    def test_parallel_builds_cannot_reserve_the_same_home(self):
        home = self.root / 'builder'
        with builder_lock(home):
            with self.assertRaisesRegex(RuntimeError, '已有构建'):
                with builder_lock(home):
                    self.fail('A second build must not enter the version transaction')
        with builder_lock(home):
            pass

    def activity_provider(self, revision='a' * 40):
        """A real, valid adapter ZIP; only the GitHub transport is synthetic."""
        fixture = self.root / 'fixture.zip'
        synthetic_activity_package(fixture)
        with zipfile.ZipFile(fixture) as archive:
            payload = {info.filename: archive.read(info) for info in archive.infolist()}
        manifest = json.loads(payload['manifest.json'])
        manifest.update(source='github-source', source_ref='main', source_commit=revision)
        payload['manifest.json'] = json.dumps(manifest).encode('utf-8')
        with zipfile.ZipFile(fixture, 'w') as archive:
            for name, data in payload.items():
                archive.writestr(name, data)
        entry = {key: manifest[key] for key in ('app', 'activity', 'app_module', 'activity_module',
                 'version', 'source', 'source_ref', 'source_commit', 'framework_api', 'python')}
        catalog = dict(schema=1, source='github-source', source_ref='main', source_commit=revision,
                       repository='h0ll0w-AkuZr0guY/MiaoTask', packages=[entry])
        calls = []
        class Provider:
            def catalog(self, *, commit):
                calls.append(('catalog', commit))
                return catalog
            def build_archive(self, selected, destination):
                calls.append(('build', selected['app'], selected['activity'], selected['source_commit']))
                shutil.copy2(fixture, destination)
                return hashlib.sha256(Path(destination).read_bytes()).hexdigest()
        return Provider, calls

    def test_preinstall_pins_selection_and_installs_only_beside_framework(self):
        provider, calls = self.activity_provider()
        selected = [{'app':'smokefixture', 'activity':'daily', 'source_commit':'a' * 40}]
        destination = self.root / 'portable/activities'
        installed = local_release.install_preselected(destination, selected, self.root / 'downloads',
                                                       provider_factory=provider)
        self.assertEqual(calls, [('catalog', 'a' * 40), ('build', 'smokefixture', 'daily', 'a' * 40)])
        self.assertEqual(installed[0]['source_commit'], 'a' * 40)
        self.assertTrue((destination / 'apps/smokefixture/activities/daily/activity.py').is_file())
        self.assertFalse((self.root / 'portable/_internal/backend/app/smokefixture').exists())
        self.assertEqual(json.loads((destination / 'installed.json').read_text())['packages'][0]['source_commit'], 'a' * 40)
        self.assertEqual(json.loads((destination / 'catalog.json').read_text())['source_commit'], 'a' * 40)
        for invalid in ([selected[0], selected[0]], [{'app':'x', 'activity':'a', 'source_commit':'main'}]):
            with self.assertRaises(ValueError):
                local_release.validate_activity_choices(invalid)
        with self.assertRaisesRegex(ValueError, '提交发生变化'):
            local_release.install_preselected(self.root / 'wrong',
                [dict(selected[0], source_commit='b' * 40)], self.root / 'wrong-downloads', provider_factory=provider)

    def test_preinstall_repacked_zip_keeps_core_empty_and_updates_instructions(self):
        provider, _ = self.activity_provider()
        workspace = self.root / 'workspace'
        package = workspace / 'dist/windows-fixture/MiaoTask'
        (package / '_internal').mkdir(parents=True)
        (package / '_internal/core.txt').write_text('synthetic core')
        (package / 'MiaoTask.exe').write_bytes(b'synthetic executable')
        (package / 'README.md').write_text('默认仅包含框架与运行环境。首次打开网页，在「活动中心」下载需要的应用与活动。', encoding='utf-8')
        (package / 'build-manifest.json').write_text(json.dumps({'bundle_mode':'framework','embedded_activities':[]}), encoding='utf-8')
        archive = workspace / 'dist/MiaoTask-v0.1.2-windows-x64.zip'
        archive.write_bytes(b'initial archive')
        selected = [{'app':'smokefixture', 'activity':'daily', 'source_commit':'a' * 40}]
        run = self.root / 'run'
        run.mkdir()
        def frozen_self_test(command, cwd, env, log):
            self.assertEqual(env['MIAO_ACTIVITIES_DIR'], str(package / 'activities'))
            self.assertEqual(env['MIAO_DATA_DIR'], str(run / 'preinstalled-check-data'))
            Path(command[-1]).write_text(json.dumps({'ok':True,'frozen':True,
                                                   'checks':{'registered_activities':1}}))
        with patch('backend.core.plugins.repository.GitHubSourceProvider', provider), \
                patch('scripts.local_release.run_logged', side_effect=frozen_self_test):
            installed = local_release.preload_package(workspace, archive, selected, sys.executable, {}, run/'log', run)
        self.assertEqual(len(installed), 1)
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
            self.assertTrue(any(name.startswith('MiaoTask/activities/apps/smokefixture/') for name in names))
            self.assertFalse(any('_internal/backend/app/smokefixture' in name for name in names))
            self.assertFalse(any('__pycache__' in name for name in names))
            self.assertIn('已预装', bundle.read('MiaoTask/README.md').decode('utf-8'))
            manifest = json.loads(bundle.read('MiaoTask/build-manifest.json'))
        self.assertEqual(manifest['embedded_activities'], [])
        self.assertEqual(manifest['preinstalled_activities'][0]['source_commit'], 'a' * 40)
        self.assertTrue(manifest['preinstalled_activity_self_test'])

    def test_legacy_source_rejects_external_preinstall_without_counter_or_overlay(self):
        entry = self.source / 'scripts/package_windows.py'
        entry.write_text('# known legacy entry\n')
        digest = hashlib.sha256(entry.read_bytes()).hexdigest()
        home = self.root / 'builder'
        with patch.dict(local_release.LEGACY_BUILDERS, {'MiaoTask-v0.1.1':digest}, clear=True), patch('builtins.print'):
            with self.assertRaisesRegex(ValueError, '旧 0.1.0'):
                build(self.source, home, activities=[{'app':'sample','activity':'daily','source_commit':'a' * 40}])
        self.assertFalse((home / 'builder-state.json').exists())
        self.assertEqual((self.source / 'backend/main.py').read_text(), '# source\n')

    def test_empty_preinstall_preserves_absent_activity_directory_for_migration(self):
        workspace = self.root / 'empty-core'
        self.assertEqual(local_release.preload_package(workspace, self.root/'no-package.zip', [],
                                                       sys.executable, {}, self.root/'log', self.root/'run'), [])
        self.assertFalse(workspace.exists())

    def test_public_directory_junction_is_rejected_before_scanning_or_exporting(self):
        junction = self.source / 'backend/linked'
        junction.mkdir()
        (junction / 'external.py').write_text('should not be read')
        is_junction = lambda path: path == junction
        with patch.object(Path, 'is_junction', is_junction, create=True):
            with self.assertRaisesRegex(ValueError, '目录联接'):
                copy_source(self.source, self.root / 'linked-copy')
            self.assertFalse((self.root / 'linked-copy/backend/linked').exists())
            with self.assertRaisesRegex(ValueError, '目录联接'):
                local_release.export_source(self.source, self.root / 'linked-source.zip')

    def test_ancestor_link_resolution_cannot_read_outside_the_source(self):
        path = self.source / 'build/README.md'
        path.parent.mkdir()
        path.write_text('not a real link')
        original_resolve = Path.resolve
        def resolved(candidate, *args, **kwargs):
            return self.root / 'outside.md' if candidate == path else original_resolve(candidate, *args, **kwargs)
        with patch.object(Path, 'resolve', resolved):
            with self.assertRaisesRegex(ValueError, '越出输入目录'):
                copy_source(self.source, self.root / 'outside-copy')


if __name__ == '__main__':
    unittest.main()
