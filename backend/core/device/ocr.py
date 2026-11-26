"""Fixed macOS Apple Silicon (M-series) system OCR powered by Apple Vision."""
from contextlib import contextmanager
from dataclasses import dataclass
import io
import logging
import platform
import sys
import threading
import time
import weakref

logger = logging.getLogger(__name__)

OCR_BACKEND = 'vision'
OCR_DEVICE_ENV = 'MIAO_OCR_DEVICE'
VISION_LANGUAGES = ('zh-Hans', 'zh-Hant', 'en-US')


@dataclass(frozen=True)
class OCRResult:
    boxes: tuple | None
    txts: tuple
    scores: tuple
    elapse: float = 0.0


def is_apple_silicon_macos(system=None, machine=None):
    """Return True only on macOS running natively on Apple Silicon (arm64)."""
    actual_system = (system if system is not None else sys.platform).lower()
    actual_machine = (machine if machine is not None else platform.machine()).lower()
    return actual_system == 'darwin' and actual_machine == 'arm64'


def require_apple_silicon_macos():
    if not is_apple_silicon_macos():
        raise RuntimeError(
            f'当前版本固定使用 macOS Apple Silicon（M 系列芯片）系统 Vision OCR，'
            f'不支持当前环境：{sys.platform}/{platform.machine()}'
        )


def vision_observation_to_box(x, y, w, h, width, height):
    """Convert Vision's bottom-left normalized rect into top-left pixel corners."""
    left = round(max(0.0, min(float(width), float(x) * width)), 4)
    right = round(max(0.0, min(float(width), (float(x) + float(w)) * width)), 4)
    top = round(max(0.0, min(float(height), (1.0 - float(y) - float(h)) * height)), 4)
    bottom = round(max(0.0, min(float(height), (1.0 - float(y)) * height)), 4)
    return ((left, top), (right, top), (right, bottom), (left, bottom))


def resolve_ocr_backend(preference=None, providers=None):
    """OCR backend is fixed to macOS Apple Silicon Vision framework."""
    return OCR_BACKEND, None


def preferred_ocr_device():
    return OCR_BACKEND


class AppleVisionEngine:
    """Synchronous in-process Apple Vision text recognizer using ANE/GPU."""

    miao_backend = OCR_BACKEND

    def __init__(self):
        require_apple_silicon_macos()
        try:
            import objc
            import Vision
            import Quartz  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                '缺少 macOS 系统 Vision OCR 组件（pyobjc-framework-Vision / Quartz），'
                '请执行 pip install -r requirements.txt'
            ) from exc
        self._objc = objc
        self._Vision = Vision
        self.miao_providers = {'det': ['AppleVision'], 'rec': ['AppleVision']}
        self.languages = self._resolve_languages()
        logger.info('本地 OCR 已固定为 macOS Apple Silicon 系统 Vision 框架（语言：%s）',
                    ','.join(self.languages))

    def _resolve_languages(self):
        with self._objc.autorelease_pool():
            probe = self._Vision.VNRecognizeTextRequest.alloc().init()
            supported = None
            selector = getattr(probe, 'supportedRecognitionLanguagesAndReturnError_', None)
            if callable(selector):
                result = selector(None)
                supported = result[0] if isinstance(result, tuple) else result
            if supported:
                available = {str(item) for item in supported}
                chosen = [lang for lang in VISION_LANGUAGES if lang in available]
                if chosen:
                    return tuple(chosen)
            return VISION_LANGUAGES

    def __call__(self, image, use_cls=False):
        started = time.perf_counter()
        width, height = image.size
        if width <= 0 or height <= 0:
            return OCRResult(None, (), (), 0.0)
        buffer = io.BytesIO()
        (image if image.mode == 'RGB' else image.convert('RGB')).save(
            buffer, format='PNG', compress_level=1
        )
        raw_png = buffer.getvalue()
        boxes, txts, scores = [], [], []
        with self._objc.autorelease_pool():
            handler = self._Vision.VNImageRequestHandler.alloc().initWithData_options_(raw_png, None)
            if handler is None:
                raise RuntimeError('无法初始化 macOS Vision 图像处理器')
            request = self._Vision.VNRecognizeTextRequest.alloc().init()
            request.setRecognitionLevel_(self._Vision.VNRequestTextRecognitionLevelAccurate)
            request.setUsesLanguageCorrection_(True)
            request.setRecognitionLanguages_(list(self.languages))
            ok, error = handler.performRequests_error_([request], None)
            if not ok or error is not None:
                raise RuntimeError(f'macOS 系统 Vision OCR 执行失败：{error}')
            for observation in (request.results() or ()):
                candidates = observation.topCandidates_(1)
                if not candidates:
                    continue
                candidate = candidates[0]
                text = str(candidate.string() or '').strip()
                if not text:
                    continue
                score = float(candidate.confidence())
                rect = observation.boundingBox()
                box = vision_observation_to_box(
                    rect.origin.x, rect.origin.y, rect.size.width, rect.size.height, width, height
                )
                if box[2][0] > box[0][0] and box[2][1] > box[0][1]:
                    boxes.append(box)
                    txts.append(text)
                    scores.append(score)
        return OCRResult(
            boxes=tuple(boxes) if boxes else None,
            txts=tuple(txts),
            scores=tuple(scores),
            elapse=time.perf_counter() - started,
        )


