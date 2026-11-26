"""Unit tests for fixed macOS Apple Silicon Vision OCR."""
from contextlib import contextmanager
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from backend.core.device import ocr, screen


def _fake_vision_modules(*, observations=(), perform_result=(True, None),
                         handler_none=False, supported_languages=('zh-Hans', 'zh-Hant', 'en-US', 'ja-JP')):
    pool_entries = []

    @contextmanager
    def autorelease_pool():
        pool_entries.append(True)
        yield

    fake_objc = SimpleNamespace(autorelease_pool=autorelease_pool, pool_entries=pool_entries)

    created_requests = []
    created_handlers = []

    class FakeTextRequest:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            self.level = None
            self.language_correction = None
            self.languages = None
            created_requests.append(self)
            return self

        def supportedRecognitionLanguagesAndReturnError_(self, _error):
            return supported_languages, None

        def setRecognitionLevel_(self, level):
            self.level = level

        def setUsesLanguageCorrection_(self, enabled):
            self.language_correction = enabled

        def setRecognitionLanguages_(self, languages):
            self.languages = list(languages)

        def results(self):
            return list(observations)

    class FakeImageHandler:
        @classmethod
        def alloc(cls):
            return cls()

        def initWithData_options_(self, raw_data, options):
            if handler_none:
                return None
            self.raw_data = raw_data
            self.options = options
            self.performed_requests = None
            created_handlers.append(self)
            return self

        def performRequests_error_(self, requests, _error):
            self.performed_requests = list(requests)
            return perform_result

    fake_vision = SimpleNamespace(
        VNRecognizeTextRequest=FakeTextRequest,
        VNImageRequestHandler=FakeImageHandler,
        VNRequestTextRecognitionLevelAccurate=1,
        created_requests=created_requests,
        created_handlers=created_handlers,
    )
    fake_quartz = SimpleNamespace()
    return fake_objc, fake_vision, fake_quartz


def _make_observation(text, confidence, x, y, w, h):
    candidate = SimpleNamespace(string=lambda: text, confidence=lambda: confidence)
    rect = SimpleNamespace(
        origin=SimpleNamespace(x=x, y=y),
        size=SimpleNamespace(width=w, height=h),
    )
    return SimpleNamespace(
        topCandidates_=lambda _n: [candidate] if text is not None else [],
        boundingBox=lambda: rect,
    )


class AppleSiliconGuardTests(unittest.TestCase):
    def test_platform_detection_requires_darwin_arm64(self):
        self.assertTrue(ocr.is_apple_silicon_macos('darwin', 'arm64'))
        self.assertTrue(ocr.is_apple_silicon_macos('Darwin', 'ARM64'))
        self.assertFalse(ocr.is_apple_silicon_macos('darwin', 'x86_64'))
        self.assertFalse(ocr.is_apple_silicon_macos('linux', 'aarch64'))
        self.assertFalse(ocr.is_apple_silicon_macos('win32', 'AMD64'))

    def test_require_apple_silicon_rejects_intel_mac_and_non_macos(self):
        with patch.object(ocr.sys, 'platform', 'darwin'), patch.object(ocr.platform, 'machine', return_value='x86_64'):
            with self.assertRaisesRegex(RuntimeError, 'Apple Silicon'):
                ocr.require_apple_silicon_macos()
        with patch.object(ocr.sys, 'platform', 'linux'), patch.object(ocr.platform, 'machine', return_value='x86_64'):
            with self.assertRaisesRegex(RuntimeError, 'Apple Silicon'):
                ocr.require_apple_silicon_macos()

    def test_fixed_backend_helpers_always_return_vision(self):
        self.assertEqual(ocr.resolve_ocr_backend(None, None), ('vision', None))
        self.assertEqual(ocr.resolve_ocr_backend('cuda', ['CUDAExecutionProvider']), ('vision', None))
        self.assertEqual(ocr.preferred_ocr_device(), 'vision')


class VisionCoordinateMappingTests(unittest.TestCase):
    def test_bottom_left_normalized_rect_maps_to_top_left_pixel_box(self):
        # x=0.1..0.5 on width=1000 -> 100..500
        # y=0.2, h=0.1 from bottom on height=2000 -> top=(1-0.3)*2000=1400, bottom=(1-0.2)*2000=1600
        box = ocr.vision_observation_to_box(0.1, 0.2, 0.4, 0.1, 1000, 2000)
        self.assertEqual(box, ((100.0, 1400.0), (500.0, 1400.0), (500.0, 1600.0), (100.0, 1600.0)))

    def test_out_of_range_normalized_coordinates_are_clamped_to_image(self):
        box = ocr.vision_observation_to_box(-0.1, -0.2, 1.5, 1.5, 400, 800)
        self.assertEqual(box, ((0.0, 0.0), (400.0, 0.0), (400.0, 800.0), (0.0, 800.0)))


