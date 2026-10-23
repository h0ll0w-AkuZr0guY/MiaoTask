"""Offline official-source contracts; every API/raw response is synthetic."""
import copy
import hashlib
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlparse
import zipfile

from backend.core.plugins.contracts import CATALOG_URL, FRAMEWORK_API, OFFICIAL_REPOSITORY
from backend.core.plugins.manager import PluginManager, validate_archive
from backend.core.plugins.repository import (GitHubSourceProvider, commit_id, git_blob_sha,
    source_url, validate_source_catalog)
from backend.core.registry import Registry
from test_activity_distribution import package_fixture


FIRST = 'a' * 40
SECOND = 'b' * 40
TREE_FIRST = 'c' * 40
TREE_SECOND = 'd' * 40


class SourceFixture:
    def __init__(self, root):
        compat = json.dumps(dict(schema=1, framework_api=FRAMEWORK_API,
                            python=f'{sys.version_info.major}.{sys.version_info.minor}')).encode()
        path, _ = package_fixture(root, extra={'backend/app/sample/activities/daily/compat.json': compat})
        with zipfile.ZipFile(path) as archive:
            files = {name: archive.read(name) for name in archive.namelist() if name != 'manifest.json'}
        weekly = 'backend/app/sample/activities/weekly/'
        for name, content in list(files.items()):
            if name.startswith('backend/app/sample/activities/daily/'):
                files[weekly + name.rsplit('/', 1)[-1]] = content.replace(b'daily', b'weekly')
        files['backend/core/never_download.py'] = b'raise RuntimeError("unselected core")\n'
        files['backend/app/unused/activities/never/activity.py'] = b'raise RuntimeError("unselected app")\n'
        self.revisions = {FIRST: files, SECOND: dict(files)}
        self.revisions[SECOND]['backend/app/sample/utils/value.py'] = b"VALUE='updated'\n"
        self.head, self.urls, self.corrupt, self.tree_mutation = FIRST, [], None, None

    def fetch(self, url, maximum):
        source_url(url)
        self.urls.append(url)
        parsed = urlparse(url)
        if url == CATALOG_URL:
            value = dict(ref='refs/heads/main', object=dict(type='commit', sha=self.head))
        elif '/git/commits/' in url:
            revision = parsed.path.rsplit('/', 1)[-1]
            value = dict(sha=revision, tree=dict(sha=TREE_FIRST if revision == FIRST else TREE_SECOND))
        elif '/git/trees/' in url:
            tree_sha = parsed.path.rsplit('/', 1)[-1]
            revision = FIRST if tree_sha == TREE_FIRST else SECOND
            entries = [dict(path=name, mode='100644', type='blob', size=len(content), sha=git_blob_sha(content))
                       for name, content in self.revisions[revision].items()]
            value = dict(sha=tree_sha, truncated=False, tree=entries)
            if self.tree_mutation:
                self.tree_mutation(value)
        else:
            revision, path = unquote(parsed.path[len('/' + OFFICIAL_REPOSITORY + '/'):]).split('/', 1)
            content = self.revisions[revision][path]
            if self.corrupt == path:
                content += b'changed'
            if len(content) > maximum:
                raise ValueError('fixture size exceeded')
            return content
        content = json.dumps(value).encode()
        if len(content) > maximum:
            raise ValueError('fixture size exceeded')
        return content

    def raw_paths(self):
        return [unquote(urlparse(url).path).split('/', 4)[-1]
                for url in self.urls if urlparse(url).hostname == 'raw.githubusercontent.com']


class GitHubActivitySourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.remote = SourceFixture(self.root)
        self.provider = GitHubSourceProvider(self.remote.fetch)
        self.package = importlib.import_module('backend.app')
        self.package_path = list(self.package.__path__)
        self.package_marker = getattr(self.package, '_miaotask_plugin_paths', ())

    def tearDown(self):
        for name in list(sys.modules):
            if name == 'backend.app.sample' or name.startswith('backend.app.sample.'):
                sys.modules.pop(name, None)
        if hasattr(self.package, 'sample'):
            delattr(self.package, 'sample')
        self.package.__path__ = self.package_path
        self.package._miaotask_plugin_paths = self.package_marker
        importlib.invalidate_caches()
        self.temporary.cleanup()

    def test_list_reads_only_declarations_and_compatibility_without_executing_python(self):
        for files in self.remote.revisions.values():
            files['backend/app/sample/manifest.py'] = b'raise RuntimeError("listing must not execute")\n' + files['backend/app/sample/manifest.py']
        catalog = self.provider.catalog()
        self.assertEqual({p['activity'] for p in catalog['packages']}, {'daily', 'weekly'})
        self.assertEqual(catalog['source_commit'], FIRST)
        self.assertFalse(catalog['warnings'])
        self.assertTrue(all(path.endswith(('manifest.py', 'compat.json')) for path in self.remote.raw_paths()))
        self.assertNotIn('backend.app.sample', sys.modules)

    def test_build_downloads_only_selected_app_common_files_and_one_activity(self):
        catalog = self.provider.catalog()
        entry = next(item for item in catalog['packages'] if item['activity'] == 'daily')
        self.remote.urls.clear()
        destination = self.root / 'source.zip'
        digest = self.provider.build_archive(entry, destination)
        manifest, files = validate_archive(destination, digest, expected_app='sample', expected_activity='daily')
        self.assertEqual(manifest['source_commit'], FIRST)
        self.assertIn('backend/app/sample/utils/value.py', files)
        self.assertTrue(all(name.startswith('backend/app/sample/') for name in files))
        self.assertFalse(any('/weekly/' in path or '/unused/' in path or '/core/' in path for path in self.remote.raw_paths()))
        self.assertFalse(any('/releases/' in url or '.zip' in url for url in self.remote.urls))

    def test_manager_installs_verified_source_and_persists_revision(self):
        manager = PluginManager(self.root / 'plugins', source_provider=self.provider)
        shown = manager.snapshot(refresh=True)
        entry = shown['packages'][0]
        record = manager.install(entry['app'], entry['activity'], source_commit=entry['source_commit'])
        self.assertEqual(record['source_commit'], FIRST)
        installed = manager.snapshot()['packages'][0]
        self.assertEqual(installed['installed_source_commit'], FIRST)
        self.assertFalse(installed['update_available'])
        registry = Registry(self.root / 'empty', plugin_dir=manager.apps_dir).discover()
        self.assertEqual(registry.get('sample', entry['activity']).factory()(None, {}).marker, 'original')

    def test_install_keeps_displayed_commit_when_main_moves(self):
        manager = PluginManager(self.root / 'plugins', source_provider=self.provider)
        entry = manager.snapshot(refresh=True)['packages'][0]
        self.remote.head = SECOND
        self.remote.urls.clear()
        record = manager.install(entry['app'], entry['activity'], source_commit=FIRST)
        self.assertEqual(record['source_commit'], FIRST)
        self.assertNotIn(CATALOG_URL, self.remote.urls)
        refreshed = manager.snapshot(refresh=True)
        self.assertEqual(refreshed['source_commit'], SECOND)
        self.assertTrue(refreshed['packages'][0]['update_available'])
        with self.assertRaisesRegex(ValueError, '目录已变化'):
            manager.install(entry['app'], entry['activity'], source_commit=FIRST)

    def test_new_process_uses_cached_displayed_commit_instead_of_new_main(self):
        manager = PluginManager(self.root / 'plugins', source_provider=self.provider)
        entry = manager.snapshot(refresh=True)['packages'][0]
        self.remote.head = SECOND
        self.remote.urls.clear()
        resumed = PluginManager(self.root / 'plugins', source_provider=GitHubSourceProvider(self.remote.fetch))
        record = resumed.install(entry['app'], entry['activity'], source_commit=FIRST)
        self.assertEqual(record['source_commit'], FIRST)
        self.assertNotIn(CATALOG_URL, self.remote.urls)
        self.assertTrue(all('/' + FIRST + '/' in url for url in self.remote.urls if 'raw.githubusercontent.com' in url))

    def test_blob_corruption_is_rejected_before_any_install(self):
        manager = PluginManager(self.root / 'plugins', source_provider=self.provider)
        entry = manager.snapshot(refresh=True)['packages'][0]
        self.remote.corrupt = 'backend/app/sample/utils/value.py'
        with self.assertRaisesRegex(ValueError, '摘要'):
            manager.install(entry['app'], entry['activity'])
        self.assertFalse(manager.state_path.exists())
        self.assertEqual(manager.installed(), [])

    def test_wrong_compatibility_blocks_install_without_downloading_implementation(self):
        for files in self.remote.revisions.values():
            files['backend/app/sample/activities/daily/compat.json'] = b'{"schema":1,"framework_api":999,"python":"3.13"}'
        catalog = self.provider.catalog()
        entry = next(item for item in catalog['packages'] if item['activity'] == 'daily')
        self.remote.urls.clear()
        with self.assertRaisesRegex(ValueError, '不兼容'):
            self.provider.build_archive(entry, self.root / 'blocked.zip')
        self.assertEqual(self.remote.urls, [])

    def test_unknown_activity_without_compatibility_is_not_guessed_compatible(self):
        for files in self.remote.revisions.values():
            files.pop('backend/app/sample/activities/daily/compat.json')
        catalog = self.provider.catalog()
        self.assertEqual([p['activity'] for p in catalog['packages']], ['weekly'])
        self.assertIn('compat.json', catalog['warnings'][0])

    def test_only_explicit_reviewed_manifest_pair_gets_legacy_compatibility(self):
        files = self.remote.revisions[FIRST]
        files.pop('backend/app/sample/activities/daily/compat.json')
        pair = (hashlib.sha256(files['backend/app/sample/manifest.py']).hexdigest(),
                hashlib.sha256(files['backend/app/sample/activities/daily/manifest.py']).hexdigest())
        with patch('backend.core.plugins.repository.LEGACY_COMPATIBLE_MANIFESTS', frozenset({pair})):
            catalog = self.provider.catalog()
        entry = next(item for item in catalog['packages'] if item['activity'] == 'daily')
        self.assertEqual((entry['framework_api'], entry['python'], entry['compatibility']), (1, '3.13', 'reviewed-legacy'))

    def test_selected_symlink_or_truncated_tree_never_installs(self):
        def truncate(value): value['truncated'] = True
        self.remote.tree_mutation = truncate
        with self.assertRaisesRegex(ValueError, '不完整'):
            self.provider.catalog()
        def symlink(value):
            for item in value['tree']:
                if item['path'] == 'backend/app/sample/utils/value.py': item['mode'] = '120000'
        self.remote.tree_mutation = symlink
        entry = self.provider.catalog()['packages'][0]
        with self.assertRaisesRegex(ValueError, '链接'):
            self.provider.build_archive(entry, self.root / 'linked.zip')

    def test_old_release_cache_is_discarded_for_normal_product_source(self):
        manager = PluginManager(self.root / 'plugins', source_provider=self.provider)
        manager.cache_path.write_text(json.dumps(dict(schema=1, repository=OFFICIAL_REPOSITORY, packages=[])), encoding='utf-8')
        snapshot = manager.snapshot()
        self.assertEqual(snapshot['source'], 'github-source')
        self.assertEqual(snapshot['source_commit'], FIRST)
        self.assertTrue(snapshot['packages'])
        self.assertIn(CATALOG_URL, self.remote.urls)

    def test_catalog_and_selected_download_have_total_budgets(self):
        current = [0.0]
        def slow_metadata(url, maximum):
            value = self.remote.fetch(url, maximum)
            current[0] += 61
            return value
        provider = GitHubSourceProvider(slow_metadata, clock=lambda:current[0])
        with self.assertRaisesRegex(TimeoutError, '总时间限制'):
            provider.catalog()
        provider = GitHubSourceProvider(self.remote.fetch, clock=lambda:current[0])
        entry = provider.catalog()['packages'][0]
        def slow_source(url, maximum):
            value = self.remote.fetch(url, maximum)
            current[0] += 121
            return value
        provider.fetch = slow_source
        with self.assertRaisesRegex(TimeoutError, '总时间限制'):
            provider.build_archive(entry, self.root / 'timeout.zip')
        self.assertFalse((self.root / 'timeout.zip').exists())

    def test_source_urls_reject_branches_other_repositories_and_traversal(self):
        good = f'https://raw.githubusercontent.com/{OFFICIAL_REPOSITORY}/{FIRST}/backend/app/sample/manifest.py'
        self.assertEqual(source_url(good), good)
        for invalid in (good.replace('/' + FIRST + '/', '/main/'), good.replace('https:', 'http:'),
                        good.replace('MiaoTask/', 'unknown/'), good.replace('manifest.py', '../outside.py'),
                        good + '?token=not-allowed', good.replace('raw.githubusercontent.com', 'example.invalid')):
            with self.subTest(url=invalid), self.assertRaises(ValueError):
                source_url(invalid)


if __name__ == '__main__':
    unittest.main()