# Engines are weakly cached; an active device run may lease them across activities.
_ENGINES = weakref.WeakValueDictionary()
_ENGINE_LOCK = threading.RLock()
_CREATION_LOCKS = weakref.WeakValueDictionary()
_ACTIVE_SCOPES = {}
_SESSION_ENGINES = {}
_GENERATION = 0


def engine_generation():
    """Identify the current engine configuration; readers compare before reuse."""
    return _GENERATION


def reset_engines():
    """Drop cached Vision engines and invalidate readers."""
    global _GENERATION
    with _ENGINE_LOCK:
        _GENERATION += 1
        _ENGINES.clear()
        _CREATION_LOCKS.clear()
        _SESSION_ENGINES.clear()


def device_ocr_scope(device):
    """Follow physical identity across worker threads."""
    for attr in ('identity_serial', 'serial'):
        value = getattr(device, attr, None)
        if isinstance(value, str) and value.strip():
            return ('device', value.strip())
    return ('device', id(device)) if device is not None else None


@contextmanager
def engine_session(scope):
    """Lazily retain engine for one run/pipeline, release strong ownership on exit."""
    with _ENGINE_LOCK:
        _ACTIVE_SCOPES[scope] = _ACTIVE_SCOPES.get(scope, 0) + 1
    try:
        yield
    finally:
        with _ENGINE_LOCK:
            remaining = _ACTIVE_SCOPES[scope] - 1
            if remaining:
                _ACTIVE_SCOPES[scope] = remaining
            else:
                _ACTIVE_SCOPES.pop(scope)
                for key in [key for key in _SESSION_ENGINES if key[0] == scope]:
                    _SESSION_ENGINES.pop(key)


def _create_engine():
    return AppleVisionEngine()


class ManagedEngine:
    def __init__(self, engine):
        self.engine = engine
        self.lock = threading.Lock()

    def __call__(self, *args, **kwargs):
        with self.lock:
            return self.engine(*args, **kwargs)

    def infer(self, image, check):
        while not self.lock.acquire(timeout=.05):
            check()
        try:
            check()
            return self.engine(image, use_cls=False)
        finally:
            self.lock.release()


def scoped_engine(scope, threads=4):
    del threads  # Vision framework manages Apple Silicon ANE/Metal scheduling natively.
    key = (scope, _GENERATION)
    with _ENGINE_LOCK:
        lock = _CREATION_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _CREATION_LOCKS[key] = lock
    with lock:
        with _ENGINE_LOCK:
            engine = _ENGINES.get(key)
        if engine is None:
            engine = ManagedEngine(_create_engine())
            with _ENGINE_LOCK:
                _ENGINES[key] = engine
        with _ENGINE_LOCK:
            if scope in _ACTIVE_SCOPES:
                _SESSION_ENGINES[key] = engine
        return engine
