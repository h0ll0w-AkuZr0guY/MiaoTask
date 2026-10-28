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


if __name__=='__main__':unittest.main()
