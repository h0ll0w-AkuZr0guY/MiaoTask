"""Stable task identity and per-progress retry tracking."""
import hashlib
import re
import logging
from collections import Counter
from backend.core.models import normalize
from backend.core.execution.task_policy import task_identity
logger = logging.getLogger('backend.execution')

class TaskTracking:
    @staticmethod
    def unavailable_key(task):
        return 'app:' + task.target_app if task.target_app else 'task:' + normalize(task.title)


    def availability_reason(self, task, check_installed=False):
        if task.kind in ('manual', 'done', 'claim'):
            return self.task_policy.reason(task) if task.kind == 'claim' else ''
        decision = self.task_policy.reason(task)
        if decision:
            return decision
        key = self.unavailable_key(task)
        saved_reason = self.unavailable_tasks.get(key, '')
        if (task.target_packages and self.device.device_type == 'uiautomator2'
                and (check_installed or saved_reason.startswith('未安装'))):
            if not self._installed_checked:
                self._installed_checked = True
                try:
                    packages = self.device.installed_packages()
                    if isinstance(packages, (set, list, tuple)):
                        self._installed_packages = set(packages)
                except Exception:
                    logger.warning('无法读取已安装应用，将依据跳转页面判断', exc_info=True)
            if self._installed_packages is not None:
                if not self._installed_packages.intersection(task.target_packages):
                    reason = f'未安装{task.target_app}，已跳过；安装后重新识别即可恢复'
                    self.unavailable_tasks[key] = reason
                    return reason
                # A new scan refreshes installation state, including after installation.
                if saved_reason.startswith('未安装'):
                    self.unavailable_tasks.pop(key, None)
        return self.unavailable_tasks.get(key, '')


    def tasks(self, screen):
        cache = getattr(screen, '_campaign_tasks', {})
        if id(self) in cache:
            return cache[id(self)]
        tasks = self.parse_tasks(screen.nodes, screen.width, screen.height)
        from backend.core.execution.browse_contract import apply_browse_contract
        tasks = [apply_browse_contract(task) for task in tasks]
        used = set()
        for task in tasks:
            if task.key.startswith('milestone-'):
                continue
            center_y = (task.bounds[1] + task.bounds[3]) / 2
            icons = self.task_assets(screen, center_y)
            asset = min(icons, key=lambda n: abs(n.center[1] - center_y)).text if icons else ''
            key = self._asset_keys.get(asset) if asset else None
            exact = [old for old in self._known_tasks.values()
                     if old.key not in used and normalize(old.title) == normalize(task.title)
                     and old.total == task.total and old.seconds == task.seconds]
            if len(exact) == 1:
                key = exact[0].key
            old_asset_task = self._known_tasks.get(key)
            if old_asset_task and (old_asset_task.total != task.total or old_asset_task.seconds != task.seconds
                                   or (task.total == 1 and old_asset_task.title != task.title)):
                # A completed one-off offer may be replaced in the same slot,
                # even reusing its icon. It must not inherit the old retry budget.
                key = None
            if key in used:
                key = None
            if key is None:
                # Task captions rotate between product names, search and browse.
                # Match existing repeatable rows by their progress contract and
                # geometry; a newly substituted one-off task gets a fresh ID.
                matches = []
                for old in self._known_tasks.values():
                    if old.key in used or old.total != task.total or old.seconds != task.seconds:
                        continue
                    same_title = old.title == task.title
                    close = abs((old.bounds[1] + old.bounds[3]) / 2 - center_y) < screen.height * .04
                    repeatable = (old.total > 1 and old.seconds > 0
                                  and old.kind in ('browse', 'search', 'done')
                                  and task.kind in ('browse', 'search', 'done'))
                    if same_title or (close and repeatable):
                        matches.append((not same_title, not close, abs(old.current - task.current), old.key))
                if matches:
                    key = min(matches)[-1]
            if key is None:
                key = task.key
                if key in used or key in self._known_tasks:
                    key += '-' + hashlib.sha256((asset or str(task.bounds)).encode()).hexdigest()[:6]
            previous = self._known_tasks.get(key)
            if (previous and previous.kind in ('browse', 'search') and task.total > 1 and previous.total == task.total
                    and previous.seconds == task.seconds
                    and re.match(r'^(?:看看|逛逛|浏览)#', normalize(task.title))):
                # Rotating product promotions are captions for the same contract,
                # not a new failure budget or a different task type.
                task.title = previous.title
                task.kind = previous.kind
            task.key = key
            used.add(key)
            if asset:
                self._asset_keys[asset] = key
            self._known_tasks[key] = task
        cache[id(self)] = tasks
        screen._campaign_tasks = cache
        return tasks


    def remember(self, tasks):
        for task in tasks:
            previous = self.records.get(task.key, {})
            reason = task.skip_reason or self.availability_reason(task)
            state = ('done' if task.kind == 'done' else 'disabled' if reason.startswith(('已跳过', '未安装', '目标应用'))
                     else 'manual' if task.skip_reason else 'disabled' if reason else 'pending')
            if (previous.get('task_id') == task_identity(task)
                    and previous.get('status') in ('failed', 'disabled') and task.current == previous.get('current')):
                if reason:
                    state = previous['status']
                    reason = reason or previous.get('reason', '')
            self.records[task.key] = dict(task.to_dict(), status=state, task_id=task_identity(task),
                                          controllable=task.kind not in ('manual', 'done') and not task.skip_reason,
                                          enabled=task.kind not in ('manual', 'done') and not reason
                                          and task.kind in self.config['enabled_task_kinds']
                                          and (task.kind != 'claim' or self.config['claim_rewards'])
                                          and (not task.target_app or self.config['allow_cross_app']),
                                          reason=reason)
        identities = Counter(row.get('task_id') for row in self.records.values())
        for row in self.records.values():
            if identities[row.get('task_id')] > 1:
                row['controllable'] = False
        self.stats.skipped = sum(r['status'] in ('manual', 'disabled') for r in self.records.values())
        self.update(tasks=list(self.records.values()))
