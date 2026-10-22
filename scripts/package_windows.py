"""Build a tested, self-contained Windows zip using an explicit file allowlist."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent
PORTABLE_DOCS = ('USAGE.md', 'SAFETY.md', 'RELEASES.md', 'LICENSING.md')
PROJECT_LICENSES = ('LICENSE', 'NOTICE')
LICENSE_TEXT_SUFFIXES = {'', '.txt', '.md', '.rst', '.html', '.apache', '.bsd',
                         '.mit', '.gpl', '.gpl2', '.gpl3', '.gplv2', '.gplv3',
                         '.lgpl', '.mpl', '.zlib', '.isc'}


def copy_documentation(destination, *, include_activities=False):
    """Keep portable help in the same relative layout as the source docs."""
    folder = destination / 'docs'
    folder.mkdir(parents=True, exist_ok=True)
    for name in PORTABLE_DOCS:
        shutil.copy2(ROOT / 'docs' / name, folder / name)
    for name in PROJECT_LICENSES:
        shutil.copy2(ROOT / name, destination / name)
    activity_help = ('本包包含源码提供的活动，可在网页中选择使用。\n\n' if include_activities else
                     '默认仅包含框架与运行环境。首次打开网页，在「活动中心」下载需要的应用与活动。\n\n')
    (destination / 'README.md').write_text(
        '# MiaoTask\n\n'
        '双击 `MiaoTask.exe`，保留 `_internal/`。升级前先退出旧助手。\n\n'
        + activity_help +
        '[使用说明](docs/USAGE.md) · [风险与隐私](docs/SAFETY.md) · '
        '[发布记录](docs/RELEASES.md) · [许可与声明](docs/LICENSING.md)\n',
        encoding='utf-8')


def source_commit():
    """Record the public source revision and refuse an uncommitted release build."""
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    status = subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=normal'],
                                     cwd=ROOT, text=True)
    if status.strip():
        raise RuntimeError('Commit the clean source tree before building a release')
    subprocess.run([sys.executable, 'scripts/check_public_files.py'], cwd=ROOT, check=True)
    return commit


def licenses(destination, adb_dir):
    destination.mkdir(parents=True, exist_ok=True)
    project = destination / 'MiaoTask'
    project.mkdir(parents=True, exist_ok=True)
    for name in PROJECT_LICENSES:
        shutil.copy2(ROOT / name, project / name)
    for dist in metadata.distributions():
        name = dist.metadata['Name']
        copied = 0
        for relative in dist.files or []:
            path = PurePosixPath(str(relative).replace('\\', '/'))
            if path.is_absolute() or '..' in path.parts:
                continue
            # Match declaration filenames, never an ancestor named licenses:
            # packaging/licenses/ also contains Python code and bytecode.
            # Package-local ONNX notices and tqdm's LICENCE remain eligible.
            is_notice = re.search(r'licen[cs]e|copying|notice', path.name, re.I)
            is_attribution = re.fullmatch(r'(?:authors?|copyright)(?:[._-].*)?', path.name, re.I)
            if (not (is_notice or is_attribution)
                    or path.suffix.lower() not in LICENSE_TEXT_SUFFIXES
                    or '__pycache__' in path.parts):
                continue
            source = Path(dist.locate_file(relative))
            if source.is_file() and b'\0' not in source.read_bytes():
                target = destination / name / Path(str(path))
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                copied += 1
        if name.lower().replace('_', '-') == 'flatbuffers' and not copied:
            if dist.version != '25.12.19':
                raise RuntimeError('Review the FlatBuffers license supplement for this version')
            target = destination / name
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / 'LICENSE', target / 'LICENSE')
            shutil.copy2(ROOT / 'docs/licenses/flatbuffers-25.12.19.txt', target / 'NOTICE.txt')
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if python_license.is_file():
        shutil.copy2(python_license, destination / 'Python-LICENSE.txt')
    adb_notice = adb_dir / 'NOTICE.txt'
    if not adb_notice.is_file():
        raise RuntimeError('ADB NOTICE.txt is required for the portable distribution')
    shutil.copy2(adb_notice, destination / 'Android-platform-tools-NOTICE.txt')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--adb-dir', help='Folder containing adb.exe, DLLs and NOTICE.txt')
    parser.add_argument('--hdc-dir', help='Optional authorized HDC runtime folder')
    parser.add_argument('--skip-tests', action='store_true', help='Skip unit tests already run on this commit')
    parser.add_argument('--include-activities', action='store_true',
                        help='Bundle source activities for offline compatibility checks (default: framework only)')
    args = parser.parse_args()
    if sys.platform != 'win32' or platform.machine().lower() not in ('amd64', 'x86_64'):
        raise SystemExit('Build this package on Windows x64')
    revision = source_commit()
    adb = shutil.which('adb')
    adb_dir = Path(args.adb_dir).resolve() if args.adb_dir else Path(adb).parent if adb else ROOT / 'build/vendor/platform-tools'
    if not all((adb_dir / name).is_file() for name in ('adb.exe', 'AdbWinApi.dll', 'AdbWinUsbApi.dll', 'NOTICE.txt')):
        raise SystemExit('ADB missing: run python scripts/fetch_platform_tools.py or pass --adb-dir')
    if not args.skip_tests:
        subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-q'], cwd=ROOT, check=True)
    version = json.loads((ROOT / 'version.json').read_text(encoding='utf-8'))['version']
    if not re.fullmatch(r'MiaoTask-v[0-9]+\.[0-9]+\.[0-9]+', version):
        raise SystemExit('Invalid release version')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    dist = (ROOT / 'dist' / f'windows-{stamp}').resolve()
    work = (ROOT / 'build' / f'pyinstaller-{stamp}').resolve()
    if not dist.is_relative_to(ROOT / 'dist') or not work.is_relative_to(ROOT / 'build'):
        raise SystemExit('Build path escapes the workspace')
    env = os.environ.copy()
    env['MIAO_ADB_DIR'] = str(adb_dir)
    # Always set the value explicitly: an inherited environment cannot silently
    # turn the default framework release into an application bundle.
    env['MIAO_INCLUDE_ACTIVITIES'] = '1' if args.include_activities else '0'
    if args.hdc_dir:
        env['MIAO_HDC_DIR'] = str(Path(args.hdc_dir).resolve())
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--distpath', str(dist),
                    '--workpath', str(work), str(ROOT / 'build.spec')], env=env, cwd=ROOT, check=True)
    package = dist / 'MiaoTask'
    executable = package / 'MiaoTask.exe'
    copy_documentation(package, include_activities=args.include_activities)
    licenses(package / 'licenses', adb_dir)
    from smoke_packaged import smoke
    from package_activities import build_packages
    activity_folder = work / 'activity-smoke'
    catalog = build_packages(ROOT, activity_folder, 'activity-smoke', version.split('-v')[-1])
    smoke_archives = [(activity_folder / entry['url'].rsplit('/', 1)[-1], entry['sha256'])
                      for entry in catalog['packages']]
    smoke_result = smoke(executable, package / 'self-test.json', activity_packages=smoke_archives)
    policy = json.loads((package / '_internal/bundle-policy.json').read_text(encoding='utf-8'))
    manifest = dict(version=version, platform='windows-x64', python=platform.python_version(),
                    source_commit=revision, source_repository='https://github.com/h0ll0w-AkuZr0guY/MiaoTask',
                    bundle_mode=policy['bundle_mode'], framework_api=policy['framework_api'],
                    embedded_activities=policy['embedded_activity_modules'],
                    verified_activity_imports=smoke_result['actual_activity_imports'],
                    built_utc=datetime.now(timezone.utc).isoformat(),
                    packages={d.metadata['Name']: d.version for d in metadata.distributions()},
                    adb_bundled=True, hdc_bundled=(package / '_internal/shared/tools/hdc.exe').exists(),
                    offline_models=True, smoke_test_passed=True)
    (package / 'build-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    suffix = '-with-activities' if args.include_activities else ''
    archive = ROOT / 'dist' / f'{version}-windows-x64{suffix}.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in sorted(package.rglob('*')):
            if path.is_file():
                if path.is_symlink() or not path.resolve().is_relative_to(package.resolve()):
                    raise RuntimeError(f'Unsafe package member: {path.name}')
                bundle.write(path, Path('MiaoTask') / path.relative_to(package))
    with archive.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    archive.with_suffix(archive.suffix + '.sha256').write_text(f'{digest}  {archive.name}\n', encoding='ascii')
    print(json.dumps(dict(archive=str(archive), exe=str(executable), bytes=archive.stat().st_size,
                          sha256=digest), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
