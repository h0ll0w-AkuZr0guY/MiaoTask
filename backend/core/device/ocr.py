"""Sparse synchronous OCR with exact text caching and non-spinning sessions."""
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import logging
import os
import threading
import time
import weakref

logger = logging.getLogger(__name__)

# auto：装了带 CUDA 的 onnxruntime-gpu 才启用；cpu/cuda 可显式指定。
OCR_DEVICE_ENV = 'MIAO_OCR_DEVICE'
_OCR_BACKENDS = ('auto', 'cpu', 'cuda')


class ExactTextCache:
    def __init__(self, recognize, capacity=512):
        self.recognize = recognize
        self.capacity = capacity
        self.cache = OrderedDict()
        self.lock = threading.RLock()
        self.last_hits = self.last_misses = 0

    def __call__(self, args):
        # Character-box consumers have a different output contract. Leave those
        # calls untouched instead of caching results with incomplete geometry.
        if args.return_word_box or args.img is None:
            return self.recognize(args)
        images = args.img if isinstance(args.img, list) else [args.img]
        with self.lock:
            started = time.perf_counter()
            keys = [(im.shape, im.dtype.str, hashlib.sha256(im.tobytes()).digest()) for im in images]
            rows = [self.cache.get(key) for key in keys]
            missing = [i for i, value in enumerate(rows) if value is None]
            self.last_hits, self.last_misses = len(rows) - len(missing), len(missing)
            if missing:
                result = self.recognize(replace(args, img=[images[i] for i in missing]))
                if result.txts is None or len(result.txts) != len(missing):
                    return self.recognize(args)
                for j, i in enumerate(missing):
                    rows[i] = (result.txts[j], float(result.scores[j]))
                    self.cache[keys[i]] = rows[i]
            for key in keys:
                if key in self.cache:
                    self.cache.move_to_end(key)
            while len(self.cache) > self.capacity:
                self.cache.popitem(last=False)
            from rapidocr.ch_ppocr_rec import TextRecOutput
            return TextRecOutput(imgs=images, txts=tuple(row[0] for row in rows),
                                 scores=tuple(row[1] for row in rows),
                                 word_results=tuple(None for _ in rows),
                                 elapse=time.perf_counter() - started)


# Engines are weakly cached; an active device run may lease them across activities.
_ENGINES = weakref.WeakValueDictionary()
_ENGINE_LOCK = threading.RLock()
_CREATION_LOCKS = weakref.WeakValueDictionary()
_CV_CONFIGURED = False
_ACTIVE_SCOPES = {}
_SESSION_ENGINES = {}
# Bumped when the backend preference changes; cached engines become stale and
# the next frame builds a fresh one instead of keeping the old provider.
_GENERATION = 0


def engine_generation():
    """Identify the current engine configuration; readers compare before reuse."""
    return _GENERATION


def reset_engines():
    """Drop cached engines after an OCR backend switch and invalidate readers."""
    global _GENERATION
    with _ENGINE_LOCK:
        _GENERATION += 1
        _ENGINES.clear()
        _CREATION_LOCKS.clear()
        _SESSION_ENGINES.clear()


def device_ocr_scope(device):
    """Follow physical identity across transport aliases and worker threads (#32)."""
    for attr in ('identity_serial', 'serial'):
        value = getattr(device, attr, None)
        if isinstance(value, str) and value.strip():
            return ('device', value.strip())
    return ('device', id(device)) if device is not None else None


def resolve_ocr_backend(preference, providers):
    """Map the MIAO_OCR_DEVICE preference and available EPs to a backend.

    Returns ``(backend, note)`` where backend is ``'cpu'`` or ``'cuda'`` and
    note is ``None`` or ``'cuda-unavailable'`` when a forced CUDA request has
    to fall back because CUDAExecutionProvider is missing.
    """
    requested = (preference or 'auto').strip().lower()
    if requested not in _OCR_BACKENDS:
        logger.warning('环境变量 %s=%r 无法识别，可选 %s，按 auto 处理',
                       OCR_DEVICE_ENV, preference, '/'.join(_OCR_BACKENDS))
        requested = 'auto'
    cuda_ready = 'CUDAExecutionProvider' in (providers or ())
    if requested == 'cpu':
        return 'cpu', None
    if cuda_ready:
        return 'cuda', None
    if requested == 'cuda':
        return 'cpu', 'cuda-unavailable'
    return 'cpu', None


def _preload_cuda_dlls(onnxruntime_module):
    """Load CUDA/cuDNN DLLs installed by ``onnxruntime-gpu[cuda,cudnn]``.

    Returns True when the runtime exposes and accepts the preload call. Older
    onnxruntime releases and system-wide CUDA installs stay on the normal DLL
    search path, so a missing hook or a failed load is not fatal.
    """
    preload = getattr(onnxruntime_module, 'preload_dlls', None)
    if preload is None:
        return False
    try:
        preload()
        return True
    except Exception:
        logger.warning('预加载 onnxruntime-gpu 附带的 CUDA/cuDNN 运行库失败，'
                       '继续按系统 PATH 查找', exc_info=True)
        return False


def preferred_ocr_device():
    """Env var wins; otherwise the console-wide saved preference."""
    explicit = os.environ.get(OCR_DEVICE_ENV)
    if explicit is not None:
        return explicit
    try:
        from backend.core.preferences import load_preferences
        return load_preferences().get('ocr_device', 'auto')
    except Exception:
        return 'auto'


