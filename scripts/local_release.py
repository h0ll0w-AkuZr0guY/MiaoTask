"""Build a distributable ZIP from an isolated source copy, without Git history."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
import zipfile

TOOL_ROOT = Path(__file__).resolve().parents[1]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))
from scripts.check_public_files import (ROOT_FILES, SOURCE_DIRS, SOURCE_SUFFIXES,
    PRIVATE_PARTS, is_maintainer_asset, audit_file)
from scripts.prepare_release import MARKER, pending_entries
from scripts.release_notes import extract_notes

MAX_SOURCE_BYTES = 512 * 1024 * 1024
LEGACY_BUILDERS = {'MiaoTask-v0.1.0': '0526a749b3ca54742a2f050c3bdb0c685a17066cd572c4c4902ae5038a72af7d'}
REQUIRED = ('main.py', 'build.spec', 'version.json', 'scripts/package_windows.py',
            'backend/main.py', 'requirements-windows.lock')


def public_name(name):
    path = PurePosixPath(name)
    if name in ROOT_FILES:
        return True
    if not path.parts or path.parts[0] not in SOURCE_DIRS or any(part in PRIVATE_PARTS for part in path.parts):
        return False
    if path.name.startswith(('.env', 'adbkey')) or re.search(
            r'(^|[._-])(device|devices|local|private|credentials)([._-]|$)', path.name, re.I) and path.suffix in {'.json', '.txt', '.xml', '.yaml', '.yml'}:
        return False
    return is_maintainer_asset(name) or path.suffix in SOURCE_SUFFIXES


def checked_write(destination, name, content):
    reasons = audit_file(name, content)
    if reasons:
        raise ValueError('源码含不适合公开的文件：' + name + '（' + ', '.join(reasons) + '）')
    target = destination / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def checked_source_path(root, path):
    """Reject linked ancestors before reading or descending into public paths."""
    root, path = Path(root), Path(path)
    if not path.resolve().is_relative_to(root):
        raise ValueError('源码路径越出输入目录，不能包含外部链接：' + path.relative_to(root).as_posix())
    current = path
    while current != root:
        if current.is_symlink() or getattr(current, 'is_junction', lambda: False)():
            raise ValueError('源码不能包含符号链接或目录联接：' + path.relative_to(root).as_posix())
        current = current.parent


def public_directories(root, folder, directories):
    relative = Path(folder).relative_to(root)
    selected = [name for name in directories if name not in PRIVATE_PARTS and
                name != '.git' and (relative.parts or name in SOURCE_DIRS)]
    for name in selected:
        checked_source_path(root, Path(folder) / name)
    return selected


def copy_source(source, destination):
    """Copy public source only; never copy environment, Git data or device state."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists():
        raise ValueError('源码工作区已存在，请使用新的运行目录')
    if destination.is_relative_to(source):
        raise ValueError('构建工作区必须与输入源码分离，不能放在输入源码内')
    destination.mkdir(parents=True)
    for folder, directories, files in os.walk(source, followlinks=False):
        directories[:] = public_directories(source, folder, directories)
        for filename in files:
            path = Path(folder) / filename
            name = path.relative_to(source).as_posix()
            if public_name(name):
                checked_source_path(source, path)
                checked_write(destination, name, path.read_bytes())
    for name in ROOT_FILES:
        path = source / name
        if '/' in name and path.is_file():
            checked_source_path(source, path)
            checked_write(destination, name, path.read_bytes())
    validate_source(destination)


def validate_source(root):
    missing = [name for name in REQUIRED if not (root / name).is_file()]
    if missing:
        raise ValueError('目录不是完整 MiaoTask 源码，缺少：' + '、'.join(missing))
    if '--local-source' not in (root / 'scripts/package_windows.py').read_text(encoding='utf-8-sig'):
        if not legacy_supported(root):
            raise ValueError('此源码尚未提供本地构建接口，且不属于已核验的旧版入口；请更新源码或使用对应构建器，工具不会替换业务代码')


