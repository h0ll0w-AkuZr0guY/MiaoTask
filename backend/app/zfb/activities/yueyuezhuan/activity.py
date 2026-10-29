"""月月赚活动：组装入口、动作、核验策略。"""
from backend.core.models import normalize
from backend.core.execution.activity import Activity
from backend.core.config.loader import load_config
from backend.app.common.utils.destinations import DestinationPolicy
from backend.app.zfb.manifest import PROFILE
from backend.app.zfb.activities.yueyuezhuan.entry import EntryPolicy
from backend.app.zfb.activities.yueyuezhuan.actions import TaskActions
from backend.app.zfb.activities.yueyuezhuan.verify import VerificationPolicy
from backend.app.zfb.activities.yueyuezhuan.tasks import (
    parse_tasks, is_task_list, CLAIM_ACTIONS, EXTERNAL_APPS,
)


class YueYueZhuanActivity(EntryPolicy, TaskActions, VerificationPolicy, DestinationPolicy, Activity):
    application = PROFILE
    parse_tasks = staticmethod(parse_tasks)
    is_task_list = staticmethod(is_task_list)
    claim_actions = CLAIM_ACTIONS
    external_apps = EXTERNAL_APPS

    # 任务行尚未渲染出来时，页面只会有活动标题，不会有任何任务按钮
    _ROW_ACTIONS = frozenset({'去完成', '去看看', '去逛逛', '领取', '立即领取'})

    @staticmethod
    def default_config():
        return load_config(app='zfb', activity='yueyuezhuan')

    @classmethod
    def panel_loading(cls, screen):
        """Navigator 契约：活动页已打开、任务行还在加载中。

        表现为能看到「月月赚/赚更多/转账红包」标题，但列表里一个任务按钮都没有，
        此时应给任务行独立的渲染等待窗口，而不是当成"不在列表页"反复返回。
        """
        if not cls.activity_landing(screen):
            return False
        return not any(normalize(n.text) in cls._ROW_ACTIONS for n in screen.nodes)

    def guard(self, screen):
        self.assert_instance(screen)
        from backend.core.execution.guard import check_attention
        check_attention(self, screen)
