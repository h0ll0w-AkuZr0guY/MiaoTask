"""Prefer readable controls; visual verification skips redundant hierarchy RPCs."""
import copy
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import hashlib
import re
import time

from backend.core.models import TextNode, bounds_of, normalize
from backend.core.device.geometry import IdentityImageProcessor, DisplayGeometry, ocr_tiles
from backend.core.context import workspace_scope
from backend.core.device.ocr import (OCR_DEVICE_ENV, device_ocr_scope, engine_generation,
                                     ocr_engine_params as _ocr_engine_params, reset_engines,
                                     resolve_ocr_backend, scoped_engine as _scoped_ocr_engine)

def local_ocr_engine(scope=None, threads=4):
    return _scoped_ocr_engine(scope if scope is not None else workspace_scope.get() or 'default', threads)


def reset_ocr_engines():
    """Drop cached recognizers so the next frame rebuilds with a new backend."""
    reset_engines()


@dataclass
class Screen:
    nodes: list
    width: int
    height: int
    package: str
    source: str = "ui"
    tree: object = None
    user_id: int | None = None
    geometry: DisplayGeometry | None = None
    obscured: bool = False
    captured_at: float = 0.0

    @property
    def text(self):
        return "\n".join(n.text for n in self.nodes)

    def find(self, pattern, exact=False):
        for node in self.nodes:
            if (normalize(node.text) == normalize(pattern) if exact else
                    re.search(pattern, normalize(node.text))):
                return node
        return None


