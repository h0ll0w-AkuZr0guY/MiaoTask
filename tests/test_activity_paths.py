"""Portable activity code and private device data keep separate ownership."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.core import runtime
from backend.core.plugins.manager import PluginManager
from backend.core.registry import Registry


class ActivityPathsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.env = patch.dict(os.environ, {'LOCALAPPDATA': str(self.root / 'private')})
        self.env.start()
        self.saved = {key: os.environ.pop(key, None) for key in ('MIAO_DATA_DIR', 'MIAO_ACTIVITIES_DIR')}
        self.frozen = patch.object(runtime.sys, 'frozen', True, create=True)
        self.frozen.start()
        self.exe = patch.object(runtime.sys, 'executable', str(self.root / 'portable/MiaoTask.exe'))
        self.exe.start()

    def tearDown(self):
        self.exe.stop()
        self.frozen.stop()
        for key, value in self.saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value
        self.env.stop()
        self.temp.cleanup()

    def legacy(self):
        old = runtime.data_dir() / 'plugins'
        (old / 'apps/sample').mkdir(parents=True)
        (old / 'apps/sample/source.py').write_text('VALUE=1\n')
        (old / 'installed.json').write_text(json.dumps({'schema': 1, 'packages': []}))
        return old

    def test_portable_registry_and_installer_share_visible_code_folder(self):
        manager = PluginManager()
        self.assertEqual(manager.root, self.root / 'portable/activities')
        self.assertEqual(Registry().plugin_root, manager.apps_dir)
        self.assertNotEqual(runtime.data_dir(), manager.root)

    def test_old_activity_source_migrates_once_and_original_is_preserved(self):
        old = self.legacy()
        new = runtime.activities_dir()
        self.assertEqual((new / 'apps/sample/source.py').read_text(), 'VALUE=1\n')
        self.assertTrue((old / 'apps/sample/source.py').exists())
        (new / 'apps/sample/source.py').write_text('VALUE=2\n')
        runtime.activities_dir()
        self.assertEqual((new / 'apps/sample/source.py').read_text(), 'VALUE=2\n')

    def test_preinstalled_activities_are_not_overwritten_by_old_user_install(self):
        self.legacy()
        installed = self.root / 'portable/activities/apps/preinstalled'
        installed.mkdir(parents=True)
        self.assertEqual(runtime.activities_dir(), installed.parents[1])
        self.assertFalse((installed.parent / 'sample').exists())

    def test_migration_rejects_windows_junctions_without_touching_old_files(self):
        old = self.legacy()
        with patch.object(Path, 'is_junction', lambda path: path == old / 'apps/sample'):
            with self.assertRaisesRegex(ValueError, '符号链接'):
                runtime.activities_dir()
        self.assertTrue((old / 'apps/sample/source.py').exists())
        self.assertFalse((self.root / 'portable/activities').exists())

    def test_data_override_isolates_code_and_does_not_read_portable_install(self):
        os.environ['MIAO_DATA_DIR'] = str(self.root / 'test-data')
        self.assertEqual(runtime.activities_dir(), self.root / 'test-data/activities')
        self.assertFalse((self.root / 'portable').exists())

    def test_explicit_activity_directory_is_strict_and_has_priority(self):
        os.environ['MIAO_DATA_DIR'] = str(self.root / 'test-data')
        os.environ['MIAO_ACTIVITIES_DIR'] = str(self.root / 'selected')
        self.assertEqual(runtime.activities_dir(), self.root / 'selected')
        with patch.object(runtime, '_writable_directory', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                runtime.activities_dir()

    def test_read_only_portable_folder_uses_writable_fallback_and_copies_preinstall(self):
        preferred = self.root / 'portable/activities'
        (preferred / 'apps/sample').mkdir(parents=True)
        (preferred / 'apps/sample/source.py').write_text('VALUE=3\n')
        original = runtime._writable_directory
        def writable(path):
            if path == preferred:
                raise PermissionError('read only')
            return original(path)
        with patch.object(runtime, '_writable_directory', side_effect=writable):
            result = runtime.activities_dir()
        self.assertEqual(result, runtime.data_dir() / 'activities')
        self.assertEqual((result / 'apps/sample/source.py').read_text(), 'VALUE=3\n')

    def test_source_checkout_has_separate_ignored_folder(self):
        with patch.object(runtime.sys, 'frozen', False), patch.object(runtime, 'ROOT', self.root / 'checkout'):
            self.assertEqual(runtime.activities_dir(), self.root / 'checkout/activities')


if __name__ == '__main__':
    unittest.main()
