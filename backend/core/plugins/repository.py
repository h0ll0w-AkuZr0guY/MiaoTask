"""Read reviewed activity source at a pinned official Git commit, without eval."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
import time
from urllib.parse import quote, unquote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

from backend.core.plugins.contracts import (ALLOWED_SUFFIXES, CATALOG_URL, FRAMEWORK_API,
    LEGACY_COMPATIBLE_MANIFESTS, MAX_ARCHIVE_BYTES, MAX_CATALOG_BYTES, MAX_EXPANDED_BYTES,
    MAX_FILES, MAX_SOURCE_FILE_BYTES, MAX_TREE_BYTES, OFFICIAL_REPOSITORY,
    PACKAGE_SCHEMA, SOURCE_REF, module_id, package_version)


def commit_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{40}', value):
        raise ValueError('GitHub 源码提交标识无效')
    return value


def safe_source_path(value):
    if (not isinstance(value, str) or not value or '\\' in value or ':' in value or value.startswith('/')
            or any(part in ('', '.', '..') or part.endswith(('.', ' ')) for part in value.split('/'))
            or any(re.fullmatch(r'(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part, re.I)
                   for part in value.split('/'))):
        raise ValueError('GitHub 源码路径无效')
    return value


def source_url(value):
    parsed = urlparse(value)
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('活动源码仅允许官方 GitHub HTTPS 地址')
    api_prefix = '/repos/' + OFFICIAL_REPOSITORY + '/'
    raw_prefix = '/' + OFFICIAL_REPOSITORY + '/'
    if parsed.netloc == 'api.github.com' and parsed.path.startswith(api_prefix):
        tail = parsed.path[len(api_prefix):]
        allowed = (tail == 'git/ref/heads/' + SOURCE_REF and not parsed.query
                   or re.fullmatch(r'git/commits/[0-9a-f]{40}', tail) and not parsed.query
                   or re.fullmatch(r'git/trees/[0-9a-f]{40}', tail) and parsed.query == 'recursive=1')
        if allowed:
            return value
    if parsed.netloc == 'raw.githubusercontent.com' and parsed.path.startswith(raw_prefix) and not parsed.query:
        remainder = parsed.path[len(raw_prefix):].split('/', 1)
        if len(remainder) == 2:
            commit_id(remainder[0]); safe_source_path(unquote(remainder[1]))
            return value
    raise ValueError('活动源码地址超出官方仓库或固定提交')


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        raise ValueError('官方源码地址发生重定向，请检查仓库地址或网络')


def download_source(url, maximum):
    source_url(url)
    request = Request(url, headers={'User-Agent': 'MiaoTask-GitHub-Activities',
                      'Accept': 'application/vnd.github+json' if urlparse(url).hostname == 'api.github.com' else 'application/octet-stream',
                      'X-GitHub-Api-Version': '2022-11-28'})
    with build_opener(_NoRedirect()).open(request, timeout=15) as response:
        source_url(response.geturl())
        content, size, deadline = [], 0, time.monotonic() + 30
        reader = getattr(response, 'read1', response.read)
        while size <= maximum:
            if time.monotonic() > deadline:
                raise TimeoutError('读取 GitHub 活动源码超时')
            block = reader(min(64 * 1024, maximum + 1 - size))
            if not block:
                break
            size += len(block); content.append(block)
        if size > maximum:
            raise ValueError('GitHub 活动源码内容超过大小限制')
        return b''.join(content)


def git_blob_sha(content):
    return hashlib.sha1(b'blob ' + str(len(content)).encode('ascii') + b'\0' + content).hexdigest()


def validate_source_catalog(value):
    from backend.core.registry import checked_id
    if (not isinstance(value, dict) or value.get('schema') != PACKAGE_SCHEMA
            or value.get('repository') != OFFICIAL_REPOSITORY or value.get('source') != 'github-source'
            or value.get('source_ref') != SOURCE_REF or not isinstance(value.get('packages'), list)):
        raise ValueError('GitHub 活动源码目录格式无效')
    revision = commit_id(value.get('source_commit'))
    seen = set()
    for item in value['packages']:
        if not isinstance(item, dict):
            raise ValueError('GitHub 活动源码目录项目无效')
        key = checked_id(item.get('app')), checked_id(item.get('activity'))
        if key in seen:
            raise ValueError('GitHub 活动源码目录包含重复活动')
        seen.add(key)
        module_id(item.get('app_module')); module_id(item.get('activity_module'))
        package_version(item.get('version'))
        if (item.get('source') != 'github-source' or item.get('source_commit') != revision
                or type(item.get('framework_api')) is not int or not isinstance(item.get('python'), str)
                or not re.fullmatch(r'\d+\.\d+', item['python'])):
            raise ValueError('GitHub 活动源码缺少固定提交或兼容声明')
    return value


class GitHubSourceProvider:
    """Small metadata reads, followed by only the explicitly selected source tree."""
    def __init__(self, fetch=None, *, clock=None):
        self.fetch = fetch or download_source
        self.clock = clock or time.monotonic
        self.deadline = None
        self.commit = ''
        self.blobs = {}
        self.contents = {}
        self.entries = {}

    def _check_budget(self):
        if self.deadline is not None and self.clock() > self.deadline:
            raise TimeoutError('GitHub 活动源码操作超过总时间限制，请检查网络后重试')

    def _read(self, url, maximum):
        self._check_budget()
        content = self.fetch(url, maximum)
        self._check_budget()
        return content

    def _json(self, url, maximum=MAX_CATALOG_BYTES):
        value = json.loads(self._read(url, maximum))
        if not isinstance(value, dict):
            raise ValueError('GitHub 源码响应格式无效')
        return value

    def _resolve(self, pinned=None):
        if pinned is None:
            ref = self._json(CATALOG_URL)
            if (ref.get('ref') != 'refs/heads/' + SOURCE_REF or not isinstance(ref.get('object'), dict)
                    or ref['object'].get('type') != 'commit'):
                raise ValueError('无法确认官方仓库 main 分支')
            commit = commit_id(ref['object'].get('sha'))
        else:
            commit = commit_id(pinned)
        base = f'https://api.github.com/repos/{OFFICIAL_REPOSITORY}/git/'
        description = self._json(base + 'commits/' + commit)
        if description.get('sha') != commit or not isinstance(description.get('tree'), dict):
            raise ValueError('GitHub 提交与分支快照不一致')
        tree_sha = commit_id(description['tree'].get('sha'))
        tree = self._json(base + 'trees/' + tree_sha + '?recursive=1', MAX_TREE_BYTES)
        if tree.get('sha') != tree_sha or tree.get('truncated') or not isinstance(tree.get('tree'), list):
            raise ValueError('GitHub 源码目录不完整，不能继续安装')
        blobs, folded = {}, set()
        for item in tree['tree']:
            if not isinstance(item, dict) or item.get('type') not in ('blob', 'tree', 'commit'):
                raise ValueError('GitHub 源码目录条目无效')
            path = safe_source_path(item.get('path'))
            if path.casefold() in folded:
                raise ValueError('GitHub 源码目录包含大小写冲突')
            folded.add(path.casefold())
            if item['type'] == 'blob':
                commit_id(item.get('sha'))
                if type(item.get('size')) is not int or item['size'] < 0:
                    raise ValueError('GitHub 源码文件大小无效')
                blobs[path] = dict(item)
        self.commit, self.blobs, self.contents, self.entries = commit, blobs, {}, {}

    def _content(self, path):
        self._check_budget()
        safe_source_path(path)
        blob = self.blobs.get(path)
        if (not blob or blob.get('mode') not in ('100644', '100755')
                or blob['size'] > MAX_SOURCE_FILE_BYTES):
            raise ValueError('活动源码缺失、过大或不是普通文件：' + path)
        if path not in self.contents:
            url = f'https://raw.githubusercontent.com/{OFFICIAL_REPOSITORY}/{self.commit}/' + quote(path, safe='/')
            content = self._read(url, MAX_SOURCE_FILE_BYTES)
            if len(content) != blob['size'] or git_blob_sha(content) != blob['sha']:
                raise ValueError('GitHub 源码文件摘要与固定提交不一致：' + path)
            self.contents[path] = content
        return self.contents[path]

    def _compatibility(self, path, app_content, activity_content):
        if path in self.blobs:
            value = json.loads(self._content(path))
            if (not isinstance(value, dict) or value.get('schema') != 1
                    or type(value.get('framework_api')) is not int
                    or not isinstance(value.get('python'), str) or not re.fullmatch(r'\d+\.\d+', value['python'])):
                raise ValueError('活动 compat.json 必须声明 schema、framework_api 和 python')
            return value['framework_api'], value['python'], 'declared'
        pair = hashlib.sha256(app_content).hexdigest(), hashlib.sha256(activity_content).hexdigest()
        if pair not in LEGACY_COMPATIBLE_MANIFESTS:
            raise ValueError('新活动或已修改的活动 manifest 缺少 compat.json，不能推断框架兼容性')
        return 1, '3.13', 'reviewed-legacy'

    def catalog(self, commit=None):
        from backend.core.plugins.manager import _declaration
        from backend.core.registry import checked_id
        self.deadline = self.clock() + 60
        self._resolve(commit)
        packages, warnings, ids, owners = [], [], set(), {}
        app_paths = sorted(path for path in self.blobs if re.fullmatch(r'backend/app/[a-z][a-z0-9_]*/manifest\.py', path))
        for app_path in app_paths:
            app_module = app_path.split('/')[2]
            if app_module == 'common':
                continue
            try:
                module_id(app_module)
                app_content = self._content(app_path)
                app = _declaration(app_content, 'APP', 'AppMeta')
                app_id, app_label = checked_id(app(0, 'id')), app(1, 'label')
                if not isinstance(app_label, str) or not app_label or len(app_label) > 120:
                    raise ValueError('应用名称无效')
                if app_id in owners and owners[app_id] != app_module:
                    raise ValueError('不同源码目录不能声明相同应用 ID')
                owners[app_id] = app_module
            except (ValueError, SyntaxError, UnicodeError) as exc:
                warnings.append(f'{app_module}: {exc}'); continue
            prefix = f'backend/app/{app_module}/activities/'
            for path in sorted(path for path in self.blobs if path.startswith(prefix)
                               and re.fullmatch(r'[a-z][a-z0-9_]*/manifest\.py', path[len(prefix):])):
                activity_module = path[len(prefix):].split('/')[0]
                try:
                    module_id(activity_module)
                    content = self._content(path)
                    activity = _declaration(content, 'META', 'ActivityMeta')
                    identity, label = checked_id(activity(1, 'id')), activity(2, 'label')
                    factory = activity(3, 'factory')
                    if (activity(0, 'app') != app_id or not isinstance(label, str) or not label or len(label) > 180
                            or not isinstance(factory, str)
                            or factory.count(':') != 1
                            or not factory.startswith(f'backend.app.{app_module}.activities.{activity_module}.')):
                        raise ValueError('活动身份或工厂声明不属于本目录')
                    factory_module, factory_class = factory.split(':')
                    if (not all(part.isidentifier() for part in factory_module.split('.'))
                            or not factory_class.isidentifier()
                            or factory_module.replace('.', '/') + '.py' not in self.blobs):
                        raise ValueError('活动工厂缺失或路径无效')
                    api, python, compatibility = self._compatibility(path.rsplit('/', 1)[0] + '/compat.json', app_content, content)
                    drivers = activity(5, 'device_types', ('uiautomator2', 'hmdriver2'))
                    if not isinstance(drivers, (list, tuple)) or not all(isinstance(value, str) for value in drivers):
                        raise ValueError('活动设备类型声明无效')
                    key = app_id, identity
                    if key in ids:
                        raise ValueError('应用或活动 ID 重复')
                    ids.add(key)
                    entry = dict(app=app_id, activity=identity, app_module=app_module,
                        activity_module=activity_module, app_label=app_label, label=label,
                        framework_api=api, python=python, source='github-source', source_ref=SOURCE_REF,
                        source_commit=self.commit, revision=self.commit[:12],
                        version='0.0.0+git.' + self.commit[:12], device_types=list(drivers), compatibility=compatibility)
                    packages.append(entry); self.entries[key] = entry
                except (ValueError, SyntaxError, UnicodeError) as exc:
                    warnings.append(f'{app_module}/{activity_module}: {exc}')
        return validate_source_catalog(dict(schema=PACKAGE_SCHEMA, framework_api=FRAMEWORK_API,
            repository=OFFICIAL_REPOSITORY, source='github-source', source_ref=SOURCE_REF,
            source_commit=self.commit, revision=self.commit[:12], packages=packages, warnings=warnings))

    def build_archive(self, entry, destination):
        """Download only this app's shared files and one selected activity."""
        self.deadline = self.clock() + 120
        key = entry.get('app'), entry.get('activity')
        if (entry.get('source_commit') != self.commit or key not in self.entries
                or self.entries[key] != entry):
            raise ValueError('活动源码快照已变化，请重新读取目录')
        if entry['framework_api'] != FRAMEWORK_API or entry['python'] != f'{sys.version_info.major}.{sys.version_info.minor}':
            raise ValueError('活动源码与当前框架接口或 Python 环境不兼容')
        prefix = f'backend/app/{entry["app_module"]}/'
        activity_prefix = prefix + f'activities/{entry["activity_module"]}/'
        selected = []
        for path, blob in sorted(self.blobs.items()):
            if not path.startswith(prefix):
                continue
            relative = path[len(prefix):]
            if relative.startswith('activities/') and relative != 'activities/__init__.py' and not path.startswith(activity_prefix):
                continue
            if '__pycache__' in PurePosixPath(path).parts or PurePosixPath(path).suffix.lower() not in ALLOWED_SUFFIXES:
                continue
            if blob.get('mode') not in ('100644', '100755'):
                raise ValueError('所选活动包含链接或特殊文件，不能安装')
            selected.append(path)
        if (len(selected) > MAX_FILES - 1 or sum(self.blobs[path]['size'] for path in selected) > MAX_EXPANDED_BYTES):
            raise ValueError('所选活动源码超过文件数量或大小限制')
        payload = {path: self._content(path) for path in selected}
        manifest = {key: entry[key] for key in ('app', 'activity', 'app_module', 'activity_module',
                   'app_label', 'label', 'framework_api', 'python', 'version', 'source_commit', 'source_ref', 'source')}
        manifest.update(schema=PACKAGE_SCHEMA, files={path: hashlib.sha256(content).hexdigest() for path, content in payload.items()})
        payload['manifest.json'] = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode('utf-8')
        destination = Path(destination)
        with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path, content in sorted(payload.items()):
                info = zipfile.ZipInfo(path, (1980, 1, 1, 0, 0, 0))
                info.external_attr = 0o100644 << 16; info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, content)
        if destination.stat().st_size > MAX_ARCHIVE_BYTES:
            destination.unlink()
            raise ValueError('所选活动生成的安装包过大')
        return hashlib.sha256(destination.read_bytes()).hexdigest()
