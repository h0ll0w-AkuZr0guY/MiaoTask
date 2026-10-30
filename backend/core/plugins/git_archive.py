"""API-independent official Git ref discovery and bounded source snapshots."""
import io
import stat
import zipfile
from backend.core.plugins.contracts import OFFICIAL_REPOSITORY, MAX_FILES, MAX_EXPANDED_BYTES, ALLOWED_SUFFIXES, branch_name

REFS_URL = f'https://github.com/{OFFICIAL_REPOSITORY}.git/info/refs?service=git-upload-pack'
MAX_REPOSITORY_ZIP = 64 * 1024 * 1024


def advertised_branches(content):
    from backend.core.plugins.repository import commit_id
    offset, result = 0, {}
    while offset < len(content):
        if offset + 4 > len(content):
            raise ValueError('Git 分支响应不完整')
        try:
            size = int(content[offset:offset+4], 16)
        except ValueError:
            raise ValueError('Git 分支响应格式无效') from None
        offset += 4
        if size == 0:
            continue
        if size < 4 or offset + size - 4 > len(content):
            raise ValueError('Git 分支数据长度无效')
        line = content[offset:offset+size-4].split(b'\0', 1)[0].strip()
        offset += size - 4
        if b' refs/heads/' not in line:
            continue
        sha, name = line.decode('utf-8').split(' refs/heads/', 1)
        name = branch_name(name)
        if name in result:
            raise ValueError('Git 分支重复')
        result[name] = commit_id(sha)
    if not result:
        raise ValueError('官方仓库未返回分支')
    return result


def source_snapshot(content, commit):
    from backend.core.plugins.repository import commit_id, git_blob_sha, safe_source_path
    commit_id(commit)
    if len(content) > MAX_REPOSITORY_ZIP:
        raise ValueError('官方源码归档过大')
    prefix = OFFICIAL_REPOSITORY.split('/')[-1] + '-' + commit + '/'
    blobs, contents, seen, total = {}, {}, set(), 0
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        if len(archive.infolist()) > 20000:
            raise ValueError('官方源码文件过多')
        for info in archive.infolist():
            if not info.filename.startswith(prefix):
                raise ValueError('官方源码归档与固定提交不符')
            relative = info.filename[len(prefix):].rstrip('/')
            if not relative:
                continue
            safe_source_path(relative)
            if relative.casefold() in seen:
                raise ValueError('官方源码存在大小写冲突')
            seen.add(relative.casefold())
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError('官方源码包含符号链接')
            if info.is_dir() or not relative.startswith('backend/app/'):
                continue
            if '__pycache__' in relative.split('/') or relative.rsplit('.', 1)[-1] not in {suffix[1:] for suffix in ALLOWED_SUFFIXES}:
                continue
            total += info.file_size
            if info.file_size > 5 * 1024 * 1024 or total > MAX_EXPANDED_BYTES or len(blobs) > MAX_FILES * 20:
                raise ValueError('活动源码解压大小超过限制')
            data = archive.read(info)
            contents[relative] = data
            blobs[relative] = dict(mode='100644', size=len(data), sha=git_blob_sha(data))
    return blobs, contents
