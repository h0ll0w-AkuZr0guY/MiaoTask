"""Execution support."""
import threading
import time
import logging
from backend.core.errors import Stopped

class RunControl:
    def __init__(self):
        self.stopped = threading.Event()
        self.paused = threading.Event()
        self._paused_seconds = 0.0
        self._pause_clock_lock = threading.Lock()
        self._pause_started = None
        self.resumed_at = 0.0
        self._cancel_lock = threading.Lock()
        self._cancel_callbacks = set()

    def on_stop(self, callback):
        with self._cancel_lock:
            self._cancel_callbacks.add(callback)
            stopped = self.stopped.is_set()
        if stopped:
            callback()
        return lambda: self._remove_callback(callback)

    def _remove_callback(self, callback):
        with self._cancel_lock:
            self._cancel_callbacks.discard(callback)

    def stop(self):
        self.stopped.set()
        with self._cancel_lock:
            callbacks = tuple(self._cancel_callbacks)
        for callback in callbacks:
            try:
                callback()
            except Exception:
                logging.getLogger(__name__).debug('取消一个私有资源失败，继续关闭其他资源', exc_info=True)

    def check(self):
        if self.paused.is_set():
            with self._pause_clock_lock:
                if self._pause_started is None:
                    self._pause_started = time.monotonic()
            try:
                while self.paused.is_set():
                    if self.stopped.wait(.15):
                        raise Stopped()
            finally:
                with self._pause_clock_lock:
                    if self._pause_started is not None:
                        self.resumed_at = time.monotonic()
                        self._paused_seconds += self.resumed_at - self._pause_started
                        self._pause_started = None
        if self.stopped.is_set():
            raise Stopped()

    def now(self):
        with self._pause_clock_lock:
            now = time.monotonic()
            active_pause = now-self._pause_started if self._pause_started is not None else 0
            return now - self._paused_seconds - active_pause

    def wait(self, seconds):
        remaining = max(0, seconds)
        while remaining > 0:
            self.check()
            chunk = min(.15, remaining)
            start = time.monotonic()
            if self.stopped.wait(chunk):
                raise Stopped()
            remaining -= time.monotonic() - start
