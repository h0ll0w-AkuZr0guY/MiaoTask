"""Clean up only newly started bundled ADB processes belonging to this installation."""
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys


class OwnedAdb:
    def __init__(self, binary):
        self.binary = str(Path(binary).resolve())
        self.before = self._processes()

    def _processes(self):
        if sys.platform != 'darwin':
            return None
        try:
            result = subprocess.run(
                ['ps', '-axo', 'pid=,lstart=,args='],
                capture_output=True, text=True, timeout=5, check=True,
            )
            owned = set()
            for raw in result.stdout.splitlines():
                parts = raw.strip().split(None, 6)
                if len(parts) < 7 or not parts[0].isdigit():
                    continue
                pid = int(parts[0])
                stamp = ' '.join(parts[1:6])
                cmd = parts[6]
                if cmd == self.binary or cmd.startswith(self.binary + ' '):
                    owned.add((pid, stamp))
            return owned
        except Exception:
            return None  # Unknown ownership must never lead to killing another tool.

    def close(self):
        current = self._processes()
        if self.before is None or current is None:
            return
        for pid, stamp in current - self.before:
            try:
                verify = subprocess.run(
                    ['ps', '-p', str(pid), '-o', 'lstart=,args='],
                    capture_output=True, text=True, timeout=3,
                )
                parts = verify.stdout.strip().split(None, 5)
                if (verify.returncode == 0 and len(parts) >= 6
                        and ' '.join(parts[:5]) == stamp
                        and (parts[5] == self.binary or parts[5].startswith(self.binary + ' '))):
                    os.kill(pid, signal.SIGTERM)
            except Exception:
                logging.getLogger('backend').warning('无法关闭本次启动的内置 ADB，请检查设备调试进程')
