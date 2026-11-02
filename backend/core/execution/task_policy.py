"""Durable daily decisions, independent of screen coordinates and row slots."""
import hashlib
import json
import threading
from backend.core.models import normalize


def task_identity(task):
    def get(name, default=''):
        return task.get(name, default) if isinstance(task, dict) else getattr(task, name, default)
    contract = [normalize(get('title')), get('kind'), get('total', 1),
                get('seconds', 0), get('target_app'), sorted(get('target_packages', ()))]
    return hashlib.sha256(json.dumps(contract, ensure_ascii=False).encode()).hexdigest()[:24]


class TaskPolicy:
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

    def fail(self, task, limit):
        with self.lock:
            item = self.entries.setdefault(task_identity(task), {})
            item['failures'] = item.get('failures', 0) + 1
            item['exhausted'] = item['failures'] >= limit
            self._save()
            return item['failures']

    def progress(self, task):
        """A verified advance ends the failure streak, without changing user choices."""
        with self.lock:
            identity = task_identity(task)
            item = self.entries.get(identity)
            if item and ('failures' in item or 'exhausted' in item):
                item.pop('failures', None)
                item.pop('exhausted', None)
                if not item:
                    self.entries.pop(identity, None)
                self._save()

    def mark_browse_once(self, task):
        with self.lock:
            self.entries.setdefault(task_identity(task), {})['browse_once_attempted'] = True
            self._save()

    def reset(self):
        with self.lock:
            self.entries.clear()
            self._save()

    def choose(self, identity, skip):
        with self.lock:
            if skip:
                self.entries.setdefault(identity, {})['skip'] = True
            else:
                self.entries.pop(identity, None)
            self._save()
