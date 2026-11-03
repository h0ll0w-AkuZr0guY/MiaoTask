import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.core.device import screen

# Preload OpenCV: ``patch.dict(sys.modules, ...)`` restores the mapping and
# would otherwise drop a module first imported inside the patched block.
import cv2  # noqa: F401


class ResolveOCRBackendTests(unittest.TestCase):
    def test_default_auto_uses_cpu_without_cuda_ep(self):
        self.assertEqual(screen.resolve_ocr_backend(None, ['CPUExecutionProvider']),
                         ('cpu', None))
        self.assertEqual(screen.resolve_ocr_backend('auto', ()), ('cpu', None))

    def test_auto_enables_cuda_when_ep_available(self):
        self.assertEqual(
            screen.resolve_ocr_backend('auto', ['CUDAExecutionProvider', 'CPUExecutionProvider']),
            ('cuda', None))

    def test_explicit_cpu_never_enables_cuda(self):
        self.assertEqual(screen.resolve_ocr_backend('cpu', ['CUDAExecutionProvider']),
                         ('cpu', None))

    def test_forced_cuda_uses_cuda_when_available(self):
        self.assertEqual(screen.resolve_ocr_backend('cuda', ['CUDAExecutionProvider']),
                         ('cuda', None))

    def test_forced_cuda_without_ep_reports_fallback(self):
        self.assertEqual(screen.resolve_ocr_backend('cuda', ['CPUExecutionProvider']),
                         ('cpu', 'cuda-unavailable'))

    def test_unknown_preference_follows_auto(self):
        self.assertEqual(screen.resolve_ocr_backend('tpu', ['CUDAExecutionProvider']),
                         ('cuda', None))
        self.assertEqual(screen.resolve_ocr_backend('tpu', []), ('cpu', None))

    def test_params_carry_cuda_flag_only_for_cuda(self):
        cpu_params = screen._ocr_engine_params('cpu')
        self.assertNotIn('EngineConfig.onnxruntime.use_cuda', cpu_params)
        self.assertNotIn('Rec.rec_batch_num', cpu_params)
        self.assertEqual(screen._ocr_engine_params('cuda')['Rec.rec_batch_num'], 1)
        self.assertIs(screen._ocr_engine_params('cuda')[
                      'EngineConfig.onnxruntime.use_cuda'], True)


class ScopedOCRBackendTests(unittest.TestCase):
    """Engine wiring without importing real rapidocr/onnxruntime packages."""

    def _wire(self, providers, env_value):
        engine = Mock()
        engine.text_det.session.session.get_providers.return_value = providers
        rapid = SimpleNamespace(RapidOCR=Mock(return_value=engine))
        ort = SimpleNamespace(get_available_providers=Mock(return_value=list(providers)),
                              preload_dlls=Mock())
        self.ort = ort
        env = dict(os.environ)
        if env_value is None:
            env.pop(screen.OCR_DEVICE_ENV, None)
        else:
            env[screen.OCR_DEVICE_ENV] = env_value
        screen.reset_ocr_engines()
        with patch.dict(sys.modules, {'rapidocr': rapid, 'onnxruntime': ort}), \
                patch.dict(os.environ, env, clear=True):
            screen.local_ocr_engine()
        return rapid.RapidOCR.call_args.kwargs['params']

    def tearDown(self):
        screen.reset_ocr_engines()

    def test_default_engine_stays_cpu(self):
        params = self._wire(['CPUExecutionProvider'], None)
        self.assertNotIn('EngineConfig.onnxruntime.use_cuda', params)

    def test_cuda_env_enables_cuda_flag(self):
        params = self._wire(['CUDAExecutionProvider', 'CPUExecutionProvider'], 'cuda')
        self.assertIs(params['EngineConfig.onnxruntime.use_cuda'], True)

    def test_auto_env_uses_cuda_when_available(self):
        params = self._wire(['CUDAExecutionProvider', 'CPUExecutionProvider'], 'auto')
        self.assertIs(params['EngineConfig.onnxruntime.use_cuda'], True)

    def test_forced_cuda_without_ep_falls_back_to_cpu_params(self):
        params = self._wire(['CPUExecutionProvider'], 'cuda')
        self.assertNotIn('EngineConfig.onnxruntime.use_cuda', params)

    def test_explicit_cpu_env_keeps_cpu_even_with_cuda_ep(self):
        params = self._wire(['CUDAExecutionProvider', 'CPUExecutionProvider'], 'cpu')
        self.assertNotIn('EngineConfig.onnxruntime.use_cuda', params)

    def test_cuda_backend_preloads_bundled_cuda_dlls(self):
        self._wire(['CUDAExecutionProvider', 'CPUExecutionProvider'], 'cuda')
        self.ort.preload_dlls.assert_called_once_with()

    def test_cpu_backend_skips_dll_preload(self):
        self._wire(['CPUExecutionProvider'], 'cpu')
        self.ort.preload_dlls.assert_not_called()

    def test_missing_preload_api_still_builds_the_cuda_engine(self):
        engine = Mock()
        rapid = SimpleNamespace(RapidOCR=Mock(return_value=engine))
        ort = SimpleNamespace(get_available_providers=Mock(
            return_value=['CUDAExecutionProvider', 'CPUExecutionProvider']))
        screen.reset_ocr_engines()
        with patch.dict(sys.modules, {'rapidocr': rapid, 'onnxruntime': ort}), \
                patch.dict(os.environ, {screen.OCR_DEVICE_ENV: 'cuda'}, clear=True):
            screen.local_ocr_engine()
        self.assertIs(rapid.RapidOCR.call_args.kwargs['params'][
                      'EngineConfig.onnxruntime.use_cuda'], True)

    def test_reset_drops_cached_engine_even_while_a_reader_holds_it(self):
        from backend.core.device.ocr import scoped_engine
        scope = ('device', 'reset-sample')
        with patch('backend.core.device.ocr._create_engine', return_value=Mock()) as build:
            first = scoped_engine(scope)
            self.assertIs(scoped_engine(scope), first)
            screen.reset_ocr_engines()
            self.assertIsNot(scoped_engine(scope), first)
            self.assertEqual(build.call_count, 2)

    def test_preload_failure_falls_back_to_the_system_search_path(self):
        engine = Mock()
        rapid = SimpleNamespace(RapidOCR=Mock(return_value=engine))
        ort = SimpleNamespace(get_available_providers=Mock(
            return_value=['CUDAExecutionProvider', 'CPUExecutionProvider']),
            preload_dlls=Mock(side_effect=OSError('missing dll')))
        screen.reset_ocr_engines()
        with patch.dict(sys.modules, {'rapidocr': rapid, 'onnxruntime': ort}), \
                patch.dict(os.environ, {screen.OCR_DEVICE_ENV: 'cuda'}, clear=True):
            screen.local_ocr_engine()
        ort.preload_dlls.assert_called_once_with()
        self.assertIs(rapid.RapidOCR.call_args.kwargs['params'][
                      'EngineConfig.onnxruntime.use_cuda'], True)


