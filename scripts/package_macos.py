"""Build a tested, self-contained macOS Apple Silicon zip using an explicit file allowlist."""
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
import stat
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent
PORTABLE_DOCS = ('USAGE.md', 'CONFIGURATION.md', 'SAFETY.md', 'RELEASES.md', 'LICENSING.md')
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
        '# MiaoTask（macOS Apple Silicon）\n\n'
        '适用于搭载 Apple Silicon（M 系列芯片）的 macOS，使用系统内置 Vision OCR 与 ADB USB 连接。\n'
        '运行 `./MiaoTask`，保留同目录的 `_internal/`。升级前先退出旧助手。\n\n'
        + activity_help +
        '活动源码位于可执行文件旁的 `activities/`，可在活动中心查看实际位置或在访达中打开文件夹；'
        '日记录与设备信息另存于 `~/Library/Application Support/MiaoTask`。\n\n'
        '[使用说明](docs/USAGE.md) · [风险与隐私](docs/SAFETY.md) · '
        '[发布记录](docs/RELEASES.md) · [许可与声明](docs/LICENSING.md)\n',
        encoding='utf-8')


def source_commit(*, allow_dirty=False):
    """Record the public source revision and refuse an uncommitted release build."""
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    status = subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=normal'],
                                     cwd=ROOT, text=True)
    if status.strip() and not allow_dirty:
        raise RuntimeError('Commit the clean source tree before building a release')
    subprocess.run([sys.executable, 'scripts/check_public_files.py'], cwd=ROOT, check=True)
    return commit


def worktree_snapshot(*, local_source=False):
    """Audit the actual candidate files, including new non-ignored source files."""
    if __package__:
        from .check_public_files import audit_file
    else:
        from check_public_files import audit_file
    if local_source:
        names = [path.relative_to(ROOT).as_posix() for path in ROOT.rglob('*') if path.is_file()
                 and path.relative_to(ROOT).parts[0] not in {'build', 'dist', 'runtime', 'activities', '.local', '.venv', '.git'}
                 and '__pycache__' not in path.parts]
        if (ROOT / 'build/README.md').is_file():
            names.append('build/README.md')
    else:
        names = subprocess.check_output(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                                        cwd=ROOT).decode('utf-8').split('\0')
    digest = hashlib.sha256()
    for name in sorted(set(names) - {''}):
        path = ROOT / name
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT.resolve()):
            raise RuntimeError('Unsafe source file: ' + name)
        digest.update(name.encode('utf-8') + b'\0')
        if not path.is_file():
            digest.update(b'deleted\0')
            continue
        content = path.read_bytes()
        reasons = audit_file(name, content)
        if reasons:
            raise RuntimeError('Public source audit failed: ' + name + ': ' + ', '.join(reasons))
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


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


def selected_packages(catalog, choices, all_activities=False):
    available = {item['app'] + '/' + item['activity']: item for item in catalog['packages']}
    if all_activities:
        if choices:
            raise ValueError('Choose all activities or explicit selections, not both')
        return [item for item in available.values() if item.get('bundle_by_default', False)]
    if len(set(choices)) != len(choices) or any(name not in available for name in choices):
        raise ValueError('Choose distinct registered activities as app/activity')
    return [available[name] for name in choices]


