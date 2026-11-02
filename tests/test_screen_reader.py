import unittest
import threading
from unittest.mock import Mock, patch

from PIL import Image

from backend.core.device.screen import ScreenReader
from backend.app.taobao.activities.double11.tasks import TextNode


def readable_tree():
    return {'hierarchy': {'children': [
        {'text': f'控件 {index}', 'bounds': [10, 70 + index * 90, 400, 120 + index * 90]}
        for index in range(6)
    ]}}


def device():
    result = Mock(preview_managed=False)
    result.dump_hierarchy.return_value = readable_tree()
    result.current_app.return_value = {'package': 'com.taobao.taobao'}
    result.screenshot.return_value = Image.new('RGB', (500, 1000), 'white')
    result.display_size = (500, 1000)
    return result


class ScreenReaderObservationTests(unittest.TestCase):
    def test_visual_ocr_does_not_hold_device_or_capture_on_worker_thread(self):
        phone = device()
        owner = threading.get_ident()
        phone.screenshot.side_effect = lambda **kw: (
            self.assertEqual(threading.get_ident(), owner) or Image.new('RGB', (500, 1000), 'white'))
        reader = ScreenReader(phone)
        entered, release = threading.Event(), threading.Event()
        observer = reader.visual_observer()
        def slow_ocr(image):
            entered.set()
            self.assertTrue(release.wait(2))
            return [TextNode('浏览完成', (1, 1, 100, 50), 'ocr')]
        observer.reader._ocr = slow_ocr
        try:
            with observer:
                self.assertTrue(observer.request())
                self.assertTrue(entered.wait(1))
                self.assertIsNone(observer.poll())
                self.assertFalse(observer.request())  # one frame in flight
                phone.swipe(250, 700, 250, 300)
                phone.swipe.assert_called_once()
                phone.dump_hierarchy.assert_not_called()
        finally:
            release.set()
            observer.thread.join(2)
        self.assertIsNone(reader.last_screen)  # result after close is discarded
        self.assertIsNone(observer.poll())

    def test_visual_observation_returns_captured_geometry_and_errors(self):
        phone = device()
        reader = ScreenReader(phone)
        observer = reader.visual_observer()
        observer.reader._ocr = Mock(side_effect=ValueError('synthetic recognition failure'))
        with observer:
            observer.request()
            # Wait deterministically for the recognizer, without device calls.
            import time
            deadline = time.monotonic() + 2
            while observer.results.empty() and time.monotonic() < deadline:
                time.sleep(.005)
            with self.assertRaisesRegex(ValueError, 'synthetic'):
                observer.poll()
        self.assertEqual((observer.captured.width, observer.captured.height), (500, 1000))
        phone.dump_hierarchy.assert_not_called()

    def test_force_ocr_skips_hierarchy_rpc_and_keeps_latest_image(self):
        phone = device()
        on_image = Mock()
        reader = ScreenReader(phone, on_image=on_image)
        reader._ocr = Mock(return_value=[TextNode('前台对话框', (10, 10, 300, 100), 'ocr')])
        screen = reader.read(force_ocr=True)
        phone.dump_hierarchy.assert_not_called()
        phone.screenshot.assert_called_once()
        phone.current_app.assert_called_once()
        self.assertEqual(screen.source, 'ocr')
        self.assertIsNone(screen.tree)
        self.assertEqual(screen.find('前台对话框').text, '前台对话框')
        self.assertEqual(reader.last_image.size, (500, 1000))
        self.assertIs(reader.last_screen, screen)
        on_image.assert_called_once_with(reader.last_image)
        self.assertGreaterEqual(reader.last_observation_seconds, 0)

    def test_readable_ui_keeps_original_tree_for_search_without_ocr(self):
        phone = device()
        reader = ScreenReader(phone)
        reader._ocr = Mock()
        screen = reader.read()
        phone.dump_hierarchy.assert_called_once()
        phone.screenshot.assert_not_called()
        reader._ocr.assert_not_called()
        self.assertIs(screen.tree, phone.dump_hierarchy.return_value)
        self.assertEqual(screen.source, 'ui')
        self.assertEqual(len(screen.nodes), 6)

    def test_managed_preview_avoids_duplicate_capture_but_not_visual_verification(self):
        phone = device()
        phone.preview_managed = True
        on_image = Mock()
        reader = ScreenReader(phone, on_image=on_image)
        reader.read()
        phone.screenshot.assert_not_called()
        on_image.assert_not_called()
        reader._ocr = Mock(return_value=[])
        reader.read(force_ocr=True)
        phone.screenshot.assert_called_once()
        on_image.assert_called_once()

    def test_unmanaged_preview_refreshes_after_one_second_and_sets_last_image(self):
        phone = device()
        on_image = Mock()
        reader = ScreenReader(phone, on_image=on_image)
        with patch('backend.core.device.screen.time.monotonic', return_value=10):
            reader.read()
        with patch('backend.core.device.screen.time.monotonic', return_value=10.8):
            reader.read()
        self.assertEqual(phone.screenshot.call_count, 1)
        with patch('backend.core.device.screen.time.monotonic', return_value=11.01):
            reader.read()
        self.assertEqual(phone.screenshot.call_count, 2)
        self.assertEqual(reader.last_image.size, (500, 1000))

    def test_pixel_identical_frame_reuses_only_last_ocr_result_and_does_not_share_nodes(self):
        phone = device()
        on_image = Mock()
        reader = ScreenReader(phone, on_image=on_image)
        reader._ocr = Mock(return_value=[TextNode('初始识别', (10, 10, 200, 100), 'ocr')])
        first = reader.read(force_ocr=True)
        first.nodes[0].text = '调用方改了当前结果'
        second = reader.read(force_ocr=True)
        self.assertEqual(reader._ocr.call_count, 1)
        self.assertEqual(second.nodes[0].text, '初始识别')
        self.assertEqual(on_image.call_count, 2)
        # A one-pixel change outside the OCR thumbnail must invalidate the cache.
        different = phone.screenshot.return_value.copy()
        different.putpixel((499, 999), (1, 2, 3))
        phone.screenshot.return_value = different
        reader.read(force_ocr=True)
        self.assertEqual(reader._ocr.call_count, 2)
        # This is a one-image cache, so returning to the original also re-runs OCR.
        phone.screenshot.return_value = Image.new('RGB', (500, 1000), 'white')
        reader.read(force_ocr=True)
        self.assertEqual(reader._ocr.call_count, 3)

    def test_equal_byte_stream_with_different_dimensions_does_not_hit_ocr_cache(self):
        phone = device()
        phone.screenshot.return_value = Image.new('RGB', (40, 20), 'white')
        reader = ScreenReader(phone)
        reader._ocr = Mock(return_value=[])
        reader.read(force_ocr=True)
        phone.screenshot.return_value = Image.new('RGB', (20, 40), 'white')
        reader.read(force_ocr=True)
        self.assertEqual(reader._ocr.call_count, 2)

    def test_disabled_ocr_still_reads_ui_when_force_requested(self):
        phone = device()
        reader = ScreenReader(phone, ocr=False)
        reader._ocr = Mock()
        screen = reader.read(force_ocr=True)
        phone.dump_hierarchy.assert_called_once()
        reader._ocr.assert_not_called()
        self.assertEqual(screen.source, 'ui')

    def test_unreadable_ui_falls_back_to_ocr_and_keeps_tree_for_native_edit_access(self):
        phone = device()
        phone.dump_hierarchy.return_value = {'text': '', 'children': []}
        reader = ScreenReader(phone)
        reader._ocr = Mock(return_value=[TextNode('搜索', (350, 100, 450, 180), 'ocr')])
        screen = reader.read()
        self.assertEqual(screen.source, 'ocr')
        self.assertIs(screen.tree, phone.dump_hierarchy.return_value)
        phone.dump_hierarchy.assert_called_once()
        phone.screenshot.assert_called_once()

