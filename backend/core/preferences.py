"""Process-wide user preferences persisted under the private data directory.

OCR runtime is fixed to macOS Apple Silicon Vision ('vision'); download proxy
remains configurable at the console level.
"""
import copy
import logging
import threading


logger = logging.getLogger(__name__)

PREFERENCES_FILE = 'preferences.json'
OCR_CHOICES = ('vision',)
_LEGACY_OCR_CHOICES = ('auto', 'cpu', 'cuda', 'dml')
DEFAULTS = {
    # Empty string: follow system/HTTPS_PROXY; 'off' forces direct,
    # otherwise a validated http(s)://host:port proxy URL.
    'download_proxy': '',
    'ocr_device': 'vision',
}
_LOCK = threading.RLock()


def preferences_path():
    from backend.core.storage.preferences import preferences_path as shared_path
    return shared_path()


def _validate(values, *, allow_legacy_ocr=False):
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
    if not isinstance(ocr_device, str):
        raise ValueError('OCR 后端固定为 vision（macOS Apple Silicon 系统 Vision OCR）')
    normalized_ocr = ocr_device.strip().lower()
    if allow_legacy_ocr and normalized_ocr in _LEGACY_OCR_CHOICES:
        normalized_ocr = 'vision'
    if normalized_ocr not in OCR_CHOICES:
        raise ValueError('OCR 后端固定为 vision（macOS Apple Silicon 系统 Vision OCR）')
    result['ocr_device'] = 'vision'
    return result


def load_preferences(force=False):
    """Read the shared atomic store; migrate legacy OCR choices to fixed Vision."""
    del force
    from backend.core.storage.preferences import read_preferences
    with _LOCK:
        try:
            return _validate(read_preferences(), allow_legacy_ocr=True)
        except ValueError as exc:
            logger.warning('网络/OCR偏好无效，使用默认值：%s', exc)
            return dict(DEFAULTS)


def save_preferences(updates):
    from backend.core.storage.preferences import _LOCK as shared_lock, save_preferences as save_shared
    if not isinstance(updates, dict):
        raise ValueError('偏好设置必须是对象')
    with _LOCK, shared_lock:
        merged = load_preferences()
        if 'ocr_device' in updates:
            _validate({'download_proxy': merged['download_proxy'], 'ocr_device': updates['ocr_device']})
        merged.update({key: value for key, value in updates.items() if key in DEFAULTS})
        merged = _validate(merged)
        save_shared(**merged)
        return copy.deepcopy(merged)


def reset_preferences_cache():
    """Compatibility hook; the shared store is read fresh on each request."""
