"""Clean up only newly started ADB processes belonging to this installation."""
import json
import logging
import os
from pathlib import Path
import subprocess


class OwnedAdb:
    def __init__(self, binary):
        self.binary = str(Path(binary).resolve())
        self.before = self._processes()

    def _powershell(self, script):
        environment = dict(os.environ, MIAO_OWN_ADB=self.binary)
        return subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                              env=environment, capture_output=True, text=True, timeout=8,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=True)

    def _processes(self):
        if os.name != 'nt':
            return None
        try:
            result = self._powershell("@(Get-CimInstance Win32_Process -Filter \"Name='adb.exe'\" | Where-Object { $_.ExecutablePath -eq $env:MIAO_OWN_ADB } | ForEach-Object { @{id=$_.ProcessId;stamp=$_.CreationDate.ToUniversalTime().Ticks.ToString()} }) | ConvertTo-Json -Compress")
            rows = json.loads(result.stdout or '[]')
            if isinstance(rows, dict):
                rows = [rows]
            return {(int(row['id']), row['stamp']) for row in rows}
        except Exception:
            return None  # Unknown ownership must never lead to killing another tool.

    def close(self):
        current = self._processes()
        if self.before is None or current is None:
            return
        for pid, stamp in current - self.before:
            if not isinstance(stamp, str) or not stamp.isdigit():
                continue
            try:
                self._powershell(f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={pid}'; if ($p -and $p.ExecutablePath -eq $env:MIAO_OWN_ADB -and $p.CreationDate.ToUniversalTime().Ticks.ToString() -eq '{stamp}') {{ Stop-Process -Id {pid} -ErrorAction Stop }}")
            except Exception:
                logging.getLogger('backend').warning('无法关闭本次启动的 ADB，请检查设备调试进程')
