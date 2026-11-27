"""Zero-cost runtime coordinate cache isolated per USB device serial."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re

from backend.app.damai.utils.locators.base import Point, Snapshot
from backend.app.damai.utils.targets import Target
from backend.core.runtime import data_dir


def target_signature(target: Target) -> str:
    kw = '|'.join(target.keywords)
    idx = '' if target.index is None else str(target.index)
    return f'{target.name}:{kw}#{idx}'


def cache_path_for_device(device) -> Path:
    serial = getattr(device, 'identity_serial', None) or getattr(device, 'serial', None) or 'default'
    safe = re.sub(r'[^A-Za-z0-9_.-]+', '_', str(serial).strip()) or 'default'
    return data_dir() / 'damai' / f'coords_{safe}.json'


class CoordCache:
    """Persist calibrated Damai points with metadata while staying compatible with [x, y] JSON."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._entries: dict[str, dict[str, object]] = {}
        self.load()

    def load(self) -> None:
        self._entries.clear()
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        for key, val in raw.items():
            if (isinstance(val, list) and len(val) == 2
                    and all(isinstance(n, (int, float)) and not isinstance(n, bool) for n in val)):
                self._entries[str(key)] = {'point': [int(val[0]), int(val[1])]}
            elif isinstance(val, dict):
                pt = val.get('point')
                if (isinstance(pt, list) and len(pt) == 2
                        and all(isinstance(n, (int, float)) and not isinstance(n, bool) for n in pt)):
                    entry = dict(val)
                    entry['point'] = [int(pt[0]), int(pt[1])]
                    self._entries[str(key)] = entry

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(self._entries, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(self.path)

    def clear(self) -> None:
        self._entries.clear()
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass

    def get(self, name: str, *, target: Target | None = None, width: int = 0, height: int = 0) -> Point | None:
        entry = self._entries.get(name)
        if not entry:
            return None
        if target is not None and entry.get('signature'):
            if entry['signature'] != target_signature(target):
                return None
        size = entry.get('screen_size')
        if width > 0 and height > 0 and isinstance(size, list) and len(size) == 2:
            if [int(size[0]), int(size[1])] != [int(width), int(height)]:
                return None
        pt = entry['point']
        return (int(pt[0]), int(pt[1]))

    def set(
        self,
        name: str,
        pt: Point,
        *,
        source: str = 'manual',
        detail: str = '',
        target: Target | None = None,
        screen_size: tuple[int, int] | None = None,
    ) -> None:
        record: dict[str, object] = {
            'point': [int(pt[0]), int(pt[1])],
            'source': source,
            'detail': detail,
            'updated_at': datetime.now().astimezone().isoformat(timespec='seconds'),
        }
        if target is not None:
            record['signature'] = target_signature(target)
        if screen_size and screen_size[0] > 0 and screen_size[1] > 0:
            record['screen_size'] = [int(screen_size[0]), int(screen_size[1])]
        self._entries[name] = record

    def entry_info(self, target: Target) -> dict[str, object]:
        entry = self._entries.get(target.name)
        if not entry:
            return {
                'name': target.name,
                'page': target.page.value,
                'cached': False,
                'stale': False,
                'point': None,
                'source': '',
                'detail': '',
                'screen_size': None,
                'updated_at': '',
            }
        sig = entry.get('signature')
        stale = bool(sig and sig != target_signature(target))
        return {
            'name': target.name,
            'page': target.page.value,
            'cached': not stale,
            'stale': stale,
            'point': list(entry['point']),
            'source': str(entry.get('source') or ''),
            'detail': str(entry.get('detail') or ''),
            'screen_size': entry.get('screen_size'),
            'updated_at': str(entry.get('updated_at') or ''),
        }

    @property
    def points(self) -> dict[str, Point]:
        return {k: (int(v['point'][0]), int(v['point'][1])) for k, v in self._entries.items()}


class CachedLocator:
    name = 'cached'

    def __init__(self, cache: CoordCache) -> None:
        self.cache = cache

    def locate(self, target: Target, snap: Snapshot) -> Point | None:
        pt = self.cache.get(target.name, target=target, width=snap.width, height=snap.height)
        if pt is None:
            return None
        x, y = pt
        if snap.width > 0 and snap.height > 0 and not (0 <= x <= snap.width and 0 <= y <= snap.height):
            return None
        return pt
