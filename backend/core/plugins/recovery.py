"""Validated local activity archives, retained independently of installed apps."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading
import uuid

_LOCK = threading.RLock()


class RecoveryStore:
    def __init__(self, directory):
        self.root = Path(directory)

    def entries(self):
        try:
            index = self.root / 'index.json'
            if self.root.is_symlink() or index.is_symlink():
                return []
            value = json.loads(index.read_text(encoding='utf-8'))
            if value.get('schema') != 1 or not isinstance(value.get('packages'), list):
                return []
            return [entry for entry in value['packages'] if isinstance(entry, dict)
                    and isinstance(entry.get('sha256'), str)
                    and re.fullmatch(r'[0-9a-f]{64}', entry['sha256'])
                    and entry.get('filename') == entry['sha256'] + '.zip'
                    and all(isinstance(entry.get(key), str) for key in ('app','activity','label','app_label','version'))]
        except (OSError, ValueError, AttributeError):
            return []

    def remember(self, archive, digest):
        from backend.core.plugins.manager import validate_archive, _atomic_json
        manifest, _ = validate_archive(archive, digest)
        with _LOCK:
            if self.root.is_symlink():
                raise ValueError('恢复目录不能是符号链接')
            self.root.mkdir(parents=True, exist_ok=True)
            filename = digest + '.zip'
            temporary = self.root / (uuid.uuid4().hex + '.tmp')
            try:
                shutil.copyfile(archive, temporary)
                os.replace(temporary, self.root / filename)
            finally:
                temporary.unlink(missing_ok=True)
            entry = {key: manifest[key] for key in ('app','activity','version','framework_api','python')}
            entry.update(filename=filename, sha256=digest, label=manifest.get('label', manifest['activity']),
                         app_label=manifest.get('app_label', manifest['app']), source_commit=manifest.get('source_commit',''))
            rows = [row for row in self.entries() if (row['app'],row['activity']) != (entry['app'],entry['activity'])]
            _atomic_json(self.root/'index.json', {'schema':1,'packages':rows+[entry]})
            return entry

    def archive(self, entry):
        path = self.root / entry['filename']
        from backend.core.plugins.contracts import MAX_ARCHIVE_BYTES
        if (not path.is_file() or path.stat().st_size > MAX_ARCHIVE_BYTES or path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve())
                or hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']):
            raise ValueError('离线恢复包校验失败，请重新解压原始 ZIP')
        return path