class AppleVisionEngineTests(unittest.TestCase):
    def tearDown(self):
        screen.reset_ocr_engines()

    def _build_engine(self, **kwargs):
        fake_objc, fake_vision, fake_quartz = _fake_vision_modules(**kwargs)
        with patch.object(ocr, 'is_apple_silicon_macos', return_value=True), \
                patch.dict(sys.modules, {'objc': fake_objc, 'Vision': fake_vision, 'Quartz': fake_quartz}):
            engine = ocr.AppleVisionEngine()
        return engine, fake_objc, fake_vision

    def test_engine_configures_accurate_chinese_and_english_recognition(self):
        obs = [
            _make_observation('去完成', 0.92, 0.7, 0.5, 0.2, 0.05),
            _make_observation('15秒', 0.5, 0.1, 0.5, 0.2, 0.05),
            _make_observation('   ', 0.99, 0.1, 0.1, 0.2, 0.05),
        ]
        engine, fake_objc, fake_vision = self._build_engine(observations=obs)
        self.assertEqual(engine.miao_backend, 'vision')
        self.assertEqual(engine.miao_providers, {'det': ['AppleVision'], 'rec': ['AppleVision']})
        self.assertEqual(engine.languages, ('zh-Hans', 'zh-Hant', 'en-US'))

        image = Image.new('RGBA', (1000, 2000), (255, 255, 255, 255))
        result = engine(image)
        self.assertEqual(result.txts, ('去完成', '15秒'))
        self.assertEqual(result.scores, (0.92, 0.5))
        self.assertEqual(result.boxes[0], ((700.0, 900.0), (900.0, 900.0), (900.0, 1000.0), (700.0, 1000.0)))
        self.assertGreaterEqual(len(fake_objc.pool_entries), 2)
        req = fake_vision.created_requests[-1]
        self.assertEqual(req.level, 1)
        self.assertTrue(req.language_correction)
        self.assertEqual(req.languages, ['zh-Hans', 'zh-Hant', 'en-US'])

    def test_engine_raises_clear_error_when_vision_request_fails(self):
        engine, _, _ = self._build_engine(perform_result=(False, 'synthetic error'))
        with self.assertRaisesRegex(RuntimeError, 'Vision OCR 执行失败'):
            engine(Image.new('RGB', (200, 100), 'white'))

    def test_screen_reader_ocr_tile_scales_large_frames_and_keeps_vision_half_tier_scores(self):
        obs_result = ocr.OCRResult(
            boxes=(
                ((720.0, 100.0), (900.0, 100.0), (900.0, 160.0), (720.0, 160.0)),
                ((100.0, 100.0), (200.0, 100.0), (200.0, 160.0), (100.0, 160.0)),
            ),
            txts=('去浏览', '噪点'),
            scores=(0.5, 0.2),
            elapse=0.04,
        )
        fake_engine = Mock(return_value=obs_result)
        fake_engine.miao_backend = 'vision'
        managed = ocr.ManagedEngine(fake_engine)
        reader = screen.ScreenReader(None)
        with patch.object(screen, 'local_ocr_engine', return_value=managed):
            # 2880x1600 scales by 0.5 -> 1440x800, then maps back by 2x -> 1440..1800, 200..320
            nodes = reader._ocr_tile(Image.new('RGB', (2880, 1600), 'white'))
        self.assertEqual(reader.ocr_backend, 'vision')
        self.assertEqual([n.text for n in nodes], ['去浏览'])
        self.assertEqual(nodes[0].bounds, (1440, 200, 1800, 320))
        resized_arg = fake_engine.call_args.args[0]
        self.assertEqual(resized_arg.size, (1440, 800))

    def test_reset_drops_cached_engine_even_while_a_reader_holds_it(self):
        scope = ('device', 'reset-sample')
        with patch('backend.core.device.ocr._create_engine', return_value=Mock()) as build:
            first = ocr.scoped_engine(scope)
            self.assertIs(ocr.scoped_engine(scope), first)
            screen.reset_ocr_engines()
            self.assertIsNot(ocr.scoped_engine(scope), first)
            self.assertEqual(build.call_count, 2)

    def test_managed_engine_checks_cancellation_while_waiting_for_lock(self):
        inner = Mock(return_value='ok')
        managed = ocr.ManagedEngine(inner)
        entered = threading.Event()
        release = threading.Event()

        def hold():
            with managed.lock:
                entered.set()
                release.wait(2)

        holder = threading.Thread(target=hold)
        holder.start()
        try:
            self.assertTrue(entered.wait(1))
            with self.assertRaisesRegex(RuntimeError, 'stopped'):
                managed.infer(Image.new('RGB', (20, 20), 'white'),
                              Mock(side_effect=RuntimeError('stopped')))
        finally:
            release.set()
            holder.join(2)


if __name__ == '__main__':
    unittest.main()
