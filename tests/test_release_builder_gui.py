"""Pure GUI planning contracts; no display, network or actual build is required."""
import json
from pathlib import Path
import queue
import sys
import tempfile
import unittest
import zipfile

from scripts.release_builder_gui import (BuildOptions, QueueWriter, local_source_version,
                                         phase_for_line, selected_activities)


class ReleaseBuilderGuiTests(unittest.TestCase):
    def test_default_patch_and_explicit_version_options_reach_builder_unchanged(self):
        arguments = BuildOptions('source.zip', Path('builder')).arguments([])
        self.assertEqual(arguments['version_mode'], 'patch')
        self.assertEqual(arguments['activities'], [])
        self.assertFalse(arguments['preview'])
        arguments = BuildOptions('https://github.com/example/project', Path('builder'),
                                 version_mode='minor', target_version='0.2.0', preview=True).arguments([])
        self.assertEqual(arguments['target_version'], '0.2.0')
        self.assertEqual(arguments['version_mode'], 'minor')
        self.assertTrue(arguments['preview'])

    def test_invalid_options_fail_before_a_worker_is_started(self):
        for source, mode, target, summary in (('', 'patch', None, ''), ('x', 'auto', None, ''),
                                             ('x', 'keep', '../version', ''), ('x', 'patch', None, 'a\nb')):
            with self.subTest(source=source, mode=mode), self.assertRaises(ValueError):
                BuildOptions(source, Path('builder'), version_mode=mode, target_version=target, summary=summary).arguments([])
        with self.assertRaises(ValueError):
            BuildOptions('source.zip', Path('builder'), preview=True, write_back_notes=True).arguments([])

    def test_selected_activities_pin_only_visible_compatible_commits(self):
        revision = 'a' * 40
        item = dict(app='sample', activity='daily', source_commit=revision,
                    framework_api=1, python=f'{sys.version_info.major}.{sys.version_info.minor}')
        self.assertEqual(selected_activities([item], [('sample', 'daily')]),
                         [dict(app='sample', activity='daily', source_commit=revision)])
        self.assertEqual(selected_activities([item], []), [])
        for packages, chosen in (([dict(item, framework_api=999)], [('sample', 'daily')]),
                                 ([dict(item, source_commit='main')], [('sample', 'daily')]),
                                 ([item], [('sample', 'unknown')])):
            with self.assertRaises(ValueError):
                selected_activities(packages, chosen)

    def test_small_directory_and_single_root_zip_versions_without_extracting(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            metadata = json.dumps({'version': 'MiaoTask-v0.1.1'}).encode()
            (root / 'version.json').write_bytes(metadata)
            self.assertEqual(local_source_version(str(root)), 'MiaoTask-v0.1.1')
            archive = root / 'source.zip'
            with zipfile.ZipFile(archive, 'w') as bundle:
                bundle.writestr('MiaoTask-main/version.json', metadata)
            self.assertEqual(local_source_version(str(archive)), 'MiaoTask-v0.1.1')
            self.assertIsNone(local_source_version('https://github.com/example/project'))
            self.assertFalse((root / 'MiaoTask-main').exists())

    def test_queue_writer_only_emits_messages_and_reports_phase_without_tk(self):
        events = queue.Queue()
        writer = QueueWriter(events)
        self.assertEqual(writer.write('running unittest'), 16)
        self.assertEqual(events.get_nowait(), ('log', 'running unittest'))
        writer.flush()
        self.assertFalse(writer.isatty())
        self.assertEqual(phase_for_line('python -m PyInstaller build.spec'), '构建 exe 与运行环境')
        self.assertIsNone(phase_for_line('ordinary output'))


if __name__ == '__main__':
    unittest.main()
