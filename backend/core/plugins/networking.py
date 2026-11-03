"""Proxy configuration shared by the official activity download paths.

Both the GitHub API/raw fetcher and release-asset downloads build their
opener here. Without explicit configuration urllib keeps using the system
and ``HTTPS_PROXY`` environment proxies; ``MIAO_DOWNLOAD_PROXY`` can
point those downloads at a specific proxy or force a direct connection when
the system proxy is unavailable. Local 127.0.0.1 test sources never use it.

One catalog refresh issues many small requests, so the proxy choice is logged
at DEBUG and a dropped handshake is retried here instead of failing the whole
update. A LAN proxy regularly loses the occasional TLS handshake.
"""
import logging
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

logger = logging.getLogger(__name__)

PROXY_ENV = 'MIAO_DOWNLOAD_PROXY'
RETRY_ATTEMPTS = 3
RETRY_PAUSE = 0.6
_DIRECT_TOKENS = frozenset({'off', 'direct', 'none', '0', 'false', 'no'})
_UNSET = object()


def normalize_proxy(raw):
    """Validate one proxy declaration into urllib proxy mappings.

    An empty/direct token returns ``{}`` so ProxyHandler disables proxies.
    Only HTTP(S) proxies are supported (urllib has no native SOCKS support).
    """
    value = (raw or '').strip()
    if not value or value.lower() in _DIRECT_TOKENS:
        return {}
    if '://' not in value:
        value = 'http://' + value
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        if scheme not in ('http', 'https') or not parsed.hostname or not parsed.port:
            raise ValueError
        if not 1 <= parsed.port <= 65535:
            raise ValueError
    except ValueError:
        raise ValueError('下载代理地址无效，示例：http://127.0.0.1:7890；'
                         '仅支持 HTTP/HTTPS 代理，填 off 可强制直连') from None
    # netloc keeps the validated host:port plus optional userinfo; the label
    # used for messages drops the userinfo so credentials never get logged.
    url = f'{scheme}://{parsed.netloc}'
    return {'http': url, 'https': url}


def proxy_label(value):
    """Return ``scheme://host:port`` for one proxy URL, or '' when unparsable."""
    try:
        parsed = urlsplit(value if '://' in value else 'http://' + value)
        if not parsed.hostname or not parsed.port:
            return ''
        return f'{parsed.scheme.lower()}://{parsed.hostname}:{parsed.port}'
    except ValueError:
        return ''


def resolve_proxies(value=_UNSET):
    """Return explicit proxy mappings, {} for direct, or None for defaults.

    Priority: explicit call argument, then the MIAO_DOWNLOAD_PROXY env var
    (non-interactive override), then the saved console preference, then the
    system/HTTPS_PROXY defaults.
    """
    if value is not _UNSET:
        raw = value
    else:
        env_raw = os.environ.get(PROXY_ENV)
        if env_raw is not None:
            raw = env_raw
        else:
            from backend.core.preferences import load_preferences
            raw = load_preferences().get('download_proxy') or None
    if raw is None:
        return None
    return normalize_proxy(raw)


def network_hint(exc):
    """Point a failed refresh at the saved proxy so the setting is actionable."""
    if not isinstance(exc, (URLError, OSError)):
        return ''
    try:
        proxies = resolve_proxies()
    except ValueError:
        return ''
    if not proxies:
        return ''
    label = proxy_label(proxies.get('https') or proxies.get('http') or '')
    if not label:
        return ''
    return f'（当前下载代理 {label}，可在「设置 - 高级选项」填 off 强制直连后重试）'


def fetch_with_retry(fetch, attempts=RETRY_ATTEMPTS, pause=RETRY_PAUSE):
    """Run one idempotent GET, retrying transient proxy/TLS failures.

    A server answer (HTTPError) is final: repeating the request cannot change
    it. Validation errors stay untouched, only connection-level noise retries.
    """
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return fetch()
        except HTTPError:
            raise
        except (URLError, OSError) as exc:
            if attempt >= attempts:
                raise
            logger.warning('活动下载连接中断，%.1f 秒后重试（第 %d/%d 次）：%s',
                           pause * attempt, attempt, attempts, exc)
            time.sleep(pause * attempt)


def official_opener(redirect_handler):
    """Opener for official GitHub downloads with the configured proxy."""
    proxies = resolve_proxies()
    if proxies is None:
        return build_opener(redirect_handler)
    if not proxies:
        logger.debug('活动下载强制直连，不使用系统代理')
    else:
        logger.debug('活动下载使用代理 %s', proxy_label(next(iter(proxies.values()))))
    return build_opener(ProxyHandler(proxies), redirect_handler)
