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

# android_instance_queries
"""Synthetic HyperOS shell failures must not invent or silently switch accounts."""
import subprocess
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from backend.core.device.adapter import DeviceAdapter
from backend.core.device.instances import AppInstance, InstanceMismatch, discover_android_instances
from backend.core.device.profile import ApplicationProfile
from backend.core.device.read_shell import AdbReadShell, ReadResult


PROFILE = ApplicationProfile('Example', 'org.example.shop', r'org\.example\.shop(?:\.clone)?')


def query_result(text='', code=0):
    return ReadResult(text, code)


class InstanceQueryTests(unittest.TestCase):
    def test_exec_out_scopes_explicit_transport_and_never_uses_pty_or_sdk(self):
        with patch('backend.core.device.read_shell.resolve_adb_binary', return_value='bundled-adb'), \
                patch('backend.core.device.read_shell._run_adb', return_value=SimpleNamespace(
                    stdout='package:org.example.shop\n', stderr='', returncode=0)) as run:
            output = AdbReadShell('synthetic-usb', timeout=6)(['pm', 'list', 'packages', '--user', '27', 'org.example.shop'])
        self.assertEqual(output, query_result('package:org.example.shop\n'))
        self.assertEqual(run.call_args.args[1], ['-s', 'synthetic-usb', 'exec-out', 'pm', 'list', 'packages', '--user', '27', 'org.example.shop'])
        self.assertEqual(run.call_args.args[-1], 6)

    def test_explicit_transport_required_and_read_channel_rejects_mutation(self):
        for serial in (None, '', '-d', 'bad serial'):
            with self.assertRaises(ValueError):
                AdbReadShell(serial)
        for command in (['input', 'text', 'abc'], ['am', 'start', 'example'], ['pm', 'clear', 'org.example.shop']):
            with self.assertRaises(ValueError):
                AdbReadShell('synthetic')(command)
        with self.assertRaises(ValueError):
            AdbReadShell('synthetic')(['pm', 'list', 'packages', '--user', '0', 'org.example.shop;id'])

    def test_total_budget_bounds_fallbacks_and_expired_budget_does_not_spawn(self):
        with patch('backend.core.device.read_shell.time.monotonic', side_effect=[0, 20, 25]), \
                patch('backend.core.device.read_shell.resolve_adb_binary', return_value='bundled-adb'), \
                patch('backend.core.device.read_shell._run_adb', return_value=SimpleNamespace(
                    stdout='', stderr='', returncode=0)) as run:
            shell = AdbReadShell('synthetic')
            shell(['pm', 'list', 'users'])
            self.assertEqual(run.call_args.args[-1], 4)
            with self.assertRaises(TimeoutError):
                shell(['cmd', 'user', 'list'])
            self.assertEqual(run.call_count, 1)

    def test_empty_filtered_result_uses_cmd_package_and_preserves_arbitrary_user(self):
        shell = Mock(side_effect=[query_result('UserInfo{0:Owner:1}\nUserInfo{27:Space:1}'),
                                  query_result(''), query_result('package:org.example.shop'),
                                  query_result(''), query_result('package:org.example.shop.clone')])
        instances, warnings = discover_android_instances(shell, PROFILE)
        self.assertEqual([item.id for item in instances], ['org.example.shop@0', 'org.example.shop.clone@27'])
        self.assertEqual(warnings, [])
        self.assertEqual(shell.call_args_list[-1].args[0],
                         ['cmd', 'package', 'list', 'packages', '--user', '27', 'org.example.shop'])

    def test_empty_or_partial_sdk_error_in_one_space_does_not_discard_verified_primary(self):
        def shell(command):
            if command == ['pm', 'list', 'users']:
                return query_result('UserInfo{0:Owner:1}\nUserInfo{999:Space:1}')
            if command[command.index('--user') + 1] == '0':
                return query_result('package:org.example.shop')
            raise RuntimeError('SDK sentinel missing; partial unrelated package output')
        instances, warnings = discover_android_instances(shell, PROFILE)
        self.assertEqual([item.id for item in instances], ['org.example.shop@0'])
        self.assertEqual(len(warnings), 1)
        self.assertIn('999', warnings[0])
        self.assertIn('无法确认', warnings[0])

    def test_empty_success_is_not_silently_reported_as_uninstalled(self):
        shell = Mock(side_effect=lambda command: query_result('UserInfo{27:Space:1}') if 'users' in command else query_result())
        instances, warnings = discover_android_instances(shell, PROFILE)
        self.assertEqual(instances, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn('无法确认', warnings[0])
        self.assertNotIn('未发现', warnings[0])
        self.assertEqual(shell.call_count, 5)

    def test_unfiltered_valid_package_list_can_confirm_absence_or_recover_filtered_fault(self):
        for package, expected in (('org.example.other', []), ('org.example.shop', ['org.example.shop@0'])):
            with self.subTest(package=package):
                def shell(command):
                    if command == ['pm', 'list', 'users']:
                        return query_result('UserInfo{0:Owner:1}')
                    return query_result('') if command[-1] == PROFILE.android_package else query_result('package:' + package)
                instances, warnings = discover_android_instances(shell, PROFILE)
                self.assertEqual([item.id for item in instances], expected)
                if expected:
                    self.assertEqual(warnings, [])
                else:
                    self.assertIn('已读取', warnings[0])
                    self.assertIn('未发现', warnings[0])

    def test_users_query_fallback_and_invalid_users_never_invent_main_or_clone(self):
        shell = Mock(side_effect=[RuntimeError('SDK truncated users'), query_result('UserInfo{27:Space:1}'),
                                  query_result('package:org.example.shop')])
        instances, _ = discover_android_instances(shell, PROFILE)
        self.assertEqual([item.user_id for item in instances], [27])
        self.assertEqual(shell.call_args_list[1].args[0], ['cmd', 'user', 'list'])
        for response in (query_result(), query_result('SecurityException'), query_result('no users')):
            with self.assertRaises(RuntimeError):
                discover_android_instances(Mock(return_value=response), PROFILE)

    def test_exec_out_timeout_is_bounded_and_does_not_parse_partial_output(self):
        with patch('backend.core.device.read_shell.resolve_adb_binary', return_value='bundled-adb'), \
                patch('backend.core.device.read_shell._run_adb', side_effect=subprocess.TimeoutExpired('adb', 8, output=b'package:org.example.shop')):
            with self.assertRaises(subprocess.TimeoutExpired):
                AdbReadShell('synthetic')(['pm', 'list', 'users'])

    def test_adapter_uses_read_channel_and_invalid_selected_clone_never_falls_back(self):
        adapter = DeviceAdapter.__new__(DeviceAdapter)
        adapter.device_type = 'uiautomator2'
        adapter.io_lock = threading.RLock()
        adapter.serial = 'synthetic-usb'
        adapter._application = PROFILE
        adapter._device = Mock()
        primary = AppInstance('org.example.shop', 0, 'main')
        adapter.selected_instance = AppInstance('org.example.shop', 999, 'clone')
        with patch('backend.core.device.read_shell.AdbReadShell') as channel, \
                patch('backend.core.device.adapter.discover_android_instances', return_value=([primary], ['space unreadable'])) as discover:
            rows = adapter.refresh_app_instances()
        channel.assert_called_once_with('synthetic-usb')
        discover.assert_called_once_with(channel.return_value, PROFILE)
        adapter._device.shell.assert_not_called()
        self.assertEqual(rows[0]['user_id'], 0)
        self.assertIsNone(adapter.selected_instance)
        self.assertEqual(adapter.instance_warnings[0], 'space unreadable')
        self.assertIn('未自动切换', adapter.instance_warnings[-1])

    def adapter(self):
        adapter = DeviceAdapter.__new__(DeviceAdapter)
        adapter.device_type = 'uiautomator2'
        adapter.io_lock = threading.RLock()
        adapter.serial = 'synthetic-usb'
        adapter._application = PROFILE
        adapter._device = Mock()
        adapter.selected_instance = AppInstance('org.example.shop', 999, 'clone')
        return adapter

    def test_installed_packages_empty_pm_uses_cmd_in_same_selected_user(self):
        adapter = self.adapter()
        with patch('backend.core.device.read_shell.AdbReadShell') as channel:
            channel.return_value.side_effect = [query_result(), query_result('package:org.example.external')]
            self.assertEqual(adapter.installed_packages(), {'org.example.external'})
        self.assertEqual([call.args[0] for call in channel.return_value.call_args_list],
                         [['pm', 'list', 'packages', '--user', '999'],
                          ['cmd', 'package', 'list', 'packages', '--user', '999']])
        adapter._device.shell.assert_not_called()

    def test_single_segment_android_system_package_keeps_full_inventory_valid(self):
        adapter = self.adapter()
        with patch('backend.core.device.read_shell.AdbReadShell') as channel:
            channel.return_value.return_value = query_result('package:android\npackage:org.example.external\n')
            self.assertEqual(adapter.installed_packages(), {'android', 'org.example.external'})
        def shell(command):
            if command == ['pm', 'list', 'users']:
                return query_result('UserInfo{0:Owner:1}')
            return query_result() if command[-1] == PROFILE.android_package else query_result('package:android\n')
        instances, warnings = discover_android_instances(shell, PROFILE)
        self.assertEqual(instances, [])
        self.assertIn('已读取', warnings[0])
        self.assertIn('未发现', warnings[0])

    def test_unreadable_package_inventory_remains_unknown_instead_of_all_uninstalled(self):
        for responses in ([query_result(), query_result()], [RuntimeError('partial SDK output'), query_result()],
                          [query_result('package:org.example.other\npack'), query_result('unrelated output')],
                          [query_result('SecurityException', 1), query_result('Error: access denied')]):
            with self.subTest(responses=responses):
                adapter = self.adapter()
                with patch('backend.core.device.read_shell.AdbReadShell') as channel:
                    channel.return_value.side_effect = responses
                    self.assertIsNone(adapter.installed_packages())
        for identity in (None, AppInstance('org.example.shop', None, 'unknown user')):
            adapter = self.adapter()
            adapter.selected_instance = identity
            with patch('backend.core.device.read_shell.AdbReadShell') as channel:
                self.assertIsNone(adapter.installed_packages())
                channel.assert_not_called()

    def test_current_app_sdk_sentinel_failure_uses_exec_out_identity_without_guessing(self):
        adapter = self.adapter()
        adapter._device.shell.side_effect = RuntimeError('SDK sentinel missing; unrelated partial output')
        with patch('backend.core.device.read_shell.AdbReadShell') as channel:
            channel.return_value.return_value = query_result('mCurrentFocus=Window{abc u999 org.example.shop/.Main}')
            info = adapter.current_app()
        self.assertEqual((info['package'], info['user_id']), ('org.example.shop', 999))
        channel.return_value.assert_called_once_with(['dumpsys', 'window', 'windows'])
        adapter._device.app_current.assert_not_called()

    def test_successful_sdk_foreground_read_keeps_fast_path(self):
        adapter = self.adapter()
        adapter._device.shell.return_value = query_result('mCurrentFocus=Window{abc u27 org.example.shop/.Main}')
        with patch('backend.core.device.read_shell.AdbReadShell') as channel:
            self.assertEqual(adapter.current_app()['user_id'], 27)
        channel.assert_not_called()
        adapter._device.shell.assert_called_once_with(['dumpsys', 'window', 'windows'])

    def test_unreadable_focus_never_recovers_previous_user_from_cache(self):
        adapter = self.adapter()
        adapter._foreground_package = ('org.example.shop', 999)
        adapter._device.shell.side_effect = RuntimeError('SDK read failed')
        adapter._device.app_current.return_value = {'package': 'org.example.shop', 'activity': '.Main'}
        with patch('backend.core.device.read_shell.AdbReadShell') as channel:
            channel.return_value.return_value = query_result()
            info = adapter.current_app()
        self.assertIsNone(info['user_id'])
        with self.assertRaises(InstanceMismatch):
            adapter.check_instance(info)
        adapter._device.app_current.side_effect = RuntimeError('SDK also failed')
        with patch('backend.core.device.read_shell.AdbReadShell') as channel:
            channel.return_value.return_value = query_result()
            with self.assertRaises(InstanceMismatch):
                adapter.current_app()

    def test_read_channel_allows_only_the_required_display_metadata_commands(self):
        commands = [['dumpsys', 'window', 'windows'], ['dumpsys', 'window', 'displays'],
                    ['dumpsys', 'activity', 'activities'], ['dumpsys', 'display']]
        with patch('backend.core.device.read_shell.resolve_adb_binary', return_value='bundled-adb'), \
                patch('backend.core.device.read_shell._run_adb', return_value=SimpleNamespace(
                    stdout='metadata', stderr='', returncode=0)) as run:
            shell = AdbReadShell('synthetic')
            for command in commands:
                shell(command)
            self.assertEqual(run.call_count, len(commands))
            with self.assertRaises(ValueError):
                shell(['dumpsys', 'activity', 'all'])

# instance_availability
"""An unverified application instance must never look executable or use old data."""
from contextlib import ExitStack
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from backend.core.device.instances import AppInstance
from backend.core.service.panel import Controller


class InstanceAvailabilityTests(unittest.TestCase):
    def test_refresh_empty_retains_phone_but_shows_recovery_reason_and_no_daily_store(self):
        controller = Controller()
        device = controller.device = Mock(device_type='uiautomator2', app_instances=[], selected_instance=None,
                                           instance_warnings=['用户空间 999 无法核验'])
        device.refresh_app_instances.return_value = []
        controller.update(connected=True, app_instances=[], selected_instance='')
        with ExitStack() as stack:
            stack.enter_context(patch('backend.core.service.panel.DailyState'))
            stack.enter_context(patch.object(controller, '_save_report'))
            controller.dispatch('refresh_instances', {})
            controller.thread.join(3)
        state = controller.snapshot()
        self.assertTrue(state['connected'])
        self.assertIs(controller.device, device)
        self.assertEqual(state['status'], 'needs_attention')
        self.assertEqual(state['phase'], 'instance_missing')
        self.assertIn('刷新实例', state['message'])
        self.assertIn('999', state['instance_warnings'][0])
        self.assertIsNone(state['daily'])
        with self.assertRaisesRegex(ValueError, '未发现可用应用实例'):
            controller.dispatch('run', {})

    def test_verified_instance_refresh_recovers_to_ready(self):
        controller = Controller()
        target = AppInstance('com.taobao.taobao', 0, 'Synthetic primary')
        device = controller.device = Mock(app_instances=[target], selected_instance=target,
                                           instance_warnings=[])
        device.refresh_app_instances.return_value = [target.to_dict()]
        controller.update(connected=True, status='needs_attention', app_instances=[], selected_instance='')
        with patch.object(controller, '_ensure_daily'), patch.object(controller, '_persist_state'), patch.object(controller, '_save_report'):
            controller.dispatch('refresh_instances', {})
            controller.thread.join(3)
        state = controller.snapshot()
        self.assertEqual(state['status'], 'ready')
        self.assertEqual(state['selected_instance'], target.id)
        self.assertEqual(state['instance_warnings'], [])

    def test_missing_selection_does_not_claim_ready_even_if_other_instances_exist(self):
        controller = Controller()
        controller.device = Mock(instance_warnings=[])
        controller.update(connected=True, app_instances=[dict(id='synthetic@0')], selected_instance='')
        controller._instance_availability('Ready')
        self.assertEqual(controller.state['status'], 'needs_attention')
        self.assertIn('未自动切换', controller.state['message'])
        self.assertEqual(controller.state['selected_instance'], '')

    def test_empty_enumeration_cannot_bind_or_overwrite_a_primary_daily_record(self):
        controller = Controller()
        controller.device = Mock(app_instances=[], selected_instance=None)
        controller.daily = Mock()
        with patch('backend.core.service.panel.DailyState') as store:
            self.assertFalse(controller._ensure_daily())
        store.assert_not_called()
        self.assertIsNone(controller.daily)
        self.assertEqual(controller.state['task_scope'], '')

class ExternalBrowserPromptTests(unittest.TestCase):
    def test_external_page_warning_with_browser_open_is_a_launch_scene(self):
        from backend.app.common.utils.destinations import DestinationPolicy
        from backend.core.device.screen import Screen
        from backend.core.models import TextNode
        policy = DestinationPolicy(); policy.config = {}
        text = '下个页面不受我们控制哦，使用时请注意安全https://example.invalid'
        screen = Screen([TextNode(text,(100,400,700,470)),TextNode('浏览器打开',(120,500,350,550)),
                         TextNode('取消',(400,500,650,550))],800,1200,'example.source')
        self.assertEqual(policy.launch_confirmation(screen).text,'浏览器打开')
        screen.nodes.append(TextNode('确认支付',(100,360,200,390)))
        self.assertIsNone(policy.launch_confirmation(screen))
