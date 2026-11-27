"""Offline unit and integration tests for Damai ticket grabbing, calibration, and Web UI contracts.

All XML hierarchies, screenshots, serials, viewer names, and API keys are 100% synthetic.
"""
from __future__ import annotations

from datetime import datetime
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from PIL import Image, ImageDraw

from backend.app.damai.utils import calibrate, clock, pages, targets
from backend.app.damai.utils.adb_io import DamaiTransport, TapGovernor
from backend.app.damai.utils.config import (
    Choice,
    DamaiConfig,
    LlmConfig,
    OcrConfig,
    damai_config_from_options,
    validate_damai_options,
)
from backend.app.damai.utils.flow import Sprint, viewer_name_matches
from backend.app.damai.utils.hierarchy import (
    Box,
    has_selected_border,
    parse_xml,
    split_rows,
    viewer_checkboxes,
)
from backend.app.damai.utils.locators import (
    CoordCache,
    LlmLocator,
    NativeLocator,
    OcrLocator,
    Snapshot,
    extract_point,
    text_matches,
)
from backend.app.damai.utils.pages import Page
from backend.core.device.instances import AppInstance
from backend.core.device.ocr import OCRResult
from backend.core.execution.run_control import RunControl
from backend.core.registry import default_registry
from backend.core.service.panel import Controller
from backend.core.storage.preferences import (
    preferences_path,
    read_activity_options,
    read_activity_secret,
    secrets_path,
)

DETAIL_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node bounds="[0,0][1080,2400]" class="android.widget.FrameLayout">
    <node resource-id="cn.damai:id/id_project_count_sell_time" text="05月02日 20:00 开抢" bounds="[48,1950][520,2010]" class="android.widget.TextView" />
    <node resource-id="cn.damai:id/trade_project_detail_purchase_status_bar_container_fl" clickable="true" bounds="[437,2147][1053,2272]" class="android.widget.FrameLayout">
      <node text="立即预订" bounds="[640,2180][850,2240]" class="android.widget.TextView" />
    </node>
  </node>
</hierarchy>
"""

SKU_UNEXPANDED_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node bounds="[0,0][1080,2400]" class="android.widget.FrameLayout">
    <node resource-id="cn.damai:id/ll_perform_item" clickable="true" bounds="[58,420][520,560]" class="android.widget.LinearLayout">
      <node text="2026-05-01 周五 19:30" bounds="[80,460][490,520]" class="android.widget.TextView" />
    </node>
    <node resource-id="cn.damai:id/ll_perform_item" clickable="true" bounds="[560,420][1022,560]" class="android.widget.LinearLayout">
      <node text="2026-05-02 周六 19:30" bounds="[580,460][990,520]" class="android.widget.TextView" />
    </node>
    <node resource-id="cn.damai:id/view_translucent" bounds="[0,2100][1080,2320]" class="android.view.View" />
  </node>
</hierarchy>
"""

SKU_EXPANDED_LIVE_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node bounds="[0,0][1080,2400]" class="android.widget.FrameLayout">
    <node resource-id="cn.damai:id/ll_perform_item" clickable="true" bounds="[58,420][520,560]" class="android.widget.LinearLayout">
      <node text="2026-05-01 周五 19:30" bounds="[80,460][490,520]" class="android.widget.TextView" />
    </node>
    <node resource-id="cn.damai:id/ll_perform_item" clickable="true" bounds="[560,420][1022,560]" class="android.widget.LinearLayout">
      <node text="2026-05-02 周六 19:30" bounds="[580,460][990,520]" class="android.widget.TextView" />
    </node>
    <node resource-id="cn.damai:id/tv_price_name" text="票档" bounds="[58,640][200,700]" class="android.widget.TextView" />
    <node resource-id="cn.damai:id/ll_perform_item" clickable="true" bounds="[58,740][520,880]" class="android.widget.LinearLayout">
      <node text="内场1580元" bounds="[90,780][420,840]" class="android.widget.TextView" />
    </node>
    <node resource-id="cn.damai:id/ll_perform_item" clickable="true" bounds="[560,740][1022,880]" class="android.widget.LinearLayout">
      <node text="看台580元" bounds="[590,780][900,840]" class="android.widget.TextView" />
    </node>
    <node resource-id="cn.damai:id/ll_perform_item" clickable="true" bounds="[58,910][520,1050]" class="android.widget.LinearLayout">
      <node text="看台380元" bounds="[90,950][380,1010]" class="android.widget.TextView" />
      <node resource-id="cn.damai:id/tv_tag" text="缺货登记" bounds="[380,915][510,955]" class="android.widget.TextView" />
    </node>
    <node resource-id="cn.damai:id/id_quick_seat_selection_bottom_left_action_layout" clickable="true" bounds="[58,2150][540,2270]" class="android.widget.FrameLayout">
      <node resource-id="cn.damai:id/id_quick_seat_selection_bottom_left_action_tv" text="一键下单" bounds="[210,2185][388,2235]" class="android.widget.TextView" />
    </node>
    <node resource-id="cn.damai:id/id_quick_seat_selection_bottom_right_action_layout" clickable="true" bounds="[540,2150][1022,2270]" class="android.widget.FrameLayout">
      <node resource-id="cn.damai:id/id_quick_seat_selection_bottom_right_action_tv" text="去选座" bounds="[700,2185][862,2235]" class="android.widget.TextView" />
    </node>
  </node>