class ScreenReader:
    def __init__(self, device, ocr=True, on_image=None, image_processor=None, engine_scope=None):
        self.device = device
        self.ocr_enabled = ocr
        self.on_image = on_image
        self._engine = None
        # Recognizers follow the device, not the calling thread, so an observer
        # thread reuses the models already loaded by the task worker.
        self._engine_generation = engine_generation()
        self._size = None
        self.last_image = None
        self.last_screen = None
        self._last_preview = 0
        self._ocr_cache_key = None
        self._ocr_cache_nodes = None
        self.last_observation_seconds = 0.0
        self.image_processor = image_processor or IdentityImageProcessor()
        self.engine_scope = engine_scope if engine_scope is not None else device_ocr_scope(device)
        self._ocr_scope = self.engine_scope if self.engine_scope is not None else workspace_scope.get() or ('reader', id(self))
        self.ocr_threads = 4
        self.control = None
        self._minimum_frame_time = 0.0
        self._image_matches_screen = True

    def require_after(self, stamp=None):
        """One-way freshness barrier for settlement checks and explicit rescans."""
        self._minimum_frame_time = max(self._minimum_frame_time,
                                       time.monotonic() if stamp is None else stamp)

    @contextmanager
    def observation_session(self, config, control):
        """Own cancellation and one synchronous reader; start no capture/OCR thread."""
        operation = getattr(type(self.device), 'operation_session', None)
        context = self.device.operation_session(control) if callable(operation) else nullcontext()
        self.control = control
        self.compressed_screenshots = config.get('compressed_screenshots', False)
        self.screenshot_quality = config.get('screenshot_quality', 70)
        try:
            with context:
                yield
        finally:
            self._engine = None
            self.control = None

    def _flatten(self, tree, foreground_package=None):
        nodes = []
        def walk(item, inherited_package=''):
            if isinstance(item, list):
                for child in item:
                    walk(child, inherited_package)
            elif isinstance(item, dict):
                attrs = item.get("attributes", item)
                if any(attrs.get(key) in (False, 'false') for key in ('visible', 'visible-to-user', 'displayed')):
                    return
                package = attrs.get("package", inherited_package)
                if (not foreground_package or not package or package == foreground_package) and not package.startswith(("com.android.systemui", "com.miui.notes", "com.google.android.inputmethod")):
                    text = attrs.get("text") or attrs.get("description") or attrs.get("content-desc") or ""
                    bounds = bounds_of(attrs.get("bounds", attrs.get("rect")))
                    if text and bounds[2] > bounds[0] and bounds[3] > bounds[1]:
                        nodes.append(TextNode(text, bounds))
                for key in ("children", "hierarchy"):
                    if isinstance(item.get(key), (list, dict)):
                        walk(item[key], package)
        walk(tree)
        return nodes

    def _ocr(self, image):
        nodes=[]
        for tile, x, y in ocr_tiles(image):
            for node in self._ocr_tile(tile):
                a,b,c,d=node.bounds
                mapped=TextNode(node.text,(a+x,b+y,c+x,d+y),node.source)
                if not any(n.text==mapped.text and abs(n.center[0]-mapped.center[0])<12 and abs(n.center[1]-mapped.center[1])<12 for n in nodes):
                    nodes.append(mapped)
        return nodes

    def _check_ocr(self):
        if self.control:
            self.control.check()

    @property
    def ocr_backend(self):
        """Actual loaded backend, including a runtime CPU fallback; never loads models."""
        return getattr(getattr(self._engine, 'engine', None), 'miao_backend', None)

    def _ocr_tile(self, image):
        self._check_ocr()
        # A backend switch bumps the generation; rebuild instead of reusing an
        # engine whose provider no longer matches the saved preference.
        if self._engine is None or self._engine_generation != engine_generation():
            generation = engine_generation()
            self._engine = local_ocr_engine(self._ocr_scope, self.ocr_threads)
            self._engine_generation = generation
        import numpy as np
        prepared = self.image_processor.prepare(image)
        image = prepared.image
        # Limit image width to reduce CPU work, then map boxes back to the phone.
        scale = min(1.0, 1000 / image.width)
        resized = image.resize((round(image.width * scale), round(image.height * scale)))
        managed = callable(getattr(type(self._engine), 'infer', None))
        result = (self._engine.infer(np.asarray(resized), self._check_ocr)
                  if managed else self._engine(np.asarray(resized), use_cls=False))
        results = zip(result.boxes, result.txts, result.scores) if result.boxes is not None else []
        nodes = []
        for box, text, confidence in results:
            if confidence < .65:
                continue
            xs, ys = zip(*box)
            bounds = prepared.to_device.bounds(tuple(round(v / scale) for v in (min(xs), min(ys), max(xs), max(ys))))
            nodes.append(TextNode(text, bounds, "ocr"))
        return nodes

    def _image_nodes(self, image):
        # Include every original RGB pixel and the dimensions. A thumbnail or
        # perceptual hash can miss a changed countdown or modal and is unsafe.
        key = (self.image_processor.cache_key, image.size, hashlib.sha256(image.tobytes()).digest())
        if key == self._ocr_cache_key:
            return copy.deepcopy(self._ocr_cache_nodes)
        nodes = self._ocr(image)
        self._ocr_cache_key = key
        self._ocr_cache_nodes = copy.deepcopy(nodes)
        return nodes

    def _capture(self):
        self._check_ocr()
        started = time.monotonic()
        self._capture_foreground = None
        device = self.device
        independent = (callable(getattr(type(device), 'capture_observation', None))
                       and getattr(device, 'device_type', '') == 'uiautomator2'
                       and not getattr(device, '_display_id', 0))
        if independent:
            self._capture_foreground, image = device.capture_observation(
                compressed=getattr(self, 'compressed_screenshots', False),
                quality=getattr(self, 'screenshot_quality', 70),
                transport=getattr(device, '_operation_io', None))
            image = image.convert('RGB')
        else:
            image = device.screenshot(max_age=0 if self._minimum_frame_time else .15).convert('RGB')
        self.last_image = image
        self._image_matches_screen = True
        self._size = image.size
        self._captured_at = started
        if self.on_image:
            self.on_image(image)
        self._last_preview = time.monotonic()
        self._check_ocr()
        return image

    def enrich_regions(self, screen, regions):
        """OCR crops of this exact observed frame, keeping full-frame guards."""
        if (not self.ocr_enabled or self.last_screen is not screen or self.last_image is None
                or not self._image_matches_screen):
            return screen
        nodes = list(screen.nodes)
        for left, top, right, bottom in regions:
            left, top = max(0, left), max(0, top)
            right, bottom = min(screen.width, right), min(screen.height, bottom)
            if left >= right or top >= bottom:
                continue
            for node in self._ocr(self.last_image.crop((left, top, right, bottom))):
                x1, y1, x2, y2 = node.bounds
                nodes.append(TextNode(node.text, (x1+left, y1+top, x2+left, y2+top), 'ocr-region'))
        from dataclasses import replace
        enriched = replace(screen, nodes=nodes, source='ocr+regions')
        self.last_screen = enriched
        return enriched

    def read(self, force_ocr=False):
        self._check_ocr()
        self._image_matches_screen = False
        started = time.monotonic()
        direct = bool(force_ocr and self.ocr_enabled)
        tree, image = None, None
        if direct:
            image = self._capture()
            foreground = getattr(self, '_capture_foreground', None) or self.device.current_app()
        else:
            tree = self.device.dump_hierarchy()
            foreground = self.device.current_app()
            self._size = self.device.display_size
        package = foreground.get('package', '')
        nodes = [] if direct else self._flatten(tree, package)
        meaningful = [n for n in nodes if n.bounds[3] - n.bounds[1] < (self._size or (1440,3200))[1] * .4]
        source = 'ui'
        obscured = False
        if self.ocr_enabled and (direct or len(meaningful) < 5):
            image = image if image is not None else self._capture()
            captured_info = getattr(self, '_capture_foreground', None)
            if captured_info:
                if captured_info.get('package') != package:
                    tree = None
                foreground, package = captured_info, captured_info.get('package', '')
            obscured = max(hi for lo,hi in image.getextrema()) < 8
            nodes = [] if obscured else self._image_nodes(image)
            source = 'ocr'
            if not nodes:
                tree = self.device.dump_hierarchy()
                nodes = self._flatten(tree, package)
                source = 'ui-fallback'
        elif self.on_image and not getattr(self.device, 'preview_managed', False) and time.monotonic()-self._last_preview >= 1:
            self._capture()
        self._check_ocr()
        screen = Screen(nodes, *self._size, package, source, tree, foreground.get('user_id'),
                        obscured=obscured, captured_at=getattr(self,'_captured_at',started) if image is not None else started)
        from backend.core.device.geometry import geometry_from_window
        if foreground.get('window_dump'):
            screen.geometry = geometry_from_window(*self._size, foreground['window_dump'])
        else:
            geometry = getattr(self.device, 'display_geometry', None)
            if isinstance(geometry, DisplayGeometry) and (geometry.width,geometry.height)==self._size:
                screen.geometry = geometry
        self.last_screen = screen
        self.last_observation_seconds = time.monotonic()-started
        self._minimum_frame_time = 0
        self.observation_metrics = dict(observation_ms=round(self.last_observation_seconds*1000), source=source)
        return screen
