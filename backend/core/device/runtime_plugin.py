"""Opt-in, versioned local OCR runtime overlay; never installs into the CPU bundle."""
import importlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import platform
import sys
import threading

logger = logging.getLogger(__name__)
_LOCK = threading.RLock()
_ATTEMPTED = False
_DLL_HANDLES = []

# DirectML overlays live in runtime_plugins/dml; the NVIDIA CUDA overlay keeps
# its historical runtime_plugins/cuda directory so existing installs keep working.
RUNTIME_FOLDERS = ('cuda', 'dml')


def validate_manifest(manifest, *, python_version, ort_version, system, machine, folder='cuda'):
    """Reject incompatible overlays before importing their native extensions."""
    kind = manifest.get('kind')
    if kind not in RUNTIME_FOLDERS:
        raise ValueError(f'运行时扩展类型不支持：{kind}')
    if kind != folder:
        raise ValueError(f'扩展声明 {kind}，但放在 {folder} 目录')
    expected = dict(schema=1, python=python_version, system=system, machine=machine.lower())
    if kind == 'cuda':
        # CUDA wheels are built from the same ORT release as the bundled CPU wheel.
        expected['onnxruntime'] = ort_version
    elif not manifest.get('runtime_version'):
        # onnxruntime-directml has its own version line (e.g. 1.24.4) and cannot
        # be required to match the bundled CPU ORT version.
        raise ValueError('DirectML扩展缺少 runtime_version')
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise ValueError(f'运行时扩展不兼容：{key} 应为 {value}')


def activate_runtime_plugin():
    """Load one trusted extracted runtime overlay, before RapidOCR imports ORT.

    A missing/incompatible overlay retains the bundled CPU runtime. Installing or
    removing an overlay requires exiting the service, not merely closing its tab.
    """
    global _ATTEMPTED
    with _LOCK:
        if _ATTEMPTED:
            return
        _ATTEMPTED = True
        root = (Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False)
                else Path(__file__).resolve().parents[3])
        folder = None
        from backend.core.device.ocr import preferred_ocr_device
        requested = str(preferred_ocr_device()).lower()
        candidates = ('dml',) if requested == 'dml' else ('cuda',) if requested == 'cuda' else RUNTIME_FOLDERS
        for name in candidates:
            candidate = root / 'runtime_plugins' / name
            if (candidate / 'manifest.json').is_file():
                folder = candidate
                break
        if folder is None:
            return
        added = []
        handles = []
        try:
            manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
            validate_manifest(manifest, python_version=f'{sys.version_info.major}.{sys.version_info.minor}',
                              ort_version=importlib.metadata.version('onnxruntime'),
                              system=platform.system(), machine=platform.machine(),
                              folder=folder.name)
            if 'onnxruntime' in sys.modules:
                raise ValueError('ORT已加载，请退出服务后重启以启用GPU扩展')
            packages = folder / 'python'
            if not (packages / 'onnxruntime' / '__init__.py').is_file():
                raise ValueError('GPU扩展缺少onnxruntime源码，请完整解压')
            sys.path.insert(0, str(packages))
            added.append(str(packages))
            # Handles must remain alive. Do not alter the user's global PATH.
            if hasattr(os, 'add_dll_directory'):
                dll_dirs = {path.parent for path in (packages / 'nvidia').rglob('*.dll')}
                dll_dirs.add(packages / 'onnxruntime' / 'capi')
                for path in sorted(dll_dirs):
                    handles.append(os.add_dll_directory(str(path)))
            importlib.invalidate_caches()
            module = importlib.import_module('onnxruntime')
            if not Path(module.__file__).resolve().is_relative_to(packages.resolve()):
                raise ValueError('GPU扩展未取得加载优先级，仍保留CPU运行时')
            if manifest['kind'] == 'dml':
                if module.__version__ != manifest['runtime_version']:
                    raise ValueError('DirectML运行库版本与清单不一致')
                if 'DmlExecutionProvider' not in module.get_available_providers():
                    raise ValueError('扩展不包含DirectML执行器')
            _DLL_HANDLES.extend(handles)
            logger.info('已加载本地%s扩展：ORT %s', manifest['kind'], module.__version__)
        except Exception as exc:
            for path in added:
                sys.path.remove(path)
            # Roll back only modules imported by this activation attempt.
            if added:
                for name in list(sys.modules):
                    module = sys.modules[name]
                    origin = getattr(module, '__file__', None)
                    if origin and Path(origin).resolve().is_relative_to(folder.resolve()):
                        sys.modules.pop(name, None)
            for handle in handles:
                handle.close()
            importlib.invalidate_caches()
            logger.warning('未启用GPU扩展，保留CPU运行时：%s', exc)
