import io
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import zipfile
from urllib.error import HTTPError
from urllib.request import urlopen

from PIL import Image

from backend.core.service.panel import Controller, make_handler
from backend.app.taobao.activities.double11.activity import NeedsAttention, load_config
from backend.core.storage.daily_state import DailyState


class DailyStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def store(self, serial='test-phone-a', day='2026-10-19'):
        return DailyState(self.root, serial, 'uiautomator2', day)

    def test_run_high_watermarks_survive_reload_and_scans_do_not_inflate_totals(self):
        store = self.store()
        tasks = [dict(key='manual-one', title='邀请好友', status='manual')]
        store.progress('first-run', dict(completed=1, attempts=1, elapsed=15), tasks,
                       {'app:未安装示例': '未安装示例应用，已跳过'})
        store.progress('first-run', dict(completed=1, attempts=1, elapsed=15), tasks)
        store.progress('first-run', dict(completed=2, attempts=2, elapsed=32), tasks)
        reloaded = self.store()
        reloaded.progress('first-run', dict(completed=2, attempts=2, elapsed=32), tasks)
        reloaded.progress('scan', dict(completed=0, skipped=1, elapsed=99), tasks, count_stats=False)
        reloaded.progress('second-run', dict(completed=1, claims=1, attempts=2, elapsed=8), tasks)
        stats = reloaded.snapshot()['stats']
        self.assertEqual((stats['completed'], stats['claims'], stats['attempts'], stats['elapsed'], stats['skipped']),
                         (3, 1, 4, 40, 1))
        self.assertIn('app:未安装示例', reloaded.unavailable_tasks())
        self.assertEqual(reloaded.snapshot()['unavailable_count'], 1)
        self.assertNotIn('test-phone-a', reloaded.stats_path.read_text(encoding='utf-8'))

    def test_new_device_and_day_are_isolated_and_no_raw_identity_in_paths(self):
        today = self.store()
        today.progress('run', dict(completed=2), unavailable_tasks={'app:示例': '未安装'})
        other_phone = self.store(serial='test-phone-b')
        tomorrow = self.store(day='2026-10-20')
        for fresh in (other_phone, tomorrow):
            self.assertEqual(fresh.snapshot()['stats']['completed'], 0)
            self.assertEqual(fresh.unavailable_tasks(), {})
            self.assertNotIn('test-phone', str(fresh.folder))
        self.assertEqual(self.store().snapshot()['stats']['completed'], 2)

    def test_cross_midnight_run_counts_only_new_day_increases(self):
        tomorrow = self.store(day='2026-10-20')
        tomorrow.progress('overnight-run', dict(completed=4, attempts=5, elapsed=90),
                          baseline=dict(completed=3, attempts=4, elapsed=80))
        tomorrow.progress('overnight-run', dict(completed=5, attempts=6, elapsed=110))
        stats = tomorrow.snapshot()['stats']
        self.assertEqual((stats['completed'], stats['attempts'], stats['elapsed']), (2, 2, 30))

    def test_temporary_task_filters_are_not_persisted_as_daily_manual_work(self):
        store = self.store()
        store.progress('run', {}, tasks=[
            dict(key='filtered', status='disabled', reason='本轮未选择该任务类型；调整设置后可再次执行'),
            dict(key='cross-app-off', status='disabled', reason='跨应用任务已关闭'),
            dict(key='not-installed', status='disabled', reason='未安装示例应用'),
        ])
        self.assertEqual(store.snapshot()['stats']['skipped'], 1)

    def test_failures_keep_png_and_json_and_export_only_selected_day_device(self):
        store = self.store()
        store.log('INFO', '连接设备 test-phone-a')
        event = dict(reason='需要安全验证', task=dict(title='浏览任务'), phase='browse')
        evidence = store.failure(event, Image.new('RGB', (60, 100), 'white'))
        self.assertEqual(store.failure(event, Image.new('RGB', (60, 100))), evidence)
        self.assertEqual(store.snapshot()['failures_count'], 1)
        self.assertTrue(Path(evidence['screenshot']).is_file())
        metadata = json.loads(Path(evidence['metadata']).read_text(encoding='utf-8'))
        self.assertEqual(metadata['reason'], '需要安全验证')
        self.store('another-phone').failure(dict(reason='其他设备不能混入'))
        exported = store.diagnostics(dict(device=dict(serial='test-phone-a'), config={'secret': 'example'}))
        with zipfile.ZipFile(io.BytesIO(exported)) as archive:
            self.assertEqual(len([name for name in archive.namelist() if name.endswith('.png')]), 1)
            report = json.loads(archive.read('report.json'))
            self.assertEqual(report['device']['serial'], '[设备标识已隐藏]')
            self.assertNotIn('config', report)
            self.assertNotIn('test-phone-a', archive.read('events.jsonl').decode())
            self.assertIn('个人信息', archive.read('READ-ME.txt').decode())

    def test_failure_callback_kind_is_saved_without_colliding_with_journal_event_type(self):
        store = self.store()
        event = dict(reason='自动化调用异常', phase='error', kind='exception', time='2026-10-19T12:00:00+08:00')
        result = store.failure(event, Image.new('RGB', (20, 40)))
        self.assertTrue(Path(result['metadata']).exists())
        journal = [json.loads(line) for line in store.events_path.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(journal[-1]['kind'], 'failure')
        self.assertEqual(journal[-1]['detail_kind'], 'exception')
        self.assertEqual(self.store().snapshot()['failures_count'], 1)

    def test_screenshot_failure_still_keeps_reason_and_corrupt_state_is_preserved(self):
        store = self.store()
        item = store.failure(dict(reason='连接中断', phase='restore', capture_error='device offline'))
        self.assertEqual(item['screenshot'], '')
        self.assertEqual(json.loads(Path(item['metadata']).read_text())['capture_error'], 'device offline')
        store.stats_path.write_text('{broken', encoding='utf-8')
        recovered = self.store()
        self.assertTrue(recovered.recovery_warning)
        self.assertEqual(len(list(recovered.folder.glob('state.corrupt-*.json'))), 1)

    def test_missing_resolved_identity_is_rejected(self):
        for value in (None, '', '自动选择', ' 自动选择 '):
            with self.assertRaises(ValueError):
                DailyState(self.root, value, 'uiautomator2')

    def test_malformed_structured_state_is_recovered_before_mutating_defaults(self):
        store = self.store()
        damaged = json.loads(store.stats_path.read_text(encoding='utf-8'))
        damaged.update(stats={'completed': 99}, skipped_task_keys=None, failures_count='bad')
        store.stats_path.write_text(json.dumps(damaged), encoding='utf-8')
        recovered = self.store()
        self.assertTrue(recovered.recovery_warning)
        self.assertEqual(recovered.snapshot()['stats']['completed'], 0)
        recovered.progress('new-run', dict(completed=1))
        self.assertEqual(recovered.snapshot()['stats']['completed'], 1)


class ControllerDailyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch('backend.core.service.panel.data_dir', return_value=Path(self.temp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.controller = Controller()
        self.controller.device = Mock(serial='resolved-test-phone', device_type='uiautomator2')
        self.controller.device.io_lock = threading.RLock()
        self.controller.device.screenshot.return_value = Image.new('RGB', (50, 100), 'white')

    def test_restart_same_device_loads_totals_and_availability_without_scanning_reset(self):
        c = self.controller
        with patch('backend.core.service.panel.local_day', return_value='2026-10-19'):
            c._ensure_daily()
            c.unavailable_tasks['app:示例'] = '未安装示例，已跳过'
            c._runner_event('run', True, stats=dict(completed=2, elapsed=20), phase='done')
            new = Controller()
            new.device = c.device
            new._ensure_daily()
            self.assertEqual(new.snapshot()['daily']['stats']['completed'], 2)
            self.assertIn('app:示例', new.unavailable_tasks)
            new._runner_event('scan', False, stats=dict(completed=0, elapsed=5), tasks=[])
            self.assertEqual(new.snapshot()['daily']['stats']['completed'], 2)

    def test_usb_and_wireless_transport_share_daily_hardware_identity(self):
        c = self.controller
        c.device.identity_serial = 'test-hardware-identity'
        c._runner_event('usb-run', True, stats=dict(completed=2))
        device_key = c.daily.device_key
        c.device = Mock(serial='192.0.2.10:5555', identity_serial='test-hardware-identity',
                        device_type='uiautomator2')
        self.assertFalse(c._ensure_daily())
        self.assertEqual(c.daily.device_key, device_key)
        self.assertEqual(c.snapshot()['daily']['stats']['completed'], 2)

    def test_midnight_preserves_runner_cache_reference_and_resets_its_contents(self):
        c = self.controller
        with patch('backend.core.service.panel.local_day', return_value='2026-10-19'):
            c._ensure_daily()
            cache = c.unavailable_tasks
            cache['app:示例'] = '未安装示例，已跳过'
            c._runner_event('overnight', True, stats=dict(completed=2, elapsed=20))
        with patch('backend.core.service.panel.local_day', return_value='2026-10-20'):
            c._runner_event('overnight', True, stats=dict(completed=3, elapsed=24))
            self.assertIs(cache, c.unavailable_tasks)
            self.assertFalse(cache)
            self.assertEqual(c.snapshot()['daily']['stats']['completed'], 1)
            self.assertEqual(c.snapshot()['daily']['stats']['elapsed'], 4)

    def test_diagnostics_switching_day_before_runner_event_does_not_import_yesterday_totals(self):
        c = self.controller
        with patch('backend.core.service.panel.local_day', return_value='2026-10-19'):
            c._runner_event('overnight', True, stats=dict(completed=8, attempts=9, elapsed=120))
        with patch('backend.core.service.panel.local_day', return_value='2026-10-20'):
            # Export/stop/pause may switch the store before the runner emits.
            c.diagnostics()
            self.assertEqual(c.snapshot()['daily']['stats']['completed'], 0)
            c._runner_event('overnight', True, stats=dict(completed=9, attempts=10, elapsed=125))
            self.assertEqual(c.snapshot()['daily']['stats']['completed'], 1)
            self.assertEqual(c.snapshot()['daily']['stats']['attempts'], 1)
            self.assertEqual(c.snapshot()['daily']['stats']['elapsed'], 5)

    def test_connection_progress_is_visible_and_failed_reconnect_detaches_previous_phone(self):
        c = self.controller
        c._ensure_daily()
        c.on_image(Image.new('RGB', (20, 40)))
        c.log('INFO', '旧手机 resolved-test-phone')
        stages = []
        def failed_connect(kind, serial, on_status, **kwargs):
            on_status('正在等待手机调试授权')
            stages.append(c.snapshot())
            raise ConnectionError('等待授权超时')
        with patch('backend.core.device.adapter.DeviceAdapter', side_effect=failed_connect):
            with self.assertLogs('backend.core.service.panel', level='ERROR'):
                c._work('connect', {}, load_config())
        self.assertEqual(stages[0]['status'], 'connecting')
        self.assertEqual(stages[0]['message'], '正在等待手机调试授权')
        self.assertFalse(stages[0]['connected'])
        final = c.snapshot()
        self.assertEqual(final['status'], 'error')
        self.assertEqual(final['phase'], 'connect_failed')
        self.assertIn('等待授权超时', final['message'])
        self.assertFalse(final['connected'])
        self.assertIsNone(c.device)
        self.assertIsNone(c.daily)
        self.assertIsNone(c.image)
        self.assertFalse(any('旧手机' in line['message'] for line in final['logs']))
        with self.assertRaisesRegex(ValueError, '请先连接设备'):
            c.diagnostics()

    def test_diagnostics_http_without_device_returns_actionable_400_and_connected_export_is_zip(self):
        c = self.controller
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(c, 'test-token'))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}/api/diagnostics'
            c.device = None
            with self.assertRaises(HTTPError) as denied:
                urlopen(url, timeout=2)
            self.assertEqual(denied.exception.code, 400)
            self.assertIn('请先连接设备', json.loads(denied.exception.read())['error'])
            c.device = Mock(serial='test-http-phone', device_type='uiautomator2')
            c._ensure_daily()
            with urlopen(url, timeout=2) as response:
                self.assertEqual(response.headers['Content-Type'], 'application/zip')
                with zipfile.ZipFile(io.BytesIO(response.read())) as archive:
                    self.assertIn('state.json', archive.namelist())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_failure_and_preview_callbacks_do_not_require_panel_lock_for_device_calls(self):
        c = self.controller
        def screenshot():
            self.assertFalse(c.lock._is_owned())
            return Image.new('RGB', (50, 100), 'white')
        c.device.screenshot.side_effect = screenshot
        c.on_failure(dict(reason='测试失败', phase='verify'))
        self.assertEqual(c.snapshot()['daily']['failures_count'], 1)
        c.on_image(Image.new('RGB', (50, 100)))
        c.on_image(Image.new('RGB', (50, 100)))
        self.assertEqual(c.snapshot()['image_sequence'], 2)
        self.assertTrue(c.snapshot()['image_updated_at'])

    def test_reported_guard_followed_by_exception_keeps_one_rich_evidence_record(self):
        c = self.controller
        def fail_after_callback(auto_open):
            c.on_failure(dict(reason='检测到安全验证，需要本人处理', phase='browse',
                              task={'title': '浏览任务'}, kind='verification'))
            raise NeedsAttention('检测到安全验证，需要本人处理')
        runner = Mock()
        runner.run.side_effect = fail_after_callback
        with patch('backend.core.service.panel.CampaignRunner', return_value=runner):
            c._work('run', {}, load_config())
        result = c.snapshot()
        self.assertEqual(result['status'], 'needs_attention')
        self.assertEqual(result['daily']['failures_count'], 1)
        self.assertEqual(result['last_failure']['task']['title'], '浏览任务')
        self.assertEqual(result['last_failure']['phase'], 'browse')

    def test_preview_skips_busy_device_lock(self):
        c = self.controller
        held = threading.Event()
        release = threading.Event()
        def hold_device():
            with c.device.io_lock:
                held.set()
                release.wait(3)
        holder = threading.Thread(target=hold_device)
        holder.start()
        self.assertTrue(held.wait(1))
        c._start_preview()
        try:
            self.assertFalse(c.device.screenshot.called)
            self.assertFalse(c._preview_stop.wait(.35))
            self.assertFalse(c.device.screenshot.called)
        finally:
            c._preview_stop.set()
            release.set()
            holder.join(1)
            c._preview_thread.join(1)

    def test_closed_browser_suspends_preview_captures(self):
        c = self.controller
        c._last_client_seen = 0
        c._start_preview()
        try:
            self.assertFalse(c._preview_stop.wait(.2))
            c.device.screenshot.assert_not_called()
        finally:
            c._preview_stop.set()
            c._preview_thread.join(1)

    def test_security_exception_notifies_desktop_without_duplicate_capture_or_resume(self):
        c = self.controller
        def stop_for_security(auto_open):
            c.on_failure(reason='请本人验证', phase='security', task={'title': '浏览任务'})
            raise NeedsAttention('请本人验证', kind='security', evidence_saved=True)
        runner = Mock()
        runner.run.side_effect = stop_for_security
        with patch('backend.core.service.panel.CampaignRunner', return_value=runner), \
                patch('backend.core.service.panel.notify_security_attention') as notify:
            c._work('run', {}, load_config())
        notify.assert_called_once_with()
        runner.run.assert_called_once()
        self.assertEqual(c.snapshot()['status'], 'needs_attention')
        self.assertEqual(c.snapshot()['daily']['failures_count'], 1)
        self.assertEqual(c.snapshot()['last_failure']['phase'], 'security')

    def test_navigation_attention_has_no_security_popup(self):
        c = self.controller
        runner = Mock()
        runner.run.side_effect = NeedsAttention('未能进入任务菜单')
        with patch('backend.core.service.panel.CampaignRunner', return_value=runner), \
                patch('backend.core.service.panel.notify_security_attention') as notify:
            c._work('run', {}, load_config())
        notify.assert_not_called()
        self.assertEqual(c.snapshot()['daily']['failures_count'], 1)

# rankings
"""Persistent aggregation preserves daily counting, identity and read-only data."""
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

from backend.core.service.panel import Controller, make_handler
from backend.core.storage.daily_state import DailyState, local_day
from backend.core.storage.namespace import StorageNamespace
from backend.core.storage.rankings import RankingStore


class RankingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = RankingStore(self.root)

    def record(self, serial='synthetic-one', app='sample', activity='daily', day=None, instance='main', **stats):
        ledger = DailyState(self.root, serial, 'uiautomator2', day=day, instance_id=instance,
                            namespace=StorageNamespace(app, activity))
        ledger.progress('synthetic-run', stats)
        return ledger

    def test_devices_instances_dates_and_activities_aggregate_without_recounting(self):
        yesterday = (date.fromisoformat(local_day()) - timedelta(days=1)).isoformat()
        a = self.record(completed=2, elapsed=100)
        self.record(day=yesterday, completed=3, elapsed=200)
        self.record(instance='clone', completed=4, elapsed=300)
        self.record(activity='weekly', completed=5, elapsed=400)
        self.record(serial='synthetic-two', completed=1, elapsed=50)
        result = self.store.snapshot()
        row = next(item for item in result['rows'] if item['device_key'] == a.device_key)
        self.assertEqual(row['stats']['completed'], 14)
        self.assertEqual(row['stats']['elapsed'], 1000)
        self.assertEqual(row['days'], 2)
        self.assertEqual(result['records'], 5)
        self.assertEqual(self.store.snapshot()['rows'], result['rows'])
        today = self.store.snapshot(period='today')['rows'][0]
        self.assertEqual(today['stats']['completed'], 11)
        activities = self.store.snapshot(group='activity', metric='completed')['rows']
        self.assertEqual(len(activities), 3)
        self.assertEqual(activities[0]['activity'], 'daily')
        self.assertEqual(activities[0]['stats']['completed'], 9)

    def test_reopen_reads_persistent_updates_and_does_not_write_counters(self):
        ledger = self.record(completed=1, elapsed=5)
        before = ledger.stats_path.read_bytes()
        self.store.snapshot()
        self.assertEqual(before, ledger.stats_path.read_bytes())
        ledger.progress('synthetic-run', {'completed': 4, 'elapsed': 9})
        reopened = RankingStore(self.root).snapshot()
        self.assertEqual(reopened['rows'][0]['stats']['completed'], 4)
        self.assertEqual(reopened['rows'][0]['stats']['elapsed'], 9)
        self.assertEqual(reopened['rows'][0]['stats']['failed'], 0)

    def test_skip_and_failure_use_ledger_counts_not_diagnostic_count(self):
        ledger = self.record(failed=2)
        ledger.progress('synthetic-run', {'failed': 2}, tasks=[{'key': 'demo', 'status': 'disabled'}])
        ledger.failure({'reason': 'synthetic security verification'})
        row = self.store.snapshot()['rows'][0]
        self.assertEqual(row['stats']['skipped'], 1)
        self.assertEqual(row['stats']['failed'], 2)

    def test_malformed_and_mismatched_records_are_retained_and_ignored(self):
        ledger = self.record()
        for content in ('{', '[]', json.dumps(dict(ledger.data, device_key='different'))):
            ledger.stats_path.write_text(content, encoding='utf-8')
            result = self.store.snapshot()
            self.assertEqual(result['rows'], [])
            self.assertEqual(result['ignored'], 1)
            self.assertEqual(ledger.stats_path.read_text(encoding='utf-8'), content)

    def test_migrated_legacy_copy_is_not_counted_twice(self):
        ledger = self.record(completed=2)
        old = self.root / 'daily' / local_day() / ledger.device_key
        old.mkdir(parents=True)
        (old / 'state.json').write_bytes(ledger.stats_path.read_bytes())
        self.assertEqual(self.store.snapshot()['rows'][0]['stats']['completed'], 2)

    def test_labels_and_anonymous_identity_without_raw_device_data(self):
        self.record()
        result = self.store.snapshot(group='activity', catalog=[dict(id='sample', label='示例应用', activities=[dict(id='daily', label='每日任务')])])
        self.assertEqual(result['rows'][0]['app_label'], '示例应用')
        self.assertEqual(result['rows'][0]['activity_label'], '每日任务')
        self.assertNotIn('synthetic-one', json.dumps(result))
        self.assertTrue(result['local_only'])

    def test_query_values_cannot_select_arbitrary_files(self):
        for kwargs in ({'group': '../daily'}, {'period': 'yesterday'}, {'metric': 'raw'}):
            with self.assertRaises(ValueError):
                self.store.snapshot(**kwargs)

    def test_apps_remain_separate_and_bad_numeric_values_do_not_poison_totals(self):
        first = self.record(app='first', completed=2, elapsed=10)
        second = self.record(app='second', completed=3, elapsed=20)
        value = json.loads(first.stats_path.read_text(encoding='utf-8'))
        value['stats'].update(elapsed=float('inf'), failed=-4, claims=True)
        first.stats_path.write_text(json.dumps(value), encoding='utf-8')
        rows = self.store.snapshot(metric='completed')['rows']
        self.assertEqual([row['app'] for row in rows], ['second', 'first'])
        self.assertEqual(rows[1]['stats']['elapsed'], 0)
        self.assertEqual(rows[1]['stats']['failed'], 0)
        self.assertEqual(rows[1]['stats']['claims'], 0)
        self.assertEqual(rows[0]['device_key'], second.device_key)

    def test_http_uses_default_private_root_and_validates_queries(self):
        self.record(completed=2)
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(Controller(), 'test-token'))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with patch('backend.core.service.panel.data_dir', return_value=self.root):
                base = f'http://127.0.0.1:{server.server_port}'
                with urlopen(base + '/api/rankings?group=activity&period=today&metric=completed', timeout=5) as response:
                    result = json.load(response)
                self.assertEqual(result['rows'][0]['stats']['completed'], 2)
                with self.assertRaises(HTTPError) as invalid:
                    urlopen(base + '/api/rankings?group=invalid', timeout=5)
                self.assertEqual(invalid.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(3)

# framework_preferences
"""Regression checks for persisted settings, browsing contracts and ADB ownership."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from backend.core.device.adb_lifecycle import OwnedAdb
from backend.core.device.comfort import device_comfort
from backend.core.execution.browse_contract import apply_browse_contract
from backend.core.execution.task_policy import TaskPolicy
from backend.core.models import Task
from backend.core.storage.daily_state import DailyState
from backend.core.storage.namespace import StorageNamespace
from backend.core.storage.preferences import read_preferences, save_preferences


class FrameworkPreferencesTests(unittest.TestCase):
    def test_preferences_merge_and_survive_new_reader(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, MIAO_DATA_DIR=folder):
            save_preferences(config={'minimum_volume': True})
            save_preferences(activity_branch='development')
            self.assertEqual(read_preferences(), {'config': {'minimum_volume': True}, 'activity_branch': 'development'})
            self.assertTrue((Path(folder) / 'preferences.json').is_file())

    def test_bad_preferences_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, MIAO_DATA_DIR=folder):
            path = Path(folder) / 'preferences.json'
            path.write_text('broken', encoding='utf-8')
            self.assertEqual(read_preferences(), {})
            self.assertEqual(path.read_text(encoding='utf-8'), 'broken')

    def task(self, title='答题领金币', detail='浏览15秒', **kwargs):
        return Task('example', title, detail, '去完成', (1, 2, 3, 4), kind='manual', skip_reason='答题', **kwargs)

    def test_explicit_browse_instruction_beats_quiz_title(self):
        task = apply_browse_contract(self.task())
        self.assertEqual((task.kind, task.seconds, task.skip_reason), ('browse', 15, ''))
        self.assertFalse(task.browse_once)

    def test_untimed_browse_is_once_and_durable(self):
        task = apply_browse_contract(self.task(detail='浏览页面领取'))
        self.assertTrue(task.browse_once)
        with tempfile.TemporaryDirectory() as folder:
            state = DailyState(folder, 'synthetic', 'uiautomator2')
            policy = TaskPolicy()
            policy.bind(state)
            policy.mark_browse_once(task)
            policy.bind(DailyState(folder, 'synthetic', 'uiautomator2'))
            self.assertIn('今日已尝试一次', policy.reason(task))

    def test_browse_instruction_does_not_override_required_actions(self):
        for title, detail in [('答题', '浏览15秒后完成答题'), ('支付订单', '浏览15秒'), ('邀请好友', '浏览页面'), ('答题', '提交答案')]:
            with self.subTest(title=title, detail=detail):
                self.assertEqual(apply_browse_contract(self.task(title, detail)).kind, 'manual')

    def test_comfort_disabled_makes_no_device_calls(self):
        device = Mock(device_type='uiautomator2', io_lock=None)
        with device_comfort(device, {}):
            pass
        device._checked_shell.assert_not_called()

    def test_comfort_restores_on_exception(self):
        device = Mock(device_type='uiautomator2', io_lock=None)
        volume = [7]
        def shell(command, **kwargs):
            if '--get' in command:
                return f'[V] volume is {volume[0]} in range [0..15]'
            if '--set' in command:
                volume[0] = int(command[-1])
            return '1' if command[-1] == 'screen_brightness_mode' else '128' if command[-1] == 'screen_brightness' else ''
        device._checked_shell.side_effect = shell
        with self.assertRaises(RuntimeError):
            with device_comfort(device, {'minimum_volume': True, 'minimum_brightness': True}):
                raise RuntimeError('synthetic stop')
        commands = [call.args[0] for call in device._checked_shell.call_args_list]
        writes = [c for c in commands if '--set' in c or c[:2] == ['settings', 'put']]
        self.assertEqual(writes[-3:], [['settings','put','system','screen_brightness','128'], ['settings','put','system','screen_brightness_mode','1'], ['cmd','media_session','volume','--stream','3','--set','7']])
        self.assertEqual(volume[0], 7)

    def test_adb_cleanup_only_new_matching_process(self):
        with patch.object(OwnedAdb, '_processes', side_effect=[{(1, '10')}, {(1, '10'), (2, '20')}]), patch.object(OwnedAdb, '_powershell') as execute:
            OwnedAdb('adb.exe').close()
            self.assertEqual(execute.call_count, 1)
            self.assertIn('ProcessId=2', execute.call_args.args[0])
            self.assertIn("-eq '20'", execute.call_args.args[0])

    def test_unknown_adb_ownership_never_kills(self):
        with patch.object(OwnedAdb, '_processes', side_effect=[None, {(2, '20')}]), patch.object(OwnedAdb, '_powershell') as execute:
            OwnedAdb('adb.exe').close()
            execute.assert_not_called()

    def test_finished_marker_scoped_to_day_instance_and_activity(self):
        with tempfile.TemporaryDirectory() as folder:
            namespace = StorageNamespace('synthetic', 'daily', False, ())
            def state(day='2026-01-02', instance='org.example@0'):
                return DailyState(folder, 'synthetic', 'uiautomator2', day, instance, namespace)
            original = state()
            self.assertFalse(original.activity_finished())
            original.mark_activity_finished()
            self.assertTrue(state().activity_finished())
            self.assertFalse(state(instance='org.example@999').activity_finished())
            self.assertFalse(state(day='2026-01-03').activity_finished())
            original.save_task_decisions({})
            self.assertFalse(state().activity_finished())

class KeepAwakeTests(unittest.TestCase):
    def test_native_timeout_restores_after_failure_without_touch_input(self):
        from backend.core.device.comfort import device_comfort
        from unittest.mock import Mock
        d = Mock(device_type='uiautomator2',io_lock=None)
        d._checked_shell.return_value = '600000'
        with self.assertRaises(RuntimeError):
            with device_comfort(d, {'keep_screen_on':True}):
                raise RuntimeError('synthetic failure')
        args = [c.args[0] for c in d._checked_shell.call_args_list]
        self.assertEqual(args[-1],['settings','put','system','screen_off_timeout','600000'])
        self.assertFalse(any(c[0]=='input' for c in args))


class MediaVolumeTests(unittest.TestCase):
    def device(self, value=7, low=0):
        device = Mock(device_type='uiautomator2', io_lock=None)
        state = {'value': value, 'low': low, 'modern_reads': 0}
        def shell(command, **kwargs):
            self.assertEqual(kwargs, {'timeout': 5})
            if '--get' in command:
                if command[0] == 'cmd':
                    state['modern_reads'] += 1
                return f'[V] volume is {state["value"]} in range [{low}..150]'
            if '--set' in command:
                state['value'] = int(command[-1])
            return ''
        device._checked_shell.side_effect = shell
        return device, state, shell

    def test_modern_command_mutes_media_on_device_and_restores_after_stop(self):
        device, state, _ = self.device(value=71)
        notify = Mock()
        with self.assertRaises(RuntimeError):
            with device_comfort(device, {'minimum_volume': True}, notify=notify):
                self.assertEqual(state['value'], 0)
                raise RuntimeError('synthetic stopped run')
        self.assertEqual(state['value'], 71)
        commands = [c.args[0] for c in device._checked_shell.call_args_list]
        self.assertTrue(all(c[:5] == ['cmd','media_session','volume','--stream','3'] for c in commands))
        self.assertFalse(any(c[0] in ('input', 'settings') for c in commands))
        self.assertFalse(any(c.args[0] == 'WARNING' for c in notify.call_args_list))

    def test_unavailable_modern_command_falls_back_to_legacy(self):
        device, state, native = self.device()
        def shell(command, **kwargs):
            return 'Unknown command: volume' if command[0] == 'cmd' else native(command, **kwargs)
        device._checked_shell.side_effect = shell
        with device_comfort(device, {'minimum_volume': True}):
            self.assertEqual(state['value'], 0)
        self.assertEqual(state['value'], 7)
        self.assertIn(['media','volume','--stream','3','--set','0'],
                      [c.args[0] for c in device._checked_shell.call_args_list])

    def test_silent_write_failure_is_verified_then_falls_back(self):
        device, state, native = self.device()
        def shell(command, **kwargs):
            if command[0] == 'cmd' and '--set' in command:
                return ''  # command succeeds but firmware ignores it
            return native(command, **kwargs)
        device._checked_shell.side_effect = shell
        with device_comfort(device, {'minimum_volume': True}):
            self.assertEqual(state['value'], 0)
        self.assertEqual(state['value'], 7)

    def test_partial_success_does_not_replace_original_volume_with_zero(self):
        device, state, native = self.device()
        def shell(command, **kwargs):
            if command[0] == 'cmd' and '--get' in command and state['modern_reads']:
                raise TimeoutError('synthetic verification transport loss')
            return native(command, **kwargs)
        device._checked_shell.side_effect = shell
        with device_comfort(device, {'minimum_volume': True}):
            self.assertEqual(state['value'], 0)
        self.assertEqual(state['value'], 7)

    def test_cannot_read_original_does_not_change_volume_and_warns(self):
        device, state, _ = self.device()
        device._checked_shell.return_value = 'Permission Denial'
        device._checked_shell.side_effect = None
        notify = Mock()
        with device_comfort(device, {'minimum_volume': True}, notify=notify):
            self.assertEqual(state['value'], 7)
        self.assertFalse(any('--set' in c.args[0] for c in device._checked_shell.call_args_list))
        self.assertTrue(any(c.args[0] == 'WARNING' for c in notify.call_args_list))

    def test_both_ignored_commands_warn_instead_of_reporting_success(self):
        device, state, native = self.device()
        device._checked_shell.side_effect = lambda command, **kw: '' if '--set' in command else native(command, **kw)
        notify = Mock()
        with device_comfort(device, {'minimum_volume': True}, notify=notify):
            self.assertEqual(state['value'], 7)
            self.assertTrue(any(c.args[0] == 'WARNING' for c in notify.call_args_list))
            self.assertFalse(any('已降至' in c.args[1] for c in notify.call_args_list))

    def test_respects_reported_system_minimum(self):
        device, state, _ = self.device(low=1)
        with device_comfort(device, {'minimum_volume': True}):
            self.assertEqual(state['value'], 1)
        self.assertEqual(state['value'], 7)

    def test_restore_can_fall_back_when_primary_channel_disappears(self):
        device, state, native = self.device()
        with device_comfort(device, {'minimum_volume': True}):
            self.assertEqual(state['value'], 0)
            device._checked_shell.side_effect = lambda command, **kw: (
                'not found' if command[0] == 'cmd' else native(command, **kw))
        self.assertEqual(state['value'], 7)

    def test_restore_failure_is_visible_and_does_not_mask_task_exception(self):
        device, state, _ = self.device()
        notify = Mock()
        with self.assertRaisesRegex(RuntimeError, 'original task error'):
            with device_comfort(device, {'minimum_volume': True}, notify=notify):
                self.assertEqual(state['value'], 0)
                device._checked_shell.side_effect = TimeoutError('synthetic disconnect')
                raise RuntimeError('original task error')
        self.assertTrue(any(c.args[0] == 'WARNING' and '媒体音量未能恢复' in c.args[1]
                            for c in notify.call_args_list))

    def test_unsupported_driver_reports_limitation_without_input_keys(self):
        device = Mock(device_type='hmdriver2')
        notify = Mock()
        with device_comfort(device, {'minimum_volume': True}, notify=notify):
            pass
        device._checked_shell.assert_not_called()
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0], 'WARNING')
