"""Process-wide user preferences persisted under the private data directory.

Unlike per-device task config, network and OCR runtime choices apply to the
whole local console. Values are a small validated JSON document; environment
variables stay the highest-priority override for non-interactive launches.
"""
import copy
import json
import logging
import threading
import uuid

from backend.core.runtime import data_dir

logger = logging.getLogger(__name__)

PREFERENCES_FILE = 'preferences.json'
OCR_CHOICES = ('auto', 'cpu', 'cuda')
DEFAULTS = {
    # Empty string: follow system/HTTPS_PROXY; 'off' forces direct,
    # otherwise a validated http(s)://host:port proxy URL.
    'download_proxy': '',
    'ocr_device': 'auto',
}
_LOCK = threading.RLock()
_cache = None


def preferences_path():
    return data_dir() / PREFERENCES_FILE


def _validate(values):
    from backend.core.plugins.networking import normalize_proxy
    result = {}
    proxy = values.get('download_proxy', DEFAULTS['download_proxy'])
    if not isinstance(proxy, str):
        raise ValueError('下载代理必须是文本')
    proxy = proxy.strip()
    if proxy:
        normalize_proxy(proxy)  # raises ValueError with guidance
    result['download_proxy'] = proxy
    ocr_device = values.get('ocr_device', DEFAULTS['ocr_device'])
    if not isinstance(ocr_device, str) or ocr_device.strip().lower() not in OCR_CHOICES:
        raise ValueError('OCR 后端只能是 auto、cpu 或 cuda')
    result['ocr_device'] = ocr_device.strip().lower()
    return result


def load_preferences(force=False):
    """Return current preferences, loading and repairing the file once."""
    global _cache
    with _LOCK:
        if _cache is not None and not force:
            return copy.deepcopy(_cache)
        current = dict(DEFAULTS)
        path = preferences_path()
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(raw, dict):
                current.update(_validate(raw))
        except FileNotFoundError:
            pass
        except (ValueError, OSError) as exc:
            logger.warning('偏好设置无法读取，使用默认值：%s', exc)
        _cache = current
        return copy.deepcopy(current)


def save_preferences(updates):
    """Validate, persist and cache a partial update; returns the full set."""
    global _cache
    if not isinstance(updates, dict):
        raise ValueError('偏好设置必须是对象')
    with _LOCK:
        current = load_preferences()
        merged = dict(current)
        merged.update({k: v for k, v in updates.items() if k in DEFAULTS})
        merged = _validate(merged)
        path = preferences_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        temporary.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + '\n',
                             encoding='utf-8')
        temporary.replace(path)
        _cache = merged
        return copy.deepcopy(merged)


def reset_preferences_cache():
    """Drop the in-memory cache, mainly for tests switching data roots."""
    global _cache
    with _LOCK:
        _cache = None
