import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.core.device import discovery as transport


class AdbTransportTests(unittest.TestCase):
    def run_connection(self, listings, serial=None, timeout=2, windows=True):
        clock = [0.]
        calls, notices = [], []
        output = list(listings)
        last = ['']

        def invoke(binary, args, env, command_timeout):
            calls.append((binary, args[:], env.copy(), command_timeout))
            if args == ['devices', '-l']:
                if output:
                    last[0] = output.pop(0)
                return subprocess.CompletedProcess(args, 0, last[0], '')
            if args[-1] == 'get-state':
                return subprocess.CompletedProcess(args, 1, '', 'device unauthorized')
            return subprocess.CompletedProcess(args, 0, '', '')

        patches = (
            patch.object(transport, 'resolve_adb_binary', return_value='chosen-adb'),
            patch.object(transport, '_is_windows', return_value=windows),
            patch.object(transport, '_run_adb', side_effect=invoke),
            patch.object(transport.time, 'monotonic', side_effect=lambda: clock[0]),
            patch.object(transport.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            try:
                result = transport.prepare_android_connection(serial, notices.append, timeout)
            except transport.AndroidConnectionError as exc:
                result = exc
        return result, calls, notices

    def test_first_authorization_waits_then_returns_authorized_serial(self):
        result, calls, notices = self.run_connection([
            'List of devices attached\nphone-a unauthorized usb:1-1\n',
            'List of devices attached\nphone-a device model:Phone_A\n',
        ])
        self.assertEqual(result, 'phone-a')
        self.assertTrue(any('允许 USB 调试' in message for message in notices))
        self.assertTrue(any(args == ['-s', 'phone-a', 'get-state'] for _, args, _, _ in calls))
        self.assertFalse(any(args == ['kill-server'] for _, args, _, _ in calls))

    def test_refused_or_missing_authorization_times_out_without_resetting_keys(self):
        result, calls, _ = self.run_connection(['phone-a unauthorized usb:1-1\n'])
        self.assertIsInstance(result, transport.AndroidConnectionError)
        self.assertIn('授权超时', str(result))
        self.assertFalse(any(args == ['kill-server'] for _, args, _, _ in calls))
        self.assertTrue(all(args in (['start-server'], ['devices', '-l'], ['-s', 'phone-a', 'get-state'])
                            for _, args, _, _ in calls))

    def test_online_device_never_restarts_an_existing_server(self):
        result, calls, _ = self.run_connection(['phone-a device model:Phone_A\n'])
        self.assertEqual(result, 'phone-a')
        self.assertFalse(any(args == ['kill-server'] for _, args, _, _ in calls))

    def test_offline_device_never_triggers_backend_restart(self):
        result, calls, _ = self.run_connection(['phone-a offline usb:1-1\n'])
        self.assertIsInstance(result, transport.AndroidConnectionError)
        self.assertIn('离线', str(result))
        self.assertFalse(any(args == ['kill-server'] for _, args, _, _ in calls))

    def test_empty_windows_list_falls_back_once_with_child_env_only(self):
        original = os.environ.get('ADB_LIBUSB')
        result, calls, notices = self.run_connection(['', '', 'phone-a device model:Phone_A\n'])
        self.assertEqual(result, 'phone-a')
        self.assertEqual(sum(args == ['kill-server'] for _, args, _, _ in calls), 1)
        restarted = [(args, env) for _, args, env, _ in calls if args == ['kill-server']]
        self.assertEqual(restarted[0][1]['ADB_LIBUSB'], '1')
        self.assertEqual(os.environ.get('ADB_LIBUSB'), original)
        self.assertEqual({binary for binary, _, _, _ in calls}, {'chosen-adb'})
        self.assertTrue(any('兼容' in notice for notice in notices))

    def test_device_appearing_before_fallback_keeps_its_server(self):
        result, calls, _ = self.run_connection(['', 'phone-a unauthorized usb:1-1\n', 'phone-a device\n'])
        self.assertEqual(result, 'phone-a')
        self.assertFalse(any(args == ['kill-server'] for _, args, _, _ in calls))

    def test_wireless_address_never_restarts_server(self):
        address = '192.0.2.10:5555'
        result, calls, _ = self.run_connection(['', address + ' device model:Phone_A\n'], serial=address)
        self.assertEqual(result, address)
        self.assertTrue(any(args == ['connect', address] for _, args, _, _ in calls))
        self.assertFalse(any(args == ['kill-server'] for _, args, _, _ in calls))

    def test_multiple_or_unknown_devices_require_selection(self):
        result, calls, _ = self.run_connection(['phone-a device\nphone-b unauthorized\n'])
        self.assertIsInstance(result, transport.AndroidConnectionError)
        self.assertIn('多台', str(result))
        self.assertFalse(any(args == ['kill-server'] for _, args, _, _ in calls))
        result, calls, _ = self.run_connection(['phone-a recovery\n'])
        self.assertIn('recovery', str(result))
        self.assertFalse(any(args == ['kill-server'] for _, args, _, _ in calls))

    def test_mismatched_selected_device_does_not_choose_another(self):
        result, calls, _ = self.run_connection(['phone-a device\n'], serial='phone-b')
        self.assertIn('指定设备', str(result))
        self.assertFalse(any(args == ['kill-server'] for _, args, _, _ in calls))

    def test_empty_non_windows_list_does_not_restart(self):
        result, calls, _ = self.run_connection([''], windows=False)
        self.assertIsInstance(result, transport.AndroidConnectionError)
        self.assertFalse(any(args == ['kill-server'] for _, args, _, _ in calls))

    def test_subprocess_is_hidden_bounded_and_uses_argument_list(self):
        with patch.object(transport, '_is_windows', return_value=True), patch.object(
                transport.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            transport._run_adb('adb.exe', ['-s', 'phone-a', 'get-state'], {'TASK_ENV': '1'}, .5)
        self.assertEqual(run.call_args.args[0], ['adb.exe', '-s', 'phone-a', 'get-state'])
        self.assertEqual(run.call_args.kwargs['timeout'], .5)
        self.assertEqual(run.call_args.kwargs['env'], {'TASK_ENV': '1'})
        self.assertEqual(run.call_args.kwargs['creationflags'], getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertNotIn('shell', run.call_args.kwargs)

    def test_subprocess_timeout_has_actionable_error(self):
        with patch.object(transport.subprocess, 'run', side_effect=subprocess.TimeoutExpired('adb', 1)):
            with self.assertRaisesRegex(transport.AndroidConnectionError, '超时'):
                transport._run_adb('adb', ['devices'], {}, 1)

    def test_discovery_exposes_states_and_model_without_mutating_server(self):
        output = '* daemon started successfully\nList of devices attached\nphone-a unauthorized usb:1-1\nphone-b device model:Phone_B transport_id:2\n'
        with patch.object(transport, '_run_adb', return_value=subprocess.CompletedProcess([], 0, output, '')) as run:
            devices = transport.list_android_devices('adb')
        self.assertEqual([device['state'] for device in devices], ['unauthorized', 'device'])
        self.assertEqual(devices[1]['model'], 'Phone_B')
        self.assertEqual(run.call_args.args[1], ['devices', '-l'])

    def test_explicit_adb_path_has_priority_over_path_for_all_clients(self):
        with tempfile.TemporaryDirectory() as folder:
            chosen = Path(folder) / 'adb.exe'
            chosen.write_bytes(b'test-executable')
            with patch.dict(os.environ, {'ADBUTILS_ADB_PATH': str(chosen)}), patch.object(
                    transport.shutil, 'which', return_value='different-adb') as which:
                self.assertEqual(transport.resolve_adb_binary(), str(chosen.resolve()))
                which.assert_not_called()

    def test_missing_explicit_executable_does_not_silently_use_another_adb(self):
        with patch.dict(os.environ, {'ADBUTILS_ADB_PATH': '/missing/task-adb.exe'}), patch.object(
                transport.shutil, 'which', return_value='different-adb') as which:
            with self.assertRaisesRegex(transport.AndroidConnectionError, '不存在'):
                transport.resolve_adb_binary()
            which.assert_not_called()

    def test_frozen_helper_uses_the_same_bundled_executable_as_main(self):
        with tempfile.TemporaryDirectory() as folder:
            tools = Path(folder) / 'shared' / 'tools'
            tools.mkdir(parents=True)
            executable = tools / 'adb.exe'
            executable.write_bytes(b'test-executable')
            with patch.dict(os.environ, {'ADBUTILS_ADB_PATH': ''}), patch.object(
                    transport.sys, 'frozen', True, create=True), patch.object(
                    transport.sys, '_MEIPASS', folder, create=True), patch.object(
                    transport.shutil, 'which', return_value='different-adb') as which:
                self.assertEqual(transport.resolve_adb_binary(), str(executable.resolve()))
                which.assert_not_called()


class PanelDiscoveryTests(unittest.TestCase):
    def test_panel_uses_shared_android_helper_and_keeps_unauthorized_selectable(self):
        from backend.core.service.panel import discover_devices
        devices = [dict(serial='phone-a', state='unauthorized', model='Phone_A', description='usb:1-1'),
                   dict(serial='phone-b', state='recovery', model='', description='usb:1-2')]
        with patch.object(transport, 'list_android_devices', return_value=devices) as listing, patch(
                'backend.core.service.panel.shutil.which', return_value=None), patch(
                'backend.core.service.panel.subprocess.run') as run:
            discovered = discover_devices()
        listing.assert_called_once_with()
        run.assert_not_called()
        self.assertEqual(discovered['devices'], [
            dict(serial='phone-a', type='uiautomator2', status='unauthorized', model='Phone_A'),
            dict(serial='phone-b', type='uiautomator2', status='recovery', model='')])

    def test_android_discovery_failure_preserves_harmony_detection(self):
        from backend.core.service.panel import discover_devices
        with patch.object(transport, 'list_android_devices', side_effect=transport.AndroidConnectionError('未找到 ADB')), patch(
                'backend.core.service.panel.shutil.which', return_value='chosen-hdc'), patch(
                'backend.core.service.panel.subprocess.run', return_value=subprocess.CompletedProcess([], 0, 'harmony-a\n', '')) as run:
            discovered = discover_devices()
        self.assertEqual(discovered['devices'], [
            dict(serial='harmony-a', type='hmdriver2', status='device', model='HarmonyOS')])
        self.assertIn('adb: 未找到 ADB', discovered['errors'])
        self.assertEqual(run.call_args.args[0], ['chosen-hdc', 'list', 'targets'])

# rpc_limits
"""Socket deadlines and text input stay per driver, display and instance."""
from types import SimpleNamespace
from unittest.mock import Mock
import socket
import time
import unittest

from backend.core.device.adapter import DeviceAdapter
from backend.core.device.rpc_limits import install_rpc_limits


class RpcLimitTests(unittest.TestCase):
    def test_rpc_and_socket_limits_do_not_mutate_another_driver(self):
        first_socket, second_socket = Mock(), Mock()
        first=SimpleNamespace(adb_device=SimpleNamespace(create_connection=Mock(return_value=first_socket)),jsonrpc_call=Mock(return_value='ok'))
        second=SimpleNamespace(adb_device=SimpleNamespace(create_connection=Mock(return_value=second_socket)),jsonrpc_call=Mock())
        original_first=first.jsonrpc_call;original_second=second.jsonrpc_call
        install_rpc_limits(first,7)
        self.assertEqual(first.jsonrpc_call('count',{},300),'ok')
        original_first.assert_called_once_with('count',{},timeout=7)
        first.adb_device.create_connection('tcp',9008)
        first_socket.settimeout.assert_called_once_with(7)
        self.assertIs(second.jsonrpc_call,original_second)
        second_socket.settimeout.assert_not_called()
        wrapped=first.jsonrpc_call
        install_rpc_limits(first,7)
        self.assertIs(first.jsonrpc_call,wrapped)

    def test_stalled_socket_times_out_even_when_sdk_ignores_http_timeout(self):
        left,right=socket.socketpair()
        try:
            adb=SimpleNamespace(create_connection=lambda *_:left)
            def stalled(method,params=None,timeout=300):
                return adb.create_connection('tcp',9008).recv(1)
            driver=SimpleNamespace(adb_device=adb,jsonrpc_call=stalled)
            install_rpc_limits(driver,.03)
            started=time.monotonic()
            with self.assertRaises(TimeoutError):driver.jsonrpc_call('dumpWindowHierarchy',timeout=300)
            self.assertLess(time.monotonic()-started,1)
        finally:
            left.close();right.close()

    def test_invalid_limits_fail_before_transport_changes(self):
        for value in (0,-1,True,float('nan'),float('inf'),'12'):
            with self.assertRaises(ValueError):install_rpc_limits(SimpleNamespace(),value)

    def test_ascii_is_bounded_validated_and_routed_to_the_current_display(self):
        adapter=DeviceAdapter.__new__(DeviceAdapter)
        adapter.device_type='uiautomator2'
        adapter._device=Mock()
        adapter._device.shell.return_value=SimpleNamespace(exit_code=0,output='')
        adapter._display_id=7
        adapter.input_ascii('phone')
        adapter._device.shell.assert_called_once_with(['input','-d','7','text','phone'],timeout=8)
        adapter._device.reset_mock()
        for text in ('手机','a b','a;b','x'*33,''):
            with self.assertRaises(ValueError):adapter.input_ascii(text)
        adapter._device.shell.assert_not_called()



class SwipeFallbackTests(unittest.TestCase):
    def test_clipboard_clear_never_reads_contents_or_changes_ime(self):
        adapter = DeviceAdapter.__new__(DeviceAdapter)
        adapter.device_type = 'uiautomator2'
        adapter._device = Mock()
        adapter.clear_clipboard()
        self.assertEqual(adapter._device.method_calls, [unittest.mock.call.set_clipboard('')])
        adapter.device_type = 'hmdriver2'
        adapter._device.reset_mock()
        with self.assertRaises(NotImplementedError):
            adapter.clear_clipboard()
        adapter._device.set_clipboard.assert_not_called()

    def test_timeout_scroll_uses_bounded_system_input(self):
        from backend.core.device.adapter import DeviceAdapter
        from unittest.mock import Mock
        import threading
        device = DeviceAdapter.__new__(DeviceAdapter)
        device.device_type = 'uiautomator2'
        device._display_id = 0
        device.io_lock = threading.RLock()
        device._device = Mock()
        device._device.swipe.side_effect = TimeoutError('synthetic busy')
        device._checked_shell = Mock()
        device.swipe(100,600,100,300,.4)
        device._checked_shell.assert_called_once_with(['input','swipe','100','600','100','300','400'],timeout=8)
