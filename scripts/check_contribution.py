"""Protect release records and report helpful PR metadata suggestions."""
import argparse
from collections import Counter
import json
from pathlib import Path
import re
import subprocess

if __package__:
    from .prepare_release import pending_entries, release_ready
else:
    from prepare_release import pending_entries, release_ready

ROOT = Path(__file__).resolve().parents[1]
TITLE = re.compile(r'^(feat|fix|refactor|docs|test|chore)\([a-zA-Z0-9_.-]+\):\s+\S.+$')
PROTECTED = {'version.json', 'docs/RELEASES.md', '.github/release-plan.json'}
PREPARATION_FILES = PROTECTED | {'docs/UNRELEASED.md'}
PENDING_MARKER = '## 待发布\n'


def pending_lines(text):
    """Keep every existing nonblank contribution, even if its style is imperfect."""
    text = text.replace('\r\n', '\n')
    if text.count(PENDING_MARKER) != 1:
        raise ValueError('UNRELEASED.md must contain exactly one pending section')
    return Counter(line.strip() for line in text.split(PENDING_MARKER, 1)[1].splitlines()
                   if line.strip())


def pending_warnings(text):
    """Formatting helps the maintainer's rollup, but does not block a code PR."""
    pending_lines(text)
    try:
        pending_entries(text.replace('\r\n', '\n'))
    except ValueError:
        return ['Contribution style: prefer one-line * 【新增/优化/修复/文档/维护】description entries in docs/UNRELEASED.md.']
    return []


def validate(event, base_files, head_files, changed, root=ROOT, *, metadata_advisory=False, warnings=None):
    """Default strict mode remains available; CI opts into advisory prose checks.

    Release identity, protected files and preservation are always mandatory.
    ``warnings`` is an optional list populated with nonblocking suggestions.
    """
    pr = event.get('pull_request', {})
    title, body = pr.get('title', ''), pr.get('body') or ''
    suggestions = warnings if warnings is not None else []

    def suggest(message):
        if metadata_advisory:
            suggestions.append(message)
        else:
            raise ValueError(message)

    if not isinstance(title, str) or not isinstance(body, str):
        raise ValueError('Expected a PR title and body as text')
    owner = pr.get('base', {}).get('repo', {}).get('owner', {}).get('login')
    author = pr.get('user', {}).get('login')
    same_repository = pr.get('head', {}).get('repo', {}).get('full_name') == pr.get('base', {}).get('repo', {}).get('full_name')
    release_pr = (same_repository and owner and author in {owner, 'github-actions[bot]'}
                  and pr.get('head', {}).get('ref', '').startswith('codex/release-prepare-')
                  and re.fullmatch(r'chore\(release\): prepare MiaoTask-v0\.\d+\.\d+', title))
    if release_pr:
        if set(changed) != PREPARATION_FILES:
            raise ValueError('A release preparation may only change its four review files')
        previous = json.loads(base_files['version.json'])['version']
        release_ready(root, previous)
        return 'reviewed-release-preparation'
    protected_changes = set(changed) & PROTECTED
    if 'docs/RELEASES.md' in protected_changes:
        before_history = base_files.get('docs/RELEASES.md')
        after_history = head_files.get('docs/RELEASES.md')
        old_repo = 'h0ll0w-AkuZr0guY/' + 'tmall-miao-double11'
        if before_history is not None and after_history == before_history.replace(old_repo, 'h0ll0w-AkuZr0guY/MiaoTask'):
            protected_changes.remove('docs/RELEASES.md')
    if protected_changes:
        raise ValueError('Ordinary PRs must not change versions, published history or release plans')
    if not TITLE.fullmatch(title):
        suggest('PR title: prefer type(scope): short description; a clear equivalent is welcome.')
    for heading in ('## 问题与结果', '## 影响与兼容', '## 实际验证'):
        if heading not in body:
            suggest('PR context: describe the problem/result, compatibility and actual validation; template headings are optional.')
            break
    if metadata_advisory:
        before = pending_lines(base_files['docs/UNRELEASED.md'])
        after = pending_lines(head_files['docs/UNRELEASED.md'])
        suggestions.extend(pending_warnings(head_files['docs/UNRELEASED.md']))
    else:
        before = Counter(pending_entries(base_files['docs/UNRELEASED.md']))
        after = Counter(pending_entries(head_files['docs/UNRELEASED.md']))
    if before - after:
        raise ValueError('Keep existing pending contributions; append your own entry')
    if not after - before:
        suggest('Contribution record: consider appending a short entry to docs/UNRELEASED.md; the maintainer can add it before release.')
    return 'ordinary-contribution'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--event', type=Path, required=True)
    parser.add_argument('--advisory-metadata', action='store_true',
                        help='Warn about prose/entry style; keep release and contribution-preservation protections mandatory')
    args = parser.parse_args()
    try:
        event = json.loads(args.event.read_text(encoding='utf-8'))
        pr = event['pull_request']
        base, head = pr['base']['sha'], pr['head']['sha']
        if not all(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{40}', value) for value in (base, head)):
            raise ValueError('Expected exact PR commit SHAs')
        changed = subprocess.check_output(['git', 'diff', '--name-only', base + '...' + head], cwd=ROOT, text=True).splitlines()
        names = ('version.json', 'docs/UNRELEASED.md', 'docs/RELEASES.md')
        base_files = {name: subprocess.check_output(['git', 'show', base + ':' + name], cwd=ROOT).decode('utf-8-sig') for name in names}
        head_files = {name: subprocess.check_output(['git', 'show', head + ':' + name], cwd=ROOT).decode('utf-8-sig') for name in names}
        warnings = []
        print(validate(event, base_files, head_files, changed,
                       metadata_advisory=args.advisory_metadata, warnings=warnings))
        for warning in warnings:
            print('::warning::' + warning)
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
