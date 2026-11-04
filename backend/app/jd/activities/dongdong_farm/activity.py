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
    # 只有真正执行（run）时才做签到/浇水/领奖；「识别」预览必须保持只读。
    # 注意：这是类属性，不能放到 setup() 里重置——Activity.run() 会先 setup()
    # 再进主循环，那样会把 run() 里刚设好的 True 又抹掉，整段农场日常都不执行。
    _running = False

    @staticmethod
    def default_config():
        return load_config(app='jd', activity='dongdong_farm')

    def setup(self):
        self._chores_done = False
        self._claims_swept = False
        self._water_actions = 0
        self._credited = False

    def run(self, auto_open=False):
        self._running = True
        try:
            return super().run(auto_open)
        finally:
            self._running = False

    def ensure_list(self, auto_open=False):
        screen = self.read()
        screen = self.dismiss_popups(screen)
        if not self.panel_visible(screen) and self.farm_visible(screen):
            # 已经站在农场页：直接开抽屉，别再走一轮「返回 → 回桌面 → 冷启动」。
            screen = self.enter_task_panel(screen)
        if not self.panel_visible(screen):
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
        if self._running:
            # 抽屉是长列表，主循环只会往下滑；停在底部时它回不到上面，就会
            # 以为「没有可执行任务」直接收工。每次重新识别都先把列表拉回顶部。
            screen = self._panel_scroll_to_top(screen, limit=6)
        if self._running and not self._claims_swept:
            # 每天第一次进来先把「去领奖」清干净。这些奖励不领，对应的任务行就
            # 一直停在「去领奖」，后面的浏览任务也没法继续，整条任务链等于断了。
            self._claims_swept = True
            try:
                screen = self.sweep_claims(screen, deep=True)
            except Exception:
                logger.exception('首次领奖扫描失败，继续做任务')
        return screen

    def return_to_home(self, screen):
        """收工前把面板里还没领的奖励再清一遍，然后收起抽屉。"""
        if self._running:
            try:
                screen = self.sweep_claims(screen, deep=True)
            except Exception:
                logger.exception('收工前领奖扫描失败')
        return super().return_to_home(screen)

    def guard(self, screen):
        self.assert_instance(screen)
        if self.verify_blocked(screen):
            # 京东风控页：先按人工方式过掉，过不掉交给 check_attention 停止本轮。
            self.solve_quick_verify(screen)
            screen = self.read()
        from backend.core.execution.guard import check_attention
        check_attention(self, screen)
