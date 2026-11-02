"""The existing activity assembled from independently replaceable policies."""
from backend.core.execution.activity import Activity
from backend.core.errors import NeedsAttention, Stopped, TaskUnavailable
from backend.core.execution.run_control import RunControl
from backend.core.execution.stats import RunStats
from backend.core.config.loader import load_config
from backend.core.config.schema import validate_config
from backend.app.taobao.manifest import PROFILE
from backend.app.common.utils.destinations import DestinationPolicy
from backend.app.taobao.utils.rescan import RescanPolicy
from backend.app.taobao.utils.search import SearchPolicy
from .entry import EntryPolicy
from .actions import TaskActions
from .verify import VerificationPolicy
from .tasks import parse_tasks, is_task_list, CLAIM_ACTIONS, EXTERNAL_APPS


class Double11Activity(RescanPolicy, EntryPolicy, TaskActions, VerificationPolicy, SearchPolicy, DestinationPolicy, Activity):
    application = PROFILE
    parse_tasks = staticmethod(parse_tasks)
    is_task_list = staticmethod(is_task_list)
    claim_actions = CLAIM_ACTIONS
    external_apps = EXTERNAL_APPS

    @staticmethod
    def default_config():
        return load_config(app='taobao', activity='double11')

    def task_assets(self, screen, center_y):
        return [n for n in screen.nodes if 'O1CN' in n.text
                and n.bounds[0] < screen.width * .2
                and abs(n.center[1] - center_y) < screen.height * .04]

    @staticmethod
    def panel_loading(screen):
        return screen.find(r'做任务赚|累计任务奖励')

    def guard(self, screen):
        self.assert_instance(screen)
        from backend.core.execution.guard import check_attention
        check_attention(self, screen)


# Import compatibility for the existing regression suite and integrations.
CampaignRunner = Double11Activity
