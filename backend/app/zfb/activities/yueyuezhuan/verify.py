"""月月赚完成核验。"""
import logging

from backend.core.models import normalize

logger = logging.getLogger('backend.execution')


class VerificationPolicy:
    def verify(self, task, before, after, explicit=False):
        """成功判定：奖励到账浮层、任务行消失、或列表出现完成/已领取文案。

        真机实测：浏览任务返回后会弹出「恭喜获得」，奖励自动到账，
        该浮层就是最直接的完成证据。
        """
        label = normalize(self.config.get('reward_popup_text', '恭喜获得'))
        if any(label in normalize(n.text) for n in after.nodes):
            return True
        after_tasks = {t.title: t for t in self.tasks(after)}
        if task.title not in after_tasks:
            return True  # 该行已消失，视为完成
        after_task = after_tasks[task.title]
        if after_task.action in self.claim_actions:
            return True  # 已变为可领取/已领取
        text = '\n'.join(normalize(n.text) for n in after.nodes)
        if any(label in text for label in self.config.get('done_labels', ['已完成', '已领取'])):
            return True
        return explicit and task.title not in {t.title for t in self.tasks(before)}
