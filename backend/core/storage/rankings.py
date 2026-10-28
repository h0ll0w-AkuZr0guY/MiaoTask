"""Read-only rankings over the existing activity-scoped daily records."""
from datetime import datetime
import json
from pathlib import Path
import re

from backend.core.storage.daily_state import _counts, local_day


class RankingStore:
    """No second counter or copied database: updates reuse the daily ledger."""

    def __init__(self, root):
        self.root = Path(root).resolve()

    def snapshot(self, *, group='app', period='all', metric='elapsed', catalog=()):
        if group not in ('app', 'activity') or period not in ('all', 'today'):
            raise ValueError('不支持的统计范围')
        if metric not in ('elapsed', 'completed', 'skipped', 'failed'):
            raise ValueError('不支持的排序指标')
        labels = {app['id']: app.get('label', app['id']) for app in catalog}
        activities = {(app['id'], activity['id']): activity.get('label', activity['id'])
                      for app in catalog for activity in app.get('activities', [])}
        rows, ignored, records = {}, 0, 0
        today = local_day()
        pattern = 'apps/*/activities/*/daily/*/*/instances/*/state.json'
        for index, path in enumerate(self.root.glob(pattern)):
            # Bound work without changing or pruning anybody's original data.
            if index >= 50000:
                ignored += 1
                break
            try:
                relative = path.relative_to(self.root)
                parts = relative.parts
                app, activity, day, device = parts[1], parts[3], parts[5], parts[6]
                if period == 'today' and day != today:
                    continue
                if not re.fullmatch(r'[0-9a-f]{16}', device):
                    raise ValueError('Invalid device key')
                datetime.strptime(day, '%Y-%m-%d')
                current = path
                while current != self.root:
                    if current.is_symlink() or current.is_junction():
                        raise ValueError('Linked data file')
                    current = current.parent
                if not path.resolve().is_relative_to(self.root) or path.stat().st_size > 8 * 1024 * 1024:
                    raise ValueError('Invalid data file')
                record = json.loads(path.read_text(encoding='utf-8'))
                if (not isinstance(record, dict) or record.get('schema') != 1
                        or record.get('app_id') != app or record.get('activity_id') != activity
                        or record.get('device_key') != device or record.get('date') != day
                        or not isinstance(record.get('stats'), dict)):
                    raise ValueError('Mismatched daily record')
            except (OSError, ValueError, TypeError, IndexError):
                ignored += 1
                continue
            key = (device, app, activity if group == 'activity' else '')
            if key not in rows:
                rows[key] = dict(device_key=device, app=app, app_label=labels.get(app, app),
                                 activity=key[2], activity_label=activities.get((app, activity), activity) if key[2] else '',
                                 stats=_counts({}), days=set())
            row = rows[key]
            for name, value in _counts(record['stats']).items():
                row['stats'][name] += value
            row['days'].add(day)
            records += 1
        result = sorted(rows.values(), key=lambda row: (-row['stats'][metric], row['device_key'], row['app'], row['activity']))
        for rank, row in enumerate(result, 1):
            row.update(rank=rank, days=len(row['days']))
        return dict(group=group, period=period, metric=metric, rows=result,
                    records=records, ignored=ignored, local_only=True, storage_dir=str(self.root),
                    updated_at=datetime.now().astimezone().isoformat(timespec='seconds'))
