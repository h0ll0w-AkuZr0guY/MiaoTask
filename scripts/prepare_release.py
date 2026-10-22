"""Roll reviewed pending contributions into the unified release history."""
import argparse
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent
MARKER = '## 待发布\n'
CATEGORIES = ('新增', '优化', '修复', '文档', '维护')


def pending_entries(text):
    if text.count(MARKER) != 1:
        raise ValueError('UNRELEASED.md must contain exactly one pending section')
    body = text.split(MARKER, 1)[1]
    entries = []
    for line in body.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r'\* 【(新增|优化|修复|文档|维护)】([^\r\n]+)', line)
        if not match:
            raise ValueError('Pending entries must use * 【分类】说明 (#PR) on one line')
        entries.append((match[1], match[2].strip()))
    return entries


def release_ready(root, previous_version=None):
    """Validate an explicit reviewed preparation; unrelated pushes are not releases."""
    if __package__:
        from .release_notes import extract_notes
    else:
        from release_notes import extract_notes
    root = Path(root)
    metadata = json.loads((root / 'version.json').read_text(encoding='utf-8-sig'))
    version = metadata.get('version')
    if not isinstance(version, str) or not re.fullmatch(r'MiaoTask-v0\.\d+\.\d+', version):
        raise ValueError('Invalid release version')
    if metadata.get('app_version') != version:
        raise ValueError('Both version fields must match')
    plan = json.loads((root / '.github/release-plan.json').read_text(encoding='utf-8-sig'))
    if plan.get('version') != version or not isinstance(plan.get('contributions'), int) or plan['contributions'] < 1:
        raise ValueError('A matching reviewed release preparation is required')
    if previous_version is not None and plan.get('previous_version') != previous_version:
        raise ValueError('Preparation does not match the previous published source version')
    if pending_entries((root / 'docs/UNRELEASED.md').read_text(encoding='utf-8-sig')):
        raise ValueError('Pending contributions remain; update the release preparation first')
    extract_notes((root / 'docs/RELEASES.md').read_text(encoding='utf-8-sig'), version)
    return version


def prepare(root, version, summary):
    if not re.fullmatch(r'MiaoTask-v0\.\d+\.\d+', version):
        raise ValueError('Expected MiaoTask-v0.x.y')
    if not summary.strip() or '\n' in summary or '\r' in summary or len(summary) > 100:
        raise ValueError('Use a short, single-line release summary')
    root = Path(root)
    pending_path, history_path = root / 'docs/UNRELEASED.md', root / 'docs/RELEASES.md'
    pending = pending_path.read_text(encoding='utf-8-sig')
    entries = pending_entries(pending)
    if not entries:
        raise ValueError('No pending contributions')
    history = history_path.read_text(encoding='utf-8-sig')
    if re.search(r'^## ' + re.escape(version) + r'\s*$', history, re.M):
        raise ValueError('Version already exists; do not overwrite release history')
    metadata_path = root / 'version.json'
    metadata = json.loads(metadata_path.read_text(encoding='utf-8-sig'))
    current = metadata.get('version', '')
    def numbers(value):
        return tuple(map(int, value.removeprefix('MiaoTask-v').split('.')))
    if not re.fullmatch(r'MiaoTask-v0\.\d+\.\d+', current) or numbers(version) < numbers(current):
        raise ValueError('Version must not go backwards')
    groups = []
    for category in CATEGORIES:
        values = [value for kind, value in entries if kind == category]
        if values:
            groups.append('* 【' + category + '】' + '；'.join(value.rstrip('。；') for value in values) + '。')
    footer = ('下载 Windows x64 ZIP，完整解压后运行 `MiaoTask.exe`，保留 `_internal/`。升级前退出旧助手。'
              '安全认证需本人处理，诊断截图分享前请脱敏。\n\n'
              '感谢 [MonsterNone/tmall-miao](https://github.com/MonsterNone/tmall-miao) 提供早期实现思路参考。'
              '[使用说明](https://github.com/h0ll0w-AkuZr0guY/MiaoTask/blob/main/docs/USAGE.md) · '
              '[项目介绍](https://github.com/h0ll0w-AkuZr0guY/MiaoTask#readme)\n\n')
    section = '## ' + version + '\n\n' + summary.strip() + '\n\n' + '\n'.join(groups) + '\n\n' + footer
    position = re.search(r'^## ', history, re.M)
    position = position.start() if position else len(history.rstrip()) + 2
    if position > len(history):
        history = history.rstrip() + '\n\n'
        position = len(history)
    metadata.update(version=version, app_version=version)
    plan = dict(version=version, previous_version=current, contributions=len(entries),
                contribution_digest=hashlib.sha256(json.dumps(entries, ensure_ascii=False).encode()).hexdigest())
    # Validate all inputs before changing any of the three reviewable files.
    history_path.write_text(history[:position] + section + history[position:], encoding='utf-8')
    pending_path.write_text(pending.split(MARKER, 1)[0] + MARKER, encoding='utf-8')
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (root / '.github').mkdir(exist_ok=True)
    (root / '.github/release-plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version')
    parser.add_argument('--summary')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--ready', action='store_true', help='Verify the reviewed preparation before building')
    parser.add_argument('--previous-version', help='Expected version of the previous main commit')
    args = parser.parse_args()
    try:
        if args.ready:
            print(release_ready(ROOT, args.previous_version))
        elif args.check:
            pending_entries((ROOT / 'docs/UNRELEASED.md').read_text(encoding='utf-8-sig'))
        else:
            if not args.version or not args.summary:
                parser.error('--version and --summary are required')
            prepare(ROOT, args.version, args.summary)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
