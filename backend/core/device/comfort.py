"""Optional bounded Android settings; restore values on completion or interruption."""
from contextlib import contextmanager, nullcontext
import logging
import re


class _MediaVolume:
    """Only the media stream; preserve the first readable value across fallbacks."""
    prefixes = (('cmd', 'media_session', 'volume'), ('media', 'volume'))

    def __init__(self, execute, report):
        self.execute, self.report = execute, report
        self.original = None
        self.preferred = None

    def read(self, prefix):
        output = self.execute([*prefix, '--stream', '3', '--get'])
        found = re.search(r'volume\s+is\s+(\d+)\s+in\s+range\s*\[\s*(\d+)\s*\.\.\s*(\d+)\s*\]',
                          output, re.I)
        if not found:
            raise ValueError('无法核定设备媒体音量')
        value, low, high = map(int, found.groups())
        if not 0 <= low <= value <= high <= 10000:
            raise ValueError('设备媒体音量范围异常')
        return value, low, high

    def set_verified(self, prefix, value):
        self.execute([*prefix, '--stream', '3', '--set', str(value)])
        current, _, _ = self.read(prefix)
        if current != value:
            raise ValueError('系统未应用目标媒体音量')

    def lower(self):
        for prefix in self.prefixes:
            try:
                current, low, _ = self.read(prefix)
                if self.original is None:
                    # A preceding write can succeed even if its verification
                    # fails. Never replace the original with that lowered value.
                    self.original = current
                self.set_verified(prefix, low)
                self.preferred = prefix
                self.report('INFO', f'设备媒体音量已降至 {low}，结束后恢复原值')
                return
            except Exception:
                logging.getLogger('backend').debug('媒体音量命令不可用或未通过读回核验：%s', prefix,
                                                   exc_info=True)
        self.report('WARNING', '未能确认设备媒体音量已降低，请在设备上手动调低；任务将继续')

    def restore(self):
        if self.original is None:
            return
        prefixes = ((self.preferred,) if self.preferred else ()) + tuple(
            p for p in self.prefixes if p != self.preferred)
        for prefix in prefixes:
            try:
                self.set_verified(prefix, self.original)
                self.report('INFO', f'设备媒体音量已恢复为 {self.original}')
                return
            except Exception:
                logging.getLogger('backend').debug('恢复媒体音量未通过核验：%s', prefix, exc_info=True)
        self.report('WARNING', '设备媒体音量未能恢复，请在设备上手动调整')


@contextmanager
def device_comfort(device, config, notify=None):
    restore = []
    shell = getattr(device, '_checked_shell', None)

    def report(level, message):
        if notify:
            notify(level, message)
        else:
            logging.getLogger('backend').log(getattr(logging, level), message)

    def execute(command):
        with getattr(device, 'io_lock', None) or nullcontext():
            return shell(command, timeout=5)

    try:
        if getattr(device, 'device_type', '') == 'uiautomator2' and callable(shell):
            def set_value(read, write, value):
                try:
                    current = re.search(r'^\s*(\d+)\s*$', execute(read))
                    if not current:
                        raise ValueError('无法读取原设置，未修改')
                    command = [*write, current[1]]
                    restore.append(lambda command=command: execute(command))
                    execute([*write, str(value)])
                except Exception as exc:
                    report('WARNING', '设备显示设置未生效：' + str(exc))
            if config.get('keep_screen_on', False):
                set_value(['settings','get','system','screen_off_timeout'], ['settings','put','system','screen_off_timeout'], 2147483647)
            if config.get('minimum_volume', False):
                volume = _MediaVolume(execute, report)
                restore.append(volume.restore)
                volume.lower()
            if config.get('minimum_brightness', False):
                set_value(['settings','get','system','screen_brightness_mode'], ['settings','put','system','screen_brightness_mode'], 0)
                set_value(['settings','get','system','screen_brightness'], ['settings','put','system','screen_brightness'], 1)
        elif config.get('minimum_volume', False):
            report('WARNING', '当前设备驱动暂不支持自动降低媒体音量，请在设备上手动调整')
        yield
    finally:
        for action in reversed(restore):
            try:
                action()
            except Exception:
                report('WARNING', '恢复设备显示或音量设置失败，请在设备上手动调整')
