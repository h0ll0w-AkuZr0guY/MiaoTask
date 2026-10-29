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
# README and public graphics are maintained directly by the maintainer. Their
# contents, individual filenames, signatures and sizes are outside this audit.
IMAGE_SUFFIXES = {'.png', '.webp', '.jpg', '.jpeg', '.gif', '.svg', '.ico', '.avif', '.bmp', '.tif', '.tiff'}
IMAGE_DIRS = {('docs', 'assets'), ('frontend', 'assets')}
SECRET_PATTERNS = (
    re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b'),
    re.compile(rb'\bAKIA[0-9A-Z]{16}\b'),
    re.compile(rb'\b[A-Za-z]:[\\/]Users[\\/][^\s\\/"\']+'),
)


def is_maintainer_asset(name):
    path = PurePosixPath(name)
    return name == 'README.md' or (path.parts[:2] in IMAGE_DIRS and path.suffix.lower() in IMAGE_SUFFIXES)


def audit_file(name, content, forbidden=()):
    """Return generic reasons, never the matched content."""
    path = PurePosixPath(name)
    problems = []
    if path.is_absolute() or '..' in path.parts or ':' in name or '\\' in name:
        problems.append('unsafe path')
    if name != 'build/README.md' and any(part in PRIVATE_PARTS for part in path.parts):
        problems.append('local/private directory')
    if is_maintainer_asset(name):
        return problems
    if name not in ROOT_FILES and (not path.parts or path.parts[0] not in SOURCE_DIRS
                                   or path.suffix not in SOURCE_SUFFIXES):
        problems.append('outside public source allowlist')
    if (path.name.startswith('.env') or path.name.startswith('adbkey')
            or re.search(r'(^|[._-])(device|devices|local|private|credentials)([._-]|$)', path.name, re.I)
            and path.suffix in {'.json', '.txt', '.xml', '.yaml', '.yml'}):
        problems.append('local configuration or credential filename')
    if len(content) > 2_000_000 or b'\0' in content:
        problems.append('binary or oversized source file')
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
