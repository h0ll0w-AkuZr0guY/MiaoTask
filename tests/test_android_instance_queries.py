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


def result(text='', code=0):
    return ReadResult(text, code)


class InstanceQueryTests(unittest.TestCase):
    def test_exec_out_scopes_explicit_transport_and_never_uses_pty_or_sdk(self):
        with patch('backend.core.device.read_shell.resolve_adb_binary', return_value='bundled-adb'), \
                patch('backend.core.device.read_shell._run_adb', return_value=SimpleNamespace(
                    stdout='package:org.example.shop\n', stderr='', returncode=0)) as run:
            output = AdbReadShell('synthetic-usb', timeout=6)(['pm', 'list', 'packages', '--user', '27', 'org.example.shop'])
        self.assertEqual(output, result('package:org.example.shop\n'))
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
        shell = Mock(side_effect=[result('UserInfo{0:Owner:1}\nUserInfo{27:Space:1}'),
                                  result(''), result('package:org.example.shop'),
                                  result(''), result('package:org.example.shop.clone')])
        instances, warnings = discover_android_instances(shell, PROFILE)
        self.assertEqual([item.id for item in instances], ['org.example.shop@0', 'org.example.shop.clone@27'])
        self.assertEqual(warnings, [])
        self.assertEqual(shell.call_args_list[-1].args[0],
                         ['cmd', 'package', 'list', 'packages', '--user', '27', 'org.example.shop'])

    def test_empty_or_partial_sdk_error_in_one_space_does_not_discard_verified_primary(self):
        def shell(command):
            if command == ['pm', 'list', 'users']:
                return result('UserInfo{0:Owner:1}\nUserInfo{999:Space:1}')
            if command[command.index('--user') + 1] == '0':
                return result('package:org.example.shop')
            raise RuntimeError('SDK sentinel missing; partial unrelated package output')
        instances, warnings = discover_android_instances(shell, PROFILE)
        self.assertEqual([item.id for item in instances], ['org.example.shop@0'])
        self.assertEqual(len(warnings), 1)
        self.assertIn('999', warnings[0])
        self.assertIn('无法确认', warnings[0])

    def test_empty_success_is_not_silently_reported_as_uninstalled(self):
        shell = Mock(side_effect=lambda command: result('UserInfo{27:Space:1}') if 'users' in command else result())
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
                        return result('UserInfo{0:Owner:1}')
                    return result('') if command[-1] == PROFILE.android_package else result('package:' + package)
                instances, warnings = discover_android_instances(shell, PROFILE)
                self.assertEqual([item.id for item in instances], expected)
                if expected:
                    self.assertEqual(warnings, [])
                else:
                    self.assertIn('已读取', warnings[0])
                    self.assertIn('未发现', warnings[0])

    def test_users_query_fallback_and_invalid_users_never_invent_main_or_clone(self):
        shell = Mock(side_effect=[RuntimeError('SDK truncated users'), result('UserInfo{27:Space:1}'),
                                  result('package:org.example.shop')])
        instances, _ = discover_android_instances(shell, PROFILE)
        self.assertEqual([item.user_id for item in instances], [27])
        self.assertEqual(shell.call_args_list[1].args[0], ['cmd', 'user', 'list'])
        for response in (result(), result('SecurityException'), result('no users')):
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
            channel.return_value.side_effect = [result(), result('package:org.example.external')]
            self.assertEqual(adapter.installed_packages(), {'org.example.external'})
        self.assertEqual([call.args[0] for call in channel.return_value.call_args_list],
                         [['pm', 'list', 'packages', '--user', '999'],
                          ['cmd', 'package', 'list', 'packages', '--user', '999']])
        adapter._device.shell.assert_not_called()

    def test_single_segment_android_system_package_keeps_full_inventory_valid(self):
        adapter = self.adapter()
        with patch('backend.core.device.read_shell.AdbReadShell') as channel:
            channel.return_value.return_value = result('package:android\npackage:org.example.external\n')
            self.assertEqual(adapter.installed_packages(), {'android', 'org.example.external'})
        def shell(command):
            if command == ['pm', 'list', 'users']:
                return result('UserInfo{0:Owner:1}')
            return result() if command[-1] == PROFILE.android_package else result('package:android\n')
        instances, warnings = discover_android_instances(shell, PROFILE)
        self.assertEqual(instances, [])
        self.assertIn('已读取', warnings[0])
        self.assertIn('未发现', warnings[0])

    def test_unreadable_package_inventory_remains_unknown_instead_of_all_uninstalled(self):
        for responses in ([result(), result()], [RuntimeError('partial SDK output'), result()],
                          [result('package:org.example.other\npack'), result('unrelated output')],
                          [result('SecurityException', 1), result('Error: access denied')]):
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
            channel.return_value.return_value = result('mCurrentFocus=Window{abc u999 org.example.shop/.Main}')
            info = adapter.current_app()
        self.assertEqual((info['package'], info['user_id']), ('org.example.shop', 999))
        channel.return_value.assert_called_once_with(['dumpsys', 'window', 'windows'])
        adapter._device.app_current.assert_not_called()

    def test_successful_sdk_foreground_read_keeps_fast_path(self):
        adapter = self.adapter()
        adapter._device.shell.return_value = result('mCurrentFocus=Window{abc u27 org.example.shop/.Main}')
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
            channel.return_value.return_value = result()
            info = adapter.current_app()
        self.assertIsNone(info['user_id'])
        with self.assertRaises(InstanceMismatch):
            adapter.check_instance(info)
        adapter._device.app_current.side_effect = RuntimeError('SDK also failed')
        with patch('backend.core.device.read_shell.AdbReadShell') as channel:
            channel.return_value.return_value = result()
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


if __name__ == '__main__':
    unittest.main()
