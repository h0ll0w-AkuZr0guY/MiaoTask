"""PR checks preserve pending contributions and separate ordinary from release PRs."""
import json
from pathlib import Path
import tempfile
import unittest

from scripts.check_contribution import validate, PREPARATION_FILES
from scripts.prepare_release import prepare


class ContributionChecksTests(unittest.TestCase):
    def event(self, **changes):
        repo = {'full_name': 'owner/MiaoTask', 'owner': {'login': 'owner'}}
        pr = dict(title='fix(task): preserve task state', body='## 问题与结果\nfix\n## 影响与兼容\ncompatible\n## 实际验证\nunit tests',
                  user={'login': 'contributor'}, base={'repo': repo}, head={'repo': {'full_name': 'contributor/MiaoTask'}, 'ref': 'fix/task'})
        pr.update(changes)
        return {'pull_request': pr}

    def setUp(self):
        self.base = {'docs/UNRELEASED.md': '## 待发布\n\n* 【维护】Existing (#1)\n'}
        self.head = {'docs/UNRELEASED.md': self.base['docs/UNRELEASED.md'] + '* 【修复】New (#2)\n'}

    def test_fork_contribution_passes_without_writes_or_credentials(self):
        self.assertEqual(validate(self.event(), self.base, self.head, ['backend/core/models.py', 'docs/UNRELEASED.md']), 'ordinary-contribution')

    def test_title_body_existing_entries_and_protected_files_are_required(self):
        cases = [
            (self.event(title='update'), self.head, []),
            (self.event(body='missing context'), self.head, []),
            (self.event(), self.base, []),
            (self.event(), {'docs/UNRELEASED.md': '## 待发布\n* 【新增】Replacement\n'}, []),
            (self.event(), self.head, ['version.json']),
            (self.event(), self.head, ['docs/RELEASES.md']),
            (self.event(), self.head, ['.github/release-plan.json']),
        ]
        for event, head, changed in cases:
            with self.subTest(event=event, changed=changed), self.assertRaises(ValueError):
                validate(event, self.base, head, changed)

    def test_fork_cannot_claim_release_preparation_exception(self):
        event = self.event(title='chore(release): prepare MiaoTask-v0.1.1',
                           head={'repo': {'full_name': 'contributor/MiaoTask'}, 'ref': 'codex/release-prepare-123'})
        with self.assertRaises(ValueError):
            validate(event, self.base, self.head, PREPARATION_FILES)

    def test_repository_rename_only_can_correct_historical_links(self):
        old = 'https://github.com/h0ll0w-AkuZr0guY/' + 'tmall-miao-double11/releases'
        base = dict(self.base, **{'docs/RELEASES.md': 'Existing release: ' + old})
        head = dict(self.head, **{'docs/RELEASES.md': base['docs/RELEASES.md'].replace(old, 'https://github.com/h0ll0w-AkuZr0guY/MiaoTask/releases')})
        self.assertEqual(validate(self.event(), base, head, ['docs/RELEASES.md']), 'ordinary-contribution')
        head['docs/RELEASES.md'] += '\nChanged historical behavior'
        with self.assertRaises(ValueError):
            validate(self.event(), base, head, ['docs/RELEASES.md'])

    def test_only_four_file_owner_or_bot_preparation_is_allowed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'docs').mkdir()
            (root / 'version.json').write_text(json.dumps({'version': 'MiaoTask-v0.1.0', 'app_version': 'MiaoTask-v0.1.0'}))
            (root / 'docs/RELEASES.md').write_text('# Releases\n## MiaoTask-v0.1.0\nPrior\n')
            (root / 'docs/UNRELEASED.md').write_text(self.head['docs/UNRELEASED.md'], encoding='utf-8')
            base = dict(self.base, **{'version.json': (root / 'version.json').read_text()})
            prepare(root, 'MiaoTask-v0.1.1', 'A release')
            repo = self.event()['pull_request']['base']['repo']
            event = self.event(title='chore(release): prepare MiaoTask-v0.1.1', user={'login': 'github-actions[bot]'},
                               head={'repo': repo, 'ref': 'codex/release-prepare-123'})
            self.assertEqual(validate(event, base, self.head, PREPARATION_FILES, root), 'reviewed-release-preparation')
            with self.assertRaises(ValueError):
                validate(event, base, self.head, PREPARATION_FILES | {'backend/core/models.py'}, root)


if __name__ == '__main__':
    unittest.main()