</hierarchy>
"""

ORDER_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node bounds="[0,0][1080,2400]" class="android.widget.FrameLayout">
    <node resource-id="cn.damai:id/order_activity_title" text="确认购买" bounds="[420,110][660,180]" class="android.widget.TextView" />
    <node bounds="[48,840][1032,940]" class="android.widget.RelativeLayout">
      <node resource-id="cn.damai:id/text_name" text="李*四" bounds="[80,865][260,915]" class="android.widget.TextView" />
      <node resource-id="cn.damai:id/checkbox" clickable="true" checked="true" bounds="[960,860][1020,920]" class="android.widget.CheckBox" />
    </node>
    <node bounds="[48,960][1032,1060]" class="android.widget.RelativeLayout">
      <node resource-id="cn.damai:id/text_name" text="张*三" bounds="[80,985][260,1035]" class="android.widget.TextView" />
      <node resource-id="cn.damai:id/checkbox" clickable="true" checked="false" bounds="[960,980][1020,1040]" class="android.widget.CheckBox" />
    </node>
    <node text="立即提交" clickable="true" bounds="[664,2154][1022,2274]" class="android.widget.TextView" />
  </node>
</hierarchy>
"""

VERIFY_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node bounds="[0,0][1080,2400]" class="android.widget.FrameLayout">
    <node resource-id="baxia-punish" text="请拖动滑块完成验证" bounds="[120,900][960,1400]" class="android.view.View" />
  </node>
