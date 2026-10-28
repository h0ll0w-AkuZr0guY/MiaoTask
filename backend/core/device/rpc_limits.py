"""Bound one Android driver's RPC sockets without changing SDK globals."""
import math


def install_rpc_limits(driver, timeout=12):
    """Cap RPC arguments AND the ADB tunnel socket used by this driver.

    The pinned SDK imports its timeout constant into several modules and its
    custom HTTP connection does not transfer HTTPConnection.timeout to the
    returned ADB socket. Changing uiautomator2.HTTP_TIMEOUT alone is ineffective.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int,float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('RPC timeout must be a positive finite number')
    if getattr(driver, '_miao_rpc_limits_installed', False) is True:
        return
    adb = driver.adb_device
    original_connect, original_rpc = adb.create_connection, driver.jsonrpc_call

    def connect(network, address):
        connection = original_connect(network, address)
        try:
            connection.settimeout(timeout)
        except Exception:
            connection.close()
            raise
        return connection

    def rpc(method, params=None, timeout_arg=None, **kwargs):
        requested = kwargs.pop('timeout', timeout_arg)
        if kwargs:
            raise TypeError('Unsupported RPC arguments')
        requested = timeout if requested is None else min(float(requested), timeout)
        return original_rpc(method, params, timeout=requested)

    adb.create_connection = connect
    driver.jsonrpc_call = rpc
    driver._miao_rpc_limits_installed = True
