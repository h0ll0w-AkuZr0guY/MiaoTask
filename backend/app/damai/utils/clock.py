"""Damai mtop server clock synchronization and interruptible sub-millisecond countdown."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import re
import statistics
import time
from typing import Callable
import urllib.error
import urllib.request

MTOP_TS_URL = 'https://mtop.damai.cn/gw/mtop.common.getTimestamp/'
SELL_RE = re.compile(r'(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})(?::(\d{2}))?')
REL_SELL_RE = re.compile(r'(今天|明天|后天)\s*(\d{1,2}):(\d{2})(?::(\d{2}))?')


def server_offset_ms(samples: int = 5, timeout_s: float = 2.0) -> float:
    """Return `server_ms - local_ms` using median low-RTT mtop samples."""
    measured: list[tuple[float, float]] = []
    headers = {'User-Agent': 'MiaoTask/DamaiClock'}
    for _ in range(max(1, samples)):
        t0 = time.time() * 1000
        try:
            req = urllib.request.Request(MTOP_TS_URL, headers=headers, method='GET')
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                payload = json.loads(resp.read().decode('utf-8', errors='replace'))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            continue
        t1 = time.time() * 1000
        raw_t = (payload.get('data') or {}).get('t') if isinstance(payload, dict) else None
        if raw_t is None:
            continue
        try:
            srv = float(raw_t)
        except (TypeError, ValueError):
            continue
        rtt = t1 - t0
        measured.append((rtt, srv - (t0 + rtt / 2)))
    if not measured:
        raise RuntimeError('无法连接大麦时间接口 mtop.damai.cn 进行时钟校准')
    measured.sort(key=lambda item: item[0])
    best = [offset for _, offset in measured[: max(1, len(measured) // 2 + 1)]]
    return float(statistics.median(best))


def parse_sell_time(text: str, now: datetime | None = None) -> datetime | None:
    """Parse Damai detail sell-time strings such as '04月29日 15:18 开抢' or '今天 20:00'."""
    if not text:
        return None
    ref = now or datetime.now()
    rel = REL_SELL_RE.search(text)
    if rel:
        day_word, hh, mm, ss = rel.groups()
        delta_days = {'今天': 0, '明天': 1, '后天': 2}[day_word]
        base = (ref + timedelta(days=delta_days)).replace(
            hour=int(hh), minute=int(mm), second=int(ss or 0), microsecond=0
        )
        return base

    match = SELL_RE.search(text)
    if not match:
        return None
    yr, mo, dy, hh, mm, ss = match.groups()
    year = int(yr) if yr else ref.year
    dt = datetime(year, int(mo), int(dy), int(hh), int(mm), int(ss or 0))
    if not yr and dt < ref and (ref - dt).days > 180:
        dt = dt.replace(year=ref.year + 1)
    return dt


def parse_config_time(value: str, now: datetime | None = None) -> datetime:
    cleaned = value.strip().replace('T', ' ')
    ref = now or datetime.now()
    for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    for fmt in ('%H:%M:%S.%f', '%H:%M:%S', '%H:%M'):
        try:
            parsed = datetime.strptime(cleaned, fmt)
            return ref.replace(
                hour=parsed.hour,
                minute=parsed.minute,
                second=parsed.second,
                microsecond=parsed.microsecond,
            )
        except ValueError:
            continue
    raise ValueError(f'无法解析开售时间：{value!r}')


def wait_until(
    target_server_ms: float,
    offset_ms: float,
    *,
    control=None,
    on_tick: Callable[[float], None] | None = None,
) -> None:
    """Wait until estimated Damai server clock reaches `target_server_ms`, respecting `RunControl`."""
    target_local_s = (target_server_ms - offset_ms) / 1000.0
    last_tick = 0.0
    while True:
        if control is not None:
            control.check()
        rem = target_local_s - time.time()
        if rem <= 0:
            return
        now_mono = time.monotonic()
        if on_tick is not None and (last_tick == 0.0 or now_mono - last_tick >= 0.5):
            last_tick = now_mono
            on_tick(rem)
        if rem > 0.05:
            chunk = min(0.25, rem - 0.02)
            if control is not None:
                control.wait(chunk)
            else:
                time.sleep(chunk)
        elif rem > 0.002:
            time.sleep(rem - 0.001)
        # Final <= 2ms: spin for sub-millisecond trigger accuracy.