</hierarchy>
"""


def make_snap(xml: str, activity: str = '', image: Image.Image | None = None) -> Snapshot:
    root = parse_xml(xml)
    w, h = image.size if image is not None else (1080, 2400)
    return Snapshot(xml=xml, root=root, image=image, width=w, height=h, activity=activity)


class DamaiCoreTests(unittest.TestCase):
    def test_split_rows_two_column_order_and_sold_out_detection(self):
        root = parse_xml(SKU_EXPANDED_LIVE_XML)
        sessions, prices = split_rows(root)
        self.assertEqual([s.label for s in sessions], ['2026-05-01 周五 19:30', '2026-05-02 周六 19:30'])
        self.assertEqual([p.label for p in prices], ['内场1580元', '看台580元', '看台380元 缺货登记'])
        self.assertFalse(prices[0].sold_out)
        self.assertFalse(prices[1].sold_out)
        self.assertTrue(prices[2].sold_out)

    def test_live_sale_selected_border_pixel_detection(self):
        img = Image.new('RGB', (1080, 2400), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        # Unselected cell [58,420][520,560]: neutral gray border
        draw.rectangle([58, 420, 519, 559], outline=(225, 225, 225), width=4)
        # Selected cell [560,420][1022,560]: Damai live-sale orange-red border
        draw.rectangle([560, 420, 1021, 559], outline=(235, 88, 11), width=4)

        self.assertFalse(has_selected_border(img, Box(58, 420, 520, 560)))
        self.assertTrue(has_selected_border(img, Box(560, 420, 1022, 560)))

        sessions, _ = split_rows(parse_xml(SKU_EXPANDED_LIVE_XML), image=img)
        self.assertFalse(sessions[0].selected)
        self.assertTrue(sessions[1].selected)

    def test_page_state_machine_detection_covers_all_pages(self):
        self.assertEqual(pages.detect('cn.damai/.ProjectDetailActivity', parse_xml(DETAIL_XML)), Page.DETAIL)
        self.assertEqual(pages.detect('cn.damai/.NcovSkuActivity', parse_xml(SKU_EXPANDED_LIVE_XML)), Page.SKU)
        self.assertEqual(pages.detect('cn.damai/.ultron.view.activity.DmOrderActivity', parse_xml(ORDER_XML)), Page.ORDER)
        self.assertEqual(pages.detect('cn.damai/.seat.ui.TSeatActivity', None), Page.SEAT)
        self.assertEqual(pages.detect('cn.damai/com.alipay.sdk.app.H5PayActivity', None), Page.PAY)
        # Verify overlay takes priority even if SKU activity is underneath
        self.assertEqual(pages.detect('cn.damai/.NcovSkuActivity', parse_xml(VERIFY_XML)), Page.VERIFY)
        self.assertEqual(
            pages.detect('cn.damai/com.alibaba.wireless.security.open.middletier.fc.ui.ContainerActivity', None),
            Page.VERIFY,
        )

    def test_text_matches_and_native_locator_prevent_integer_collision_and_support_quick_order(self):
        self.assertTrue(text_matches('580', '看台580元'))
        self.assertFalse(text_matches('580', '内场1580元'))

        cfg_quick = DamaiConfig(session=Choice('05-02'), price=Choice('580'), order_mode='quick_order')
        tmap_quick = targets.build(cfg_quick)
        snap = make_snap(SKU_EXPANDED_LIVE_XML, 'cn.damai/.NcovSkuActivity')
        native = NativeLocator()

        self.assertEqual(native.locate(tmap_quick['session'], snap), (791, 490))
        # Must pick 看台580元 (791, 810), NOT 内场1580元 (289, 810)!
        self.assertEqual(native.locate(tmap_quick['price'], snap), (791, 810))
        # Must pick 一键下单 (299, 2210) in quick_order mode!
        self.assertEqual(native.locate(tmap_quick['confirm'], snap), (299, 2210))

        cfg_seat = DamaiConfig(session=Choice('05-02'), price=Choice('580'), order_mode='seat_map')
        tmap_seat = targets.build(cfg_seat)
        self.assertEqual(native.locate(tmap_seat['confirm'], snap), (781, 2210))

        order_snap = make_snap(ORDER_XML, 'cn.damai/.ultron.view.activity.DmOrderActivity')
        self.assertEqual(native.locate(tmap_quick['submit'], order_snap), (843, 2214))

    def test_ocr_locator_strips_and_snaps_to_row_center(self):
        img = Image.new('RGB', (1080, 1200), (255, 255, 255))

        def fake_engine(strip_img, language_correction=False):
            # Emit a text hit inside the second price row [560,740][1022,880] on the second strip (top=550)
            if strip_img.size[1] == 650:
                poly = ((600, 240), (760, 240), (760, 280), (600, 280))  # global y = 550 + 260 = 810
                return OCRResult((poly,), ('看台580元',), (0.95,), 0.01)
            return OCRResult((), (), (), 0.01)

        locator = OcrLocator(OcrConfig(enabled=True, min_conf=0.3), engine=fake_engine)
        snap = make_snap(SKU_EXPANDED_LIVE_XML, 'cn.damai/.NcovSkuActivity', image=img)
        tmap = targets.build(DamaiConfig(price=Choice('580')))
        self.assertEqual(locator.locate(tmap['price'], snap), (791, 810))

    def test_llm_extract_point_and_gemini_rejection(self):
        self.assertEqual(extract_point('{"found": true, "x": 299, "y": 2210, "reason": "ok"}'), (299, 2210))
        self.assertIsNone(extract_point('{"found": false, "reason": "absent"}'))
        with self.assertRaisesRegex(ValueError, '偏差超过 300px'):
            LlmLocator(LlmConfig(enabled=True, base_url='https://example.invalid', api_key='sk-test', model='gemini-2.5-pro'))
        with self.assertRaisesRegex(ValueError, '偏差超过 300px'):
            validate_damai_options({'llm_enabled': True, 'llm_model': 'gemini-2.5-flash'})

    def test_arbitration_and_coord_cache_signature_invalidation(self):
        t580 = targets.build(DamaiConfig(price=Choice('580')))['price']
        t1580 = targets.build(DamaiConfig(price=Choice('1580')))['price']
        v = calibrate.decide(t580, {'native': (790, 810), 'ocr': (794, 812), 'llm': (200, 200)}, tol=45)
        self.assertEqual(v.status, 'agree')
        self.assertEqual(v.point, (792, 811))

        with tempfile.TemporaryDirectory() as tmp:
            cache = CoordCache(Path(tmp) / 'coords.json')
            cache.set('price', (792, 811), source='agree', target=t580, screen_size=(1080, 2400))
            cache.save()
            reloaded = CoordCache(Path(tmp) / 'coords.json')
            self.assertEqual(reloaded.get('price', target=t580, width=1080, height=2400), (792, 811))
            # Changing price keyword invalidates stale cached point!
            self.assertIsNone(reloaded.get('price', target=t1580, width=1080, height=2400))
            self.assertTrue(reloaded.entry_info(t1580)['stale'])

    def test_step_by_step_live_sku_calibration_taps_session_to_reveal_prices(self):
        img = Image.new('RGB', (1080, 2400), (255, 255, 255))
        taps: list[tuple[int, int]] = []
        stage = {'expanded': False}

        transport = Mock(spec=DamaiTransport)
        transport.snapshot.side_effect = lambda need_image=False: make_snap(
            SKU_EXPANDED_LIVE_XML if stage['expanded'] else SKU_UNEXPANDED_XML,
            'cn.damai/.NcovSkuActivity',
            image=img if need_image else None,
        )

        def record_tap(pt, jitter_ms=(0, 0)):
            taps.append(pt)
            stage['expanded'] = True

        transport.tap.side_effect = record_tap
        transport.wait.side_effect = lambda s: None

        cfg = DamaiConfig(
            session=Choice('05-02'),
            price=Choice('580'),
            order_mode='quick_order',
            auto_select_session_on_calibrate=True,
        )
        with tempfile.TemporaryDirectory() as tmp:
            cache = CoordCache(Path(tmp) / 'coords.json')
            empty_ocr = lambda strip_img, language_correction=False: OCRResult((), (), (), 0.0)
            page, verdicts, _ = calibrate.calibrate_live(transport, cfg, cache, ocr_engine=empty_ocr)
            self.assertEqual(page, Page.SKU)
            self.assertEqual(taps, [(791, 490)])
            by_name = {v.target: v.point for v in verdicts}
            self.assertEqual(by_name['session'], (791, 490))
            self.assertEqual(by_name['price'], (791, 810))
            self.assertEqual(by_name['confirm'], (299, 2210))

    def test_sprint_full_live_path_presale_relocation_viewer_check_and_verify_resume(self):
        self.assertTrue(viewer_name_matches('张三', '张*三'))
        self.assertTrue(viewer_name_matches('李四', '李*四'))
        self.assertFalse(viewer_name_matches('王五', '张*三'))

        state = {'step': 'detail'}
        taps: list[tuple[int, int]] = []

        def fake_snapshot(need_image=False):
            step = state['step']
            if step == 'detail':
                return make_snap(DETAIL_XML, 'cn.damai/.ProjectDetailActivity')
            if step == 'sku_unexpanded':
                return make_snap(SKU_UNEXPANDED_XML, 'cn.damai/.NcovSkuActivity')
            if step == 'sku_expanded':
                return make_snap(SKU_EXPANDED_LIVE_XML, 'cn.damai/.NcovSkuActivity')
            if step == 'verify':
                state['step'] = 'order'
                return make_snap(VERIFY_XML, 'cn.damai/com.alibaba.wireless.security.open.middletier.fc.ui.ContainerActivity')
            if step == 'order':
                return make_snap(ORDER_XML, 'cn.damai/.ultron.view.activity.DmOrderActivity')
            return make_snap('', 'cn.damai/com.alipay.sdk.app.H5PayActivity')

        def fake_tap(pt, jitter_ms=(0, 0)):
            taps.append(pt)
            if pt == (745, 2209):
                state['step'] = 'sku_unexpanded'
            elif pt == (791, 490):
                state['step'] = 'sku_expanded'
            elif pt == (299, 2210):
                # Simulate Baxia slider appearing right after 一键下单!
                state['step'] = 'verify'
            elif pt == (843, 2214):
                state['step'] = 'pay'

        transport = Mock(spec=DamaiTransport)
        transport.snapshot.side_effect = fake_snapshot
        transport.tap.side_effect = fake_tap
        transport.wait.side_effect = lambda s: None
        transport.check.side_effect = lambda: None

        cfg = DamaiConfig(
            session=Choice('05-02'),
            price=Choice('580'),
            order_mode='quick_order',
            viewer_count=2,
            viewer_names=('李四', '张三'),
            submit_order=True,
            jitter_ms=(0, 0),
            auto_resume_verify=True,
            verify_wait_s=5.0,
        )
        with tempfile.TemporaryDirectory() as tmp:
            cache = CoordCache(Path(tmp) / 'coords.json')
            # Seed stale presale confirm button (839, 2213) -> Sprint must auto-relocate to 一键下单 (299, 2210)!
            tmap = targets.build(cfg)
            cache.set('confirm', (839, 2213), source='single', target=tmap['confirm'], screen_size=(1080, 2400))
            with patch('backend.core.service.notify.notify_security_attention') as mock_notify:
                sprint = Sprint(transport, cfg, cache)
                result = sprint.run()
                self.assertEqual(result.status, 'submitted')
                self.assertEqual(result.page, Page.PAY)
                mock_notify.assert_called_once()

            # Verify tap sequence:
            # 1. Detail buy_bar (745, 2209)
            # 2. SKU session 05-02 (791, 490)
            # 3. SKU price 580 (791, 810)
            # 4. SKU live 一键下单 (299, 2210) -- NOT stale (839, 2213)!
            # 5. Order unchecked 张*三 checkbox (990, 1010) -- already-checked 李*四 (990, 890) NEVER tapped!
            # 6. Order 立即提交 (843, 2214)
            self.assertEqual(
                taps,
                [(745, 2209), (791, 490), (791, 810), (299, 2210), (990, 1010), (843, 2214)],
            )

    def test_tap_governor_enforces_sliding_window_rate_limit(self):
        clock_val = [100.0]
        slept: list[float] = []
        gov = TapGovernor(max_taps=5, window_s=1.5)
        for _ in range(5):
            w = gov.throttle(now_fn=lambda: clock_val[0], wait_fn=lambda d: slept.append(d))
            self.assertEqual(w, 0.0)
            clock_val[0] += 0.1
        # 6th tap at t=100.5s must sleep until 100.0 + 1.51 = 101.51s (~1.01s)
        def advance(d):
            slept.append(round(d, 3))
            clock_val[0] += d

        waited = gov.throttle(now_fn=lambda: clock_val[0], wait_fn=advance)
        self.assertGreaterEqual(waited, 1.0)
        self.assertEqual(len(slept), 1)

    def test_clock_parsers_and_stop_interrupt(self):
        ref = datetime(2026, 4, 28, 12, 0, 0)
        self.assertEqual(clock.parse_sell_time('05月02日 20:00 开抢', now=ref), datetime(2026, 5, 2, 20, 0, 0))
        self.assertEqual(clock.parse_sell_time('明天 15:18 开抢', now=ref), datetime(2026, 4, 29, 15, 18, 0))
        self.assertEqual(clock.parse_config_time('20:00:00', now=ref), datetime(2026, 4, 28, 20, 0, 0))

        ctrl = RunControl()
        ctrl.stop()
        from backend.core.errors import Stopped
        with self.assertRaises(Stopped):
            clock.wait_until((clock.time.time() + 10.0) * 1000, 0.0, control=ctrl)

    def test_transport_tap_uses_adb_shell_input_tap_and_dump_fallback(self):
        calls = []

        def fake_adb(args, timeout=6.0, binary_output=False):
            calls.append(args)
            if args[:2] == ('exec-out', 'cat'):
                return subprocess.CompletedProcess(args, 0, stdout=DETAIL_XML, stderr='')
            return subprocess.CompletedProcess(args, 0, stdout='', stderr='')

        dev = Mock()
        dev.serial = 'SYNTHETICUSB01'
        dev.raw = None
        transport = DamaiTransport(dev, adb_runner=fake_adb)
        transport.tap((300, 2200), jitter_ms=(0, 0))
        self.assertEqual(calls[0], ('shell', 'input', 'tap', '300', '2200'))
        xml = transport.dump_xml()
        self.assertIn('trade_project_detail_purchase_status_bar_container_fl', xml)


class DamaiPanelAndSecretIsolationTests(unittest.TestCase):
    def test_registry_discovers_damai_and_secrets_never_leak_into_state_or_diagnostics(self):
        registry = default_registry()
        reg = registry.get('damai', 'ticket_grab')
        self.assertEqual(reg.app.label, '大麦')
        self.assertEqual(reg.activity.label, '定时抢票与坐标校准')

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'MIAO_DATA_DIR': tmp}):
            with patch('backend.core.service.panel.data_dir', return_value=Path(tmp)):
                ctrl = Controller(registry=registry, app='damai', activity='ticket_grab', persist_preferences=True)
                secret_marker = 'sk-ant-synthetic-secret-99887766'

                ctrl.dispatch(
                    'configure',
                    {
                        'app': 'damai',
                        'activity': 'ticket_grab',
                        'config': {
                            'activity_options': {
                                'session_text': '2026-05-02',
                                'price_text': '580',
                                'lead_ms': 95,
                                'order_mode': 'quick_order',
                                'submit_order': True,
                            }
                        },
                        'activity_secrets': {'llm_api_key': secret_marker},
                    },
                )

                snap = ctrl.snapshot()
                serialized_state = json.dumps(snap, ensure_ascii=False)
                self.assertNotIn(secret_marker, serialized_state)
                self.assertTrue(snap['activity_state']['has_llm_key'])
                self.assertEqual(snap['config']['activity_options']['price_text'], '580')
                self.assertEqual(read_activity_options('damai', 'ticket_grab')['lead_ms'], 95)
                self.assertEqual(read_activity_secret('damai', 'llm_api_key'), secret_marker)
                self.assertNotIn(secret_marker, preferences_path().read_text(encoding='utf-8'))
                self.assertTrue(secrets_path().is_file())

                # Reject broken Gemini vision models synchronously in dispatch
                with self.assertRaisesRegex(ValueError, '偏差超过 300px'):
                    ctrl.dispatch(
                        'configure',
                        {
                            'app': 'damai',
                            'activity': 'ticket_grab',
                            'config': {'activity_options': {'llm_enabled': True, 'llm_model': 'gemini-2.5-pro'}},
                        },
                    )

                # Offline clear_cache action works without device connected
                ctrl.dispatch('activity_action', {'app': 'damai', 'activity': 'ticket_grab', 'name': 'clear_cache'})
                ctrl.thread.join(3)
                self.assertEqual(ctrl.snapshot()['status'], 'disconnected')

                # Connect synthetic device and verify diagnostics zip never contains secret_marker
                fake_dev = Mock()
                fake_dev.device_type = 'uiautomator2'
                fake_dev.serial = 'SYNTHETICUSB01'
                fake_dev.identity_serial = 'SYNTHETICUSB01'
                inst = AppInstance('cn.damai', 0, '大麦')
                fake_dev.app_instances = [inst]
                fake_dev.selected_instance = inst
                fake_dev.screenshot.return_value = Image.new('RGB', (108, 240), (255, 255, 255))
                ctrl.device = fake_dev
                ctrl.update(connected=True, app_instances=[inst.to_dict()], selected_instance=inst.id)

                diag_bytes = ctrl.diagnostics()
                with zipfile.ZipFile(io.BytesIO(diag_bytes)) as zf:
                    for name in zf.namelist():
                        self.assertNotIn(secret_marker.encode('utf-8'), zf.read(name), name)


if __name__ == '__main__':
    unittest.main()
