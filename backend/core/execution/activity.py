"""Activity lifecycle contract composed from the preserved session machinery.

Implement entry, parsing, actions and verification in an application package.
Core owns the loop, identity tracking, control and bounded return budget.
"""
from abc import ABC, abstractmethod
from backend.core.execution.session import Session
from backend.core.execution.retry import TaskTracking
from backend.core.execution.loop import SessionLoop
from backend.core.execution.navigator import Navigator


class Activity(SessionLoop, Navigator, TaskTracking, Session, ABC):
    claim_actions = frozenset()
    external_apps = ()

    @abstractmethod
    def default_config(self):
        pass

    @abstractmethod
    def parse_tasks(self, nodes, width, height):
        pass

    @abstractmethod
    def is_task_list(self, nodes):
        pass

    @abstractmethod
    def execute(self, task, before):
        pass

    @abstractmethod
    def verify(self, task, before, after, explicit=False):
        pass

    def task_assets(self, screen, center_y):
        return []

    def alt_swipe(self, screen):
        """Optional hook: swipe in the opposite direction.

        Some lists keep the viewport at the same offset after paging, so the
        remaining rows are only reachable by scrolling the other way. Return
        True when such a swipe was performed. Default: not supported.
        """
        return False

    def view_signature(self, screen):
        """Optional hook: a fingerprint of what is visible right now.

        Activities whose rows are not all returned by tasks() can expose the
        rendered content here so the loop can tell whether another swipe still
        reveals something new. Returning None keeps the default behaviour.
        """
        return None

    def next_list_page(self, screen):
        """Optional hook: a control that swaps in another batch of tasks.

        列表不能滑动、或用「更多任务」换一批 rows 的活动，在这里返回该控件；
        主循环在滚动不再产生新任务时点击它。返回 None 保持原滚动逻辑。
        """
        return None

    def return_to_home(self, screen):
        """Optional hook:回到应用首页（会话结束、没有可执行任务时调用）。

        默认不处理；需要返回首页的活动自行实现，避免结束后停在活动页/抽奖页。
        """
        return screen

    def guard(self, screen):
        self.assert_instance(screen)
        from backend.core.execution.guard import check_attention
        check_attention(self, screen)

    def setup(self):
        pass

    def teardown(self):
        pass

    def run(self, auto_open=False):
        try:
            self.setup()
            return super().run(auto_open)
        finally:
            self.teardown()