def ocr_engine_params(backend, threads=4):
    """RapidOCR parameters for an already resolved backend."""
    params = {
        'EngineConfig.onnxruntime.intra_op_num_threads': threads,
        'EngineConfig.onnxruntime.inter_op_num_threads': 1,
        'Global.log_level': 'warning',
    }
    if backend == 'cuda':
        params['EngineConfig.onnxruntime.use_cuda'] = True
        # Dynamic-width PP-OCR recognition batches can fail inside cuDNN
        # ReduceMean on Windows. Single-crop GPU inference avoids that path;
        # retain the existing batch size for CPU and the exact-crop cache.
        params['Rec.rec_batch_num'] = 1
    return params


def engine_params(threads):
    """Build RapidOCR parameters, enabling CUDA only when it is usable."""
    from backend.core.device.runtime_plugin import activate_runtime_plugin
    activate_runtime_plugin()
    providers = ()
    try:
        import onnxruntime
        providers = onnxruntime.get_available_providers()
    except ImportError:
        pass
    backend, note = resolve_ocr_backend(preferred_ocr_device(), providers)
    if note == 'cuda-unavailable':
        logger.warning('已请求 GPU 识别（cuda），但当前 onnxruntime 不含 CUDAExecutionProvider；'
                       '需安装与本机 CUDA/cuDNN 版本匹配的 onnxruntime-gpu，已回退 CPU')
    if backend == 'cuda':
        # pip 安装的 nvidia-* 运行库不在 Windows DLL 搜索路径中，先显式加载。
        _preload_cuda_dlls(onnxruntime)
    logger.info('本地 OCR 推理后端：%s（onnxruntime 可用执行器：%s）',
                backend, ','.join(providers) or 'CPUExecutionProvider')
    return ocr_engine_params(backend, threads)


@contextmanager
def engine_session(scope):
    """Lazily retain models for one run/pipeline, release strong ownership on exit."""
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


def available_cpus():
    return max(1, getattr(os, 'process_cpu_count', os.cpu_count)() or 1)


def _sleeping_session(model):
    """Configure the locked RapidOCR ONNX runtime, without global monkeypatches."""
    import onnxruntime as ort
    session = model.session.session
    options = session.get_session_options()
    options.add_session_config_entry('session.intra_op.allow_spinning', '0')
    options.add_session_config_entry('session.inter_op.allow_spinning', '0')
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    providers = session.get_providers()
    provider_options = session.get_provider_options()
    model.session.session = ort.InferenceSession(session._model_path, sess_options=options,
                                                providers=providers,
                                                provider_options=[provider_options.get(name, {})
                                                                  for name in providers])



def _create_engine(threads, backend=None):
    global _CV_CONFIGURED
    from backend.core.device.runtime_plugin import activate_runtime_plugin
    activate_runtime_plugin()
    from rapidocr import RapidOCR
    import cv2
    with _ENGINE_LOCK:
        if not _CV_CONFIGURED:
            cv2.setNumThreads(1)
            _CV_CONFIGURED = True
    params = engine_params(threads) if backend is None else ocr_engine_params(backend, threads)

    def build(parameters):
        engine = RapidOCR(params=parameters)
        if callable(getattr(type(engine), '_load_det_model', None)):
            det = engine._load_det_model()
            _sleeping_session(det)
            rec = engine._load_rec_model()
            _sleeping_session(rec)
            engine.miao_providers = {'det': det.session.session.get_providers(),
                                     'rec': rec.session.session.get_providers()}
            engine.miao_backend = ('cuda' if any('CUDAExecutionProvider' in providers
                                   for providers in engine.miao_providers.values()) else 'cpu')
            engine.text_rec = ExactTextCache(rec)
            logger.info('OCR实际模型执行器：%s', engine.miao_providers)
        return engine

    try:
        return build(params)
    except Exception:
        if not params.get('EngineConfig.onnxruntime.use_cuda'):
            raise
        logger.warning('CUDA模型初始化失败，改用CPU重试一次', exc_info=True)
        return build(ocr_engine_params('cpu', threads))


class ManagedEngine:
    def __init__(self, engine, threads=4):
        self.threads = threads
        self.engine = engine
        self.lock = threading.Lock()

    def __call__(self, *args, **kwargs):
        with self.lock:
            return self._run(*args, **kwargs)

    def _run(self, *args, **kwargs):
        try:
            return self.engine(*args, **kwargs)
        except Exception:
            if getattr(self.engine, 'miao_backend', None) != 'cuda':
                raise
            logger.warning('CUDA推理失败，释放该引擎并回退CPU重试本帧一次', exc_info=True)
            self.engine = None
            self.engine = _create_engine(self.threads, backend='cpu')
            return self.engine(*args, **kwargs)

    def infer(self, image, check):
        while not self.lock.acquire(timeout=.05):
            check()
        try:
            check()
            return self._run(image, use_cls=False)
        finally:
            self.lock.release()


def scoped_engine(scope, threads=4):
    threads = min(threads, max(1, available_cpus() // 2))
    # The generation keeps keys unique, so a switched backend never reuses an
    # engine that was built for the previous one.
    key = (scope, threads, _GENERATION)
    with _ENGINE_LOCK:
        lock = _CREATION_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _CREATION_LOCKS[key] = lock
    with lock:
        with _ENGINE_LOCK:
            engine = _ENGINES.get(key)
        if engine is None:
            engine = ManagedEngine(_create_engine(threads), threads)
            with _ENGINE_LOCK:
                _ENGINES[key] = engine
        with _ENGINE_LOCK:
            if scope in _ACTIVE_SCOPES:
                _SESSION_ENGINES[key] = engine
        return engine