def legacy_supported(root, original_version=None):
    metadata = json.loads((root / 'version.json').read_text(encoding='utf-8-sig'))
    digest = hashlib.sha256((root / 'scripts/package_windows.py').read_bytes()).hexdigest()
    return LEGACY_BUILDERS.get(original_version or metadata.get('version')) == digest


def legacy_build_worker(root, adb_dir, original_version=None):
    """Adapt one reviewed build entry only, preserving its original application."""
    root = Path(root).resolve()
    if not legacy_supported(root, original_version):
        raise ValueError('旧版构建入口的版本或内容不属于已核验范围，拒绝自动适配')
    sys.path[:0] = [str(root), str(root / 'scripts')]
    spec = importlib.util.spec_from_file_location('_miaotask_verified_legacy_build', root / 'scripts/package_windows.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # The reviewed function only reads git/provenance and calls an index audit.
    # Source content was independently audited before the isolated copy. Do not
    # replace specs, smoke tests, resources, dependencies or application code.
    module.source_commit = lambda: ''
    sys.argv = ['package_windows.py', '--skip-tests', '--adb-dir', str(Path(adb_dir).resolve())]
    module.main()
    version = json.loads((root / 'version.json').read_text(encoding='utf-8-sig'))['version']
    archive = root / 'dist' / f'{version}-windows-x64.zip'
    temporary = archive.with_suffix('.zip.tmp')
    with zipfile.ZipFile(archive) as original, zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as rebuilt:
        for info in original.infolist():
            content = original.read(info)
            if info.filename == 'MiaoTask/build-manifest.json':
                manifest = json.loads(content)
                manifest.update(local_source=True, bundle_mode='full', compatibility_adapter='verified-v0.1.0-build-entry')
                content = json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8')
            rebuilt.writestr(info, content)
    os.replace(temporary, archive)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix('.zip.sha256').write_text(f'{checksum}  {archive.name}\n', encoding='ascii')


def normalized_version(value):
    if isinstance(value, str) and re.fullmatch(r'0\.\d+\.\d+', value):
        value = 'MiaoTask-v' + value
    if not isinstance(value, str) or not re.fullmatch(r'MiaoTask-v0\.\d+\.\d+', value):
        raise ValueError('版本需要使用 MiaoTask-v0.x.y 或 0.x.y')
    return value


def version_numbers(value):
    return tuple(map(int, normalized_version(value).removeprefix('MiaoTask-v').split('.')))


def plan_version(source_version, home, version_mode='patch', target_version=None):
    """Pure preview: a failed/cancelled build never reserves a version."""
    source_version = normalized_version(source_version)
    if target_version:
        return normalized_version(target_version)
    if version_mode not in ('patch', 'minor', 'keep'):
        raise ValueError('版本方式只能是 patch、minor 或 keep')
    if version_mode == 'keep':
        return source_version
    path = Path(home) / 'builder-state.json'
    previous = source_version
    if path.is_file():
        state = json.loads(path.read_text(encoding='utf-8-sig'))
        if state.get('schema') != 1:
            raise ValueError('本地构建版本记录格式无效，请保留记录后检查 builder-state.json')
        previous = normalized_version(state.get('last_successful_version'))
    major, minor, patch = max(version_numbers(source_version), version_numbers(previous))
    minor, patch = (minor + 1, 0) if version_mode == 'minor' else (minor, patch + 1)
    return f'MiaoTask-v{major}.{minor}.{patch}'


def record_success(home, version, archive):
    path = Path(home) / 'builder-state.json'
    greatest = version
    if path.is_file():
        previous = json.loads(path.read_text(encoding='utf-8-sig'))['last_successful_version']
        if version_numbers(previous) > version_numbers(greatest):
            greatest = previous
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(dict(schema=1, last_successful_version=greatest,
        last_built_version=version, archive=str(archive)), ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


@contextmanager
def builder_lock(home):
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    with (home / 'builder.lock').open('a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('同一工具目录已有构建运行，请等待完成，避免重复分配版本') from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def extract_source(archive, destination):
    destination = Path(destination)
    if destination.exists():
        raise ValueError('源码工作区已存在，请使用新的运行目录')
    with zipfile.ZipFile(archive) as bundle:
        infos, names, expanded = bundle.infolist(), set(), 0
        if len(infos) > 30000:
            raise ValueError('源码 ZIP 文件数量过多')
        files = {}
        for info in infos:
            name = info.filename
            parts = name.rstrip('/').split('/')
            mode = info.external_attr >> 16
            if (not name or '\\' in name or ':' in name or name.startswith('/')
                    or any(part in ('', '.', '..') or part.endswith(('.', ' ')) for part in parts)
                    or any(re.fullmatch(r'(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part) for part in parts)
                    or name.casefold() in names or stat.S_ISLNK(mode) or info.flag_bits & 1):
                raise ValueError('源码 ZIP 包含不安全路径、重复项或链接')
            names.add(name.casefold())
            if info.is_dir():
                continue
            if stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                raise ValueError('源码 ZIP 包含特殊文件')
            expanded += info.file_size
            if expanded > MAX_SOURCE_BYTES or info.file_size > 20 * 1024 * 1024:
                raise ValueError('源码 ZIP 过大，请提供源码包，不要打包环境或历史产物')
            files[name] = info
        prefixes = []
        for name in files:
            if name == 'version.json' or name.endswith('/version.json'):
                prefix = name[:-len('version.json')]
                if all(prefix + item in files for item in REQUIRED):
                    prefixes.append(prefix)
        if len(prefixes) != 1:
            raise ValueError('ZIP 中未找到唯一完整 MiaoTask 源码目录')
        prefix = prefixes[0]
        destination.mkdir(parents=True)
        for name, info in files.items():
            if name.startswith(prefix):
                relative = name[len(prefix):]
                if public_name(relative):
                    checked_write(destination, relative, bundle.read(info))
    validate_source(destination)


def github_archive_url(value):
    url = urlsplit(value)
    if url.scheme != 'https' or url.username or url.password or url.query or url.fragment:
        raise ValueError('请提供不带凭据的 HTTPS GitHub 源码链接')
    parts = [part for part in url.path.split('/') if part]
    if url.hostname == 'github.com' and len(parts) >= 2:
        owner, repo = parts[:2]
        repo = repo.removesuffix('.git')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', owner) or not re.fullmatch(r'[A-Za-z0-9_.-]+', repo):
            raise ValueError('GitHub 仓库链接无效')
        if len(parts) == 2:
            return f'https://api.github.com/repos/{owner}/{repo}/zipball'
        if parts[2] == 'tree' and len(parts) > 3:
            return f'https://api.github.com/repos/{owner}/{repo}/zipball/' + quote('/'.join(parts[3:]), safe='')
        if parts[2] == 'archive' and url.path.endswith('.zip'):
            return value
    if url.hostname == 'codeload.github.com' and len(parts) >= 4 and parts[2] in ('zip', 'legacy.zip'):
        return value
    if url.hostname == 'api.github.com' and len(parts) >= 4 and parts[0] == 'repos' and parts[3] == 'zipball':
        return value
    raise ValueError('支持 GitHub 仓库、分支页面或源码 ZIP 链接，不支持下载运行程序')


def download_source(url, target):
    request = Request(github_archive_url(url), headers={'User-Agent': 'MiaoTask-Local-Release-Builder'})
    with urlopen(request, timeout=60) as response:
        final = urlsplit(response.geturl())
        if final.scheme != 'https' or final.hostname not in {'github.com', 'api.github.com', 'codeload.github.com'}:
            raise ValueError('源码下载发生不受信任的重定向')
        with Path(target).open('wb') as output:
            total = 0
            while data := response.read(1024 * 1024):
                total += len(data)
                if total > MAX_SOURCE_BYTES:
                    raise ValueError('源码下载超过大小上限')
                output.write(data)


def rollup_local(root, summary=''):
    """Collect pending contributions under the existing manual version only."""
    root = Path(root)
    metadata = json.loads((root / 'version.json').read_text(encoding='utf-8-sig'))
    version = metadata.get('version')
    if not isinstance(version, str) or not re.fullmatch(r'MiaoTask-v0\.\d+\.\d+', version):
        raise ValueError('version.json 需要手工指定 MiaoTask-v0.x.y')
    if metadata.get('app_version') != version:
        raise ValueError('version 与 app_version 必须手工设置成相同版本')
    if summary and ('\n' in summary or '\r' in summary or len(summary) > 100):
        raise ValueError('更新概述应为不超过 100 字的单行文本')
    pending_path, history_path = root / 'docs/UNRELEASED.md', root / 'docs/RELEASES.md'
    pending = pending_path.read_text(encoding='utf-8-sig')
    entries = pending_entries(pending)
    history = history_path.read_text(encoding='utf-8-sig')
    headings = list(re.finditer(r'^##[ \t]+([^\r\n]+)\r?$', history, re.M))
    matching = [item for item in headings if item[1].strip() == version]
    if len(matching) > 1:
        raise ValueError('发布记录包含重复版本，请先整理')
    if entries:
        lines = [f'* 【{category}】{text}' for category, text in dict.fromkeys(entries)]
        if matching:
            heading = matching[0]
            following = next((item.start() for item in headings if item.start() > heading.start()), len(history))
            body = history[heading.end():following].rstrip()
            additions = [line for line in lines if line not in body.splitlines()]
            if additions:
                bullets = list(re.finditer(r'^\* 【[^\r\n]+', body, re.M))
                position = bullets[-1].end() if bullets else len(body)
                body = body[:position] + '\n' + '\n'.join(additions) + body[position:]
            history = history[:heading.end()] + body + '\n\n' + history[following:]
        else:
            section = f'## {version}\n\n' + ((summary.strip() + '\n\n') if summary.strip() else '') + '\n'.join(lines) + '\n\n'
            position = headings[0].start() if headings else len(history.rstrip())
            history = history[:position].rstrip() + '\n\n' + section + history[position:]
        history_path.write_text(history, encoding='utf-8')
        pending_path.write_text(pending.split(MARKER, 1)[0] + MARKER, encoding='utf-8')
    elif not matching:
        section = f'## {version}\n\n' + (summary.strip() or '按当前源码构建，无新增待发布贡献。') + '\n\n'
        position = headings[0].start() if headings else len(history.rstrip())
        history_path.write_text(history[:position].rstrip() + '\n\n' + section + history[position:], encoding='utf-8')
    return version, len(entries)


def environment_check(python, lock, cwd, env, log):
    code = ("import importlib.metadata as m,pathlib,re,sys; bad=[]\n"
            "if sys.version_info[:2]!=(3,13): bad.append('需要 Python 3.13')\n"
            "for line in pathlib.Path(sys.argv[1]).read_text(encoding='utf-8-sig').splitlines():\n"
            " p=re.fullmatch(r'([A-Za-z0-9_.-]+)==([^ ;]+)',line.strip())\n"
            " if p:\n"
            "  try: actual=m.version(p[1])\n"
            "  except m.PackageNotFoundError: actual='未安装'\n"
            "  if actual!=p[2]: bad.append(p[1]+': '+actual+'，需要 '+p[2])\n"
            "if bad: print('环境与源码锁定依赖不一致，请使用 --create-env：\\n'+'\\n'.join(bad))\n"
            "raise SystemExit(bool(bad))")
    run_logged([python, '-c', code, lock], cwd, env, log)


def export_source(workspace, archive):
    workspace = Path(workspace).resolve()
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for folder, directories, files in os.walk(workspace, followlinks=False):
            directories[:] = public_directories(workspace, folder, directories)
            for filename in sorted(files):
                path = Path(folder) / filename
                name = path.relative_to(workspace).as_posix()
                if public_name(name):
                    checked_source_path(workspace, path)
                    content = path.read_bytes()
                    if audit_file(name, content):
                        raise ValueError('整理后源码未通过公开检查：' + name)
                    bundle.writestr('MiaoTask/' + name, content)
        build_help = workspace / 'build/README.md'
        if build_help.is_file():
            checked_source_path(workspace, build_help)
            bundle.write(build_help, 'MiaoTask/build/README.md')


def validate_activity_choices(activities):
    from backend.core.plugins.repository import commit_id
    from backend.core.registry import checked_id
    if not isinstance(activities, list) or len(activities) > 64:
        raise ValueError('预装活动需要列表，最多选择 64 项')
    seen = set()
    for item in activities:
        if not isinstance(item, dict) or set(item) != {'app', 'activity', 'source_commit'}:
            raise ValueError('预装活动必须包含 app、activity 和固定 source_commit')
        key = checked_id(item['app']), checked_id(item['activity'])
        commit_id(item['source_commit'])
        if key in seen:
            raise ValueError('预装活动不能重复选择')
        seen.add(key)


def install_preselected(destination, activities, downloads, *, provider_factory=None):
    """Fetch only selected official adapters at the exact commits shown in UI."""
    from backend.core.plugins.manager import PluginManager
    from backend.core.plugins.repository import GitHubSourceProvider
    validate_activity_choices(activities)
    if not activities:
        return []
    destination, downloads = Path(destination), Path(downloads)
    downloads.mkdir(parents=True, exist_ok=True)
    manager = PluginManager(root=destination)
    factory = provider_factory or GitHubSourceProvider
    providers, catalogs, installed = {}, {}, []
    for index, item in enumerate(activities):
        revision = item['source_commit']
        if revision not in providers:
            provider = factory()
            catalog = provider.catalog(commit=revision)
            if catalog.get('source_commit') != revision:
                raise ValueError('预装活动的 GitHub 提交发生变化，未继续安装')
            providers[revision], catalogs[revision] = provider, catalog
        provider, catalog = providers[revision], catalogs[revision]
        entry = next((entry for entry in catalog['packages'] if
                      (entry['app'], entry['activity']) == (item['app'], item['activity'])), None)
        if entry is None or entry.get('source_commit') != revision:
            raise ValueError('固定 GitHub 提交中没有所选活动')
        archive = downloads / f'selected-{index}.zip'
        digest = provider.build_archive(entry, archive)
        record = manager.install_local(archive, digest, expected_app=item['app'], expected_activity=item['activity'])
        if record.get('source_commit') != revision:
            raise ValueError('安装活动未保留所选 GitHub 提交')
        installed.append({key: record[key] for key in ('app', 'activity', 'version', 'source_commit', 'sha256')})
        # Offline clients see the latest pinned catalog fetched in this build;
        # installed.json separately preserves every selected activity's commit.
        manager.cache_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding='utf-8')
    return installed


def repack_directory(package, archive):
    temporary = Path(archive).with_suffix('.zip.tmp')
    with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in sorted(Path(package).rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix not in ('.pyc', '.pyo'):
                if path.is_symlink() or not path.resolve().is_relative_to(Path(package).resolve()):
                    raise ValueError('发行包存在不安全链接')
                bundle.write(path, Path('MiaoTask') / path.relative_to(package))
    os.replace(temporary, archive)
    checksum = hashlib.sha256(Path(archive).read_bytes()).hexdigest()
    Path(archive).with_suffix('.zip.sha256').write_text(f'{checksum}  {Path(archive).name}\n', encoding='ascii')


def preload_package(workspace, archive, activities, executable, env, log, run):
    if not activities:
        return []
    packages = list((workspace / 'dist').glob('windows-*/MiaoTask'))
    if len(packages) != 1:
        raise ValueError('不能唯一定位本次核心程序目录')
    package = packages[0]
    manifest_path = package / 'build-manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('bundle_mode') != 'framework' or manifest.get('embedded_activities'):
        raise ValueError('活动预装仅支持不含业务代码的独立框架包')
    installed = install_preselected(package / 'activities', activities, run / 'activity-downloads')
    verify_env = dict(env, MIAO_ACTIVITIES_DIR=str(package / 'activities'),
                      MIAO_DATA_DIR=str(run / 'preinstalled-check-data'))
    verify_env.pop('PYTHONHOME', None)
    verify_env.pop('PYTHONPATH', None)
    report = run / 'preinstalled-self-test.json'
    run_logged([package / 'MiaoTask.exe', '--self-test', '--output', report], run, verify_env, log)
    proof = json.loads(report.read_text(encoding='utf-8'))
    if (not proof.get('ok') or not proof.get('frozen')
            or proof.get('checks', {}).get('registered_activities') != len(installed)):
        raise ValueError('预装活动未通过实际 exe 的离线发现与工厂验证')
    manifest['preinstalled_activities'] = installed
    manifest['preinstalled_activity_self_test'] = True
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    readme = package / 'README.md'
    if readme.is_file():
        content = readme.read_text(encoding='utf-8')
        content = content.replace(
            '默认仅包含框架与运行环境。首次打开网页，在「活动中心」下载需要的应用与活动。',
            '本包已预装构建时勾选的活动，打开网页即可选择使用。更多应用与活动可在「活动中心」按需下载。')
        readme.write_text(content, encoding='utf-8')
    repack_directory(package, archive)
    return installed


def run_logged(command, cwd, env, log):
    print('执行：' + ' '.join(str(value) for value in command), flush=True)
    with Path(log).open('a', encoding='utf-8') as output:
        process = subprocess.Popen([str(value) for value in command], cwd=cwd, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace')
        for line in process.stdout:
            print(line, end='', flush=True)
            output.write(line)
        if process.wait():
            raise RuntimeError('步骤失败，详见日志：' + str(log))


def _build(source, home, *, python=None, adb_dir=None, summary='', create_env=False, write_back_notes=False,
           version_mode='patch', target_version=None, preview=False, activities=None):
    home = Path(home).expanduser().resolve()
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    run = home / 'runs' / stamp
    run.mkdir(parents=True)
    workspace, output = run / 'source', home / 'outputs' / stamp
    raw = str(source).strip().strip('"')
    original = Path(raw).expanduser().resolve() if not raw.startswith('https://') else None
    if preview and write_back_notes:
        raise ValueError('预览构建不回写输入文档，请关闭回写选项')
    if activities is not None:
        validate_activity_choices(activities)
    if write_back_notes and (original is None or not original.is_dir()):
        raise ValueError('--write-back-notes 只适用于源码目录；ZIP/链接会输出整理后的源码 ZIP')
    if raw.startswith('https://'):
        archive = run / 'download.zip'
        print('下载 GitHub 源码…', flush=True)
        download_source(raw, archive)
        extract_source(archive, workspace)
    else:
        path = Path(raw).expanduser().resolve()
        if path.is_dir():
            copy_source(path, workspace)
        elif path.is_file() and path.suffix.lower() == '.zip':
            extract_source(path, workspace)
        else:
            raise ValueError('请输入完整源码目录、源码 ZIP 文件或 GitHub 链接')
    original_metadata = json.loads((workspace / 'version.json').read_text(encoding='utf-8-sig'))
    original_version = normalized_version(original_metadata.get('version'))
    legacy = legacy_supported(workspace)
    if legacy and activities:
        raise ValueError('旧 0.1.0 使用原本内置活动的完整包，活动预装需要新版独立框架源码')
    version = plan_version(original_version, home, version_mode, target_version)
    original_metadata.update(version=version, app_version=version)
    (workspace / 'version.json').write_text(json.dumps(original_metadata, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    version, contributions = rollup_local(workspace, summary)
    if not shutil.which('git') or not shutil.which('node'):
        raise ValueError('本地回归需要 Git 命令行与 Node.js，请先安装并加入 PATH；输入源码不需要 .git、提交或 GitHub 登录')
    executable = Path(python or sys.executable).expanduser().resolve()
    env = os.environ.copy()
    for key in ('PYTHONPATH', 'PYTHONHOME', 'MIAO_DATA_DIR', 'MIAO_ACTIVITIES_DIR', 'MIAO_TEST_ACTIVITY_SOURCE_DIR'):
        env.pop(key, None)
    env['MIAO_DATA_DIR'] = str(run / 'runtime')
    env['PYTHONIOENCODING'] = 'utf-8'
    log = run / 'build.log'
    if create_env:
        environment = home / 'environment'
        if not (environment / 'Scripts/python.exe').is_file():
            run_logged([executable, '-m', 'venv', environment], workspace, env, log)
        executable = environment / 'Scripts/python.exe'
        run_logged([executable, '-m', 'pip', 'install', '-r', workspace / 'requirements-windows.lock'], workspace, env, log)
    run_logged([executable, '-m', 'pip', 'check'], workspace, env, log)
    environment_check(executable, workspace / 'requirements-windows.lock', workspace, env, log)
    run_logged([executable, '-m', 'unittest', 'discover', '-s', 'tests', '-q'], workspace, env, log)
    run_logged([executable, '-m', 'compileall', '-q', 'main.py', 'backend', 'scripts'], workspace, env, log)
    node = shutil.which('node')
    for script in sorted((workspace / 'frontend/js').rglob('*.js')):
        run_logged([node, '--check', script], workspace, env, log)
    if adb_dir is None:
        candidate = TOOL_ROOT / 'build/vendor/platform-tools'
        if (candidate / 'adb.exe').is_file():
            adb_dir = candidate
        else:
            run_logged([executable, 'scripts/fetch_platform_tools.py'], workspace, env, log)
            adb_dir = workspace / 'build/vendor/platform-tools'
    if legacy:
        print('使用已核验的 0.1.0 构建入口：保留旧版完整应用，不注入新框架能力。', flush=True)
        run_logged([executable, Path(__file__).resolve(), '--legacy-build-worker', workspace,
                    '--legacy-original-version', original_version, '--adb-dir', Path(adb_dir).resolve()], workspace, env, log)
    else:
        run_logged([executable, 'scripts/package_windows.py', '--local-source', '--skip-tests', '--adb-dir',
                    Path(adb_dir).resolve()], workspace, env, log)
    archive = workspace / 'dist' / f'{version}-windows-x64.zip'
    if not archive.is_file():
        raise RuntimeError('构建没有生成预期 ZIP')
    preinstalled = preload_package(workspace, archive, activities or [], executable, env, log, run)
    output.mkdir(parents=True)
    target = output / archive.name
    shutil.copy2(archive, target)
    checksum = archive.with_suffix(archive.suffix + '.sha256')
    if checksum.is_file():
        shutil.copy2(checksum, output / checksum.name)
    for name in ('RELEASES.md', 'UNRELEASED.md'):
        destination = output / 'docs' / name
        destination.parent.mkdir(exist_ok=True)
        shutil.copy2(workspace / 'docs' / name, destination)
    history = (workspace / 'docs/RELEASES.md').read_text(encoding='utf-8')
    try:
        notes = extract_notes(history, version)
    except ValueError:
        notes = summary.strip() + '\n' if summary.strip() else '按当前源码构建，无新增待发布贡献。\n'
    (output / 'release-notes.md').write_text(notes, encoding='utf-8')
    source_archive = output / f'{version}-source.zip'
    export_source(workspace, source_archive)
    if write_back_notes:
        for name in ('RELEASES.md', 'UNRELEASED.md'):
            destination = original / 'docs' / name
            if destination.is_symlink() or not destination.resolve().is_relative_to(original):
                raise ValueError('原源码文档路径不安全，未同步更新记录')
        for name in ('RELEASES.md', 'UNRELEASED.md'):
            shutil.copy2(workspace / 'docs' / name, original / 'docs' / name)
    result = dict(ok=True, version=version, source_version=original_version,
                  preview=preview, contributions=contributions, preinstalled_activities=preinstalled,
                  bundle_mode='legacy-full' if legacy else 'framework', archive=str(target),
                  source_workspace=str(workspace), source_archive=str(source_archive),
                  notes=str(output / 'release-notes.md'), notes_written_back=write_back_notes,
                  log=str(log), source_input=raw)
    (output / 'build-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    if not preview:
        record_success(home, version, target)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return result


def build(source, home, *, python=None, adb_dir=None, summary='', create_env=False, write_back_notes=False,
          version_mode='patch', target_version=None, preview=False, activities=None):
    """GUI/CLI entry: serialize version assignment; commit only successful builds."""
    with builder_lock(Path(home).expanduser().resolve()):
        return _build(source, home, python=python, adb_dir=adb_dir, summary=summary,
                      create_env=create_env, write_back_notes=write_back_notes,
                      version_mode=version_mode, target_version=target_version,
                      preview=preview, activities=activities)


def main():
    parser = argparse.ArgumentParser(description='MiaoTask 本地一键测试与发行包构建，不提交或发布 GitHub')
    parser.add_argument('source', nargs='?', help='源码 ZIP、完整目录或 GitHub 源码链接')
    parser.add_argument('--home', type=Path, default=TOOL_ROOT.parent / 'MiaoTask Release Builder')
    parser.add_argument('--python', help='复用已经安装锁定依赖的 Python 环境')
    parser.add_argument('--adb-dir', help='现有官方 platform-tools 目录')
    parser.add_argument('--summary', default='', help='新增版本小节的简短介绍')
    parser.add_argument('--create-env', action='store_true', help='在工具目录创建独立环境并安装源码锁定依赖')
    parser.add_argument('--write-back-notes', action='store_true', help='构建成功后仅同步两份更新文档至输入源码目录')
    parser.add_argument('--legacy-build-worker', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--legacy-original-version', help=argparse.SUPPRESS)
    parser.add_argument('--version-mode', choices=('patch', 'minor', 'keep'), default='patch', help='默认自动增加补丁号；minor增加次版本，keep沿用源码版本')
    parser.add_argument('--target-version', help='手工指定目标版本，优先于自动版本规则')
    parser.add_argument('--preview', action='store_true', help='完整测试和打包，但不消耗本地成功版本计数')
    parser.add_argument('--activities-file', type=Path, help='待预装活动的JSON列表，逐项填写app/activity/source_commit')
    args = parser.parse_args()
    if args.legacy_build_worker:
        if not args.adb_dir:
            parser.error('Legacy build worker requires --adb-dir')
        legacy_build_worker(args.legacy_build_worker, args.adb_dir, args.legacy_original_version)
        return
    source = args.source or input('请粘贴源码 ZIP、源码目录或 GitHub 链接：').strip()
    try:
        build(source, args.home, python=args.python, adb_dir=args.adb_dir,
              summary=args.summary, create_env=args.create_env, write_back_notes=args.write_back_notes,
              version_mode=args.version_mode, target_version=args.target_version, preview=args.preview,
              activities=json.loads(args.activities_file.read_text(encoding='utf-8-sig')) if args.activities_file else None)
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        parser.exit(1, '构建失败：' + str(exc) + '\n')


if __name__ == '__main__':
    main()