# region_ocr
import unittest
from unittest.mock import Mock

from PIL import Image

from backend.app.taobao.activities.double11.activity import CampaignRunner, NeedsAttention
from backend.core.device.screen import Screen, ScreenReader
from backend.app.taobao.activities.double11.tasks import TextNode


class RegionOCRTests(unittest.TestCase):
    def test_crop_maps_coordinates_and_keeps_full_frame_security_context(self):
        reader = ScreenReader(Mock())
        screen = Screen([TextNode('请完成安全验证', (10, 10, 300, 70))],
                        1440, 3200, 'com.taobao.taobao', 'ocr')
        reader.last_screen = screen
        reader.last_image = Image.new('RGB', (1440, 3200))
        reader._ocr = Mock(return_value=[TextNode('去赚能量', (20, 30, 180, 80))])
        enriched = reader.enrich_regions(screen, [(1000, 1100, 1440, 1400)])
        self.assertEqual(enriched.nodes[-1].bounds, (1020, 1130, 1180, 1180))
        self.assertIn(screen.nodes[0], enriched.nodes)
        reader.device.screenshot.assert_not_called()
        runner = CampaignRunner(Mock())
        with self.assertRaises(NeedsAttention) as raised:
            runner.guard(enriched)
        self.assertEqual(raised.exception.kind, 'security')

    def test_crop_never_reuses_pixels_from_a_different_observation(self):
        reader = ScreenReader(Mock())
        screen = Screen([], 1440, 3200, 'com.taobao.taobao', 'ocr')
        reader.last_screen = Screen([], 1440, 3200, 'com.taobao.taobao', 'ocr')
        reader.last_image = Image.new('RGB', (1440, 3200))
        reader._ocr = Mock()
        self.assertIs(reader.enrich_regions(screen, [(1000, 1100, 1440, 1400)]), screen)
        reader._ocr.assert_not_called()

    def test_region_bounds_are_clamped_to_current_frame(self):
        reader = ScreenReader(Mock())
        screen = Screen([], 1440, 3200, 'com.taobao.taobao', 'ocr')
        reader.last_screen = screen
        reader.last_image = Image.new('RGB', (1440, 3200))
        sizes = []
        reader._ocr = Mock(side_effect=lambda image: sizes.append(image.size) or [])
        reader.enrich_regions(screen, [(-20, -30, 1500, 3300), (1500, 0, 1600, 50)])
        self.assertEqual(sizes, [(1440, 3200)])

