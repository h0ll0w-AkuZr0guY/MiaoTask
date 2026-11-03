"""东东农场活动：组装入口、动作、核验策略。

和淘宝/支付宝的活动一样，这里只实现页面策略；循环、重试、统计、实例校验
都由 core 提供。和它们不同的是：京东的「领奖励」是两段式（先做、再领），
所以 browse 与 claim 是同一条任务行的两个阶段，解析时按右侧按钮区分。
"""
import logging

from backend.core.execution.activity import Activity
from backend.core.config.loader import load_config
from backend.app.common.utils.destinations import DestinationPolicy
from backend.app.jd.manifest import PROFILE
from backend.app.jd.activities.dongdong_farm.entry import EntryPolicy
from backend.app.jd.activities.dongdong_farm.actions import TaskActions
from backend.app.jd.activities.dongdong_farm.verify import VerificationPolicy
from backend.app.jd.activities.dongdong_farm.tasks import TaskRows, is_task_list

logger = logging.getLogger('backend.execution')


class DongDongFarmActivity(EntryPolicy, TaskRows, TaskActions, VerificationPolicy,
                           DestinationPolicy, Activity):
    application = PROFILE
    parse_tasks = TaskRows.parse_tasks
    is_task_list = staticmethod(is_task_list)
    claim_actions = frozenset({'claim'})
    external_apps = ()

    # 抽屉里能点出结果的按钮；一个都没有说明行还没渲染完
    _ROW_ACTIONS = frozenset({'去完成', '逛一逛', '去领奖', '去浇水', '去关注'})

    @staticmethod
    def default_config():
        return load_config(app='jd', activity='dongdong_farm')

    def setup(self):
        self._chores_done = False
        self._water_actions = 0
        self._running = False
        self._credited = False

    def run(self, auto_open=False):
        # 只有真正执行（run）时才做签到/浇水；面板的「识别」预览必须保持只读。
        self._running = True
        try:
            return super().run(auto_open)
        finally:
            self._running = False

    def ensure_list(self, auto_open=False):
        screen = self.read()
        if self.homepage_visible(screen) and not self.farm_visible(screen):
            screen = self.scroll_home_to_top(screen)
        screen = super().ensure_list(auto_open)
        if self._running and not self._chores_done:
            self._chores_done = True
            try:
                screen = self.do_chores(screen)
            except Exception:
                logger.exception('农场日常（签到/领水滴/浇水）失败，继续做任务')
            screen = self.ensure_panel(screen)
        return screen

    def guard(self, screen):
        self.assert_instance(screen)
        if self.verify_blocked(screen):
            # 京东风控页：先按人工方式过掉，过不掉交给 check_attention 停止本轮。
            self.solve_quick_verify(screen)
            screen = self.read()
        from backend.core.execution.guard import check_attention
        check_attention(self, screen)
