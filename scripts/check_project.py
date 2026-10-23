"""Run the same source audit and offline checks locally and in pull requests."""
import compileall
from pathlib import Path
import shutil
import subprocess
import sys

if __package__:
    from .check_contribution import pending_warnings
    from .check_public_files import audit_file
else:
    from check_contribution import pending_warnings
    from check_public_files import audit_file

ROOT = Path(__file__).resolve().parents[1]


def audit_worktree(root=ROOT):
    """Inspect current public files, including newly added and unstaged changes."""
    root = Path(root).resolve()
    names = subprocess.check_output(
        ['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=root,
    ).decode('utf-8').split('\0')
    problems = []
    count = 0
    for name in sorted(set(names) - {''}):
        path = root / name
        # A tracked directory can be replaced with a junction after checkout.
        # Check every component before reading bytes from a public source path.
        unsafe = not path.resolve().is_relative_to(root)
        current = path
        while current != root and current.is_relative_to(root):
            if current.is_symlink() or current.is_junction():
                unsafe = True
                break
            current = current.parent
        if unsafe:
            count += 1
            problems.append((name, ['linked or outside source path']))
            continue
        if not path.exists() and not path.is_symlink():
            continue  # A tracked deletion is not a file that will be published.
        count += 1
        reasons = audit_file(name, path.read_bytes())
        if reasons:
            problems.append((name, reasons))
    if not count:
        raise ValueError('No public source files found')
    if problems:
        for name, reasons in problems:
            print(name + ': ' + ', '.join(reasons), file=sys.stderr)
        raise ValueError('Public source audit failed; inspect the file paths above')
    return count


def main():
    try:
        print('Checking current public source...', flush=True)
        print(f'{audit_worktree()} files checked', flush=True)
        pending = (ROOT / 'docs/UNRELEASED.md').read_text(encoding='utf-8-sig')
        for message in pending_warnings(pending):
            print('::warning::' + message)
        node = shutil.which('node')
        if not node:
            raise ValueError('Node.js is required for JavaScript syntax checks (no npm install needed)')
        print('Running offline regression tests...', flush=True)
        subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-q'], cwd=ROOT, check=True)
        print('Checking Python syntax...', flush=True)
        if not all(compileall.compile_dir(ROOT / folder, quiet=1) for folder in ('backend', 'scripts', 'tests')):
            raise ValueError('Python syntax check failed')
        if not compileall.compile_file(ROOT / 'main.py', quiet=1):
            raise ValueError('Python entry syntax check failed')
        print('Checking JavaScript syntax...', flush=True)
        for path in sorted((ROOT / 'frontend/js').rglob('*.js')):
            subprocess.run([node, '--check', str(path)], cwd=ROOT, check=True)
        print('Project checks passed. PR metadata suggestions remain advisory.', flush=True)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == '__main__':
    main()
