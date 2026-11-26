"""Lifecycle tray hook; macOS uses terminal/browser lifecycle without Windows DLLs."""
import threading


class WindowsTray:
    def __init__(self, open_console, exit_service):
        self.open_console = open_console
        self.exit_service = exit_service
        self.ready = threading.Event()
        self.thread = None
        self.error = None
        self._exiting = False
        self.icon_loaded = False

    def start(self):
        return False

    def activate(self, command):
        if command == 1:
            threading.Thread(target=self.open_console, daemon=True).start()
        elif command == 2 and not self._exiting:
            self._exiting = True
            threading.Thread(target=self.exit_service, name='miao-tray-exit', daemon=True).start()

    def stop(self):
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(3)
