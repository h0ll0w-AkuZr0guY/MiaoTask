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

    def read(self, force_ocr=False):
        task = getattr(self, '_active_task', None)
        visual = bool(force_ocr or self.config['ocr'] and self._phase == 'browsing'
                      and task is not None and task.kind in ('browse', 'search'))
        screen = super().read(force_ocr=visual)
        if screen.source == 'ui' and self.config['ocr'] and self.is_task_list(screen.nodes):
            # The H5 tree can retain old product captions and progress after a claim.
            # Keep native controls for navigation; verify list content from its paint.
            screen = super().read(force_ocr=True)
        return screen

    @staticmethod
    def browse_observation_interval(scrolling):
        # Keep gestures flowing between costly observations of animated pages.
        return 3 if scrolling else 1

    def task_assets(self, screen, center_y):
        header = screen.find('累计任务奖励', exact=True)
        left = max(0, header.bounds[0]-screen.width*.04) if header else 0
        return [n for n in screen.nodes if 'O1CN' in n.text
                and left <= n.bounds[0] < screen.width * .55
                and 0 < n.bounds[2]-n.bounds[0] < screen.width*.14
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
