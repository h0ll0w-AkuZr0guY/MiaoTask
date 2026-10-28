from contextlib import nullcontext
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from backend.core.device.instances import AppInstance, InstanceMismatch, discover_android_instances, parse_foreground
from backend.app.taobao.activities.double11.activity import CampaignRunner, NeedsAttention, Stopped, load_config
from backend.core.service.panel import Controller
from backend.core.storage.daily_state import DailyState
from backend.core.device.adapter import DeviceAdapter
from backend.core.device.screen import Screen


PRIMARY = AppInstance('com.taobao.taobao', 0, '主应用')
CLONE = AppInstance('com.taobao.taobao', 999, '分身')


def result(output='', code=0):
    return Mock(output=output, exit_code=code)


class DiscoveryTests(unittest.TestCase):
    def test_same_package_in_arbitrary_users_and_renamed_package_are_separate(self):
        responses = [result('Users:\n UserInfo{0:private-name:13}\n UserInfo{27:clone:1}\n UserInfo{999:XSpace:1}'),
                     result('package:com.taobao.taobao\npackage:com.taobao.idlefish'),
                     result('package:com.taobao.taobao.clone'), result('package:com.taobao.taobao')]
        shell = Mock(side_effect=responses)
        instances, warnings = discover_android_instances(shell)
        self.assertEqual([i.id for i in instances], ['com.taobao.taobao@0', 'com.taobao.taobao.clone@27', 'com.taobao.taobao@999'])
        self.assertFalse(warnings)
        self.assertNotIn('private-name', str([i.to_dict() for i in instances]))
        self.assertEqual(shell.call_args_list[2].args[0][-3:], ['--user', '27', 'com.taobao.taobao'])

    def test_restricted_user_is_reported_and_never_offered_as_runnable(self):
        shell = Mock(side_effect=[result('UserInfo{0:main:1}\nUserInfo{10:work:1}'),
                                  result('package:com.taobao.taobao'), result('SecurityException', 1)])
        instances, warnings = discover_android_instances(shell)
        self.assertEqual([i.user_id for i in instances], [0])
        self.assertIn('10', warnings[0])

    def test_missing_users_does_not_invent_primary_or_clone(self):
        for response in [result('', 1), result('unknown')]:
            with self.assertRaises(RuntimeError):
                discover_android_instances(Mock(return_value=response))

    def test_foreground_uses_focused_record_not_background_instance(self):
        text = ('mFocusedApp=ActivityRecord{abc u0 com.taobao.taobao/.Old t1}\n'
                'mCurrentFocus=Window{def u999 com.taobao.taobao/com.taobao.tao.Welcome}')
        self.assertEqual(parse_foreground(text)['user_id'], 999)
        self.assertEqual(parse_foreground('topResumedActivity=ActivityRecord{a u27 com.taobao.taobao/.Main t2}')['user_id'], 27)
        self.assertIsNone(parse_foreground('ActivityRecord{a u0 com.taobao.taobao/.Old t1}'))


