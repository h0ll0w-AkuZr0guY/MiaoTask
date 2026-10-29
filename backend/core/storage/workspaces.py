"""Persisted console workspaces: caption, last device and task settings.

The running device session, preview, tasks and pairing codes stay in memory;
only the data a user expects back after restarting the console is written to
``workspaces.json`` in the private data directory. The file never contains
Wi-Fi pairing codes, credentials, logs or captured frames.
"""
import copy
import json
import logging
import re
import threading
import uuid
from pathlib import Path

from backend.core.config.schema import validate_config
from backend.core.runtime import data_dir

logger = logging.getLogger(__name__)

STORE_VERSION = 1
WORKSPACES_FILE = 'workspaces.json'
_DEVICE_TYPES = ('uiautomator2', 'hmdriver2')
_FIELDS = ('label', 'device_type', 'serial', 'auto_instances', 'config')


def _workspace_id(value):
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError('工作区标识无效')
    if value != 'default' and not re.fullmatch(r'[a-f0-9-]{1,64}', value):
        raise ValueError('工作区标识无效')
    return value


def _clean(entry):
    """Validate one persisted entry; unknown/malformed fields are dropped."""
    if not isinstance(entry, dict):
        return None
    try:
        key = _workspace_id(entry.get('id'))
    except ValueError:
        return None
    result = {'id': key}
    label = entry.get('label')
    if isinstance(label, str) and 1 <= len(label.strip()) <= 60:
        result['label'] = label.strip()
    device_type = entry.get('device_type')
    if device_type in _DEVICE_TYPES:
        result['device_type'] = device_type
    serial = entry.get('serial')
    if isinstance(serial, str) and 1 <= len(serial) <= 128:
        result['serial'] = serial
    if isinstance(entry.get('auto_instances'), bool):
        result['auto_instances'] = entry['auto_instances']
    config = entry.get('config')
    if isinstance(config, dict):
        candidate = copy.deepcopy(config)
        try:
            validate_config(candidate)
            result['config'] = candidate
        except (ValueError, TypeError):
            logger.warning('工作区 %s 的已保存任务设置无效，恢复为活动默认值', key)
    return result


class WorkspaceStore:
    def __init__(self, path=None):
        self._path = Path(path) if path else data_dir() / WORKSPACES_FILE
        self._lock = threading.RLock()
        self._entries = {}
        self._loaded = False

    @property
    def path(self):
        return self._path

    def _load(self):
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = json.loads(self._path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return
        except (ValueError, OSError) as exc:
            logger.warning('工作区设置无法读取，使用空工作区：%s', exc)
            return
        entries = raw.get('workspaces') if isinstance(raw, dict) else None
        if not isinstance(entries, list) or len(entries) > 16:
            return
        for entry in entries:
            cleaned = _clean(entry)
            if cleaned:
                self._entries[cleaned['id']] = cleaned

    def get(self, key):
        with self._lock:
            self._load()
            return copy.deepcopy(self._entries.get(key))

    def all(self):
        with self._lock:
            self._load()
            return [copy.deepcopy(entry) for entry in self._entries.values()]

    def update(self, key, **fields):
        key = _workspace_id(key)
        unknown = set(fields) - set(_FIELDS)
        if unknown:
            raise ValueError('未知的工作区设置字段：' + ','.join(sorted(unknown)))
        with self._lock:
            self._load()
            entry = self._entries.setdefault(key, {'id': key})
            if 'label' in fields:
                label = fields['label']
                if not isinstance(label, str) or not 1 <= len(label.strip()) <= 60:
                    raise ValueError('设备名称应为 1～60 个字符')
                entry['label'] = label.strip()
            if 'device_type' in fields:
                if fields['device_type'] not in _DEVICE_TYPES:
                    raise ValueError('设备类型无效')
                entry['device_type'] = fields['device_type']
            if 'serial' in fields:
                serial = fields['serial']
                if serial is not None and (not isinstance(serial, str) or not 1 <= len(serial) <= 128):
                    raise ValueError('设备序列号无效')
                if serial:
                    entry['serial'] = serial
                else:
                    entry.pop('serial', None)
            if 'auto_instances' in fields:
                if not isinstance(fields['auto_instances'], bool):
                    raise ValueError('分身开关必须为布尔值')
                entry['auto_instances'] = fields['auto_instances']
            if 'config' in fields:
                config = fields['config']
                if not isinstance(config, dict):
                    raise ValueError('任务设置必须是对象')
                validate_config(copy.deepcopy(config))
                entry['config'] = copy.deepcopy(config)
            self._write_locked()
            return copy.deepcopy(entry)

    def remove(self, key):
        with self._lock:
            self._load()
            if self._entries.pop(key, None) is not None:
                self._write_locked()

    def _write_locked(self):
        payload = {'version': STORE_VERSION,
                   'workspaces': [self._entries[k] for k in sorted(self._entries)]}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(self._path.name + '.' + uuid.uuid4().hex + '.tmp')
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
                             encoding='utf-8')
        temporary.replace(self._path)