def _write_zip_preserving_mode(bundle, path, arcname):
    info = zipfile.ZipInfo.from_file(path, arcname=str(arcname))
    mode = stat.S_IMODE(path.stat().st_mode)
    info.create_system = 3  # Unix
    info.external_attr = ((stat.S_IFREG | mode) & 0xFFFF) << 16
    with path.open('rb') as src:
        bundle.writestr(info, src.read(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--adb-dir', help='Folder containing macOS adb and NOTICE.txt')
    parser.add_argument('--skip-tests', action='store_true', help='Skip unit tests already run on this commit')
    parser.add_argument('--preview', action='store_true',
                        help='Local candidate only: audit uncommitted source, mark the manifest and ZIP as a preview')
    parser.add_argument('--local-source', action='store_true',
                        help='Build an audited isolated source copy without requiring Git or changing the version')
    parser.add_argument('--include-activities', action='store_true',
                        help='Bundle source activities for offline compatibility checks')
    parser.add_argument('--preinstall-activity', action='append', default=[], metavar='APP/ACTIVITY',
                        help='Preinstall a selected source activity beside the executable')
    parser.add_argument('--preinstall-all', action='store_true',
                        help='Preinstall source activities whose bundle_by_default flag is true')
    parser.add_argument('--framework-only', action='store_true',
                        help='Exclude default-preinstalled activities')
    args = parser.parse_args()
    if args.framework_only and (args.preinstall_all or args.preinstall_activity or args.include_activities):
        parser.error('Pure framework cannot be combined with activity selections')
    if sys.platform != 'darwin' or platform.machine().lower() != 'arm64':
        raise SystemExit('Build this package on macOS Apple Silicon (arm64)')
    if args.local_source and args.preview:
        parser.error('--local-source and --preview are separate build modes')
    if args.include_activities and (args.preinstall_activity or args.preinstall_all):
        parser.error('Use either embedded activities or selected external preinstallation')
    revision = '' if args.local_source else source_commit(allow_dirty=args.preview)
    snapshot = worktree_snapshot(local_source=args.local_source)
    source_dirty = False if args.local_source else bool(
        subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip()
    )
    adb = shutil.which('adb')
    adb_dir = Path(args.adb_dir).resolve() if args.adb_dir else Path(adb).parent if adb else ROOT / 'build/vendor/platform-tools'
    if not all((adb_dir / name).is_file() for name in ('adb', 'NOTICE.txt')):
        raise SystemExit('macOS ADB missing: run python scripts/fetch_platform_tools.py or pass --adb-dir')
    if not args.skip_tests:
        subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-q'], cwd=ROOT, check=True)
    version = json.loads((ROOT / 'version.json').read_text(encoding='utf-8'))['version']
    if not re.fullmatch(r'MiaoTask-v[0-9]+\.[0-9]+\.[0-9]+', version):
        raise SystemExit('Invalid release version')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    dist = (ROOT / 'dist' / f'macos-{stamp}').resolve()
    work = (ROOT / 'build' / f'pyinstaller-{stamp}').resolve()
    if not dist.is_relative_to(ROOT / 'dist') or not work.is_relative_to(ROOT / 'build'):
        raise SystemExit('Build path escapes the workspace')
    env = os.environ.copy()
    env['MIAO_ADB_DIR'] = str(adb_dir)
    env['MIAO_INCLUDE_ACTIVITIES'] = '1' if args.include_activities else '0'
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--distpath', str(dist),
                    '--workpath', str(work), str(ROOT / 'build.spec')], env=env, cwd=ROOT, check=True)
    package = dist / 'MiaoTask'
    executable = package / 'MiaoTask'
    copy_documentation(package, include_activities=args.include_activities)
    licenses(package / 'licenses', adb_dir)
    from smoke_packaged import smoke
    from package_activities import build_packages
    activity_folder = work / 'activity-smoke'
    catalog = build_packages(ROOT, activity_folder, 'activity-smoke', version.split('-v')[-1],
                             require_clean=not args.preview, local_source=args.local_source)
    smoke_archives = [(activity_folder / entry['url'].rsplit('/', 1)[-1], entry['sha256'])
                      for entry in catalog['packages']]
    smoke_result = smoke(executable, package / 'self-test.json', activity_packages=smoke_archives)
    policy = json.loads((package / '_internal/bundle-policy.json').read_text(encoding='utf-8'))
    manifest = dict(
        version=version, platform='macos-arm64', python=platform.python_version(),
        source_commit=revision, source_repository='https://github.com/heyunl/MiaoTask-macOS',
        preview=args.preview, local_source=args.local_source, source_dirty=source_dirty, source_tree_sha256=snapshot,
        bundle_mode=policy['bundle_mode'], framework_api=policy['framework_api'],
        embedded_activities=policy['embedded_activity_modules'],
        verified_activity_imports=smoke_result['actual_activity_imports'],
        built_utc=datetime.now(timezone.utc).isoformat(),
        packages={d.metadata['Name']: d.version for d in metadata.distributions()},
        adb_bundled=True, ocr_backend='vision', smoke_test_passed=True,
    )
    selections = selected_packages(
        catalog, args.preinstall_activity,
        args.preinstall_all or not (args.preinstall_activity or args.framework_only or args.include_activities),
    )
    if selections:
        from backend.core.plugins.manager import PluginManager
        manager = PluginManager(root=package / 'activities')
        from backend.core.plugins.recovery import RecoveryStore
        recovery = RecoveryStore(package / '_internal/activity-recovery')
        installed = []
        for entry in selections:
            path = activity_folder / entry['url'].rsplit('/', 1)[-1]
            manager.install_local(path, entry['sha256'])
            recovery.remember(path, entry['sha256'])
            installed.append({key: entry[key] for key in ('app', 'activity', 'version', 'sha256')})
        report = work / 'preinstalled-self-test.json'
        check_env = dict(env, MIAO_DATA_DIR=str(work / 'preinstalled-check-data'),
                         MIAO_ACTIVITIES_DIR=str(package / 'activities'))
        subprocess.run([str(executable), '--self-test', '--output', str(report)],
                       env=check_env, timeout=120, check=True)
        proof = json.loads(report.read_text(encoding='utf-8'))
        if not proof.get('ok') or not proof.get('frozen') or proof['checks']['registered_activities'] != len(selections):
            raise RuntimeError('Selected activity preinstallation failed frozen validation')
        manifest.update(preinstalled_activities=installed, preinstalled_activity_self_test=True)
        smoke_result['preinstalled'] = proof
        (package / 'self-test.json').write_text(json.dumps(smoke_result, ensure_ascii=False, indent=2), encoding='utf-8')
        help_path = package / 'README.md'
        help_path.write_text(help_path.read_text(encoding='utf-8').replace(
            '默认仅包含框架与运行环境。首次打开网页，在「活动中心」下载需要的应用与活动。',
            '本包预装：' + '、'.join(item['app'] + '/' + item['activity'] for item in installed) + '。其他活动从「活动中心」按需获取。'), encoding='utf-8')
    (package / 'build-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    if snapshot != worktree_snapshot(local_source=args.local_source):
        raise RuntimeError('Source changed during the build; rebuild the final candidate')
    suffix = ('-with-activities' if args.include_activities else '') + ('-preview' if args.preview else '')
    archive = ROOT / 'dist' / f'{version}-macos-arm64{suffix}.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in sorted(package.rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                if path.is_symlink() or not path.resolve().is_relative_to(package.resolve()):
                    raise RuntimeError(f'Unsafe package member: {path.name}')
                _write_zip_preserving_mode(bundle, path, Path('MiaoTask') / path.relative_to(package))
    with archive.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    archive.with_suffix(archive.suffix + '.sha256').write_text(f'{digest}  {archive.name}\n', encoding='ascii')
    print(json.dumps(dict(archive=str(archive), exe=str(executable), bytes=archive.stat().st_size,
                          sha256=digest), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
