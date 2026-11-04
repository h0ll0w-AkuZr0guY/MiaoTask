"""Process-wide user preferences persisted under the private data directory.

Unlike per-device task config, network and OCR runtime choices apply to the
whole local console. Values are a small validated JSON document; environment
variables stay the highest-priority override for non-interactive launches.
"""
import copy
import logging
import threading


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


def preferences_path():
    from backend.core.storage.preferences import preferences_path as shared_path
    return shared_path()


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
    """Read the shared atomic store; never maintain a competing stale document."""
    from backend.core.storage.preferences import read_preferences
    with _LOCK:
        try:
            return _validate(read_preferences())
        except ValueError as exc:
            logger.warning('网络/OCR偏好无效，使用默认值：%s', exc)
            return dict(DEFAULTS)


def save_preferences(updates):
    from backend.core.storage.preferences import _LOCK as shared_lock, save_preferences as save_shared
    if not isinstance(updates, dict):
        raise ValueError('偏好设置必须是对象')
    with _LOCK, shared_lock:
        merged = load_preferences()
        merged.update({key:value for key,value in updates.items() if key in DEFAULTS})
        merged = _validate(merged)
        save_shared(**merged)
        return copy.deepcopy(merged)


def reset_preferences_cache():
    """Compatibility hook; the shared store is read fresh on each request."""
