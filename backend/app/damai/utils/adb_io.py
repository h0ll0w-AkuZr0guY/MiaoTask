"""ADB USB tap governor, XML hierarchy dump with shell fallback, and screenshot bridge."""
from __future__ import annotations

from collections import deque
import io
import logging
import random
import re
import subprocess
import time
from typing import Callable

from PIL import Image

from backend.app.damai.utils.hierarchy import parse_bounds, parse_xml
from backend.app.damai.utils.locators.base import Point, Snapshot
from backend.core.device.discovery import resolve_adb_binary
from backend.core.execution.run_control import RunControl

log = logging.getLogger('backend.damai.adb')
FOCUS_RE = re.compile(r'([A-Za-z0-9_.]+/[A-Za-z0-9_.$]+)')


class TapGovernor:
    """Sliding-window rate guard keeping taps <= max_taps per window_s to avoid Baxia captcha."""

    def __init__(self, max_taps: int = 5, window_s: float = 1.5) -> None:
        self.max_taps = max(1, int(max_taps))
        self.window_s = max(0.1, float(window_s))
        self._timestamps: deque[float] = deque()

    def throttle(self, *, now_fn: Callable[[], float] = time.monotonic, wait_fn: Callable[[float], None] = time.sleep) -> float:
        now = now_fn()
        cutoff = now - self.window_s
        while self._timestamps and self._timestamps[0] <= cutoff:
            self._timestamps.popleft()
        waited = 0.0
        if len(self._timestamps) >= self.max_taps:
            sleep_for = (self._timestamps[0] + self.window_s + 0.01) - now
            if sleep_for > 0:
                wait_fn(sleep_for)
                waited = sleep_for
            now = now_fn()
            cutoff = now - self.window_s
            while self._timestamps and self._timestamps[0] <= cutoff:
                self._timestamps.popleft()
        self._timestamps.append(now)
        return waited


