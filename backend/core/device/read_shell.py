"""Bounded Android metadata reads and cancellation of per-run device I/O."""
from dataclasses import dataclass
import math
import os
import re
import time
import socket
import shlex
from contextlib import nullcontext, contextmanager
import threading
import subprocess
from backend.core.errors import Stopped

from backend.core.device.discovery import _run_adb, resolve_adb_binary



@dataclass(frozen=True)
class ReadResult:
    output: str
    exit_code: int


def parse_package_list(output):
    """A blank, diagnostic or truncated record is unknown, never an empty set."""
    if not isinstance(output, str):
        return None
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return None
    result = set()
    for line in lines:
        match = re.fullmatch(r'package:([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*)', line)
        if match is None:
            return None
        result.add(match[1])
    return result


class AdbReadShell:
    """Avoid SDK shell sentinel/PTY handling for read-only instance discovery."""
    def __init__(self, serial, *, timeout=8, budget=24, transport=None, prefer_socket=False):
        if (not isinstance(serial, str) or not serial or len(serial) > 256
                or serial.startswith('-') or re.search(r'\s|[\x00-\x1f]', serial)):
            raise ValueError('实例查询需要明确的设备连接标识，请重新连接设备')
        self.serial = serial
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not math.isfinite(value) or value <= 0 for value in (timeout, budget)):
            raise ValueError('实例查询超时必须为有限的正数')
        self.timeout = timeout
        self.transport = transport
        self.prefer_socket = prefer_socket
        self.deadline = time.monotonic() + min(budget, 24)

    def __call__(self, args):
        valid = args in (['pm', 'list', 'users'], ['cmd', 'user', 'list'],
                         ['dumpsys', 'window', 'windows'], ['dumpsys', 'window', 'displays'],
                         ['dumpsys', 'activity', 'activities'], ['dumpsys', 'display'])
        if isinstance(args, list) and all(isinstance(value, str) for value in args):
            for prefix in (['pm', 'list', 'packages'], ['cmd', 'package', 'list', 'packages']):
                if args[:len(prefix)] == prefix:
                    tail = args[len(prefix):]
                    valid = (len(tail) in (2, 3) and tail[0] == '--user' and tail[1].isdigit()
                             and (len(tail) == 2 or re.fullmatch(r'[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*', tail[2]) is not None))
        if not valid:
            raise ValueError('此通道只允许读取用户空间和应用列表')
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('实例查询已达到总时间上限，请重连后刷新')
        timeout = min(self.timeout, remaining, 8)
        if self.prefer_socket:
            return self._read_socket(args, timeout)
        command = ['-s', self.serial, 'exec-out', *args]
        result = (self.transport.run([resolve_adb_binary(), *command], timeout, text=True)
                  if self.transport else _run_adb(resolve_adb_binary(), command, os.environ.copy(), timeout))
        output = result.stdout
        if result.stderr:
            output += '\n' + result.stderr
        return ReadResult(output, result.returncode)

    def _read_socket(self, args, timeout):
        """ADB exec protocol, no shell sentinel or Windows child-process pipes."""
        deadline = time.monotonic()+timeout
        address = (os.environ.get('ANDROID_ADB_SERVER_HOST', '127.0.0.1'),
                   int(os.environ.get('ANDROID_ADB_SERVER_PORT', '5037')))
        with socket.create_connection(address, timeout=timeout) as channel:
            def cancel():
                try:
                    channel.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                channel.close()
            with self.transport.own(cancel) if self.transport else nullcontext():
                def receive(size):
                    remaining = deadline-time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('ADB只读查询超时')
                    channel.settimeout(remaining)
                    return channel.recv(size)
                def exact(size):
                    output = bytearray()
                    while len(output) < size:
                        part = receive(size-len(output))
                        if not part:
                            raise ConnectionError('ADB只读通道提前断开')
                        output.extend(part)
                    return bytes(output)
                def command(text):
                    encoded = text.encode('utf-8')
                    channel.sendall(f'{len(encoded):04x}'.encode()+encoded)
                    status = exact(4)
                    if status != b'OKAY':
                        if status == b'FAIL':
                            length = int(exact(4),16)
                            raise ConnectionError(exact(length).decode('utf-8','replace'))
                        raise ConnectionError('ADB只读通道协议错误')
                command('host:transport:'+self.serial)
                command('exec:'+shlex.join(args))
                output = bytearray()
                while True:
                    part = receive(65536)
                    if not part:
                        break
                    output.extend(part)
                    if len(output) > 8*1024*1024:
                        raise ValueError('设备只读查询返回内容过大')
                return ReadResult(output.decode('utf-8','replace'),0)


class DeviceIO:
    """Cancel only this run's sockets/processes; never kill the shared ADB server."""
    def __init__(self):
        self.closed = threading.Event()
        self.lock = threading.Lock()
        self.callbacks = set()

    def check(self):
        if self.closed.is_set():
            raise Stopped()

    @contextmanager
    def own(self, cancel):
        with self.lock:
            self.check()
            self.callbacks.add(cancel)
        try:
            yield
        except Exception:
            # Closing a live socket can raise OSError before returning.
            # A deliberate stop must not become a task failure/restart.
            self.check()
            raise
        else:
            self.check()
        finally:
            with self.lock:
                self.callbacks.discard(cancel)

    def cancel(self):
        with self.lock:
            self.closed.set()
            callbacks = tuple(self.callbacks)
        for cancel in callbacks:
            try:
                cancel()
            except Exception:
                pass

    def run(self, args, timeout=8, *, text=False):
        self.check()
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        def kill():
            if process.poll() is None:
                process.kill()
        try:
            with self.own(kill):
                output, error = process.communicate(timeout=timeout)
            if text:
                output, error = output.decode('utf-8', 'replace'), error.decode('utf-8', 'replace')
            return subprocess.CompletedProcess(args, process.returncode, output, error)
        finally:
            kill()
            process.communicate()
