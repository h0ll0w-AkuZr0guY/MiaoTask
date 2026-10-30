"""Small atomic user preferences, independent of the installation directory."""
import json
import logging
import os
from pathlib import Path
import threading
import uuid

_LOCK = threading.RLock()


def preferences_path():
    base = os.environ.get('MIAO_DATA_DIR')
    return (Path(base) if base else Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'MiaoTask') / 'preferences.json'


def read_preferences():
    with _LOCK:
        try:
            value = json.loads(preferences_path().read_text(encoding='utf-8'))
            return value if isinstance(value, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            logging.getLogger('backend').warning('用户配置无法读取，保留原文件并使用默认设置')
            return {}


def save_preferences(**updates):
    with _LOCK:
        value = read_preferences()
        value.update(updates)
        path = preferences_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
