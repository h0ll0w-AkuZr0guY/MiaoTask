"""Official source branches stay pinned, cache-isolated and explicitly selected."""
import json
from pathlib import Path
import tempfile
import unittest

from backend.core.plugins.contracts import CATALOG_URL, OFFICIAL_REPOSITORY, branch_name
from backend.core.plugins.repository import GitHubSourceProvider, source_url
from backend.core.plugins.manager import PluginManager
from test_activity_repository import SourceFixture


class ActivityBranchesTests(unittest.TestCase):
    def test_branch_names_and_urls(self):
        for name in ('main', 'feature/new-app', 'codex/demo', '开发/闲鱼'):
            self.assertEqual(branch_name(name), name)
            url = f'https://api.github.com/repos/{OFFICIAL_REPOSITORY}/git/ref/heads/{name}'
            self.assertEqual(source_url(url), url)
        for name in ('../main', 'main?token=x', '-switch', 'refs//x', 'dev.lock'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                branch_name(name)

    def test_listing_and_pinned_nonmain_catalog(self):
        with tempfile.TemporaryDirectory() as folder:
            fixture = SourceFixture(Path(folder))
            def fetch(url, maximum):
                if 'matching-refs' in url:
                    return json.dumps([{'ref': 'refs/heads/main'}, {'ref': 'refs/heads/feature/new-app'}]).encode()
                if url.endswith('/ref/heads/feature/new-app'):
                    value = json.loads(fixture.fetch(CATALOG_URL, maximum))
                    value['ref'] = 'refs/heads/feature/new-app'
                    return json.dumps(value).encode()
                return fixture.fetch(url, maximum)
            provider = GitHubSourceProvider(fetch, source_ref='feature/new-app')
            self.assertEqual(provider.branches(), ['main', 'feature/new-app'])
            catalog = provider.catalog()
            self.assertEqual(catalog['source_ref'], 'feature/new-app')
            self.assertTrue(catalog['packages'])
            self.assertTrue(all(row['source_ref'] == 'feature/new-app' for row in catalog['packages']))
            archive = Path(folder) / 'branch.zip'
            provider.build_archive(catalog['packages'][0], archive)
            self.assertTrue(archive.is_file())

    def test_wrong_branch_cache_is_not_offline_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            fixture = SourceFixture(Path(folder))
            provider = GitHubSourceProvider(fixture.fetch)
            manager = PluginManager(Path(folder) / 'installed', source_provider=provider)
            first, _ = manager.catalog(refresh=True)
            self.assertTrue(first['packages'])
            provider.source_ref = 'feature/offline'
            provider.fetch = lambda *args: (_ for _ in ()).throw(OSError('synthetic offline'))
            value, cached = manager.catalog()
            self.assertEqual(value['packages'], [])
            self.assertFalse(cached)
            self.assertIn('synthetic offline', manager.last_error)

    def test_failed_refresh_warning_survives_poll_until_success(self):
        with tempfile.TemporaryDirectory() as folder:
            fixture = SourceFixture(Path(folder))
            provider = GitHubSourceProvider(fixture.fetch)
            manager = PluginManager(Path(folder) / 'installed', source_provider=provider)
            manager.catalog(refresh=True)
            provider.fetch = lambda *args: (_ for _ in ()).throw(OSError('synthetic rate limit'))
            _, cached = manager.catalog(refresh=True)
            self.assertTrue(cached)
            self.assertIn('synthetic rate limit', manager.last_error)
            manager.catalog()
            self.assertIn('synthetic rate limit', manager.last_error)
            provider.fetch = fixture.fetch
            manager.catalog(refresh=True)
            self.assertEqual(manager.last_error, '')