class DamaiTransport:
    """Device I/O adapter for Damai using direct ADB USB input tap and resilient observation."""

    def __init__(
        self,
        device,
        control: RunControl | None = None,
        *,
        on_image: Callable[[Image.Image], None] | None = None,
        adb_runner: Callable[..., subprocess.CompletedProcess] | None = None,
        governor: TapGovernor | None = None,
    ) -> None:
        self.device = device
        self.control = control or RunControl()
        self.on_image = on_image
        self._adb_runner = adb_runner
        self.governor = governor or TapGovernor(max_taps=5, window_s=1.5)
        self._adb_binary_cache: str | None = None
        self._last_size: tuple[int, int] = (0, 0)

    @property
    def serial(self) -> str:
        return str(getattr(self.device, 'serial', '') or '').strip()

    def check(self) -> None:
        self.control.check()

    def wait(self, seconds: float) -> None:
        if seconds > 0:
            self.control.wait(seconds)
        else:
            self.control.check()

    def _adb_cmd(self, *args: str) -> list[str]:
        if self._adb_binary_cache is None:
            self._adb_binary_cache = resolve_adb_binary()
        prefix = [self._adb_binary_cache]
        if self.serial:
            prefix.extend(['-s', self.serial])
        prefix.extend(args)
        return prefix

    def run_adb(self, *args: str, timeout: float = 6.0, binary_output: bool = False) -> subprocess.CompletedProcess:
        self.check()
        if self._adb_runner is not None:
            return self._adb_runner(args, timeout=timeout, binary_output=binary_output)
        cmd = self._adb_cmd(*args)
        if binary_output:
            return subprocess.run(cmd, capture_output=True, timeout=timeout, check=True)
        return subprocess.run(
            cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout, check=True
        )

    def tap(self, pt: Point, *, jitter_ms: tuple[int, int] = (0, 0)) -> None:
        """Tap via `adb shell input tap` (never u2.click) with sliding-window anti-risk guard."""
        self.check()
        x, y = int(pt[0]), int(pt[1])
        self.governor.throttle(now_fn=time.monotonic, wait_fn=self.wait)
        last_err: Exception | None = None
        for attempt in range(2):
            try:
                self.run_adb('shell', 'input', 'tap', str(x), str(y), timeout=4.0)
                last_err = None
                break
            except Exception as exc:
                last_err = exc
                if attempt == 0:
                    self.wait(0.08)
        if last_err is not None:
            raise RuntimeError(f'ADB 点击 ({x}, {y}) 失败：{last_err}') from last_err

        lo, hi = int(jitter_ms[0]), int(jitter_ms[1])
        if hi > 0:
            delay_s = random.uniform(max(0, lo), max(lo, hi)) / 1000.0
            if delay_s > 0:
                self.wait(delay_s)

    def current_activity(self) -> str:
        self.check()
        try:
            info = self.device.current_app() or {}
            pkg = str(info.get('package') or '')
            act = str(info.get('activity') or '')
            if pkg and act:
                return act if '/' in act else f'{pkg}/{act}'
            if act or pkg:
                return act or pkg
        except Exception:
            pass
        try:
            proc = self.run_adb('shell', 'dumpsys', 'window', timeout=4.0)
            for line in (proc.stdout or '').splitlines():
                if 'mCurrentFocus' in line or 'mFocusedApp' in line or 'topResumedActivity' in line:
                    match = FOCUS_RE.search(line)
                    if match:
                        return match.group(1)
        except Exception:
            pass
        return ''

    def dump_xml(self) -> str:
        self.check()
        raw = getattr(self.device, 'raw', None)
        dump_fn = getattr(raw, 'dump_hierarchy', None) if raw is not None else None
        if callable(dump_fn):
            for attempt in range(2):
                try:
                    xml = dump_fn(compressed=False)
                    if isinstance(xml, str) and '<hierarchy' in xml:
                        return xml
                except Exception as exc:
                    log.debug('u2 dump_hierarchy 第 %d 次失败：%s', attempt + 1, exc)
                    if attempt == 0:
                        self.wait(0.08)
        # Fallback to adb shell uiautomator dump
        remote = '/sdcard/miaotask_damai_window.xml'
        try:
            self.run_adb('shell', 'uiautomator', 'dump', remote, timeout=8.0)
            proc = self.run_adb('exec-out', 'cat', remote, timeout=5.0)
            if '<hierarchy' in (proc.stdout or ''):
                return proc.stdout
        except Exception as exc:
            log.warning('ADB uiautomator dump 回退失败：%s', exc)
        return ''

    def capture_image(self) -> Image.Image | None:
        self.check()
        try:
            img = self.device.screenshot()
            if img is not None:
                self._last_size = img.size
                if self.on_image is not None:
                    self.on_image(img)
                return img
        except Exception as exc:
            log.debug('DeviceAdapter.screenshot 失败，尝试 ADB screencap：%s', exc)
        try:
            proc = self.run_adb('exec-out', 'screencap', '-p', timeout=6.0, binary_output=True)
            if proc.stdout:
                img = Image.open(io.BytesIO(proc.stdout))
                img.load()
                self._last_size = img.size
                if self.on_image is not None:
                    self.on_image(img)
                return img
        except Exception as exc:
            # FLAG_SECURE (e.g. Alipay H5PayActivity) may block screenshot capture.
            log.debug('ADB screencap 不可用（可能处于收银台安全隐私页）：%s', exc)
        return None

    def _infer_size(self, root, image: Image.Image | None) -> tuple[int, int]:
        if image is not None:
            return image.size
        if root is not None:
            for el in root.iter():
                box = parse_bounds(el.attrib.get('bounds', ''))
                if box and box.right >= 400 and box.bottom >= 600:
                    self._last_size = (box.right, box.bottom)
                    return self._last_size
        if self._last_size != (0, 0):
            return self._last_size
        win_fn = getattr(self.device, 'window_size', None)
        if callable(win_fn):
            try:
                w, h = win_fn()
                if w > 0 and h > 0:
                    self._last_size = (int(w), int(h))
                    return self._last_size
            except Exception:
                pass
        return (1080, 2400)

    def snapshot(self, *, need_image: bool = False) -> Snapshot:
        self.check()
        act = self.current_activity()
        xml = self.dump_xml()
        root = parse_xml(xml)
        img = self.capture_image() if need_image else None
        width, height = self._infer_size(root, img)
        return Snapshot(xml=xml, root=root, image=img, width=width, height=height, activity=act)
