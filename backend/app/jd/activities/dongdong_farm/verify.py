"""东东农场完成核验。

真机验证（2026-11-03）：
* 浏览类任务做完回到面板，行会从「(0/1) 去完成」变成「(1/1) 去领奖」；
* 领奖成功后该行整条消失；可重复的任务（精选活动推荐 0/7）领完仍是「去完成」，
  但进度会往上走一格；
* 落地页停留够了会直接弹「恭喜获得奖励」，这也算到账证据。

因此核验只看三件事：到账浮层、行消失、进度增加。任一成立即成功。
"""
import logging

from backend.core.models import normalize

logger = logging.getLogger('backend.execution')


class VerificationPolicy:
    def verify(self, task, before, after, explicit=False):
        if self.reward_seen(after):
            return True
        if not self.panel_visible(after):
            # 没回到面板就无从比较，交给主循环重试；只有明确到账才算成功。
            return False
        rows = {row.title: row for row in self.tasks(after)}
        now = rows.get(task.title)
        if now is None:
            return True
        if now.current > task.current:
            return True
        if task.kind == 'claim' and now.kind != 'claim':
            return True
        if explicit and normalize(now.detail) != normalize(task.detail):
            return True
        return False
