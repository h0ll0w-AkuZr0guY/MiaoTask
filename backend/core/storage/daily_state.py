"""Local, atomic per-device daily progress and diagnostic evidence.

Only a hash of the adapter's resolved identity is used as the directory name.
Raw device serials never belong in the portable distribution or this store.
"""
import copy
from datetime import datetime
import hashlib
import io
import json
import math
import os
from pathlib import Path
import threading
import time
import uuid
import zipfile


STAT_FIELDS = ('completed', 'failed', 'skipped', 'claims', 'cross_app', 'attempts', 'elapsed')


def local_day():
    return datetime.now().astimezone().date().isoformat()


def _timestamp():
    return datetime.now().astimezone().isoformat(timespec='milliseconds')


def _atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _counts(value):
    if not isinstance(value, dict):
        value = {}
    return {key: max(0, int(value.get(key, 0)))
            if (isinstance(value.get(key, 0), (int, float)) and not isinstance(value.get(key), bool)
                and math.isfinite(value.get(key, 0)))
            else 0 for key in STAT_FIELDS}


class DailyState:
    """One device, one local calendar day; progress events are cumulative per run."""

    def __init__(self, root, serial, device_type, day=None, instance_id='', namespace=None):
        if not isinstance(serial, str) or not serial.strip() or serial.strip() == '自动选择':
            raise ValueError('无法取得真实设备标识，不能安全加载设备当天记录')
        self.serial = serial.strip()
        self.redaction_values = {self.serial}
        self.device_type = device_type
        self.instance_id = instance_id
        self.date = day or local_day()
        # Validate before using the date as a directory component.
        datetime.strptime(self.date, '%Y-%m-%d')
        self.device_key = hashlib.sha256((device_type + '\0' + self.serial).encode()).hexdigest()[:16]
        from backend.core.storage.namespace import legacy_folder
        from backend.core.registry import default_registry
        primary = namespace.primary_instances if namespace else default_registry().default_app().profile.legacy_primary_instances
        self.namespace = namespace
        self.app_id = namespace.app if namespace else ''
        self.activity_id = namespace.activity if namespace else ''
        if namespace:
            namespace.adopt_legacy(root, self.date, self.device_key, instance_id)
            self.folder = namespace.folder(root, self.date, self.device_key, instance_id)
        else:
            self.folder = legacy_folder(root, self.date, self.device_key, instance_id, primary)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.failures_dir = self.folder / 'failures'
        self.failures_dir.mkdir(exist_ok=True)
        self.stats_path = self.folder / 'state.json'
        self.events_path = self.folder / 'events.jsonl'
        self.lock = threading.RLock()
        self._last_failure = ('', 0.0, None)
        self.recovery_warning = ''
        self.data = dict(app_id=self.app_id, activity_id=self.activity_id, schema=1, date=self.date, device_key=self.device_key,
                         instance_id=instance_id,
                         device_type=device_type, stats=_counts({}), unavailable_tasks={},
                         activity_finished=False, watermarks={}, skipped_task_keys=[], failures_count=0, task_decisions={},
                         created_at=_timestamp(), updated_at=_timestamp(), last_state={})
        if self.stats_path.exists():
            try:
                loaded = json.loads(self.stats_path.read_text(encoding='utf-8'))
                if (loaded.get('app_id', self.app_id) != self.app_id or loaded.get('activity_id', self.activity_id) != self.activity_id
                        or loaded.get('schema') != 1 or loaded.get('date') != self.date
                        or loaded.get('device_key') != self.device_key
                        or (loaded.get('instance_id', instance_id) != instance_id and not
                            (instance_id in primary
                             and loaded.get('instance_id', '') == ''))
                        or not isinstance(loaded.get('activity_finished', False), bool)
                        or not isinstance(loaded.get('stats'), dict)
                        or not isinstance(loaded.get('unavailable_tasks'), dict)
                        or not all(isinstance(key, str) and isinstance(value, str)
                                   for key, value in loaded.get('unavailable_tasks', {}).items())
                        or not isinstance(loaded.get('watermarks'), dict)
                        or not all(isinstance(value, dict) for value in loaded.get('watermarks', {}).values())
                        or not isinstance(loaded.get('skipped_task_keys'), list)
                        or not all(isinstance(key, str) for key in loaded.get('skipped_task_keys', []))
                        or not isinstance(loaded.get('failures_count'), int)
                        or isinstance(loaded.get('failures_count'), bool)
                        or loaded.get('failures_count', -1) < 0
                        or not isinstance(loaded.get('last_state'), dict)):
                    raise ValueError('记录格式与当前设备不符')
                decisions = loaded.get('task_decisions', {})
                if (not isinstance(decisions, dict) or any(
                        not isinstance(key, str) or len(key) != 24 or not isinstance(item, dict)
                        or set(item) - {'skip', 'failures', 'exhausted', 'browse_once_attempted'}
                        or not isinstance(item.get('browse_once_attempted', False), bool)
                        or not isinstance(item.get('skip', False), bool)
                        or not isinstance(item.get('exhausted', False), bool)
                        or type(item.get('failures', 0)) is not int or item.get('failures', 0) < 0
                        for key, item in decisions.items())):
                    raise ValueError('任务重试记录格式无效')
                # Accept only this schema's fields after checking the complete
                # document. A malformed file must not partly replace defaults.
                replacement = {key: loaded[key] for key in self.data if key in loaded}
                replacement['stats'] = _counts(loaded['stats'])
                replacement['watermarks'] = {key: _counts(value) for key, value in loaded['watermarks'].items()}
                self.data.update(replacement)
            except (ValueError, TypeError, AttributeError, OSError):
                backup = self.folder / ('state.corrupt-' + uuid.uuid4().hex[:8] + '.json')
                os.replace(self.stats_path, backup)
                self.recovery_warning = '当天记录损坏，已保留原文件并建立新记录'
        self._save()

    def redact(self, value):
        """Export useful diagnostics without repeating the raw adapter identity."""
        if isinstance(value, str):
            for identifier in self.redaction_values:
                value = value.replace(identifier, '[设备标识已隐藏]')
            return value
        if isinstance(value, dict):
            return {key: self.redact(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact(item) for item in value]
        return value

    def _save(self):
        self.data['updated_at'] = _timestamp()
        _atomic_json(self.stats_path, self.data)

    def _event(self, event_kind, **event):
        entry = dict(time=_timestamp())
        entry.update(event)
        if 'kind' in entry:
            entry['detail_kind'] = entry['kind']
        entry['kind'] = event_kind
        entry = self.redact(entry)
        with self.events_path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(entry, ensure_ascii=False, separators=(',', ':')) + '\n')
            stream.flush()

    def activity_finished(self):
        with self.lock:
            return self.data.get('activity_finished') is True

    def mark_activity_finished(self, finished=True):
        with self.lock:
            self.data['activity_finished'] = bool(finished)
            self._save()

    def snapshot(self):
        with self.lock:
            return dict(app_id=self.app_id, activity_id=self.activity_id, date=self.date, device_key=self.device_key,
                        instance_id=self.instance_id,
                        stats=copy.deepcopy(self.data['stats']),
                        unavailable_count=len(self.data['unavailable_tasks']),
                        failures_count=self.data['failures_count'],
                        stats_path=str(self.stats_path), events_path=str(self.events_path),
                        failures_dir=str(self.failures_dir), local_only=True)

    def unavailable_tasks(self):
        with self.lock:
            return copy.deepcopy(self.data['unavailable_tasks'])

    def task_decisions(self):
        with self.lock:
            return copy.deepcopy(self.data['task_decisions'])

    def save_task_decisions(self, entries):
        with self.lock:
            self.data['activity_finished'] = False
            self.data['task_decisions'] = copy.deepcopy(entries)
            self._save()
            self._event('task_decisions', entries=entries)

    def progress(self, session_id, stats, tasks=None, unavailable_tasks=None,
                 count_stats=True, baseline=None, state=None):
        """Apply only increases from a runner's previously seen cumulative values.

        ``baseline`` is used only when a run crosses midnight. It prevents the
        new day's first event from importing the previous day's completed work.
        Scans may update availability and unique skipped tasks, never run totals.
        """
        with self.lock:
            values = _counts(stats)
            if count_stats:
                previous = _counts(self.data['watermarks'].get(session_id, baseline or {}))
                for key in STAT_FIELDS:
                    if key != 'skipped':
                        self.data['stats'][key] += max(0, values[key] - previous[key])
                self.data['watermarks'][session_id] = {
                    key: max(values[key], previous[key]) for key in STAT_FIELDS}
            skipped = set(self.data.get('skipped_task_keys', []))
            for task in tasks or []:
                if task.get('status') in ('manual', 'disabled'):
                    reason = task.get('reason', '')
                    if reason.startswith(('本轮未选择', '跨应用任务已关闭')):
                        continue
                    key = task.get('key') or hashlib.sha256(task.get('title', '').encode()).hexdigest()[:20]
                    skipped.add(key)
            self.data['skipped_task_keys'] = sorted(skipped)
            self.data['stats']['skipped'] = len(skipped)
            if unavailable_tasks is not None:
                self.data['unavailable_tasks'] = self.redact(copy.deepcopy(unavailable_tasks))
            if state:
                self.data['last_state'] = self.redact(copy.deepcopy(state))
            self._save()
            self._event('progress', session=session_id, counted=count_stats, stats=values,
                        daily_stats=self.data['stats'], state=state or {})
            return self.snapshot()

    def save_state(self, state, unavailable_tasks=None):
        with self.lock:
            self.data['last_state'] = self.redact(copy.deepcopy(state))
            if unavailable_tasks is not None:
                self.data['unavailable_tasks'] = self.redact(copy.deepcopy(unavailable_tasks))
            self._save()
            self._event('state', state=state)

    def log(self, level, message):
        with self.lock:
            self._event('log', level=level, message=message)

    def failure(self, event, image=None):
        """Keep screenshot plus reason even if capture itself was unavailable."""
        with self.lock:
            details = self.redact(copy.deepcopy({key: value for key, value in event.items() if key != 'image'}))
            fingerprint = json.dumps(details, ensure_ascii=False, sort_keys=True, default=str)
            if fingerprint == self._last_failure[0] and time.monotonic() - self._last_failure[1] < 3:
                return copy.deepcopy(self._last_failure[2])
            name = datetime.now().strftime('%H%M%S-%f') + '-' + uuid.uuid4().hex[:6]
            metadata_path = self.failures_dir / (name + '.json')
            screenshot_path = self.failures_dir / (name + '.png')
            details.update(time=_timestamp(), date=self.date, device_key=self.device_key, instance_id=self.instance_id,
                           metadata=str(metadata_path), screenshot='')
            if image is not None:
                temporary = screenshot_path.with_suffix('.png.tmp')
                try:
                    image.convert('RGB').save(temporary, format='PNG')
                    os.replace(temporary, screenshot_path)
                    details['screenshot'] = str(screenshot_path)
                except Exception as exc:
                    details['capture_error'] = self.redact(f'{type(exc).__name__}: {exc}')
                finally:
                    temporary.unlink(missing_ok=True)
            # These files are local evidence, never automatically sent to GitHub.
            _atomic_json(metadata_path, details)
            self.data['failures_count'] += 1
            self._save()
            self._event('failure', **details)
            self._last_failure = (fingerprint, time.monotonic(), copy.deepcopy(details))
            return details

    def diagnostics(self, report):
        """Export the current device/day only. Screenshots need human review."""
        with self.lock:
            report = self.redact(copy.deepcopy(report))
            report.pop('config', None)
            if isinstance(report.get('device'), dict):
                report['device']['serial'] = '[设备标识已隐藏]'
            output = io.BytesIO()
            with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('READ-ME.txt',
                                 '本文件为当前设备当天的本地诊断记录。设备标识已隐藏。\n'
                                 '失败截图可能含账号、余额、搜索记录等个人信息，提交 Issue 前请逐一检查并遮挡。\n'
                                 '文件不会自动上传。\n')
                archive.writestr('report.json', json.dumps(report, ensure_ascii=False, indent=2))
                for source in (self.stats_path, self.events_path):
                    if source.exists():
                        archive.write(source, source.name)
                total = 0
                for source in sorted(self.failures_dir.glob('*')):
                    if source.is_file() and source.suffix in ('.png', '.json'):
                        total += source.stat().st_size
                        if total > 200 * 1024 * 1024:
                            archive.writestr('LIMIT.txt', '证据超过 200 MiB，仅导出部分截图；完整记录在本地 failures 目录。')
                            break
                        archive.write(source, 'failures/' + source.name)
            return output.getvalue()
