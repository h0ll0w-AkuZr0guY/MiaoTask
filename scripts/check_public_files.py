"""Audit Git's actual index before a public push; never print secret values."""
import argparse
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

ROOT = Path(__file__).resolve().parent.parent
ROOT_FILES = {
    '.gitignore', '.gitattributes', '.github/CODEOWNERS', 'README.md', 'AGENTS.md', 'LICENSE', 'NOTICE', 'main.py', 'build.spec', 'start.bat',
    'build/README.md', 'requirements.txt', 'requirements-build.txt', 'requirements-windows.lock', 'version.json',
}
SOURCE_DIRS = {'.github', 'configs', 'docs', 'modules', 'scripts', 'tests', 'web', 'frontend', 'backend', 'shared'}
PRIVATE_PARTS = {'runtime', 'private', 'screenshots', 'captures', 'reports', 'backups',
                 '.venv', 'venv', 'env', 'build', 'dist', '__pycache__', '.ruff_cache', '.idea', '.vscode'}
SOURCE_SUFFIXES = {'.py', '.md', '.json', '.yml', '.yaml', '.html', '.css', '.js', '.txt', '.lock'}
# Individually reviewed documentation graphics only. Never allow arbitrary captures.
PUBLIC_IMAGES = {
    'docs/assets/miaotask-overview.webp': 'webp',
    'docs/assets/feature-wifi.webp': 'webp',
    'docs/assets/feature-devices.webp': 'webp',
    'docs/assets/feature-queue.webp': 'webp',
    'docs/assets/feature-scroll.webp': 'webp',
}
PUBLIC_IMAGES.update({f'frontend/assets/miaotask-{size}.png': 'png'
                      for size in (16, 20, 24, 32, 40, 48, 64, 128, 180, 256, 512, 1024)})
PUBLIC_IMAGES.update({'frontend/assets/miaotask-master.png': 'png', 'frontend/assets/miaotask.ico': 'ico'})
SECRET_PATTERNS = (
    re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b'),
    re.compile(rb'\bAKIA[0-9A-Z]{16}\b'),
    re.compile(rb'\b[A-Za-z]:[\\/]Users[\\/][^\s\\/"\']+'),
)


def audit_file(name, content, forbidden=()):
    """Return generic reasons, never the matched content."""
    path = PurePosixPath(name)
    problems = []
    if path.is_absolute() or '..' in path.parts or ':' in name or '\\' in name:
        problems.append('unsafe path')
    if name != 'build/README.md' and any(part in PRIVATE_PARTS for part in path.parts):
        problems.append('local/private directory')
    image_type = PUBLIC_IMAGES.get(name)
    if not image_type and name not in ROOT_FILES and (not path.parts or path.parts[0] not in SOURCE_DIRS
                                   or path.suffix not in SOURCE_SUFFIXES):
        problems.append('outside public source allowlist')
    if (path.name.startswith('.env') or path.name.startswith('adbkey')
            or re.search(r'(^|[._-])(device|devices|local|private|credentials)([._-]|$)', path.name, re.I)
            and path.suffix in {'.json', '.txt', '.xml', '.yaml', '.yml'}):
        problems.append('local configuration or credential filename')
    if len(content) > 2_000_000 or not image_type and b'\0' in content:
        problems.append('binary or oversized source file')
    if image_type:
        valid = (image_type == 'png' and content.startswith(b'\x89PNG\r\n\x1a\n')
                 or image_type == 'webp' and content.startswith(b'RIFF') and content[8:12] == b'WEBP'
                 or image_type == 'ico' and content.startswith(b'\x00\x00\x01\x00') and len(content) > 6
                 and int.from_bytes(content[4:6], 'little') > 0)
        if not valid:
            problems.append('invalid public image format')
    if any(pattern.search(content) for pattern in SECRET_PATTERNS):
        problems.append('credential or personal machine path')
    if any(value and value.encode('utf-8') in content for value in forbidden):
        problems.append('private local identifier')
    return problems


def audit_index(forbidden=()):
    names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode('utf-8').split('\0')
    names = [name for name in names if name]
    if not names:
        raise RuntimeError('No staged or tracked source files to audit')
    problems = []
    for name in names:
        content = subprocess.check_output(['git', 'show', ':' + name], cwd=ROOT)
        reasons = audit_file(name, content, forbidden)
        if reasons:
            problems.append({'file': name, 'reasons': reasons})
    return dict(ok=not problems, files=len(names), problems=problems)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--forbid-file', help='Private JSON string array kept OUTSIDE the repository')
    args = parser.parse_args()
    forbidden = []
    if args.forbid_file:
        source = Path(args.forbid_file).resolve()
        if source.is_relative_to(ROOT):
            raise SystemExit('Keep the private identifier file outside the repository')
        forbidden = json.loads(source.read_text(encoding='utf-8'))
        if not isinstance(forbidden, list) or not all(isinstance(value, str) for value in forbidden):
            raise SystemExit('The private identifier file must be a JSON string array')
    result = audit_index(forbidden)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['ok'] else 1)


if __name__ == '__main__':
    main()
