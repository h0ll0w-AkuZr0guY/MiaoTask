"""Optional bounded Android settings; restore values on completion or interruption."""
from contextlib import contextmanager, nullcontext
import logging
import re


@contextmanager
def device_comfort(device, config):
    restore = []
    shell = getattr(device, '_checked_shell', None)
    def execute(command):
        with getattr(device, 'io_lock', None) or nullcontext():
            return shell(command, timeout=5)
    if getattr(device, 'device_type', '') == 'uiautomator2' and callable(shell):
        def set_value(read, write, value, pattern=r'^\s*(\d+)\s*$'):
            try:
                current = re.search(pattern, execute(read))
                if not current:
                    raise ValueError('无法读取原设置，未修改')
                restore.append([*write, current[1]])
                execute([*write, str(value)])
            except Exception as exc:
                logging.getLogger('backend').warning('设备舒适设置未生效：%s', exc)
        if config.get('minimum_volume', False):
            set_value(['media','volume','--stream','3','--get'], ['media','volume','--stream','3','--set'], 0, r'volume is (\d+)')
        if config.get('minimum_brightness', False):
            set_value(['settings','get','system','screen_brightness_mode'], ['settings','put','system','screen_brightness_mode'], 0)
            set_value(['settings','get','system','screen_brightness'], ['settings','put','system','screen_brightness'], 1)
    try:
        yield
    finally:
        for command in reversed(restore):
            try:
                execute(command)
            except Exception:
                logging.getLogger('backend').warning('恢复设备音量或亮度失败，请在设备上手动调整')
