"""Non-blocking macOS reminder for a stopped security challenge."""
import logging
import subprocess
import sys
import threading

logger = logging.getLogger(__name__)
_popup_lock = threading.Lock()


def _show_security_dialog():
    script = (
        'display alert "喵任务 · 请在手机上手工验证" message '
        '"手机页面出现安全验证，喵任务已停止自动操作并保存失败现场。\\n\\n'
        '请解锁手机，由本人完成滑块或图片等验证。\\n'
        '处理完成后，回到网页控制台点击「开始自动执行」。\\n\\n'
        '点击「好」只关闭此提醒，不会继续操作手机。" as warning'
    )
    subprocess.run(['osascript', '-e', script], capture_output=True, timeout=60, check=False)


def notify_security_attention():
    """One visible reminder at a time, without blocking the server or exit."""
    if sys.platform != 'darwin' or not _popup_lock.acquire(blocking=False):
        return False

    def show():
        try:
            _show_security_dialog()
        except Exception:
            logger.warning('macOS 提醒未能显示，请查看网页中的安全验证提示', exc_info=True)
        finally:
            _popup_lock.release()

    worker = threading.Thread(target=show, name='security-reminder', daemon=True)
    try:
        worker.start()
    except Exception:
        _popup_lock.release()
        raise
    return True
