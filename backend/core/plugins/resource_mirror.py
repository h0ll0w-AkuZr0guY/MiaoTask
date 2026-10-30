"""Static catalog transport, shared by Pages and reviewed future resource mirrors."""
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import quote
from urllib.request import Request, build_opener

from backend.core.plugins.contracts import MAX_ARCHIVE_BYTES, MAX_CATALOG_BYTES, branch_name

RESOURCE_MIRRORS = ('https://h0ll0w-akuzr0guy.github.io/MiaoTask/activities/',)


def branch_key(source_ref):
    return hashlib.sha256(branch_name(source_ref).encode('utf-8')).hexdigest()


class ResourceMirror:
    def __init__(self, base=RESOURCE_MIRRORS[0], *, fetch=None, source_ref='main'):
        if base not in RESOURCE_MIRRORS:
            raise ValueError('资源镜像未列入维护者审核的来源列表')
        self.base, self.fetch, self.source_ref = base, fetch or self._download, branch_name(source_ref)

    def _download(self, url, maximum):
        from backend.core.plugins.repository import _NoRedirect
        if not url.startswith(self.base):
            raise ValueError('镜像资源超出配置路径')
        with build_opener(_NoRedirect()).open(Request(url, headers={'User-Agent':'MiaoTask-Resources'}), timeout=15) as response:
            parts, size, deadline = [], 0, time.monotonic()+30
            reader = getattr(response, 'read1', response.read)
            while size <= maximum:
                if time.monotonic()>deadline:
                    raise TimeoutError('读取资源镜像超时')
                part = reader(min(65536,maximum+1-size))
                if not part:
                    break
                parts.append(part);size+=len(part)
            content = b''.join(parts)
        if len(content)>maximum:
            raise ValueError('镜像资源超过大小限制')
        return content

    def catalog(self, commit=None):
        from backend.core.plugins.repository import commit_id, validate_source_catalog
        path = ('commits/'+commit_id(commit)+'/'+branch_key(self.source_ref)+'.json' if commit else
                'branches/'+branch_key(self.source_ref)+'.json')
        value = validate_source_catalog(json.loads(self.fetch(self.base+path,MAX_CATALOG_BYTES)))
        if value['source_ref'] != self.source_ref or commit and value['source_commit'] != commit:
            raise ValueError('镜像分支或固定提交不符')
        for entry in value['packages']:
            if (not re.fullmatch(r'packages/[A-Za-z0-9._-]+\.zip', entry.get('archive',''))
                    or not re.fullmatch(r'[0-9a-f]{64}',entry.get('sha256',''))):
                raise ValueError('镜像活动包路径或摘要无效')
        value['transport'] = 'pages'
        self.value = value
        return value

    def build_archive(self, entry, destination):
        from backend.core.plugins.manager import validate_archive
        if entry not in self.value['packages']:
            raise ValueError('镜像快照已变化')
        content = self.fetch(self.base+entry['archive'],MAX_ARCHIVE_BYTES)
        if hashlib.sha256(content).hexdigest()!=entry['sha256']:
            raise ValueError('镜像活动包摘要不符')
        Path(destination).write_bytes(content)
        manifest, _ = validate_archive(destination,entry['sha256'],expected_app=entry['app'],expected_activity=entry['activity'])
        if manifest.get('source_commit') != entry['source_commit']:
            raise ValueError('镜像活动包提交不符')
        return entry['sha256']
