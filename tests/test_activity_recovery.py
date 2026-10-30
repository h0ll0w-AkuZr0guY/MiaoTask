"""An API outage must not make uninstall irreversible."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError
import zipfile

from backend.core.plugins.manager import PluginManager
from backend.core.plugins.recovery import RecoveryStore
from backend.core.plugins.repository import GitHubSourceProvider
from backend.core.plugins.git_archive import advertised_branches, source_snapshot, REFS_URL
from backend.core.plugins.resource_mirror import ResourceMirror
from backend.core.plugins.contracts import OFFICIAL_REPOSITORY
from test_activity_distribution import package_fixture
from test_activity_repository import SourceFixture, FIRST


def packet(line):
    return f'{len(line)+4:04x}'.encode()+line


class RecoveryTests(unittest.TestCase):
    def test_installed_archive_survives_uninstall_and_offline_restore(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);archive,digest=package_fixture(root)
            provider=Mock(source_ref='main')
            provider.catalog.side_effect=OSError('offline')
            manager=PluginManager(root/'install',source_provider=provider)
            manager.install_local(archive,digest)
            manager.uninstall('sample','daily')
            self.assertEqual(manager.installed(),[])
            snapshot=manager.snapshot(refresh=True)
            self.assertTrue(snapshot['packages'][0]['offline_recovery'])
            self.assertEqual(snapshot['packages'][0]['source'],'bundled-recovery')
            manager.snapshot()
            self.assertEqual(provider.catalog.call_count,1,'idle polling must not repeat failed network requests')
            manager.restore('sample','daily')
            self.assertEqual(manager.installed()[0]['activity'],'daily')
            self.assertEqual(provider.catalog.call_count,1)

    def test_fresh_install_can_restore_from_immutable_bundled_store(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);archive,digest=package_fixture(root)
            baseline=RecoveryStore(root/'bundled')
            baseline.remember(archive,digest)
            manager=PluginManager(root/'empty',recovery_dir=baseline.root)
            manager.restore('sample','daily')
            self.assertTrue(manager.installed())

    def test_corrupt_archive_never_restores(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);archive,digest=package_fixture(root)
            store=RecoveryStore(root/'bundled');entry=store.remember(archive,digest)
            (store.root/entry['filename']).write_bytes(b'corrupted')
            manager=PluginManager(root/'empty',recovery_dir=store.root)
            with self.assertRaisesRegex(ValueError,'校验失败'):
                manager.restore('sample','daily')
            self.assertEqual(manager.installed(),[])

    def test_git_archive_fallback_pins_snapshot_without_api(self):
        with tempfile.TemporaryDirectory() as folder:
            fixture=SourceFixture(Path(folder))
            buffer=io.BytesIO()
            prefix=OFFICIAL_REPOSITORY.split('/')[-1]+'-'+FIRST+'/'
            with zipfile.ZipFile(buffer,'w') as archive:
                for name,content in fixture.revisions[FIRST].items():
                    archive.writestr(prefix+name,content)
            calls=[]
            def fetch(url,maximum):
                calls.append(url)
                if url==REFS_URL:
                    return packet((FIRST+' refs/heads/main\0capabilities\n').encode())+b'0000'
                if url.startswith('https://codeload.github.com/'):
                    return buffer.getvalue()
                raise HTTPError(url,403,'rate limit exceeded',{},None)
            provider=GitHubSourceProvider(fetch)
            catalog=provider.catalog()
            self.assertEqual(catalog['source_commit'],FIRST)
            self.assertEqual(catalog['transport'],'github-archive')
            self.assertEqual(len(catalog['packages']),2)
            target=Path(folder)/'selected.zip'
            provider.build_archive(catalog['packages'][0],target)
            self.assertTrue(target.is_file())
            self.assertEqual(sum(url.startswith('https://codeload.github.com/') for url in calls),1)

    def test_git_archive_rejects_wrong_commit_and_bad_refs(self):
        with self.assertRaises(ValueError):
            advertised_branches(b'0010short')
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w') as archive:
            archive.writestr('wrong/manifest.py','')
        with self.assertRaises(ValueError):
            source_snapshot(buffer.getvalue(),FIRST)

    def test_pages_catalog_and_selected_archive_digest(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            def add_revision(manifest, files):
                manifest.update(source_commit=FIRST,source='github-source',source_ref='main')
            archive,digest=package_fixture(root,mutate=add_revision)
            entry=dict(app='sample',activity='daily',app_module='sample',activity_module='daily',
                       app_label='Sample',label='Daily',version='0.1.1',framework_api=1,python='3.13',
                       source='github-source',source_ref='main',source_commit=FIRST,archive='packages/sample.zip',sha256=digest)
            catalog=dict(schema=1,repository=OFFICIAL_REPOSITORY,source='github-source',source_ref='main',source_commit=FIRST,packages=[entry])
            def fetch(url,maximum):
                return archive.read_bytes() if url.endswith('.zip') else json.dumps(catalog).encode()
            mirror=ResourceMirror(fetch=fetch)
            self.assertEqual(mirror.catalog()['transport'],'pages')
            target=root/'mirror.zip'
            self.assertEqual(mirror.build_archive(entry,target),digest)
            mirror.fetch=lambda *args:b'changed'
            with self.assertRaisesRegex(ValueError,'摘要'):
                mirror.build_archive(entry,target)