class CUDAFailureRegressionTests(unittest.TestCase):
    def test_runtime_manifest_requires_matching_python_ort_and_platform(self):
        from backend.core.device.runtime_plugin import validate_manifest
        manifest = dict(schema=1, kind='cuda', python='3.13', onnxruntime='1.31.0',
                        system='Windows', machine='amd64')
        expected = dict(python_version='3.13', ort_version='1.31.0', system='Windows', machine='AMD64')
        validate_manifest(manifest, **expected)
        for key in manifest:
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_manifest({**manifest, key: 'incompatible'}, **expected)

    def test_runtime_locks_share_identical_common_dependencies(self):
        from pathlib import Path
        from packaging.requirements import Requirement
        root = Path(__file__).resolve().parents[1]
        def locked(name):
            return {Requirement(line).name: str(Requirement(line).specifier)
                    for line in (root / name).read_text(encoding='utf-8').splitlines()
                    if line.strip() and not line.startswith('#')}
        cpu = locked('requirements-windows.lock')
        gpu = locked('configs/environments/requirements-cuda.lock')
        self.assertEqual(cpu.pop('onnxruntime'), gpu.pop('onnxruntime-gpu'))
        self.assertEqual(cpu, {key: value for key, value in gpu.items() if not key.startswith('nvidia-')})

    def test_model_initialization_failure_retries_cpu_once(self):
        from backend.core.device import ocr
        fake=SimpleNamespace(RapidOCR=Mock(side_effect=[RuntimeError('CUDA dll missing'),Mock()]))
        with patch.dict(sys.modules,{'rapidocr':fake}),patch.object(ocr,'engine_params',return_value=ocr.ocr_engine_params('cuda')):
            engine=ocr._create_engine(2)
        self.assertEqual(fake.RapidOCR.call_count,2)
        self.assertFalse(fake.RapidOCR.call_args.kwargs['params'].get('EngineConfig.onnxruntime.use_cuda',False))

    def test_runtime_failure_retries_same_image_on_cpu(self):
        from backend.core.device import ocr
        gpu=Mock(side_effect=RuntimeError('CUDA device lost'));gpu.miao_backend='cuda'
        cpu=Mock(return_value='recognized');cpu.miao_backend='cpu'
        managed=ocr.ManagedEngine(gpu)
        with patch.object(ocr,'_create_engine',return_value=cpu) as build:
            self.assertEqual(managed.infer('frame',lambda:None),'recognized')
            self.assertEqual(managed.infer('next',lambda:None),'recognized')
        build.assert_called_once_with(4,backend='cpu')
        cpu.assert_any_call('frame',use_cls=False)

    def test_non_spinning_session_preserves_provider_options(self):
        from backend.core.device import ocr
        session = Mock(_model_path='model.onnx')
        session.get_providers.return_value = ['CUDAExecutionProvider', 'CPUExecutionProvider']
        session.get_provider_options.return_value = {
            'CUDAExecutionProvider': {'device_id': '1', 'cudnn_conv_algo_search': 'HEURISTIC'},
            'CPUExecutionProvider': {'arena_extend_strategy': 'kSameAsRequested'},
        }
        ort = SimpleNamespace(InferenceSession=Mock(), ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL=0))
        model = SimpleNamespace(session=SimpleNamespace(session=session))
        with patch.dict(sys.modules, {'onnxruntime': ort}):
            ocr._sleeping_session(model)
        self.assertEqual(ort.InferenceSession.call_args.kwargs['provider_options'], [
            {'device_id': '1', 'cudnn_conv_algo_search': 'HEURISTIC'},
            {'arena_extend_strategy': 'kSameAsRequested'},
        ])


if __name__ == '__main__':
    unittest.main()
