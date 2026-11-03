"""Durable daily decisions, independent of screen coordinates and row slots."""
import hashlib
import json
import threading
from backend.core.models import normalize


def keyword_skip_reason(task, words):
    def get(name, default=''):
        return task.get(name, default) if isinstance(task, dict) else getattr(task, name, default)
    if get('kind') == 'done':
        return ''
    text = normalize(get('title') + ' ' + (get('detail') or '')).casefold()
    word = next((word for word in words if normalize(word).casefold() in text), None)
    return f'已跳过：命中常驻关键词“{word}”；请在公共词表中修改' if word else ''


def task_identity(task):
    def get(name, default=''):
        return task.get(name, default) if isinstance(task, dict) else getattr(task, name, default)
    contract = [normalize(get('title')), get('kind'), get('total', 1),
                get('seconds', 0), get('target_app'), sorted(get('target_packages', ()))]
    return hashlib.sha256(json.dumps(contract, ensure_ascii=False).encode()).hexdigest()[:24]


class TaskPolicy:
    # 基于「上一轮那次尝试」得出的临时判定：可以重新来过，不属于用户选择。
    CLEAR_ON_PROGRESS = ('browse_once_attempted', 'failures', 'exhausted', 'deferred')
    # 重新进入列表后只放行「无明确时长只试一次」这条锁；失败次数是当日额度，
    # 清了就等于把 max_attempts 变成无限重试，正在出问题的任务会被反复空跑。
    CLEAR_ON_REENTRY = ('browse_once_attempted',)

    def __init__(self):
        self.lock = threading.RLock()
        self.store = None
        self.entries = {}

    def bind(self, store):
        with self.lock:
            self.store = store
            self.entries = store.task_decisions() if store else {}

    def _save(self):
        if self.store:
            self.store.save_task_decisions(self.entries)

    def reason(self, task):
        with self.lock:
            item = self.entries.get(task_identity(task), {})
            if item.get('skip'):
                return '已跳过：用户选择今日跳过'
            if item.get('browse_once_attempted'):
                return '已跳过：无明确浏览时长，今日已尝试一次'
            if item.get('exhausted'):
                return '已跳过：今日失败已达上限，点击执行可重新尝试'
            return ''

    def failures(self, task):
        with self.lock:
            return self.entries.get(task_identity(task), {}).get('failures', 0)

    def defer(self, task, category, limit=1):
        """Persist bounded unconfirmed attempts without claiming task success."""
        if category not in ('navigation', 'settlement'):
            raise ValueError('unknown task deferral category')
        with self.lock:
            item = self.entries.setdefault(task_identity(task), {})
            if item.get('skip') or item.get('exhausted'):
                return False
            deferred = item.setdefault('deferred', {})
            used = deferred.get(category, 0)
            if used >= limit:
                return False
            deferred[category] = used + 1
            self._save()
            return True

    def retry_needed(self, task):
        with self.lock:
            item = self.entries.get(task_identity(task), {})
            return bool(item.get('failures') or item.get('deferred'))

    def fail(self, task, limit):
        with self.lock:
            item = self.entries.setdefault(task_identity(task), {})
            item['failures'] = item.get('failures', 0) + 1
            item['exhausted'] = item['failures'] >= limit
            self._save()
            return item['failures']

    def progress(self, task):
        """A verified advance ends the failure streak, without changing user choices.

        进度被核验通过，说明这个任务是真的能推进的：这一条同时要解开「无明确
        浏览时长，今日只试一次」的锁。否则做到 5/6、只差最后一次的任务，会因为
        第一次尝试留下的锁被整天放弃。
        """
        with self.lock:
            identity = task_identity(task)
            item = self.entries.get(identity)
            if item and set(item) & set(self.CLEAR_ON_PROGRESS):
                for key in self.CLEAR_ON_PROGRESS:
                    item.pop(key, None)
                if not item:
                    self.entries.pop(identity, None)
                self._save()

    def mark_browse_once(self, task):
        with self.lock:
            self.entries.setdefault(task_identity(task), {})['browse_once_attempted'] = True
            self._save()

    def retry_transient(self, seen=None):
        """给「试过一次就锁住」的任务一次重新来过的机会。

        补扫前客户端已经被强停重开、列表也重新拉了一份，上一轮得出的尝试结论可能
        已经过期：任务进度涨了、入口换了，或者当时只是页面没刷新。重新进入时解开
        「无明确浏览时长，今日只试一次」这条锁，让真正还能做的任务回到候选里。

        失败次数（``failures`` / ``exhausted``）不在放行范围内：那是当日额度，清掉
        等于把 ``max_attempts`` 变成无限重试，坏掉的任务会被反复空跑。

        ``seen`` 是本次运行已经放行过的任务标识集合：同一个任务只放行一次，避免
        「一直失败 → 一直清零」把重试额度变成无限。用户手动跳过的（``skip``）是
        明确选择，永远不动。
        """
        with self.lock:
            granted = set() if seen is None else seen
            cleared = 0
            for identity in list(self.entries):
                item = self.entries[identity]
                if item.get('skip') or identity in granted:
                    continue
                if not set(item) & set(self.CLEAR_ON_REENTRY):
                    continue
                for key in self.CLEAR_ON_REENTRY:
                    item.pop(key, None)
                granted.add(identity)
                cleared += 1
                if not item:
                    self.entries.pop(identity, None)
            if cleared:
                self._save()
            return cleared

    def reset(self):
        with self.lock:
            self.entries.clear()
            self._save()

    def choose(self, identity, skip):
        with self.lock:
            if skip:
                self.entries.setdefault(identity, {})['skip'] = True
            else:
                self.entries[identity] = {'skip': False}
            self._save()

    def explicitly_enabled(self, task):
        with self.lock:
            return self.entries.get(task_identity(task), {}).get('skip') is False
