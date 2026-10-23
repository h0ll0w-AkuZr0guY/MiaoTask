"""Install reviewed Python activities; this is deliberately not a code sandbox.

The product reads only the official repository's main source at a pinned commit.
An explicit local archive + digest entry point exists for offline build
verification. The service must hold its global idle gate while mutating packages.
"""
import ast
from functools import lru_cache
import hashlib
import importlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
from urllib.parse import unquote, urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid
import zipfile

from backend.core.plugins.contracts import (ALLOWED_SUFFIXES, CATALOG_URL, FRAMEWORK_API,
    MAX_ARCHIVE_BYTES, MAX_CATALOG_BYTES, MAX_EXPANDED_BYTES, MAX_FILES,
    OFFICIAL_REPOSITORY, PACKAGE_SCHEMA, module_id, package_version)
from backend.core.runtime import activities_dir

_LOCK = threading.RLock()


def _digest(content):
    return hashlib.sha256(content).hexdigest()


def _public_id(value):
    from backend.core.registry import checked_id
    return checked_id(value)


def _sha(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('活动包 SHA256 无效')
    return value


def _declaration(content, name, constructor):
    """Distribution manifests declare literal identities, before Python import."""
    tree = ast.parse(content.decode('utf-8-sig'))
    calls = [node.value for node in tree.body if isinstance(node, ast.Assign)
             and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)]
    if (len(calls) != 1 or not isinstance(calls[0], ast.Call)
            or not isinstance(calls[0].func, ast.Name) or calls[0].func.id != constructor):
        raise ValueError('分发 manifest 必须直接声明 ' + name)
    call = calls[0]
    keywords = {node.arg: node.value for node in call.keywords if node.arg}

    def value(index, key, default=None):
        node = call.args[index] if index < len(call.args) else keywords.get(key)
        try:
            return ast.literal_eval(node) if node is not None else default
        except (ValueError, TypeError):
            raise ValueError('分发 manifest 身份和路径必须使用字面量') from None
    return value


def official_asset_url(value):
    parsed = urlparse(value)
    prefix = '/' + OFFICIAL_REPOSITORY + '/releases/download/'
    if (parsed.scheme != 'https' or parsed.netloc != 'github.com' or parsed.query or parsed.fragment
            or not parsed.path.startswith(prefix) or len(parsed.path[len(prefix):].split('/')) != 2
            or any(part in ('.', '..', '') for part in unquote(parsed.path[len(prefix):]).split('/'))):
        raise ValueError('只允许下载 MiaoTask 官方仓库发布附件')
    return value


