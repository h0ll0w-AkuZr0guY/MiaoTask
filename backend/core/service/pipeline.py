"""Per-device activity plans; a running step always owns one application identity."""
import copy

from backend.core.config.loader import load_config, read_config
from backend.core.device.instances import InstanceMismatch
from backend.core.execution.stats import RunStats
from backend.core.runtime import assets_dir
from backend.core.registry import checked_id
from dataclasses import asdict


class ActivityPipeline:
    def _validate_plan(self, activities):
        if not isinstance(activities, list) or len(activities) > 16:
            raise ValueError('活动流水线最多选择 16 项')
        result, seen = [], set()
        for item in activities:
            if not isinstance(item, dict) or set(item) != {'app', 'activity'}:
                raise ValueError('活动选择必须包含 app 和 activity')
            registration = self.registry.get(checked_id(item['app']), checked_id(item['activity']))
            key = (registration.app.id, registration.activity.id)
            if key in seen:
                raise ValueError('活动流水线不能重复选择同一活动')
            if self.device is not None and self.device.device_type not in registration.activity.device_types:
                raise ValueError('活动不支持当前设备驱动：' + registration.activity.label)
            seen.add(key)
            result.append(dict(app=key[0], activity=key[1]))
        return result

    def _remember_instances(self, ids=None):
        if self.registration is None or self.device is None:
            return
        if ids is None:
            ids = [row['id'] for row in self.state['instance_queue'] if row.get('id')]
            ids = ids or [self.state['selected_instance']]
        if ids and all(isinstance(value, str) and value for value in ids):
            self._app_instance_plans[self.registration.app.id] = list(ids)

    def _activate_registration(self, registration, config):
        self.control.check()
        old_app = self.registration.app.id if self.registration else None
        if self.device is not None and old_app != registration.app.id:
            self._remember_instances()
            try:
                self.device.bind_application(registration.app.profile)
                known = {item.id: item for item in self.device.app_instances}
                preferred = self._app_instance_plans.get(registration.app.id)
                if preferred:
                    if any(value not in known for value in preferred):
                        raise InstanceMismatch('此应用之前选择的实例已失效，请重新选择；未切换其他账号')
                    self.device.select_app_instance(preferred[0])
                elif self.device.app_instances:
                    primary = next((item for item in self.device.app_instances if item.user_id == 0), None)
                    if primary is None and any(item.user_id is not None for item in self.device.app_instances):
                        raise InstanceMismatch('此应用没有默认主实例，请先明确选择应用分身')
                    self.device.select_app_instance((primary or self.device.app_instances[0]).id)
            except Exception:
                self.device = None
                self._preview_stop.set()
                if getattr(self, 'release_device', None):
                    self.release_device()
                self.update(connected=False, app_instances=[], selected_instance='')
                raise
        self.registration = registration
        with self._daily_lock:
            self.daily = None
            self.unavailable_tasks.clear()
        self.update(app=registration.app.id, activity=registration.activity.id, config=config,
                    tasks=[], stats=asdict(RunStats()), daily=None, task_scope='',
                    current_task='', remaining=0, last_failure=None, instance_queue=[],
                    app_instances=[item.to_dict() for item in self.device.app_instances] if self.device else [],
                    selected_instance=self.device.selected_instance.id if self.device and self.device.selected_instance else '')
        if self.device:
            self._ensure_daily()

    def _set_plan(self, activities):
        activities = self._validate_plan(activities)
        if activities:
            first = activities[0]
            registration = self.registry.get(first['app'], first['activity'])
            if self.registration is None or (self.state['app'], self.state['activity']) != (first['app'], first['activity']):
                self._activate_registration(registration, load_config(registry=self.registry, **first))
        self.update(activity_plan=activities, activity_pipeline=[],
                    status='ready' if self.device else 'disconnected', phase='ready',
                    message='已保存活动流水线，点击开始后按顺序执行' if activities else '未选择活动；请勾选需要执行的活动')

    def _run_activity_pipeline(self, action, payload, config):
        plan = self._validate_plan(payload.get('activities', self.state['activity_plan']))
        if not plan:
            raise ValueError('请至少选择一个已安装活动')
        if len(plan) > 1 and not payload.get('auto_open', True):
            raise ValueError('多个活动的流水线必须自动进入活动')
        rows = []
        for item in plan:
            registration = self.registry.get(**item)
            rows.append(dict(item, label=registration.activity.label, app_label=registration.app.label,
                             status='pending', stats={}))
        self.update(activity_plan=plan, activity_pipeline=rows)
        shared_keys = set(read_config(assets_dir() / 'backend/core/config/defaults.json'))
        shared_overrides = {key: copy.deepcopy(value) for key, value in config.items() if key in shared_keys}
        first_identity = (self.state['app'], self.state['activity'])
        for index, item in enumerate(plan):
            self.control.check()
            registration = self.registry.get(**item)
            settings = (config if index == 0 and (item['app'], item['activity']) == first_identity else
                        load_config(registry=self.registry, overrides=shared_overrides, **item))
            if (self.state['app'], self.state['activity']) != (item['app'], item['activity']):
                self._activate_registration(registration, settings)
            self.update(config=settings)
            self._activity_queue_status(index, 'running')
            current = dict(payload)
            ids = payload.get('instance_ids') if index == 0 else None
            if payload.get('all_instances', self.state.get('all_instances', False)):
                ids = [row['id'] for row in self.state['app_instances']]
                if not ids:
                    raise InstanceMismatch('未发现当前应用的可执行实例，请重新连接设备')
            elif 'all_instances' in payload or 'all_instances' in self.state:
                primary = next((row['id'] for row in self.state['app_instances'] if row.get('user_id') == 0), None)
                ids = [primary or self.state['selected_instance'] or None]
            if ids is not None and (not isinstance(ids, list) or not 1 <= len(ids) <= 16 or
                                    any(not isinstance(value, str) or not value for value in ids) or
                                    len(set(ids)) != len(ids)):
                raise ValueError('请选择有效且不重复的应用实例')
            ids = ids or self._app_instance_plans.get(item['app']) or [self.state['selected_instance'] or None]
            if self.state['app_instances']:
                known = {row['id'] for row in self.state['app_instances']}
                if any(value not in known for value in ids):
                    raise InstanceMismatch('活动流水线中的应用实例已失效，请重新选择；未切换其他账号')
            self._remember_instances(ids)
            current['instance_ids'] = ids
            self._execute_current_activity(action, current, settings)
            stats = {key: sum(row.get('stats', {}).get(key, 0) for row in self.state['instance_queue'])
                     for key in asdict(RunStats())}
            self._activity_queue_status(index, 'finished', stats)

    def _activity_queue_status(self, index, status, stats=None):
        with self.lock:
            if index < len(self.state['activity_pipeline']):
                row = self.state['activity_pipeline'][index]
                row['status'] = status
                if stats is not None:
                    row['stats'] = stats

    def _finish_activity_pipeline(self, status):
        with self.lock:
            for row in self.state['activity_pipeline']:
                if row['status'] == 'running':
                    row['status'] = status

    def rebind_registry(self, registry):
        """The hub calls this only while every device worker is idle."""
        previous = (self.state['app'], self.state['activity'])
        old_plan = self.state['activity_plan']
        self.registry = registry
        available = {(row['id'], activity['id']) for row in registry.catalog() for activity in row['activities']}
        plan = [item for item in old_plan if (item['app'], item['activity']) in available]
        if previous in available:
            self.registration = registry.get(*previous)
            shared_keys = set(read_config(assets_dir() / 'backend/core/config/defaults.json'))
            overrides = {key: copy.deepcopy(value) for key, value in self.state['config'].items() if key in shared_keys}
            config = load_config(registry=registry, app=previous[0], activity=previous[1], overrides=overrides)
            with self._daily_lock:
                self.daily = None
                self.unavailable_tasks.clear()
            self.update(config=config, tasks=[], task_scope='', daily=None, current_task='',
                        stats=asdict(RunStats()), instance_queue=[], remaining=0, last_failure=None)
            if self.device:
                self._ensure_daily()
        else:
            self.device = None
            self._preview_stop.set()
            if getattr(self, 'release_device', None):
                self.release_device()
            self.registration = registry.get() if registry.has_activities else None
            config = load_config(registry=registry)
            with self._daily_lock:
                self.daily = None
                self.unavailable_tasks.clear()
            self.update(app=self.registration.app.id if self.registration else '',
                        activity=self.registration.activity.id if self.registration else '',
                        config=config, connected=False, device={}, daily=None, task_scope='',
                        app_instances=[], selected_instance='', tasks=[], instance_queue=[],
                        stats=asdict(RunStats()), current_task='', remaining=0)
            self.image = None
            if not previous[0] and self.registration:
                plan = [dict(app=self.registration.app.id, activity=self.registration.activity.id)]
        self.update(catalog=registry.catalog(), activity_plan=plan, activity_pipeline=[],
                    message='活动目录已更新，可选择活动后连接设备',
                    status='ready' if self.device else 'disconnected')
