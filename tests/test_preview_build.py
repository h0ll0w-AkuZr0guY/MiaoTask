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


if __name__ == '__main__':
    unittest.main()