def _official_redirect_url(url):
    parsed = urlparse(url)
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.hostname not in
            {'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}):
        raise ValueError('活动下载发生不受信任的重定向')


class _OfficialRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        _official_redirect_url(new_url)
        return super().redirect_request(request, fp, code, message, headers, new_url)


def _bounded_read(response, maximum):
    chunks, size, deadline = [], 0, time.monotonic() + 45
    reader = getattr(response, 'read1', response.read)
    while size <= maximum:
        if time.monotonic() > deadline:
            raise TimeoutError('活动下载超过总时间限制')
        chunk = reader(min(64 * 1024, maximum + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    if size > maximum:
        raise ValueError('活动下载内容过大')
    return b''.join(chunks)


def _download(url, maximum):
    official_asset_url(url)
    request = Request(url, headers={'User-Agent': 'MiaoTask-Activity-Installer', 'Accept': 'application/octet-stream'})
    with build_opener(_OfficialRedirect()).open(request, timeout=20) as response:
        _official_redirect_url(response.geturl())
        return _bounded_read(response, maximum)


def _local_source_url(value, origin=None):
    parsed = urlparse(value)
    try:
        valid_port = parsed.port is not None and 1 <= parsed.port <= 65535
    except ValueError:
        valid_port = False
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not valid_port
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or any(part in ('.', '..') for part in unquote(parsed.path).split('/'))
            or origin is not None and (parsed.scheme, parsed.netloc) != origin):
        raise ValueError('测试活动源仅允许显式启用的同一 127.0.0.1 服务')
    return parsed.scheme, parsed.netloc


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        raise ValueError('本机测试活动源不允许重定向')


def _download_local(url, maximum, origin):
    _local_source_url(url, origin)
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    request = Request(url, headers={'User-Agent': 'MiaoTask-Local-Activity-Test'})
    with opener.open(request, timeout=20) as response:
        _local_source_url(response.geturl(), origin)
        return _bounded_read(response, maximum)


def _atomic_json(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_archive(path, expected_sha256, *, expected_app=None, expected_activity=None):
    """Return metadata and verified payload bytes without executing any manifest."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError('活动压缩包不存在或超过大小限制')
    if _digest(path.read_bytes()) != _sha(expected_sha256):
        raise ValueError('活动压缩包校验失败')
    files, folded, expanded = {}, set(), 0
    with zipfile.ZipFile(path) as archive:
        if len(archive.infolist()) > MAX_FILES:
            raise ValueError('活动压缩包文件过多')
        for info in archive.infolist():
            name = info.filename
            relative = PurePosixPath(name)
            mode = info.external_attr >> 16
            if (not name or '\\' in name or ':' in name or name.startswith('/')
                    or any(part in ('', '.', '..') for part in name.rstrip('/').split('/'))
                    or relative.is_absolute() or name.casefold() in folded
                    or any(part.endswith(('.', ' ')) for part in relative.parts)
                    or any(re.fullmatch(r'(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part, re.I)
                           for part in relative.parts)
                    or stat.S_ISLNK(mode) or info.flag_bits & 1):
                raise ValueError('活动压缩包包含不安全路径或链接')
            folded.add(name.casefold())
            if info.is_dir():
                continue
            if stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                raise ValueError('活动压缩包包含特殊文件')
            expanded += info.file_size
            if (info.file_size > 5 * 1024 * 1024 or expanded > MAX_EXPANDED_BYTES
                    or info.file_size > max(1024 * 1024, info.compress_size * 200)):
                raise ValueError('活动压缩包解压大小或压缩率异常')
            files[name] = archive.read(info)
    if 'manifest.json' not in files:
        raise ValueError('活动压缩包缺少分发清单')
    manifest = json.loads(files.pop('manifest.json').decode('utf-8'))
    if not isinstance(manifest, dict) or manifest.get('schema') != PACKAGE_SCHEMA:
        raise ValueError('活动包清单版本不支持')
    if manifest.get('framework_api') != FRAMEWORK_API:
        raise ValueError('活动包需要不同版本的框架接口，请更新 MiaoTask')
    if manifest.get('python') != f'{sys.version_info.major}.{sys.version_info.minor}':
        raise ValueError('活动包与当前 Python 运行环境不兼容')
    app, activity = _public_id(manifest.get('app')), _public_id(manifest.get('activity'))
    app_module, activity_module = module_id(manifest.get('app_module')), module_id(manifest.get('activity_module'))
    package_version(manifest.get('version'))
    if ('source_commit' in manifest and (not isinstance(manifest['source_commit'], str)
            or manifest['source_commit'] and not re.fullmatch(r'[0-9a-f]{40}', manifest['source_commit']))):
        raise ValueError('活动源码提交标识无效')
    if expected_app is not None and app != expected_app or expected_activity is not None and activity != expected_activity:
        raise ValueError('活动压缩包与所选应用或活动不一致')
    prefix = f'backend/app/{app_module}/'
    activity_prefix = prefix + f'activities/{activity_module}/'
    expected = manifest.get('files')
    if not isinstance(expected, dict) or set(expected) != set(files):
        raise ValueError('活动文件清单不完整或包含额外内容')
    required = {prefix + '__init__.py', prefix + 'manifest.py', prefix + 'configs/base.json',
                activity_prefix + '__init__.py', activity_prefix + 'manifest.py'}
    if not required.issubset(files):
        raise ValueError('活动包缺少应用或活动入口')
    for name, content in files.items():
        relative = name[len(prefix):] if name.startswith(prefix) else ''
        if (not relative or PurePosixPath(name).suffix.lower() not in ALLOWED_SUFFIXES
                or '__pycache__' in PurePosixPath(name).parts
                or relative.startswith('activities/') and relative != 'activities/__init__.py'
                and not name.startswith(activity_prefix)):
            raise ValueError('活动包试图覆盖框架或其他活动')
        if _digest(content) != _sha(expected[name]):
            raise ValueError('活动文件校验失败')
        if name.endswith('.py'):
            ast.parse(content.decode('utf-8-sig'), filename=name)
    declared_app = _declaration(files[prefix + 'manifest.py'], 'APP', 'AppMeta')
    declared_activity = _declaration(files[activity_prefix + 'manifest.py'], 'META', 'ActivityMeta')
    factory = declared_activity(3, 'factory')
    factory_prefix = f'backend.app.{app_module}.activities.{activity_module}.'
    if (declared_app(0, 'id') != app or declared_activity(0, 'app') != app
            or declared_activity(1, 'id') != activity or not isinstance(factory, str)
            or not factory.startswith(factory_prefix) or factory.count(':') != 1):
        raise ValueError('分发身份与 Python manifest 或工厂声明不一致')
    factory_module, factory_class = factory.split(':')
    if (not all(part.isidentifier() for part in factory_module.split('.'))
            or not factory_class.isidentifier() or factory_module.replace('.', '/') + '.py' not in files):
        raise ValueError('活动工厂不属于本次安装包')
    for directory, declared_config in ((prefix, declared_app(3, 'config', 'configs/base.json')),
                                       (activity_prefix, declared_activity(4, 'config', 'config.json'))):
        if (not isinstance(declared_config, str) or '\\' in declared_config
                or any(part in ('', '.', '..') for part in declared_config.split('/'))
                or directory + declared_config not in files):
            raise ValueError('活动配置路径无效或缺失')
    return manifest, files


class PluginManager:
    def __init__(self, root=None, *, catalog_url=None, allow_local=False, source_provider=None):
        self.root = Path(root).resolve() if root is not None else activities_dir()
        self.apps_dir = self.root / 'apps'
        self.root.mkdir(parents=True, exist_ok=True)
        self.apps_dir.mkdir(parents=True, exist_ok=True)
        self.cache_path = self.root / 'catalog.json'
        self.state_path = self.root / 'installed.json'
        self.last_error = ''
        self.catalog_url = catalog_url or CATALOG_URL
        self.local_origin = _local_source_url(self.catalog_url) if allow_local else None
        self.source_provider = None
        if not allow_local:
            if self.catalog_url != CATALOG_URL:
                raise ValueError('正常运行只能使用内置官方活动目录')
            from backend.core.plugins.repository import GitHubSourceProvider
            self.source_provider = source_provider or GitHubSourceProvider()
        elif source_provider is not None:
            raise ValueError('本机验证源不能同时指定在线源码提供方')

    def _fetch(self, url, maximum):
        return (_download_local(url, maximum, self.local_origin) if self.local_origin is not None
                else _download(url, maximum))

    def _state(self):
        if not self.state_path.is_file():
            return {'schema': 1, 'packages': []}
        state = json.loads(self.state_path.read_text(encoding='utf-8'))
        if not isinstance(state, dict) or state.get('schema') != 1 or not isinstance(state.get('packages'), list):
            raise ValueError('本地活动安装记录损坏，请保留现场后重新安装活动')
        return state

    def installed(self):
        with _LOCK:
            return [dict(item) for item in self._state()['packages']]

    @staticmethod
    def _validate_catalog(value, *, local_origin=None, source_only=False):
        if isinstance(value, dict) and value.get('source') == 'github-source':
            if local_origin is not None:
                raise ValueError('本机验证源不能混入在线源码目录')
            from backend.core.plugins.repository import validate_source_catalog
            return validate_source_catalog(value)
        if source_only:
            raise ValueError('旧活动附件目录已停用，请读取官方 main 源码')
        if (not isinstance(value, dict) or value.get('schema') != PACKAGE_SCHEMA
                or value.get('repository') != OFFICIAL_REPOSITORY or not isinstance(value.get('packages'), list)):
            raise ValueError('官方活动目录格式无效')
        identities = set()
        for item in value['packages']:
            if not isinstance(item, dict):
                raise ValueError('活动目录项目无效')
            key = (_public_id(item.get('app')), _public_id(item.get('activity')))
            if key in identities:
                raise ValueError('活动目录包含重复项目')
            identities.add(key)
            module_id(item.get('app_module')); module_id(item.get('activity_module'))
            package_version(item.get('version')); _sha(item.get('sha256'))
            if local_origin is None:
                official_asset_url(item.get('url', ''))
            else:
                _local_source_url(item.get('url', ''), local_origin)
            if not isinstance(item.get('size'), int) or not 0 < item['size'] <= MAX_ARCHIVE_BYTES:
                raise ValueError('活动目录压缩包大小无效')
            if not isinstance(item.get('framework_api'), int) or not isinstance(item.get('python'), str):
                raise ValueError('活动目录缺少兼容声明')
        return value

    def catalog(self, refresh=False):
        """Read cache by default; explicit refresh resolves official main once."""
        with _LOCK:
            cached = None
            if self.cache_path.is_file():
                try:
                    cached = self._validate_catalog(json.loads(self.cache_path.read_text(encoding='utf-8')), local_origin=self.local_origin, source_only=self.source_provider is not None)
                except (ValueError, OSError):
                    pass
            self.last_error = ''
            if refresh or cached is None:
                try:
                    value = (self.source_provider.catalog() if self.source_provider is not None
                             else json.loads(self._fetch(self.catalog_url, MAX_CATALOG_BYTES)))
                    value = self._validate_catalog(value, local_origin=self.local_origin, source_only=self.source_provider is not None)
                    _atomic_json(self.cache_path, value)
                    return value, False
                except Exception as exc:
                    self.last_error = '无法更新官方活动目录：' + str(exc)
                    if cached is None:
                        return {'schema': 1, 'repository': OFFICIAL_REPOSITORY, 'packages': []}, False
            return cached, True

    def snapshot(self, registry=None, refresh=False):
        catalog, cached = self.catalog(refresh)
        installed = self.installed()
        by_id = {(p['app'], p['activity']): p for p in installed}
        applications, packages = {}, []
        for item in catalog['packages']:
            entry = dict(item)
            local = by_id.get((item['app'], item['activity']))
            entry.update(id=item['activity'], installed=local is not None,
                         installed_version=local['version'] if local else '',
                         installed_source_commit=local.get('source_commit', '') if local else '',
                         installed_revision=local.get('source_commit', '')[:12] if local else '',
                         update_available=bool(local and (local.get('source_commit') != item.get('source_commit')
                             if item.get('source') == 'github-source' else local['version'] != item['version'])),
                         compatible=item['framework_api'] == FRAMEWORK_API
                         and item['python'] == f'{sys.version_info.major}.{sys.version_info.minor}',
                         source='local-test' if self.local_origin else 'github-source')
            packages.append(entry)
            app = applications.setdefault(item['app'], {'id': item['app'], 'label': item.get('app_label', item['app']), 'activities': []})
            app['activities'].append(entry)
        return {'framework_api': FRAMEWORK_API, 'repository': OFFICIAL_REPOSITORY,
                'catalog': list(applications.values()), 'packages': packages,
                'installed': installed, 'error': self.last_error, 'cached': cached,
                'source': 'local-test' if self.local_origin else 'github-source', 'test_source': self.local_origin is not None,
                'source_commit': catalog.get('source_commit', ''), 'source_ref': catalog.get('source_ref', ''),
                'revision': catalog.get('revision', ''), 'warnings': catalog.get('warnings', []),
                'activities_dir': str(self.root), 'source_dir': str(self.apps_dir),
                'code_notice': '活动来自官方源码，包含 Python 代码；安装前核对来源，不提供沙箱隔离。'}

    def install(self, app, activity, *, source_commit=None):
        # A catalog request already in flight before the service's idle gate
        # must not replace this provider's pinned tree during source download.
        with _LOCK:
            return self._install_selected(app, activity, source_commit=source_commit)

    def _install_selected(self, app, activity, *, source_commit=None):
        app, activity = _public_id(app), _public_id(activity)
        catalog, _ = self.catalog(refresh=self.source_provider is None)
        if self.source_provider is not None and self.last_error:
            raise ValueError(self.last_error)
        entry = next((p for p in catalog['packages'] if (p['app'], p['activity']) == (app, activity)), None)
        if entry is None:
            raise ValueError(self.last_error or '官方目录中没有此活动')
        if self.source_provider is not None:
            from backend.core.plugins.repository import commit_id
            if source_commit is not None and commit_id(source_commit) != entry['source_commit']:
                raise ValueError('已展示的源码目录已变化，请刷新后重新确认活动')
            if self.source_provider.commit != entry['source_commit']:
                pinned = self.source_provider.catalog(commit=entry['source_commit'])
                entry = next((p for p in pinned['packages'] if (p['app'], p['activity']) == (app, activity)), None)
                if entry is None:
                    raise ValueError('当前固定提交中的活动声明已无效')
        with tempfile.TemporaryDirectory(prefix='download-', dir=self.root) as folder:
            archive = Path(folder) / 'activity.zip'
            if self.source_provider is not None:
                digest = self.source_provider.build_archive(entry, archive)
            else:
                content = self._fetch(entry['url'], MAX_ARCHIVE_BYTES)
                if len(content) != entry['size']:
                    raise ValueError('活动下载大小与验证目录不一致')
                archive.write_bytes(content)
                digest = entry['sha256']
            return self.install_local(archive, digest, expected_app=app, expected_activity=activity)

    def install_local(self, archive_path, expected_sha256, *, expected_app=None, expected_activity=None):
        """Explicit offline verifier entry point; never exposed as an arbitrary URL API."""
        manifest, files = validate_archive(archive_path, expected_sha256,
                                           expected_app=expected_app, expected_activity=expected_activity)
        with _LOCK:
            state = self._state()
            app, activity = manifest['app'], manifest['activity']
            app_module, activity_module = manifest['app_module'], manifest['activity_module']
            existing = [p for p in state['packages'] if p['app'] == app]
            if any(p['app_module'] != app_module for p in existing):
                raise ValueError('同一应用的 Python 模块身份发生变化')
            if any(p['app'] != app and p['app_module'] == app_module for p in state['packages']):
                raise ValueError('应用模块名称与其他已安装应用冲突')
            if any(p['activity'] != activity and p['activity_module'] == activity_module for p in existing):
                raise ValueError('活动模块名称与其他已安装活动冲突')
            target = self.apps_dir / app_module
            prefix = f'backend/app/{app_module}/'
            activity_relative = f'activities/{activity_module}/'
            shared_files = {name[len(prefix):]: _digest(content) for name, content in files.items()
                            if not name[len(prefix):].startswith(activity_relative)}
            for old in existing:
                if old['activity'] == activity:
                    continue
                if old.get('shared_files') is not None and old['shared_files'] != shared_files:
                    raise ValueError('应用共享组件与其他已安装活动版本不同，请先卸载该应用的其他活动后再升级')
                for name, content in files.items():
                    relative = name[len(prefix):]
                    current = target / relative
                    if not relative.startswith(activity_relative) and current.is_file() and _digest(current.read_bytes()) != _digest(content):
                        raise ValueError('应用共享组件与其他已安装活动版本不同，请先卸载该应用的其他活动后再升级')
            if target.exists() and any(path.is_symlink() for path in target.rglob('*')):
                raise ValueError('已安装活动目录存在符号链接，拒绝更新')
            with tempfile.TemporaryDirectory(prefix='stage-', dir=self.root) as folder:
                staged = Path(folder) / app_module
                if target.is_dir():
                    shutil.copytree(target, staged, ignore=shutil.ignore_patterns('__pycache__'))
                else:
                    staged.mkdir()
                # New package contents define the shared app surface exactly.
                # Do not retain removed modules/assets from an earlier version.
                for current in list(staged.rglob('*')):
                    if not current.is_file():
                        continue
                    relative = current.relative_to(staged).as_posix()
                    if not relative.startswith('activities/') and relative not in shared_files:
                        current.unlink()
                for old in existing:
                    if old['activity'] == activity:
                        old_activity = staged / 'activities' / old['activity_module']
                        if old_activity.is_dir():
                            shutil.rmtree(old_activity)
                for name, content in files.items():
                    destination = staged / name[len(prefix):]
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(content)
                self._validate_staged(staged, app, activity)
                record = {key: manifest[key] for key in ('app', 'activity', 'app_module', 'activity_module', 'version', 'framework_api', 'python')}
                record['sha256'] = expected_sha256
                record['label'] = manifest.get('label', activity)
                record['app_label'] = manifest.get('app_label', app)
                record['shared_files'] = shared_files
                for field in ('source', 'source_ref', 'source_commit'):
                    if field in manifest:
                        record[field] = manifest[field]
                next_state = {'schema': 1, 'packages': [p for p in state['packages'] if (p['app'], p['activity']) != (app, activity)] + [record]}
                self._swap(target, staged, next_state)
            return record

    @staticmethod
    def _validate_staged(staged, app, activity):
        """Resolve reviewed imports/config before swapping, restoring live modules.

        This intentionally executes trusted official activity imports. It checks
        that a new activity works with this exact frozen framework and its bundled
        dependencies; it never installs dependencies or treats code as sandboxed.
        """
        from backend.core.config.loader import load_resolved
        from backend.core.registry import Registry
        package = importlib.import_module('backend.app')
        namespace = 'backend.app.' + staged.name
        saved_modules = {name: module for name, module in list(sys.modules.items())
                         if name == namespace or name.startswith(namespace + '.')}
        saved_path = list(package.__path__)
        saved_marker = getattr(package, '_miaotask_plugin_paths', ())
        saved_attribute = getattr(package, staged.name, None)
        for name in saved_modules:
            sys.modules.pop(name, None)
        try:
            registry = Registry(staged.parent / '_empty', plugin_dir=staged.parent).discover()
            registration = registry.get(app, activity)
            if not callable(registration.factory()):
                raise ValueError('活动工厂不可调用')
            load_resolved(registration)
        finally:
            for name in list(sys.modules):
                if name == namespace or name.startswith(namespace + '.'):
                    sys.modules.pop(name, None)
            sys.modules.update(saved_modules)
            package.__path__ = saved_path
            package._miaotask_plugin_paths = saved_marker
            if saved_attribute is not None:
                setattr(package, staged.name, saved_attribute)
            elif hasattr(package, staged.name):
                delattr(package, staged.name)
            importlib.invalidate_caches()

    def _swap(self, target, staged, next_state):
        backup = self.root / ('rollback-' + uuid.uuid4().hex)
        moved = target.exists()
        if moved:
            os.replace(target, backup)
        committed = False
        try:
            os.replace(staged, target)
            _atomic_json(self.state_path, next_state)
            committed = True
        except Exception:
            if target.exists():
                shutil.rmtree(target)
            if moved:
                os.replace(backup, target)
            raise
        finally:
            if committed and backup.exists():
                # A successful commit remains successful if Windows temporarily
                # holds an old imported file. A leftover backup is never loaded.
                shutil.rmtree(backup, ignore_errors=True)

    def uninstall(self, app, activity):
        app, activity = _public_id(app), _public_id(activity)
        with _LOCK:
            state = self._state()
            record = next((p for p in state['packages'] if (p['app'], p['activity']) == (app, activity)), None)
            if record is None:
                raise ValueError('此活动尚未安装')
            target = self.apps_dir / module_id(record['app_module'])
            next_state = {'schema': 1, 'packages': [p for p in state['packages'] if (p['app'], p['activity']) != (app, activity)]}
            with tempfile.TemporaryDirectory(prefix='stage-', dir=self.root) as folder:
                staged = Path(folder) / target.name
                shutil.copytree(target, staged, ignore=shutil.ignore_patterns('__pycache__'))
                activity_path = staged / 'activities' / module_id(record['activity_module'])
                if activity_path.is_dir():
                    shutil.rmtree(activity_path)
                if not any(p['app'] == app for p in next_state['packages']):
                    # Keep a directory until the atomic state swap has succeeded.
                    shutil.rmtree(staged)
                    staged.mkdir()
                self._swap(target, staged, next_state)
                if not any(p['app'] == app for p in next_state['packages']):
                    shutil.rmtree(target, ignore_errors=True)
            return {'app': app, 'activity': activity, 'removed': True}


@lru_cache(maxsize=1)
def default_plugin_manager():
    return PluginManager()


def reload_plugin_registry():
    """Called only behind the service's global idle gate; return a fresh registry."""
    from backend.core.registry import default_registry
    package = importlib.import_module('backend.app')
    removed = set()
    for name in list(sys.modules):
        if (name.startswith('backend.app.') and name != 'backend.app.common'
                and not name.startswith('backend.app.common.')):
            removed.add(name.split('.')[2])
            del sys.modules[name]
    for name in removed:
        if hasattr(package, name):
            delattr(package, name)
    importlib.invalidate_caches()
    default_registry.cache_clear()
    return default_registry()
