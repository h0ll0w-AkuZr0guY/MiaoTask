"""Preview scheduling, isolated from task execution."""
import io
import time
import threading
import logging
from datetime import datetime
logger=logging.getLogger(__name__)
from backend.core.context import device_scoped

class PreviewWorker:
    def on_image(self, image):
        preview = image.convert('RGB').copy()
        preview.thumbnail((540, 1200))
        output = io.BytesIO()
        preview.save(output, format='JPEG', quality=78)
        with self.lock:
            self._preview_failures = 0
            self.state['preview_error'] = False
            self.image = output.getvalue()
            self.state['image_sequence'] += 1
            self.state['display'] = dict(width=image.width,height=image.height)
            self.state['image_updated_at'] = datetime.now().astimezone().isoformat(timespec='milliseconds')
        self._preview_last = time.monotonic()

    def _preview_failed(self, device):
        """Report observed transport trouble without interrupting a task."""
        with self.lock:
            if self.device is device:
                self._preview_failures = getattr(self, '_preview_failures', 0) + 1
                self.state['preview_error'] = self._preview_failures >= 3

    @device_scoped
    def _preview_loop(self):
        while not self._preview_stop.wait(.05):
            if time.monotonic() - self._last_client_seen > 5:
                # Closed browser tabs have no preview consumer. The task worker
                # still makes its own observations; idle phones stop capturing.
                continue
            if time.monotonic() - self._preview_last < .35:
                continue
            with self.lock:
                device = self.device
            if device is None:
                continue
            io_lock = getattr(device, 'io_lock', None)
            if io_lock is None or not io_lock.acquire(blocking=False):
                continue
            try:
                image = device.screenshot(max_age=.15)
                if self.device is device:
                    self.on_image(image)
            except Exception:
                # A preview failure does not interrupt an ongoing recovery.
                self._preview_last = time.monotonic()
                self._preview_failed(device)
                logger.debug('后台截图预览失败', exc_info=True)
            finally:
                io_lock.release()

    def _start_preview(self):
        if self._preview_thread and self._preview_thread.is_alive():
            return
        self._preview_thread = threading.Thread(target=self._preview_loop, daemon=True,
                                                name='miao-preview-worker')
        self._preview_thread.start()
