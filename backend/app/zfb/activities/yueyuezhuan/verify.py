"""月月赚完成核验。"""
import logging

from backend.core.models import normalize

logger = logging.getLogger('backend.execution')


class VerificationPolicy:
    def verify(self, task, before, after, explicit=False):
        """Require a receipt or this row's completion, never a blank/missing row.

        真机实测：浏览任务返回后会弹出「恭喜获得」，奖励自动到账，
        该浮层就是最直接的完成证据。
        """
        self.assert_instance(after)
        if not self.application.matches(after.package):
            return False
        label = normalize(self.config.get('reward_popup_text', '恭喜获得'))
        if any(label in normalize(n.text) for n in after.nodes):
            return True
        # execute() records a real receipt before dismissing its overlay. That
        # evidence remains valid even if closing it changes the visible rows.
        if explicit:
            return True
        if not (self.activity_landing(after) or self.is_task_list(after.nodes)):
            return False
        after_tasks = {t.title: t for t in self.tasks(after)}
        after_task = after_tasks.get(task.title)
        if after_task and (after_task.kind == 'done' or after_task.current > task.current):
            return True
        titles = [n for n in after.nodes if normalize(task.title) == normalize(n.text)]
        done = {normalize(text) for text in self.config.get('done_labels', ['已完成', '已领取'])}
        return any(normalize(n.text) in done
                   and abs(n.center[1] - title.center[1]) < after.height * .04
                   for title in titles for n in after.nodes)
