import io
import os
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from backend.core.device import discovery as transport
from backend.core.device.adapter import DeviceAdapter
from backend.core.device.rpc_limits import install_rpc_limits


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
    def test_continuous_swipe_checks_identity_and_uses_bounded_system_input(self):
        import threading
        from types import SimpleNamespace
        from unittest.mock import patch
        adapter = DeviceAdapter.__new__(DeviceAdapter)
        adapter.io_lock = threading.RLock()
        adapter.device_type = 'uiautomator2'
        adapter.serial = 'synthetic'
        adapter._display_id = 0
        adapter._instance_guard_active = True
        adapter.current_app = Mock(return_value={'package':'org.example.shop','user_id':0})
        adapter.check_instance = Mock()
        with patch('backend.core.device.discovery.resolve_adb_binary', return_value='adb.exe'), \
                patch('backend.core.device.discovery._run_adb', return_value=SimpleNamespace(returncode=0, stdout='', stderr='')) as run:
            adapter.continuous_swipe(100,700,100,300,.5)
            adapter.check_instance.assert_called_once_with(adapter.current_app.return_value)
            self.assertEqual(run.call_args.args[1], ['-s','synthetic','shell','input','swipe','100','700','100','300','500'])
            self.assertEqual(run.call_args.args[-1], 3)
            run.reset_mock()
            from backend.core.device.instances import InstanceMismatch
            adapter.check_instance.side_effect = InstanceMismatch('synthetic wrong account')
            with self.assertRaises(InstanceMismatch):
                adapter.continuous_swipe(100,700,100,300,.5)
            run.assert_not_called()

    def test_compressed_capture_uses_private_socket_with_read_timeout(self):
        import base64, json
        from types import SimpleNamespace
        from unittest.mock import patch
        adapter = DeviceAdapter.__new__(DeviceAdapter)
        adapter.serial = 'synthetic'
        adapter._device = SimpleNamespace(_device_server_port=9008)
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = 200
        encoded = base64.b64encode(b'image').decode()
        response.read.return_value = json.dumps({'result': encoded+'\n'}).encode()
        with patch('uiautomator2.core.AdbHTTPConnection') as channel, patch('adbutils.AdbClient') as client:
            channel.return_value.__enter__.return_value = connection
            self.assertEqual(adapter._capture_jpeg(45), b'image')
            client.assert_called_once_with(socket_timeout=3)
            connection.sock.settimeout.assert_called_once_with(8)
            payload = json.loads(connection.request.call_args.args[2])
            self.assertEqual((payload['method'],payload['params']),('takeScreenshot',[1,45]))

    def test_background_capture_uses_separate_read_only_command_and_rejects_other_display(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from backend.core.device.instances import InstanceMismatch
        device = DeviceAdapter.__new__(DeviceAdapter)
        device.device_type = 'uiautomator2'
        device.serial = 'synthetic-device'
        device._display_id = 0
        output = io.BytesIO()
        Image.new('RGB', (100, 200), 'white').save(output, format='PNG')
        device._capture_jpeg = Mock(return_value=output.getvalue())
        with patch('backend.core.device.read_shell.AdbReadShell') as read, \
                patch('backend.core.device.displays.focused_display', return_value=0) as display, \
                patch('backend.core.device.discovery.resolve_adb_binary', return_value='adb.exe'), \
                patch('subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=output.getvalue())) as run:
            read.return_value.side_effect = lambda args: SimpleNamespace(exit_code=0, output=(
                'mDisplayId=0\n mPrimaryDisplayDevice=screen(local:987654321)' if args == ['dumpsys', 'display']
                else 'mCurrentFocus=Window{abc u999 org.example.shop/.Main}'))
            foreground, image = device.capture_observation(compressed=True)
            self.assertEqual(foreground['user_id'], 999)
            self.assertEqual(image.size, (100, 200))
            device._capture_jpeg.assert_called_once_with(70)
            run.assert_not_called()
            device.capture_observation()
            self.assertEqual(run.call_args.kwargs['timeout'], 8)
            self.assertEqual(run.call_args.args[0][-5:], ['exec-out','screencap','-p','-d','987654321'])
            display.return_value = 2
            run.reset_mock()
            with self.assertRaises(InstanceMismatch):
                device.capture_observation()
            run.assert_not_called()
        device._display_id = 2
        self.assertIsNone(device.capture_observation())

    def test_png_capture_selects_primary_physical_display_before_decoding(self):
        metadata = ('mDisplayId=7\n mPrimaryDisplayDevice=external(local:123456789)\n'
                    'mDisplayId=0\n mPrimaryDisplayDevice=builtin(local:987654321)\n')
        output = io.BytesIO()
        Image.new('RGB', (100, 200), 'white').save(output, format='PNG')
        png = output.getvalue()

        def capture(command, *args, **kwargs):
            # Android can prefix a warning and choose an arbitrary display when
            # screencap is not given an explicit physical display ID.
            prefix = b'' if command[-2:] == ['-d', '987654321'] else b'[Warning] Multiple displays were found\n'
            return SimpleNamespace(returncode=0, stdout=prefix + png)

        for owned in (False, True):
            with self.subTest(cancellable_transport=owned):
                device = self.focus_adapter()
                device._device = Mock()
                device._capture_focus = Mock(return_value=({'package': 'org.example.shop'}, 'synthetic focus'))
                owner = Mock() if owned else None
                if owner is not None:
                    owner.run.side_effect = capture
                with patch('backend.core.device.read_shell.AdbReadShell') as read, \
                        patch('backend.core.device.discovery.resolve_adb_binary', return_value='adb.exe'), \
                        patch('subprocess.run', side_effect=capture) as run:
                    read.return_value.return_value = SimpleNamespace(exit_code=0, output=metadata)
                    info, image = device.capture_observation(transport=owner)
                    self.assertEqual(image.size, (100, 200))
                    self.assertEqual(info['window_dump'], 'synthetic focus')
                    read.assert_called_once_with(device.serial, prefer_socket=True, timeout=2, budget=2, transport=owner)
                    read.return_value.assert_called_once_with(['dumpsys', 'display'])
                    self.assertEqual(device._device.mock_calls, [])
                    device._capture_jpeg.assert_not_called()
                    device._capture_focus.assert_called_once_with(owner)
                    if owner is not None:
                        self.assertEqual(owner.run.call_args.args[1], 8)
                        run.assert_not_called()
                    else:
                        self.assertEqual(run.call_args.kwargs['timeout'], 8)

    def test_png_capture_rejects_missing_or_failed_primary_mapping_without_capture(self):
        results = [SimpleNamespace(exit_code=0, output=''),
                   SimpleNamespace(exit_code=0, output='mDisplayId=7\n mPrimaryDisplayDevice=external(local:123456789)'),
                   SimpleNamespace(exit_code=1, output='mDisplayId=0\n mPrimaryDisplayDevice=builtin(local:987654321)')]
        for metadata in results:
            with self.subTest(metadata=metadata):
                device = self.focus_adapter()
                device._capture_focus = Mock(return_value=({}, 'synthetic focus'))
                owner = Mock()
                with patch('backend.core.device.read_shell.AdbReadShell') as read, patch('subprocess.run') as run:
                    read.return_value.return_value = metadata
                    with self.assertRaises(RuntimeError):
                        device.capture_observation(transport=owner)
                    owner.run.assert_not_called()
                    run.assert_not_called()

    def test_png_mapping_propagates_stop_and_timeout_without_capture(self):
        from backend.core.errors import Stopped
        for error in (Stopped(), TimeoutError('synthetic mapping timeout')):
            with self.subTest(error=type(error).__name__):
                device = self.focus_adapter()
                device._capture_focus = Mock(return_value=({}, 'synthetic focus'))
                owner = Mock()
                with patch('backend.core.device.read_shell.AdbReadShell') as read, patch('subprocess.run') as run:
                    read.return_value.side_effect = error
                    with self.assertRaises(type(error)):
                        device.capture_observation(transport=owner)
                    owner.run.assert_not_called()
                    run.assert_not_called()

    def focus_adapter(self):
        device = DeviceAdapter.__new__(DeviceAdapter)
        device.serial = 'synthetic-focus-device'
        device.device_type = 'uiautomator2'
        device._display_id = 0
        device._bounded_capture = True
        device._capture_jpeg = Mock()
        return device

    def test_capture_focus_uses_full_windows_when_summary_omits_identity(self):
        from subprocess import TimeoutExpired
        for missing in (SimpleNamespace(exit_code=0, output='mTopFocusedDisplayId=0\nmCurrentFocus=null'),
                        TimeoutExpired(['synthetic-adb', 'dumpsys'], 2)):
            with self.subTest(missing=type(missing).__name__):
                device = self.focus_adapter()
                focused = SimpleNamespace(exit_code=0, output='mCurrentFocus=Window{abc u0 org.example.shop/.Main}')
                with patch('backend.core.device.read_shell.AdbReadShell') as shell:
                    shell.return_value.side_effect = [missing, focused]
                    info, evidence = device._capture_focus()
                self.assertEqual(info['package'], 'org.example.shop')
                self.assertEqual(info['user_id'], 0)
                self.assertEqual(evidence, focused.output)
                self.assertEqual(shell.return_value.call_args_list[-1].args[0], ['dumpsys', 'window', 'windows'])
                device._capture_jpeg.assert_not_called()

    def test_capture_focus_waits_for_launch_transition_without_using_cached_identity(self):
        device = self.focus_adapter()
        device._foreground_package = ('org.example.old', 999)
        missing = SimpleNamespace(exit_code=0, output='mTopFocusedDisplayId=0\nmFocusedApp=null')
        focused = SimpleNamespace(exit_code=0, output='mCurrentFocus=Window{abc u0 org.example.new/.Main}')
        with patch('backend.core.device.read_shell.AdbReadShell') as shell, \
                patch('backend.core.device.adapter.time.sleep') as pause:
            shell.return_value.side_effect = [missing, missing, focused]
            info, _ = device._capture_focus()
        self.assertEqual(info['package'], 'org.example.new')
        pause.assert_called_once_with(.15)
        self.assertEqual(shell.return_value.call_count, 3)

    def test_capture_focus_exhaustion_is_bounded_and_never_captures_unknown_page(self):
        from backend.core.device.instances import InstanceMismatch
        device = self.focus_adapter()
        with patch('backend.core.device.read_shell.AdbReadShell') as shell, \
                patch('backend.core.device.adapter.time.sleep'):
            shell.return_value.side_effect = TimeoutError('synthetic timeout')
            with self.assertRaisesRegex(InstanceMismatch, '读取超时或断流') as raised:
                device.capture_observation(compressed=True)
        self.assertEqual(shell.call_args.kwargs['budget'], 8)
        self.assertLessEqual(shell.return_value.call_count, 6)
        self.assertEqual(raised.exception.capture_diagnostics[0], dict(query='displays', error='TimeoutError'))
        device._capture_jpeg.assert_not_called()

    def test_capture_focus_does_not_retry_actual_instance_mismatch(self):
        from backend.core.device.instances import AppInstance, InstanceMismatch
        device = self.focus_adapter()
        device._instance_guard_active = True
        device.selected_instance = AppInstance('org.example.shop', 0, 'synthetic main')
        device._application = SimpleNamespace(matches=lambda package: package == 'org.example.shop')
        with patch('backend.core.device.read_shell.AdbReadShell') as shell:
            shell.return_value.return_value = SimpleNamespace(exit_code=0,
                output='mCurrentFocus=Window{abc u999 org.example.shop/.Main}')
            with self.assertRaises(InstanceMismatch):
                device.capture_observation(compressed=True)
        shell.return_value.assert_called_once()
        device._capture_jpeg.assert_not_called()

    def test_capture_focus_stop_interrupts_transition_wait(self):
        from backend.core.errors import Stopped
        device = self.focus_adapter()
        device._run_control = Mock()
        device._run_control.wait.side_effect = Stopped()
        with patch('backend.core.device.read_shell.AdbReadShell') as shell:
            shell.return_value.return_value = SimpleNamespace(exit_code=0, output='mCurrentFocus=null')
            with self.assertRaises(Stopped):
                device.capture_observation(compressed=True)
        device._capture_jpeg.assert_not_called()

    def test_input_focus_uses_same_recovery_and_ignores_old_identity(self):
        device = self.focus_adapter()
        device._foreground_package = ('org.example.old', 999)
        focused = {'package': 'org.example.new', 'user_id': 0}
        device._capture_focus = Mock(return_value=(focused, 'fresh window'))
        self.assertEqual(device.current_app(), focused)
        device._capture_focus.assert_called_once_with(None)
        self.assertEqual(device._window_dump, 'fresh window')

    def test_focus_channels_are_independent_across_devices(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        barrier = threading.Barrier(2)
        devices = [self.focus_adapter(), self.focus_adapter()]
        devices[0].serial, devices[1].serial = 'synthetic-one', 'synthetic-two'
        def create(serial, **kwargs):
            def query(args):
                barrier.wait(timeout=3)
                return SimpleNamespace(exit_code=0,
                    output='mCurrentFocus=Window{abc u0 org.example.' + serial.replace('-', '') + '/.Main}')
            return query
        with patch('backend.core.device.read_shell.AdbReadShell', side_effect=create):
            with ThreadPoolExecutor(2) as pool:
                values = list(pool.map(lambda d: d.current_app(), devices))
        self.assertEqual([v['package'] for v in values],
                         ['org.example.syntheticone', 'org.example.synthetictwo'])

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


class DirectAdbReadTests(unittest.TestCase):
    def test_stopping_read_maps_closed_socket_error_to_stopped(self):
        from backend.core.device.read_shell import AdbReadShell, DeviceIO
        from backend.core.errors import Stopped
        io = DeviceIO()
        channel = Mock()
        channel.__enter__ = Mock(return_value=channel)
        channel.__exit__ = Mock(return_value=False)
        replies = iter((b'OKAY', b'OKAY'))
        def receive(size):
            reply = next(replies, None)
            if reply is not None:
                return reply
            io.cancel()
            raise OSError('socket closed by stop')
        channel.recv.side_effect = receive
        with patch('backend.core.device.read_shell.socket.create_connection', return_value=channel):
            with self.assertRaises(Stopped):
                AdbReadShell('synthetic', prefer_socket=True, transport=io)(['dumpsys', 'window', 'displays'])
        channel.close.assert_called_once()
        self.assertFalse(io.callbacks)

    def test_transport_failure_without_stop_keeps_its_error(self):
        from backend.core.device.read_shell import DeviceIO
        io = DeviceIO()
        cancel = Mock()
        with self.assertRaisesRegex(TimeoutError, 'network timeout'):
            with io.own(cancel):
                raise TimeoutError('network timeout')
        cancel.assert_not_called()
        self.assertFalse(io.callbacks)

    def test_socket_exec_handles_fragmented_status_and_payload_without_cli(self):
        from backend.core.device.read_shell import AdbReadShell
        from unittest.mock import Mock,patch
        data=bytearray(b'OKAYOKAYfocused window metadata')
        channel=Mock();channel.__enter__=Mock(return_value=channel);channel.__exit__=Mock()
        def recv(size):
            count=min(size,3,len(data));part=bytes(data[:count]);del data[:count];return part
        channel.recv.side_effect=recv
        with patch('backend.core.device.read_shell.socket.create_connection',return_value=channel),patch('backend.core.device.read_shell._run_adb') as cli:
            result=AdbReadShell('synthetic',prefer_socket=True)(['dumpsys','window','displays'])
            self.assertEqual(result.output,'focused window metadata');cli.assert_not_called()
            wire=b''.join(call.args[0] for call in channel.sendall.call_args_list)
            self.assertIn(b'host:transport:synthetic',wire)
            self.assertIn(b'exec:dumpsys window displays',wire)

    def test_direct_queries_retain_read_only_allowlist(self):
        from backend.core.device.read_shell import AdbReadShell
        from unittest.mock import patch
        with patch('backend.core.device.read_shell.socket.create_connection') as connection:
            with self.assertRaises(ValueError):AdbReadShell('synthetic',prefer_socket=True)(['input','tap','1','2'])
            connection.assert_not_called()

    def test_production_metadata_failure_does_not_enter_sdk_shell_retry(self):
        from backend.core.device.adapter import DeviceAdapter
        from backend.core.device.read_shell import ReadResult
        from unittest.mock import Mock,patch
        phone=DeviceAdapter.__new__(DeviceAdapter);phone.serial='synthetic';phone._bounded_capture=True
        phone._device=Mock()
        with patch('backend.core.device.read_shell.AdbReadShell',return_value=Mock(return_value=ReadResult('',1))):
            result=phone._read_metadata(['dumpsys','window','windows'])
        self.assertEqual(result.exit_code,1);phone._device.shell.assert_not_called()


class WirelessFocusRecoveryTests(unittest.TestCase):
    @patch('backend.core.device.read_shell.resolve_adb_binary', return_value='synthetic-adb')
    def test_reconnect_is_targeted_and_never_restarts_adb_server(self, _adb):
        from backend.core.device.read_shell import reconnect_offline_wireless
        owner = Mock()
        owner.run.side_effect = [SimpleNamespace(returncode=1), SimpleNamespace(returncode=0)]
        self.assertTrue(reconnect_offline_wireless('192.0.2.1:5555', 3, owner))
        self.assertEqual(owner.run.call_args_list[0].args[0][-3:], ['-s', '192.0.2.1:5555', 'get-state'])
        self.assertEqual(owner.run.call_args_list[1].args[0][-2:], ['connect', '192.0.2.1:5555'])

    @patch('backend.core.device.read_shell.resolve_adb_binary', return_value='synthetic-adb')
    def test_live_transport_or_usb_is_not_reconnected(self, _adb):
        from backend.core.device.read_shell import reconnect_offline_wireless
        owner = Mock(); owner.run.return_value.returncode = 0
        self.assertFalse(reconnect_offline_wireless('192.0.2.1:5555', 3, owner))
        self.assertEqual(owner.run.call_count, 1)
        owner.reset_mock()
        self.assertFalse(reconnect_offline_wireless('synthetic-usb', 3, owner))
        owner.run.assert_not_called()


class CaptureStreamRecoveryTests(unittest.TestCase):
    def test_stalled_jpeg_is_retried_once_with_fresh_identity(self):
        device = DeviceAdapter.__new__(DeviceAdapter)
        device.device_type = 'uiautomator2'; device._display_id = 0
        device._capture_focus = Mock(side_effect=[({'package':'org.example.old'}, 'old'),
                                                  ({'package':'org.example.fresh'}, 'fresh')])
        raw = io.BytesIO(); Image.new('RGB', (20, 30)).save(raw, 'JPEG')
        device._capture_jpeg = Mock(side_effect=[TimeoutError(), raw.getvalue()])
        info, image = device.capture_observation(compressed=True)
        self.assertEqual(info['package'], 'org.example.fresh')
        self.assertEqual(image.size, (20, 30))
        self.assertEqual(device._capture_focus.call_count, 2)
        device._capture_focus.side_effect = None
        device._capture_focus.return_value = ({'package':'org.example.fresh'}, 'fresh')
        device._capture_jpeg.reset_mock()
        device._capture_jpeg.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):device.capture_observation(compressed=True)


class NotificationFocusTests(unittest.TestCase):
    def test_notification_waits_without_using_background_instance(self):
        from backend.core.device.instances import parse_foreground
        blocked = 'mCurrentFocus=Window{abc u0 NotificationShade}\nmFocusedApp=ActivityRecord{abc u999 org.example.shop/.Main}'
        self.assertIsNone(parse_foreground(blocked))
        d = SwipeFallbackTests().focus_adapter()
        d._instance_guard_active = True
        d.check_instance = Mock()
        fresh = 'mCurrentFocus=Window{abc u0 org.example.shop/.Main}'
        with patch('backend.core.device.read_shell.AdbReadShell') as shell, patch('backend.core.device.adapter.time.sleep'):
            shell.return_value.side_effect = [SimpleNamespace(exit_code=0, output=blocked),
                                               SimpleNamespace(exit_code=0, output=fresh)]
            info, _ = d._capture_focus()
        self.assertEqual(info['user_id'], 0)
        d.check_instance.assert_called_once_with(info)
        d._capture_jpeg.assert_not_called()

    def test_background_notification_does_not_override_real_focus(self):
        from backend.core.device.instances import focused_notification, parse_foreground
        text = 'mCurrentFocus=Window{abc u0 org.example.shop/.Main}\nWindow{background u0 NotificationShade}'
        self.assertFalse(focused_notification(text))
        self.assertEqual(parse_foreground(text)['package'], 'org.example.shop')

    def test_real_chat_activity_is_not_silently_ignored(self):
        from backend.core.device.instances import focused_notification, parse_foreground
        text = 'mCurrentFocus=Window{abc u0 org.example.messenger/.ChatActivity}'
        self.assertFalse(focused_notification(text))
        self.assertEqual(parse_foreground(text)['package'], 'org.example.messenger')

    def test_persistent_notification_is_bounded_and_never_captured(self):
        d = SwipeFallbackTests().focus_adapter()
        with patch('backend.core.device.read_shell.AdbReadShell') as shell, patch('backend.core.device.adapter.time.sleep'):
            shell.return_value.return_value = SimpleNamespace(exit_code=0,
                output='mCurrentFocus=Window{abc u0 NotificationShade}')
            with self.assertRaisesRegex(Exception, '通知窗口尚未消退'):
                d.capture_observation(compressed=True)
        self.assertLessEqual(shell.return_value.call_count, 32)
        d._capture_jpeg.assert_not_called()


class FocusGapGraceTests(unittest.TestCase):
    def test_fast_empty_focus_does_not_abort_after_only_three_rounds(self):
        d = SwipeFallbackTests().focus_adapter()
        missing = SimpleNamespace(exit_code=0, output='mCurrentFocus=null\nmFocusedApp=null')
        ready = SimpleNamespace(exit_code=0, output='mCurrentFocus=Window{abc u0 org.example.shop/.Main}')
        with patch('backend.core.device.read_shell.AdbReadShell') as shell, patch('backend.core.device.adapter.time.sleep'):
            shell.return_value.side_effect = [missing] * 8 + [ready]
            info, _ = d._capture_focus()
        self.assertEqual(info['package'], 'org.example.shop')
        self.assertEqual(shell.return_value.call_count, 9)
