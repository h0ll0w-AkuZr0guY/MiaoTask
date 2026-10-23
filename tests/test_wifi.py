"""Wireless transport regressions; all addresses and codes are synthetic."""
import json
import subprocess
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from backend.core.device import wifi
from backend.core.device.discovery import AndroidConnectionError
from backend.core.service.panel import Controller, make_handler


class WifiTests(unittest.TestCase):
    def test_explicit_endpoints_validate_port_ip_and_argument_injection(self):
        self.assertEqual(wifi.endpoint(' 192.0.2.10:12345 '), '192.0.2.10:12345')
        self.assertEqual(wifi.endpoint('[2001:db8::1]:34567'), '[2001:db8::1]:34567')
        for value in (None, True, '-s phone', 'http://192.0.2.10:5555', '192.0.2.10',
                      '192.0.2.10:0', '192.0.2.10:65536', '999.0.0.1:55',
                      '0.0.0.0:5555', '224.0.0.1:5555', '192.0.2.10:55\n--help'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                wifi.endpoint(value)

    def test_pair_code_requires_six_ascii_digits(self):
        self.assertEqual(wifi.pairing_code('012345'), '012345')
        for value in (123456, None, '12345', '1234567', '１２３４５６', '123456\n'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                wifi.pairing_code(value)

    def test_pair_code_only_uses_stdin_and_success_is_confirmed(self):
        notices = []
        with patch.object(wifi, 'resolve_adb_binary', return_value='chosen-adb'), patch.object(
                wifi.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'Successfully paired to target', '')) as run:
            wifi.pair_wifi('192.0.2.10:34567', '012345', notices.append)
        self.assertEqual(run.call_args.args[0], ['chosen-adb', 'pair', '192.0.2.10:34567'])
        self.assertEqual(run.call_args.kwargs['input'], '012345\n')
        self.assertLessEqual(run.call_args.kwargs['timeout'], 20)
        self.assertNotIn('shell', run.call_args.kwargs)
        self.assertNotIn('012345', str(notices))

    def test_failed_pairing_does_not_echo_raw_code_or_device_errors(self):
        for result in (subprocess.CompletedProcess([], 1, '012345 rejected', 'secret phone details'),
                       subprocess.CompletedProcess([], 0, '', '')):
            with patch.object(wifi, 'resolve_adb_binary', return_value='adb'), patch.object(
                    wifi.subprocess, 'run', return_value=result):
                with self.assertRaises(AndroidConnectionError) as raised:
                    wifi.pair_wifi('192.0.2.10:34567', '012345')
            self.assertNotIn('012345', str(raised.exception))
            self.assertNotIn('secret', str(raised.exception))

    def test_pair_timeout_is_bounded_and_explains_network_limit(self):
        with patch.object(wifi, 'resolve_adb_binary', return_value='adb'), patch.object(
                wifi.subprocess, 'run', side_effect=subprocess.TimeoutExpired('adb', 20, output='012345')):
            with self.assertRaisesRegex(AndroidConnectionError, '校园网') as raised:
                wifi.pair_wifi('192.0.2.10:34567', '012345')
        self.assertNotIn('012345', str(raised.exception))

    def test_discovery_only_reads_advertised_services_and_existing_transports(self):
        output = ('List of discovered mdns services\n'
                  'synthetic-a _adb-tls-pairing._tcp. 192.0.2.10:34567\n'
                  'synthetic-b _adb-tls-connect._tcp 192.0.2.10:45678\n'
                  'printer _ipp._tcp 192.0.2.11:631\n'
                  'bad _adb-tls-connect._tcp --help\n')
        with patch.object(wifi, 'resolve_adb_binary', return_value='adb'), patch.object(
                wifi, '_run_adb', return_value=subprocess.CompletedProcess([], 0, output, '')) as run, patch.object(
                wifi, 'list_android_devices', return_value=[dict(serial='usb-phone'), dict(serial='192.0.2.10:45678')]):
            found = wifi.discover_wifi()
        self.assertEqual(run.call_args.args[1], ['mdns', 'services'])
        self.assertEqual([x['kind'] for x in found['services']], ['pairing', 'connect'])
        self.assertEqual(found['devices'], [dict(serial='192.0.2.10:45678')])

    def test_connect_checks_adb_success_message_not_only_returncode(self):
        with patch.object(wifi, 'resolve_adb_binary', return_value='adb'), patch.object(
                wifi, '_run_adb', return_value=subprocess.CompletedProcess([], 0, 'failed to connect', '')):
            with self.assertRaisesRegex(AndroidConnectionError, '连接失败'):
                wifi.connect_wifi('192.0.2.10:45678')
        with patch.object(wifi, 'resolve_adb_binary', return_value='adb'), patch.object(
                wifi, '_run_adb', return_value=subprocess.CompletedProcess([], 0, 'already connected to 192.0.2.10:45678', '')) as run:
            self.assertEqual(wifi.connect_wifi('192.0.2.10:45678'), '192.0.2.10:45678')
            self.assertEqual(run.call_args.args[1], ['connect', '192.0.2.10:45678'])

    def test_known_usb_does_not_prevent_connecting_explicit_wifi_target(self):
        from test_adb_transport import AdbTransportTests
        address = '192.0.2.10:45678'
        result, calls, _ = AdbTransportTests().run_connection([
            'usb-phone device\n', 'usb-phone device\n' + address + ' device\n'], serial=address)
        self.assertEqual(result, address)
        self.assertIn(['connect', address], [args for _, args, _, _ in calls])
        self.assertNotIn(['kill-server'], [args for _, args, _, _ in calls])


class WifiPanelTests(unittest.TestCase):
    def test_wireless_connection_reuses_original_adapter_and_instance_discovery(self):
        from PIL import Image
        controller = Controller()
        device = Mock(serial='192.0.2.10:45678', app_instances=[], selected_instance=None, instance_warnings=[], display_size=(20,40))
        device.current_app.return_value = {'package':'synthetic.app'}
        device.screenshot.return_value = Image.new('RGB', (20, 40))
        with patch.object(wifi, 'connect_wifi', return_value=device.serial) as connect, patch(
                'backend.core.device.adapter.DeviceAdapter', return_value=device) as adapter, patch.object(
                controller, '_ensure_daily'), patch.object(controller, '_start_preview'), patch.object(
                controller, '_persist_state'), patch.object(controller, '_save_report'):
            controller.dispatch('wifi_connect', {'connect_address':device.serial, 'device_type':'hmdriver2'})
            controller.thread.join(3)
        connect.assert_called_once()
        self.assertEqual(adapter.call_args.args, ('uiautomator2', device.serial))
        self.assertIs(adapter.call_args.kwargs['application'], controller.registration.app.profile)
        self.assertTrue(controller.snapshot()['connected'])
        self.assertEqual(controller.snapshot()['status'], 'needs_attention')
        self.assertEqual(controller.snapshot()['phase'], 'instance_missing')

    def test_pair_without_connect_address_does_not_open_or_select_a_phone(self):
        controller = Controller()
        with patch.object(wifi, 'pair_wifi') as pair, patch(
                'backend.core.device.adapter.DeviceAdapter') as adapter:
            controller.dispatch('wifi_pair', dict(pair_address='192.0.2.10:34567', pair_code='012345'))
            controller.thread.join(3)
            self.assertFalse(controller.thread.is_alive())
        pair.assert_called_once()
        adapter.assert_not_called()
        state = controller.snapshot()
        self.assertFalse(state['connected'])
        self.assertTrue(state['wifi_paired'])
        self.assertNotIn('012345', json.dumps(state))
        self.assertIn('配对成功', state['message'])

    def test_bad_address_is_rejected_before_clearing_existing_connection(self):
        controller = Controller()
        existing = controller.device = Mock()
        with self.assertRaises(ValueError):
            controller.dispatch('wifi_connect', {'connect_address':'--help'})
        self.assertIs(controller.device, existing)
        self.assertIsNone(controller.thread)

    def test_busy_worker_prevents_pairing_or_switching_transport(self):
        controller = Controller()
        controller.thread = Mock(is_alive=lambda: True)
        with self.assertRaisesRegex(ValueError, '先停止'):
            controller.dispatch('wifi_pair', dict(pair_address='192.0.2.10:34567', pair_code='012345'))

    def test_pair_failure_erases_payload_code_and_keeps_public_state_clean(self):
        controller = Controller()
        payload = dict(pair_address='192.0.2.10:34567', pair_code='012345')
        with patch.object(wifi, 'pair_wifi', side_effect=AndroidConnectionError('配对失败')):
            controller._work('wifi_pair', payload, controller.state['config'])
        self.assertNotIn('pair_code', payload)
        self.assertNotIn('012345', json.dumps(controller.snapshot()))
        self.assertEqual(controller.state['phase'], 'connect_failed')

    def test_wifi_routes_keep_host_origin_and_token_protection(self):
        controller = Controller()
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(controller, 'test-token'))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with patch.object(wifi, 'discover_wifi', return_value=dict(services=[], devices=[], errors=[])):
                with urlopen(base + '/api/wifi/devices') as response:
                    self.assertEqual(json.load(response)['services'], [])
            for headers in ({}, {'X-Miao-Token':'test-token', 'Origin':'https://example.org'}):
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(base + '/api/wifi/pair', data=b'{}', headers=headers))
                self.assertEqual(error.exception.code, 403)
            with patch.object(controller, 'dispatch') as dispatch:
                with urlopen(Request(base + '/api/wifi/connect', data=b'{"connect_address":"192.0.2.10:45678"}',
                                    headers={'X-Miao-Token':'test-token','Content-Type':'application/json'})) as response:
                    self.assertEqual(response.status, 202)
                self.assertEqual(dispatch.call_args.args[0], 'wifi_connect')
            with urlopen(base + '/js/components/wifi-connect.js') as response:
                self.assertIn(b'pair_address', response.read())
        finally:
            server.shutdown(); server.server_close(); thread.join(3)


if __name__ == '__main__':
    unittest.main()
