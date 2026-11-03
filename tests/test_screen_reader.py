import threading
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image
from rapidocr.ch_ppocr_rec import TextRecInput, TextRecOutput

from backend.app.taobao.activities.double11.activity import CampaignRunner, NeedsAttention
from backend.app.taobao.activities.double11.tasks import TextNode
from backend.core.device.adapter import DeviceAdapter
from backend.core.device.ocr import ExactTextCache
from backend.core.device.screen import Screen, ScreenReader


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


class RegionOCRTests(unittest.TestCase):
    def test_ui_read_without_new_image_cannot_crop_an_old_frame(self):
        phone = device();phone.preview_managed=True
        reader=ScreenReader(phone)
        reader._ocr=Mock(return_value=[TextNode('旧页面',(10,10,100,50))])
        reader.read(force_ocr=True)
        current=reader.read()
        reader._ocr.reset_mock()
        self.assertIs(reader.enrich_regions(current,[(0,0,500,200)]),current)
        reader._ocr.assert_not_called()

    def test_explicitly_hidden_native_nodes_never_enter_recognition(self):
        phone=device()
        phone.dump_hierarchy.return_value['hierarchy']['children'].append(
            {'text':'验证码登录','bounds':(10,800,400,850),'visible-to-user':'false'})
        reader=ScreenReader(phone)
        current=reader.read()
        self.assertNotIn('验证码登录',current.text)
        self.assertEqual(current.source,'ui')

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


class SerialObservationTests(unittest.TestCase):
    def test_session_starts_no_capture_or_recognition_thread(self):
        from backend.core.execution.run_control import RunControl
        phone=device();reader=ScreenReader(phone)
        with patch('threading.Thread',side_effect=AssertionError('No background observation thread')):
            with reader.observation_session({'compressed_screenshots':True,'screenshot_quality':45},RunControl()):
                self.assertTrue(reader.compressed_screenshots)
                self.assertEqual(reader.screenshot_quality,45)
                self.assertEqual(reader.read().source,'ui')
        self.assertIsNone(reader._engine)

    def test_primary_capture_is_synchronous_and_uses_user_quality(self):
        from backend.core.execution.run_control import RunControl
        class Phone:
            device_type='uiautomator2';_display_id=0
            def capture_observation(self,**kwargs):
                self.kwargs=kwargs;self.thread=threading.get_ident()
                return {'package':'org.example.sample','user_id':7},Image.new('RGB',(500,1000),'white')
        phone=Phone();reader=ScreenReader(phone)
        with patch.object(reader,'_image_nodes',return_value=[TextNode('任务',(10,10,100,40))]):
            with reader.observation_session({'compressed_screenshots':True,'screenshot_quality':45},RunControl()):
                frame=reader.read(force_ocr=True)
        self.assertEqual(phone.thread,threading.get_ident())
        self.assertEqual(phone.kwargs['quality'],45)
        self.assertTrue(phone.kwargs['compressed'])
        self.assertEqual((frame.package,frame.user_id),('org.example.sample',7))

    def test_idle_engine_is_weakly_held(self):
        import gc,weakref
        from backend.core.device.ocr import scoped_engine
        with patch('backend.core.device.ocr._create_engine',return_value=Mock()):
            engine=scoped_engine(('synthetic-serial',0));ref=weakref.ref(engine)
            self.assertIs(engine,scoped_engine(('synthetic-serial',0)))
            del engine;gc.collect();self.assertIsNone(ref())

    def test_private_transport_is_cancellable_without_killing_shared_adb(self):
        import sys,time
        from backend.core.device.read_shell import DeviceIO
        from backend.core.errors import Stopped
        io=DeviceIO();done=threading.Event();errors=[]
        def run():
            try:io.run([sys.executable,'-c','import time;time.sleep(30)'],30)
            except Stopped:errors.append('stopped')
            finally:done.set()
        thread=threading.Thread(target=run);thread.start();deadline=time.monotonic()+2
        while not io.callbacks:
            self.assertLess(time.monotonic(),deadline);time.sleep(.005)
        io.cancel();self.assertTrue(done.wait(1));thread.join(1)
        self.assertEqual(errors,['stopped'])


class PhysicalModelSessionTests(unittest.TestCase):
    def test_transport_aliases_and_new_readers_keep_physical_scope(self):
        first = ScreenReader(Mock(serial='usb-sample', identity_serial='physical-sample'))
        other = ScreenReader(Mock(serial='192.0.2.10:5555', identity_serial='physical-sample'))
        self.assertEqual(first.engine_scope, other.engine_scope)
        self.assertEqual(first.engine_scope, ('device', 'physical-sample'))
        self.assertNotEqual(first.engine_scope, ScreenReader(Mock(serial='another')).engine_scope)
        self.assertEqual(ScreenReader(None, engine_scope='explicit').engine_scope, 'explicit')

    def test_run_lease_reuses_models_across_readers_then_releases_them(self):
        import gc, weakref
        from backend.core.device.ocr import engine_session, scoped_engine
        scope = ('device', 'synthetic-pipeline')
        with patch('backend.core.device.ocr._create_engine', return_value=Mock()) as build:
            with engine_session(scope):
                self.assertEqual(build.call_count, 0)
                engine = scoped_engine(scope)
                ref = weakref.ref(engine)
                del engine
                gc.collect()
                self.assertIsNotNone(ref())
                with engine_session(scope):
                    self.assertIs(scoped_engine(scope), ref())
                self.assertEqual(build.call_count, 1)
            gc.collect()
            self.assertIsNone(ref())

    def test_concurrent_model_requests_build_one_model_for_one_device(self):
        from backend.core.device.ocr import scoped_engine
        start = threading.Barrier(4, timeout=2)
        engines, errors = [], []
        def request():
            try:
                start.wait()
                engines.append(scoped_engine(('device', 'synthetic-concurrent')))
            except Exception as exc:
                errors.append(exc)
        with patch('backend.core.device.ocr._create_engine', return_value=Mock()) as build:
            workers = [threading.Thread(target=request) for _ in range(4)]
            for worker in workers: worker.start()
            for worker in workers: worker.join(3)
        self.assertEqual(errors, [])
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(build.call_count, 1)
        self.assertEqual(len({id(engine) for engine in engines}), 1)

    def test_same_device_inference_is_serial_and_different_devices_can_progress(self):
        from backend.core.device.ocr import ManagedEngine
        engine = ManagedEngine(Mock())
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        def hold(*args, **kwargs):
            entered.set()
            if not release.wait(2): raise AssertionError('test inference was not released')
        engine.engine.side_effect = hold
        worker = threading.Thread(target=lambda: engine.infer(None, lambda: None))
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertFalse(engine.lock.acquire(blocking=False))
            second = ManagedEngine(Mock(return_value='other'))
            self.assertEqual(second.infer(None, lambda:None), 'other')
        finally:
            release.set();worker.join(3)
        self.assertFalse(worker.is_alive())

    def test_exception_ends_lease_and_releases_models(self):
        import gc,weakref
        from backend.core.device.ocr import engine_session,scoped_engine
        scope=('device','synthetic-aborted')
        with patch('backend.core.device.ocr._create_engine',return_value=Mock()):
            with self.assertRaisesRegex(RuntimeError,'aborted'):
                with engine_session(scope):
                    engine=scoped_engine(scope);ref=weakref.ref(engine)
                    del engine
                    raise RuntimeError('aborted')
            gc.collect()
            self.assertIsNone(ref())
