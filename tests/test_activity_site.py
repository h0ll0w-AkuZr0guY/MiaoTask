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
