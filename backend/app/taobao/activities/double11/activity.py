"""The existing activity assembled from independently replaceable policies."""
import logging

from backend.core.execution.activity import Activity
from backend.core.errors import NeedsAttention, Stopped, TaskUnavailable
from backend.core.execution.run_control import RunControl
from backend.core.execution.stats import RunStats
from backend.core.models import normalize
from backend.core.config.loader import load_config
from backend.core.config.schema import validate_config
from backend.app.taobao.manifest import PROFILE
from backend.app.common.utils.destinations import DestinationPolicy
from backend.app.taobao.utils.rescan import RescanPolicy
from backend.app.taobao.utils.search import SearchPolicy
from backend.app.taobao.utils.daily import HARD_STOP_RE, sanitize_product_slider
from .entry import EntryPolicy
from .actions import TaskActions
from .verify import VerificationPolicy
from .tasks import parse_tasks, is_task_list, CLAIM_ACTIONS, EXTERNAL_APPS

logger = logging.getLogger('backend.execution')


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
        return self.refine_task_rows(screen)

    def return_to_home(self, screen):
        # A reward can become claimable only after the final task settles, or
        # keep the same card key as an earlier tier. Read the painted list again
        # instead of trusting today's completed-row flags for reward cards.
        if not self.config.get('claim_rewards', True) or 'claim' not in self.config['enabled_task_kinds']:
            return screen
        if self.stats.attempts >= self.config['max_tasks']:
            return screen
        screen = self.ensure_list(True)
        seen = set()
        for _ in range(6):
            self.control.check()
            self.guard(screen)
            if not self.is_task_list(screen.nodes) or self.stats.attempts >= self.config['max_tasks']:
                break
            claim = next((t for t in self.tasks(screen)
                          if t.key.startswith('milestone-') and t.kind == 'claim'
                          and t.key not in seen and not self.task_policy.reason(t)), None)
            if claim is None:
                break
            seen.add(claim.key)
            self.remember([claim])
            self.stats.attempts += 1
            before = screen
            screen, explicit, _ = self.execute(claim, before)
            self.guard(screen)
            if not self.verify(claim, before, screen, explicit=explicit):
                self.daily_exhausted = False
                self.records[claim.key].update(status='pending', reason='累计奖励尚未核验领取')
                logger.info('累计奖励尚未核验领取，保留待下次检查')
                break
            self.stats.claims += 1
            self.task_policy.progress(claim)
            self.records[claim.key].update(status='done', current=claim.total,
                                            reason='收工前已核验累计奖励领取')
            self.update(tasks=list(self.records.values()))
            screen = self._refresh_after_success(claim)
        return screen

    def task_assets(self, screen, center_y):
        header = screen.find('累计任务奖励', exact=True)
        left = max(0, header.bounds[0]-screen.width*.04) if header else 0
        return [n for n in screen.nodes if 'O1CN' in n.text
                and left <= n.bounds[0] < screen.width * .55
                and 0 < n.bounds[2]-n.bounds[0] < screen.width*.14
                and abs(n.center[1] - center_y) < screen.height * .04]

    @staticmethod
    def rotating_task_caption(task):
        import re
        from backend.core.models import normalize
        return bool(task.total > 1 and task.seconds > 0 and task.kind in ('browse', 'search', 'done')
                    and re.fullmatch(r'好物.{0,5}[逛看]|搜一搜.+宝贝|(?:看看|逛逛|浏览).+', normalize(task.title)))

    @staticmethod
    def panel_loading(screen):
        return screen.find(r'做任务赚|累计任务奖励')

    def guard(self, screen):
        """核心词表把“滑块”这类商品名词也算成验证页，先净化再判定。

        浏览任务的落地页是商品信息流，商品标题常带“精密滑块/导轨滑块”，
        核心 guard 一撞上裸词“滑块”就整轮停，可屏幕上根本没有验证。
        所以核心判定只当提醒：只有出现拖动滑块、拼图、验证码、登录框、
        支付密码这些“必须人动手”的证据（HARD_STOP_RE）才真正停止。
        """
        self.assert_instance(screen)
        from backend.core.execution.guard import check_attention
        try:
            check_attention(self, sanitize_product_slider(screen))
        except NeedsAttention:
            text = normalize(screen.text)
            if HARD_STOP_RE.search(text) or getattr(screen, 'obscured', False):
                raise
            # 只撞到“滑块”这类商品名词，页面上没有必须人工处理的元素，继续执行。
            logger.info('忽略疑似拦截：只撞到商品词表，页面上没有验证/登录元素；片段=%s',
                        text[:60].replace('\n', ' '))


# Import compatibility for the existing regression suite and integrations.
CampaignRunner = Double11Activity
