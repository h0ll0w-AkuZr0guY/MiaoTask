"""The shared PR/local audit checks current source instead of stale staged blobs."""
from contextlib import redirect_stderr
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.check_project import audit_worktree


class ProjectChecksTests(unittest.TestCase):
    def test_working_changes_and_new_files_are_audited_once_while_deletions_are_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'scripts').mkdir()
            (root / 'README.md').write_text('Current content', encoding='utf-8')
            (root / 'scripts/new.py').write_text('value = 1\n', encoding='utf-8')
            names = b'README.md\0scripts/new.py\0scripts/new.py\0docs/deleted.md\0'
            with patch('scripts.check_project.subprocess.check_output', return_value=names) as git:
                self.assertEqual(audit_worktree(root), 2)
                self.assertIn('--others', git.call_args.args[0])
                self.assertIn('--exclude-standard', git.call_args.args[0])
            # A secret in an unstaged edit still fails before publishing.
            (root / 'scripts/new.py').write_text('-----BEGIN ' + 'PRIVATE KEY-----', encoding='utf-8')
            with patch('scripts.check_project.subprocess.check_output', return_value=names), redirect_stderr(io.StringIO()), self.assertRaises(ValueError):
                audit_worktree(root)

    def test_unignored_environment_or_build_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'output.zip').write_bytes(b'archive')
            with patch('scripts.check_project.subprocess.check_output', return_value=b'output.zip\0'), redirect_stderr(io.StringIO()), self.assertRaises(ValueError):
                audit_worktree(root)

    def test_ancestor_junction_is_rejected_before_reading_its_contents(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / 'scripts').mkdir()
            (root / 'scripts/example.py').write_text('value = 1\n', encoding='utf-8')
            with (patch('scripts.check_project.subprocess.check_output', return_value=b'scripts/example.py\0'),
                  patch.object(Path, 'is_junction', lambda path: path == root / 'scripts'),
                  patch('scripts.check_project.audit_file') as audit,
                  redirect_stderr(io.StringIO()), self.assertRaises(ValueError)):
                audit_worktree(root)
            audit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