# performance_paths
import threading
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image
from rapidocr.ch_ppocr_rec import TextRecInput, TextRecOutput

from backend.core.device.adapter import DeviceAdapter
from backend.core.device.ocr import ExactTextCache


class ExactTextCacheTests(unittest.TestCase):
    def backend(self):
        return Mock(side_effect=lambda args: TextRecOutput(
            imgs=args.img, txts=tuple(str(int(im.flat[0])) for im in args.img),
            scores=tuple(.99 for _ in args.img), word_results=tuple(None for _ in args.img)))

    def test_unchanged_text_is_reused_but_one_pixel_change_is_recognized(self):
        backend = self.backend()
        cache = ExactTextCache(backend)
        first = np.zeros((8, 20, 3), dtype=np.uint8)
        cache(TextRecInput(img=[first]))
        same = cache(TextRecInput(img=[first.copy()]))
        self.assertEqual(same.txts, ('0',))
        self.assertEqual(backend.call_count, 1)
        changed = first.copy()
        changed[0, 0, 0] = 7
        result = cache(TextRecInput(img=[first, changed]))
        self.assertEqual(result.txts, ('0', '7'))
        self.assertEqual(len(backend.call_args.args[0].img), 1)
        self.assertEqual((cache.last_hits, cache.last_misses), (1, 1))

    def test_equal_bytes_with_different_geometry_are_not_reused(self):
        backend = self.backend()
        cache = ExactTextCache(backend)
        cache(TextRecInput(img=[np.zeros((8, 20, 3), dtype=np.uint8)]))
        cache(TextRecInput(img=[np.zeros((20, 8, 3), dtype=np.uint8)]))
        self.assertEqual(backend.call_count, 2)

    def test_cache_is_bounded_and_word_geometry_requests_bypass_it(self):
        backend = self.backend()
        cache = ExactTextCache(backend, capacity=2)
        images = [np.full((8, 20, 3), i, dtype=np.uint8) for i in range(3)]
        cache(TextRecInput(img=images))
        self.assertEqual(len(cache.cache), 2)
        cache(TextRecInput(img=[images[0]]))
        self.assertEqual(cache.last_misses, 1)
        cache(TextRecInput(img=[images[0]], return_word_box=True))
        self.assertTrue(backend.call_args.args[0].return_word_box)


class FreshFrameTests(unittest.TestCase):
    def adapter(self):
        adapter = DeviceAdapter.__new__(DeviceAdapter)
        adapter.device_type = 'uiautomator2'
        adapter.io_lock = threading.RLock()
        adapter._device = Mock()
        adapter._device.shell.return_value = Mock(exit_code=1, output='')
        adapter._device.screenshot.return_value = Image.new('RGB', (40, 80), 'white')
        return adapter

    def test_near_simultaneous_preview_and_ocr_share_one_frame_without_aliasing(self):
        phone = self.adapter()
        with patch('backend.core.device.adapter.time.monotonic', return_value=10):
            phone.screenshot()
        with patch('backend.core.device.adapter.time.monotonic', return_value=10.1):
            image = phone.screenshot(max_age=.15)
            image.putpixel((0, 0), (0, 0, 0))
            self.assertEqual(phone.screenshot(max_age=.15).getpixel((0, 0)), (255, 255, 255))
        self.assertEqual(phone._device.screenshot.call_count, 1)

    def test_expired_frames_and_failure_evidence_always_capture_again(self):
        phone = self.adapter()
        with patch('backend.core.device.adapter.time.monotonic', return_value=10):
            phone.screenshot()
        with patch('backend.core.device.adapter.time.monotonic', return_value=10.2):
            phone.screenshot(max_age=5)
            phone.screenshot()
        self.assertEqual(phone._device.screenshot.call_count, 3)

    def test_input_and_failed_input_invalidate_shared_frames(self):
        phone = self.adapter()
        with patch('backend.core.device.adapter.time.monotonic', return_value=10):
            phone.screenshot()
            phone.click(20, 30)
            phone.screenshot(max_age=.15)
            phone._device.press.side_effect = OSError('input failed')
            with self.assertRaises(OSError):
                phone.go_back()
            phone.screenshot(max_age=.15)
        self.assertEqual(phone._device.screenshot.call_count, 3)

    def test_foreground_switch_invalidates_cached_frame(self):
        phone = self.adapter()
        phone._device.app_current.return_value = {'package': 'com.taobao.taobao'}
        phone.current_app()
        with patch('backend.core.device.adapter.time.monotonic', return_value=10):
            phone.screenshot()
            phone._device.app_current.return_value = {'package': 'com.example.other'}
            phone.current_app()
            phone.screenshot(max_age=.15)
        self.assertEqual(phone._device.screenshot.call_count, 2)
