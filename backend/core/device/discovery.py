"""Bounded macOS ADB USB discovery and first-use USB authorization.

Only direct USB hardware devices are discovered and connected; wireless TCP/IP,
mDNS endpoints and emulator transports are rejected.
"""
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time


class AndroidConnectionError(ConnectionError):
    """A connection needs a USB cable, phone authorization, or device selection."""


def is_usb_serial(serial):
    """Accept only direct USB hardware serials; reject TCP/IP, mDNS and emulators."""
    if not isinstance(serial, str) or not serial or len(serial) > 256:
        return False
    if re.search(r'\s|[\x00-\x1f]', serial) or serial.startswith('-'):
        return False
    lowered = serial.lower()
    if ':' in serial or '._adb-tls-' in lowered or lowered.startswith('emulator-'):
        return False
    return True


def resolve_adb_binary():
    """Use the same bundled/explicit executable as adbutils, then the PATH one."""
    explicit = os.environ.get('ADBUTILS_ADB_PATH')
    if explicit:
        candidate = Path(explicit).expanduser()
        if not candidate.is_file():
            raise AndroidConnectionError('配置的 ADB 程序不存在，请检查路径或重新解压完整应用包')
        return str(candidate.resolve())
    if getattr(sys, 'frozen', False):
        candidate = Path(sys._MEIPASS) / 'shared' / 'tools' / 'adb'
        if not candidate.is_file():
            raise AndroidConnectionError('应用包缺少内置 ADB，请完整解压后再运行')
        return str(candidate.resolve())
    candidate = shutil.which('adb')
    if not candidate:
        raise AndroidConnectionError('未找到 macOS ADB，请执行 brew install android-platform-tools 或将 platform-tools 加入 PATH')
    return str(Path(candidate).resolve())


def _run_adb(binary, args, env, timeout):
    try:
        return subprocess.run(
            [binary, *args], capture_output=True, text=True, encoding='utf-8',
            errors='replace', timeout=max(.01, timeout), env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise AndroidConnectionError('ADB 连接调用超时，请检查 USB 数据线和手机调试状态') from exc
    except OSError as exc:
        raise AndroidConnectionError('无法运行 ADB，请检查 platform-tools 是否完整可执行') from exc


def _parse_devices(output):
    devices = []
    for raw in output.splitlines():
        line = raw.strip()
        if (not line or line.startswith(('List of devices attached', '*', 'adb:', 'adb server '))):
            continue
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        serial, state = parts[:2]
        if not is_usb_serial(serial):
            continue
        detail = parts[2] if len(parts) > 2 else ''
        if state == 'no' and detail.startswith('permissions'):
            state = 'no permissions'
        model = re.search(r'(?:^|\s)model:([^\s]+)', detail)
        devices.append(dict(serial=serial, state=state, model=model.group(1) if model else '',
                            description=detail))
    return devices


def list_android_devices(adb_path=None, *, timeout=8, env=None):
    """Return USB serial/state/model records; discovery never restarts a server."""
    binary = adb_path or resolve_adb_binary()
    child_env = dict(os.environ if env is None else env)
    result = _run_adb(binary, ['devices', '-l'], child_env, timeout)
    if result.returncode:
        raise AndroidConnectionError('ADB 未能列出 USB 设备，请检查手机连接后重试')
    return _parse_devices(result.stdout)


def android_identity(serial):
    """Read-only stable identity for physical USB device lease deduplication."""
    if not is_usb_serial(serial):
        raise ValueError('仅支持 USB 设备序列号，不支持无线调试或模拟器连接')
    binary = resolve_adb_binary()
    for args in (['getprop', 'ro.serialno'], ['getprop', 'ro.boot.serialno'],
                 ['settings', 'get', 'secure', 'android_id']):
        result = _run_adb(binary, ['-s', serial, 'shell', *args], os.environ.copy(), 8)
        value = result.stdout.strip()
        if (result.returncode == 0 and re.fullmatch(r'[A-Za-z0-9._-]{3,128}', value)
                and value.lower() not in ('unknown', 'null', 'none')):
            return ('android-' if args[0] == 'settings' else '') + value
    raise AndroidConnectionError('无法核定设备身份，为避免重复控制同一设备，请恢复 USB 调试连接后重试')


def prepare_android_connection(serial=None, on_status=None, timeout=45):
    """Return a single authorized USB serial, waiting for the user's phone approval.

    ``on_status(message)`` receives progress text. This function cannot approve
    the RSA prompt: it only establishes USB ADB handshakes while the user approves
    it on the unlocked phone. Neither ADB keys nor system settings change.
    """
    if serial is not None and not is_usb_serial(serial):
        raise ValueError('仅支持通过 macOS ADB USB 连接的设备序列号，不支持无线调试或模拟器地址')
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('ADB 等待时间必须为正数')
    notify = on_status or (lambda message: None)
    binary = resolve_adb_binary()
    env = os.environ.copy()
    deadline = time.monotonic() + timeout

    def remaining():
        return max(.01, min(8, deadline - time.monotonic()))

    def run(args):
        return _run_adb(binary, args, env, remaining())

    def devices():
        return list_android_devices(binary, timeout=remaining(), env=env)

    notify('正在通过 USB 连接 ADB；首次连接请保持手机解锁，留意 USB 调试授权弹窗')
    if run(['start-server']).returncode:
        raise AndroidConnectionError('ADB 服务启动失败，请关闭其他占用 ADB 的程序后重试')
    found = devices()
    selected = serial
    last_state = ''
    while True:
        if selected is None:
            if len(found) > 1:
                raise AndroidConnectionError('检测到多台 USB Android 设备，请先在控制台选择设备后连接')
            if found:
                selected = found[0]['serial']
        target = next((device for device in found if device['serial'] == selected), None)
        if target:
            state = target['state']
            if state == 'device':
                notify('USB 设备已授权，正在建立自动化连接')
                return selected
            if state not in ('unauthorized', 'offline', 'authorizing', 'connecting'):
                raise AndroidConnectionError(f'设备当前处于 {state[:40]} 状态，请恢复正常 Android 系统后重试')
            if state != last_state:
                if state in ('unauthorized', 'authorizing'):
                    notify('请解锁手机，在“允许 USB 调试”弹窗点击允许；正在等待手机授权')
                else:
                    notify('USB 设备暂时离线，正在等待恢复；请检查数据线并保持手机解锁')
                last_state = state
            # Each selected transport handshake lets Android display its own
            # RSA authorization dialog. Never accept it or replace ADB keys.
            run(['-s', selected, 'get-state'])
        elif found:
            raise AndroidConnectionError('指定 USB 设备未出现在 ADB 列表中，请重新选择当前连接的设备')
        elif not last_state:
            notify('等待手机通过 USB 连接，请开启 USB 调试并使用支持数据传输的数据线')
            last_state = 'missing'
        if time.monotonic() >= deadline:
            break
        time.sleep(min(.5, max(0, deadline - time.monotonic())))
        if time.monotonic() >= deadline:
            break
        found = devices()
    if last_state in ('unauthorized', 'authorizing'):
        raise AndroidConnectionError('等待手机 USB 调试授权超时。请解锁手机，在系统弹窗点击允许后重新连接')
    if last_state in ('offline', 'connecting'):
        raise AndroidConnectionError('USB 设备一直处于离线状态，请重新插拔数据线并确认手机 USB 调试授权')
    raise AndroidConnectionError('未检测到可用的 USB Android 设备；请检查数据线、扩展坞和手机 USB 调试开关')
