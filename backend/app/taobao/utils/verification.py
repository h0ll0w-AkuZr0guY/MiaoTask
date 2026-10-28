"""Taobao progress and row geometry evidence, shared with daily activities."""
import logging
import re
from backend.core.models import normalize
logger = logging.getLogger('backend.execution')

class VerificationPolicy:
    @staticmethod
    def list_progress(screen):
        text = normalize(screen.text)
        found = re.search(r'当前进度(\d+)\s*/\s*\d+', text)
        return int(found.group(1)) if found else None


    @staticmethod
    def milestone_received(task, screen):
        header = screen.find('累计任务奖励', exact=True)
        if not header:
            return False
        center_x = (task.bounds[0] + task.bounds[2]) / 2
        return any('已领取' in normalize(n.text)
                   and abs(n.center[0] - center_x) < screen.width * .13
                   and header.bounds[1] - screen.height * .02 < n.center[1] < header.bounds[3] + screen.height * .07
                   and n.bounds[2] - n.bounds[0] < screen.width * .5 for n in screen.nodes)


    def verify(self, task, before, after, explicit=False):
        after_task = next((x for x in self.tasks(after) if x.key == task.key), None)
        if after_task and (after_task.current > task.current or after_task.kind == 'done'
                           or (task.kind != 'claim' and after_task.action in self.claim_actions)):
            return True
        if task.kind == 'claim' and after_task and after_task.action != task.action:
            return True
        if task.key.startswith('milestone-'):
            return after_task is None and self.milestone_received(task, after)
        old, new = self.list_progress(before), self.list_progress(after)
        if old is not None and new is not None and new > old:
            return True
        # At the final search repetition the application removes the row entirely. The
        # cumulative counter can already be capped at 50/50, so require both
        # visible neighbours and their row-gap contraction instead of assuming
        # that any off-screen/missing row completed.
        if (after_task is None and task.kind in ('browse', 'search', 'visit')
                and task.current == task.total - 1
                and (task.total > 1 or after.source == 'ocr')):
            rows = sorted((t for t in self.tasks(before) if not t.key.startswith('milestone-')),
                          key=lambda t: t.bounds[1])
            index = next((i for i, t in enumerate(rows) if t.key == task.key), -1)
            if 0 < index < len(rows) - 1:
                upper, lower = rows[index - 1], rows[index + 1]
                found = {t.key: t for t in self.tasks(after)}
                if upper.key in found and lower.key in found:
                    upper_after, lower_after = found[upper.key], found[lower.key]
                    gap_before = lower.bounds[1] - upper.bounds[1]
                    gap_after = lower_after.bounds[1] - upper_after.bounds[1]
                    if (abs(upper_after.bounds[1] - upper.bounds[1]) < after.height * .025
                            and 0 < gap_after < gap_before * .75):
                        return True
            elif index == 0 and len(rows) >= 3 and after.source == 'ocr':
                # The first one-off slot also disappears after a successful
                # jump. Require the fixed reward header and TWO following rows
                # moving up together, confirmed from the rendered screenshot.
                header_before = before.find('累计任务奖励', exact=True)
                header_after = after.find('累计任务奖励', exact=True)
                found = {t.key: t for t in self.tasks(after)}
                following = [t for t in rows[1:] if t.key in found][:2]
                if (header_before and header_after
                        and abs(header_before.center[1] - header_after.center[1]) < after.height * .025
                        and len(following) == 2 and following[0].key == rows[1].key):
                    shifts = [t.bounds[1] - found[t.key].bounds[1] for t in following]
                    if (min(shifts) > after.height * .04 and abs(shifts[0] - shifts[1]) < after.height * .025
                            and abs(found[following[0].key].bounds[1] - task.bounds[1]) < after.height * .04):
                        return True
        return explicit and after_task is None
