"""Bounded, non-PTY Android metadata queries on one explicit transport."""
from dataclasses import dataclass
import math
import os
import re
import time

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
    def __init__(self, serial, *, timeout=8, budget=24):
        if (not isinstance(serial, str) or not serial or len(serial) > 256
                or serial.startswith('-') or re.search(r'\s|[\x00-\x1f]', serial)):
            raise ValueError('实例查询需要明确的设备连接标识，请重新连接设备')
        self.serial = serial
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not math.isfinite(value) or value <= 0 for value in (timeout, budget)):
            raise ValueError('实例查询超时必须为有限的正数')
        self.timeout = timeout
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
        result = _run_adb(resolve_adb_binary(), ['-s', self.serial, 'exec-out', *args],
                          os.environ.copy(), min(self.timeout, remaining, 8))
        output = result.stdout
        if result.stderr:
            output += '\n' + result.stderr
        return ReadResult(output, result.returncode)