class ScopedDeviceTests(unittest.TestCase):
    def phone(self, target=CLONE):
        d = DeviceAdapter.__new__(DeviceAdapter)
        d.device_type = 'uiautomator2'
        d.io_lock = threading.RLock()
        d.selected_instance = target
        d.app_instances = [PRIMARY, CLONE]
        d._instance_guard_active = False
        d._device = Mock()
        return d

    def test_restart_and_resolve_explicitly_scope_clone_without_global_stop(self):
        d = self.phone()
        def shell(args):
            if args[:2] == ['dumpsys', 'window']:
                return result('mCurrentFocus=Window{a u999 com.taobao.taobao/.Welcome}')
            if 'resolve-activity' in args:
                return result('com.taobao.taobao/com.taobao.tao.welcome.Welcome')
            return result('Starting: Intent')
        d._device.shell.side_effect = shell
        d.restart_app(CLONE.package)
        calls = [call.args[0] for call in d._device.shell.call_args_list]
        self.assertIn(['am', 'force-stop', '--user', '999', CLONE.package], calls)
        start = next(args for args in calls if args[:2] == ['am', 'start'])
        self.assertEqual(start[2:4], ['--user', '999'])
        resolve = next(args for args in calls if 'resolve-activity' in args)
        self.assertEqual(resolve[4:6], ['--user', '999'])
        d._device.app_start.assert_not_called()
        d._device.app_stop.assert_not_called()

    def test_profile_shell_error_is_not_retried_as_primary(self):
        d = self.phone()
        d._device.shell.return_value = result('SecurityException', 1)
        with self.assertRaises(RuntimeError):
            d.restart_app(CLONE.package)
        d._device.app_start.assert_not_called()

    def test_launch_waits_for_identity_after_transient_foreground_read_error(self):
        d = self.phone()
        d._device.shell.side_effect = [result('com.taobao.taobao/.Welcome'), result('Starting')]
        d.current_app = Mock(side_effect=[OSError('transition'), dict(package=CLONE.package, user_id=999)])
        with patch('backend.core.device.adapter.time.sleep'):
            d.resume_app(CLONE.package)
        self.assertEqual(d.current_app.call_count, 2)
        d._device.app_start.assert_not_called()

    def test_cold_start_reactivates_same_instance_once_after_old_task_falls_back(self):
        d = self.phone()
        starts = []
        def shell(args):
            if 'resolve-activity' in args:
                return result('com.taobao.taobao/.Welcome')
            if args[:2] == ['am', 'start']:
                starts.append(args)
            return result('Starting')
        d._device.shell.side_effect = shell
        d.current_app = Mock(side_effect=lambda: dict(package=CLONE.package, user_id=999) if len(starts) == 2 else dict(package='com.example.previous', user_id=0))
        clock = [0.0]
        with patch('backend.core.device.adapter.time.monotonic', side_effect=lambda: clock[0]), patch('backend.core.device.adapter.time.sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            d.restart_app(CLONE.package)
        self.assertEqual(len(starts), 2)
        self.assertTrue(all(args[2:4] == ['--user', '999'] for args in starts))
        stops = [c.args[0] for c in d._device.shell.call_args_list if c.args[0][:2] == ['am', 'force-stop']]
        self.assertEqual(stops, [['am', 'force-stop', '--user', '999', CLONE.package]])
        self.assertEqual(d.selected_instance, CLONE)

    def test_reactivation_never_accepts_unknown_or_other_user_identity(self):
        for user in (0, None):
            d = self.phone()
            d._device.shell.return_value = result('com.taobao.taobao/.Welcome')
            d.current_app = Mock(return_value=dict(package=CLONE.package, user_id=user))
            clock = [0.0]
            with patch('backend.core.device.adapter.time.monotonic', side_effect=lambda: clock[0]), patch('backend.core.device.adapter.time.sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
                with self.assertRaises(InstanceMismatch):
                    d.resume_app(CLONE.package)
            starts = [c.args[0] for c in d._device.shell.call_args_list if c.args[0][:2] == ['am', 'start']]
            self.assertEqual(len(starts), 2)
            self.assertTrue(all(args[2:4] == ['--user', '999'] for args in starts))
            d._device.click.assert_not_called()
            d._device.press.assert_not_called()
            self.assertEqual(d.selected_instance, CLONE)

    def test_other_or_unknown_same_package_user_gets_no_click_swipe_back_or_text(self):
        for user in (0, None):
            for method, args in [('click', (100, 200)), ('swipe', (100, 400, 100, 200)),
                                 ('go_back', ()), ('input_text', ('abc',)), ('input_ascii', ('phone',)), ('press', ('enter',))]:
                with self.subTest(user=user, method=method):
                    d = self.phone()
                    d.current_app = Mock(return_value=dict(package=PRIMARY.package, user_id=user))
                    with d.instance_session(), self.assertRaises(InstanceMismatch):
                        getattr(d, method)(*args)
                    d._device.click.assert_not_called()
                    d._device.swipe.assert_not_called()
                    d._device.press.assert_not_called()
                    self.assertFalse(d._instance_guard_active)

    def test_active_session_cannot_change_target_but_can_interact_with_external_app(self):
        d = self.phone()
        d.current_app = Mock(return_value=dict(package='com.example.external', user_id=0))
        with d.instance_session():
            with self.assertRaises(ValueError):
                d.select_app_instance(PRIMARY.id)
            with self.assertRaises(ValueError):
                d.refresh_app_instances()
            d.go_back()
        d._device.press.assert_called_once_with('back')
        self.assertEqual(d.selected_instance, CLONE)

    def test_package_availability_is_checked_in_selected_user(self):
        d = self.phone()
        d.serial = 'synthetic-usb'
        with patch('backend.core.device.read_shell.AdbReadShell') as channel:
            channel.return_value.return_value = result('package:com.sina.weibo')
            self.assertEqual(d.installed_packages(), {'com.sina.weibo'})
            channel.return_value.assert_called_once_with(['pm', 'list', 'packages', '--user', '999'])
        d._device.shell.assert_not_called()

    def test_same_package_user_switch_invalidates_preview_frame(self):
        d = self.phone()
        d._device.shell.side_effect = [result('mCurrentFocus=Window{a u0 com.taobao.taobao/.Main}'),
                                       result('mCurrentFocus=Window{b u999 com.taobao.taobao/.Main}')]
        d.current_app()
        d._screenshot_cache = (0, 'old-primary-frame')
        self.assertEqual(d.current_app()['user_id'], 999)
        self.assertIsNone(d._screenshot_cache)

    def test_runner_never_accepts_other_user_task_list_or_recovers_from_mismatch(self):
        d = self.phone()
        runner = CampaignRunner(d)
        wrong = Screen([], 1080, 2400, PRIMARY.package, user_id=0)
        with self.assertRaises(InstanceMismatch):
            runner._accept_list(wrong)
        runner.ensure_list = Mock(side_effect=InstanceMismatch('wrong profile'))
        runner.recover = Mock()
        with self.assertRaises(InstanceMismatch):
            runner.run(auto_open=True)
        runner.recover.assert_not_called()


class InstanceDailyAndQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_primary_preserves_v8_folder_and_clone_has_own_daily_records(self):
        old = DailyState(self.root, 'test-handset', 'uiautomator2', '2026-10-20')
        old.progress('old', dict(completed=2))
        main = DailyState(self.root, 'test-handset', 'uiautomator2', '2026-10-20', PRIMARY.id)
        clone = DailyState(self.root, 'test-handset', 'uiautomator2', '2026-10-20', CLONE.id)
        self.assertEqual(main.folder, old.folder)
        self.assertEqual(main.snapshot()['stats']['completed'], 2)
        clone.progress('clone', dict(completed=1), unavailable_tasks={'app:example': 'not installed'})
        self.assertNotEqual(clone.folder, main.folder)
        self.assertFalse(main.unavailable_tasks())
        self.assertEqual(clone.snapshot()['stats']['completed'], 1)
        tomorrow = DailyState(self.root, 'test-handset', 'uiautomator2', '2026-10-21', CLONE.id)
        self.assertFalse(tomorrow.unavailable_tasks())

    def controller(self):
        c = Controller()
        d = Mock(serial='test-handset', identity_serial='test-handset', device_type='uiautomator2')
        d.selected_instance, d.app_instances = PRIMARY, [PRIMARY, CLONE]
        d.io_lock = threading.RLock()
        d.screenshot.return_value = Image.new('RGB', (40, 80))
        def select(identity):
            d.selected_instance = next(i for i in d.app_instances if i.id == identity)
            return d.selected_instance
        d.select_app_instance.side_effect = select
        d.instance_session.side_effect = lambda: nullcontext()
        c.device = d
        c.update(app_instances=[i.to_dict() for i in d.app_instances], selected_instance=PRIMARY.id, connected=True)
        return c

    def test_queue_runs_in_order_after_previous_runner_returns_and_scopes_statistics(self):
        c, trace = self.controller(), []
        def create(device, config, control, emit, image, **kwargs):
            target = device.selected_instance.id
            trace.append(('created', target))
            runner = Mock()
            def run(auto_open):
                self.assertFalse(control.stopped.is_set())
                trace.append(('run', target))
                emit(stats=dict(completed=1, attempts=1))
            runner.run.side_effect = run
            return runner
        with patch('backend.core.service.panel.data_dir', return_value=self.root), patch('backend.core.service.panel.CampaignRunner', side_effect=create):
            c._work('run', dict(instance_ids=[CLONE.id, PRIMARY.id]), load_config())
        self.assertEqual(trace, [('created', CLONE.id), ('run', CLONE.id), ('created', PRIMARY.id), ('run', PRIMARY.id)])
        state = c.snapshot()
        self.assertEqual(state['status'], 'finished')
        self.assertEqual([q['status'] for q in state['instance_queue']], ['finished', 'finished'])
        self.assertEqual([q['stats']['completed'] for q in state['instance_queue']], [1, 1])
        self.assertEqual(state['daily']['stats']['completed'], 1)

    def test_attention_mismatch_and_stop_never_start_next_instance(self):
        for error in (NeedsAttention('verify', kind='security'), InstanceMismatch('wrong'), Stopped()):
            with self.subTest(error=type(error).__name__):
                c = self.controller()
                runner = Mock()
                runner.run.side_effect = error
                with patch('backend.core.service.panel.data_dir', return_value=self.root), \
                        patch('backend.core.service.panel.CampaignRunner', return_value=runner) as factory, \
                        patch('backend.core.service.panel.notify_security_attention'):
                    c._work('run', dict(instance_ids=[PRIMARY.id, CLONE.id]), load_config())
                self.assertEqual(factory.call_count, 1)
                self.assertEqual(c.snapshot()['instance_queue'][1]['status'], 'pending')
                self.assertEqual(c.device.selected_instance.id, PRIMARY.id)

    def test_invalid_duplicate_or_current_page_batch_is_rejected_before_worker(self):
        for payload in [dict(instance_ids=[PRIMARY.id, PRIMARY.id]), dict(instance_ids=['other']),
                        dict(instance_ids=[PRIMARY.id, CLONE.id], auto_open=False)]:
            c = self.controller()
            with self.assertRaises(ValueError):
                c.dispatch('run', payload)
            self.assertIsNone(c.thread)
