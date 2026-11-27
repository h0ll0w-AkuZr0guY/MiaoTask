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
    if base:
        return Path(base) / 'preferences.json'
    from backend.core.runtime import user_data_root
    return user_data_root() / 'preferences.json'


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


def read_activity_options(app_id, activity_id):
    if not app_id or not activity_id:
        return {}
    with _LOCK:
        table = read_preferences().get('activity_options')
        if not isinstance(table, dict):
            return {}
        entry = table.get(f'{app_id}/{activity_id}')
        return dict(entry) if isinstance(entry, dict) else {}


def save_activity_options(app_id, activity_id, options):
    if not app_id or not activity_id or not isinstance(options, dict):
        return
    with _LOCK:
        value = read_preferences()
        table = value.get('activity_options')
        if not isinstance(table, dict):
            table = {}
        table[f'{app_id}/{activity_id}'] = dict(options)
        value['activity_options'] = table
        value['task_wordlists_schema'] = 1
        _write_preferences(value)


def secrets_path():
    return preferences_path().with_name('secrets.json')


def _read_secrets_map():
    try:
        value = json.loads(secrets_path().read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logging.getLogger('backend').warning('本地密钥文件无法读取，已忽略损坏内容')
        return {}


def read_activity_secret(app_id, name):
    if not app_id or not name:
        return ''
    with _LOCK:
        app_map = _read_secrets_map().get(str(app_id))
        if not isinstance(app_map, dict):
            return ''
        value = app_map.get(str(name), '')
        return value if isinstance(value, str) else ''


def has_activity_secret(app_id, name):
    return bool(read_activity_secret(app_id, name))


def save_activity_secret(app_id, name, secret_value):
    if not app_id or not name or secret_value is None:
        return
    if not isinstance(secret_value, str) or len(secret_value) > 4096:
        raise ValueError('密钥格式无效')
    with _LOCK:
        data = _read_secrets_map()
        app_map = data.get(str(app_id))
        if not isinstance(app_map, dict):
            app_map = {}
        cleaned = secret_value.strip()
        if cleaned:
            app_map[str(name)] = cleaned
        else:
            app_map.pop(str(name), None)
        if app_map:
            data[str(app_id)] = app_map
        else:
            data.pop(str(app_id), None)
        path = secrets_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def _write_preferences(value):
    path = preferences_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

