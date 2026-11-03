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
            if not isinstance(value, dict):
                return {}
            config = value.get('config')
            if isinstance(config, dict) and value.get('task_wordlists_schema') != 1:
                from backend.core.config.loader import read_config
                from backend.core.runtime import assets_dir
                defaults = read_config(assets_dir() / 'backend/core/config/defaults.json')
                for name in ('task_search_keywords', 'skip_task_keywords'):
                    if not config.get(name):
                        config[name] = list(defaults[name])
                config.pop('default_skip_task_patterns', None)
                value['task_wordlists_schema'] = 1
                try:
                    _write_preferences(value)
                except OSError:
                    logging.getLogger('backend').warning('词表迁移暂未写入磁盘，本次仍使用已迁移配置')
            return value
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            logging.getLogger('backend').warning('用户配置无法读取，保留原文件并使用默认设置')
            return {}


def save_preferences(**updates):
    with _LOCK:
        value = read_preferences()
        value.update(updates)
        value['task_wordlists_schema'] = 1
        _write_preferences(value)


def _write_preferences(value):
    path = preferences_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
