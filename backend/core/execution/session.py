"""Session state, observation and events."""
from collections import Counter
from dataclasses import asdict
import logging
import random
import time
from backend.core.execution.run_control import RunControl
from backend.core.execution.stats import RunStats
from backend.core.execution.gestures import BrowseGestures
from backend.core.device.instances import AppInstance, InstanceMismatch
from backend.core.device.screen import ScreenReader
logger = logging.getLogger('backend.execution')

class Session:
    def __init__(self, device, config=None, control=None, emit=None, on_image=None, unavailable_tasks=None,
                 on_failure=None, task_policy=None):
        self.device = device
        self.config = config or self.default_config()
        self.control = control or RunControl()
        self.emit = emit or (lambda **event: None)
        self.reader = ScreenReader(device, self.config['ocr'], on_image=on_image)
        self.reader.compressed_screenshots = self.config.get('compressed_screenshots', False)
        self.reader.screenshot_quality = self.config.get('screenshot_quality', 70)
        self.stats = RunStats()
        self.failures = Counter()
        from backend.core.execution.task_policy import TaskPolicy
        self.task_policy = task_policy or TaskPolicy()
        self.records = {}
        self._verified_progress = set()
        target = getattr(device, 'selected_instance', None)
        self.target_instance = target if isinstance(target, AppInstance) else None
        self.origin = self.target_instance.package if self.target_instance else None
        self.gestures = BrowseGestures(self.config)
        self.started = time.monotonic()
        self._external_seen = set()
        self._known_tasks = {}
        self._asset_keys = {}
        self.unavailable_tasks = unavailable_tasks if unavailable_tasks is not None else {}
        self._installed_checked = False
        self._installed_packages = None
        self.on_failure = on_failure
        self._active_task = None
        self._phase = 'locating'
        self._task_restart_used = False

    def next_search_keyword(self):
        """Task queries share the public list; navigation keeps activity wording."""
        words = self.config.get('task_search_keywords', [])
        if not words:
            return self.config.get('task_search_keyword', '123')
        return random.choice(words)

    def settle_completion(self, *, deadline=None):
        """Random pause between the completion prompt and the return navigation.

        `completion_return_ms` caps the wait; the drawn value is uniform in
        0..N so 0 keeps the immediate return. `control.wait` still honours
        stop requests during the pause.
        """
        limit = self.config.get('completion_return_ms', 1000) or 0
        if isinstance(limit, bool) or not isinstance(limit, (int, float)) or limit <= 0:
            return
        delay = random.uniform(0, min(limit, 10000)) / 1000
        if deadline is not None:
            delay = min(delay, max(0, deadline - self.control.now()))
        if delay > 0:
            self.control.wait(delay)


    def reset_task_marks(self):
        """Discard scheduling decisions/aliases, retaining accumulated run statistics."""
        self.task_policy.reset()
        self.records.clear()
        self.failures.clear()
        self._verified_progress.clear()
        self._known_tasks.clear()
        self._asset_keys.clear()
        self._active_task = None
        self._task_restart_used = False
        self._task_budget_counted = False
        for key in list(self.unavailable_tasks):
            if key.startswith('task:'):
                self.unavailable_tasks.pop(key, None)

    def report_failure(self, reason, task=None, phase=None):
        """Capture evidence at the failing page, before recovery changes it."""
        if self.on_failure:
            try:
                self.on_failure(reason=reason, task=(task or self._active_task).to_dict()
                                if (task or self._active_task) else None,
                                phase=phase or self._phase)
                return True
            except Exception:
                logger.warning('保存失败现场时发生异常', exc_info=True)
        return False


    def update(self, phase=None, **data):
        self.stats.elapsed = int(time.monotonic() - self.started)
        self.stats.skipped = sum(r.get('status') in ('manual', 'disabled') for r in self.records.values())
        if phase:
            data['phase'] = phase
            self._phase = phase
        self.emit(stats=asdict(self.stats), **data)


    def read(self, force_ocr=False):
        self.control.check()
        screen = self.reader.read(force_ocr=force_ocr) if force_ocr else self.reader.read()
        self.control.check()
        self.assert_instance(screen)
        self.update(package=screen.package, source=screen.source,
                    observation_ms=round(self.reader.last_observation_seconds * 1000),
                    observation_metrics=getattr(self.reader, 'observation_metrics', {}))
        return screen


    def assert_instance(self, screen):
        target = self.target_instance
        if target and self.application.matches(screen.package) and (screen.package != target.package or
                target.user_id is not None and screen.user_id != target.user_id):
            raise InstanceMismatch('前台应用与本轮选定实例不一致或无法核定，已停止；不会继续操作另一账号')


    def click(self, node):
        self.control.check()
        geometry = getattr(self.reader.last_screen, 'geometry', None)
        if geometry and not geometry.contains(*node.center):
            from backend.core.errors import NeedsAttention
            raise NeedsAttention('目标位于系统栏、屏幕裁切或不可操作区域，请检查窗口布局')
        self.device.click(*node.center)


    def browse_swipe(self, screen):
        self.control.check()
        if screen.geometry:
            left,top,right,bottom=screen.geometry.content_bounds
            x1,y1,x2,y2,duration=self.gestures.sample(right-left,bottom-top)
            x1,x2=x1+left,x2+left;y1,y2=y1+top,y2+top
        else:
            x1, y1, x2, y2, duration = self.gestures.sample(screen.width, screen.height)
        send = (self.device.continuous_swipe if callable(getattr(type(self.device), 'continuous_swipe', None))
                else self.device.swipe)
        send(x1, y1, x2, y2, duration=duration)
        return duration


    def scan(self, auto_open=False):
        with self.reader.observation_session(self.config, self.control):
            screen = self.ensure_list(auto_open)
        self.remember(self.tasks(screen))
        self.update('ready', message='已识别当前屏幕任务，可以开始执行')
        return list(self.records.values())
