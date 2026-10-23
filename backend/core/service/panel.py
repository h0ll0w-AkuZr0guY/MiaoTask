"""Loopback-only control panel; a single background worker owns the phone."""
import copy
from contextlib import nullcontext
from dataclasses import asdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import logging
import socket
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import urlsplit, parse_qs
import webbrowser

from backend.core.registry import create_runner as CampaignRunner, default_registry
from backend.core.config.loader import load_config
from backend.core.config.schema import validate_config
from backend.core.errors import NeedsAttention, Stopped
from backend.core.execution.run_control import RunControl
from backend.core.execution.stats import RunStats
from backend.core.storage.namespace import StorageNamespace
from backend.core.service.security import allowed_host, allowed_write
from backend.core.service.static import static_response
from backend.core.storage.daily_state import DailyState, local_day
from backend.core.device.instances import AppInstance, InstanceMismatch
from backend.core.service.notify import notify_security_attention
from backend.core.runtime import data_dir, version
from backend.core.service.branding import PUBLIC_ICON_FILES
from backend.core.context import device_scoped
from backend.core.service.pipeline import ActivityPipeline

logger = logging.getLogger(__name__)
from backend.core.runtime import ROOT


class LocalHTTPServer(ThreadingHTTPServer):
    # Windows SO_REUSEADDR permits two listeners on the same address. Use an
    # exclusive bind so a second launch cannot create a second device worker.
    allow_reuse_address = False

    def server_bind(self):
        if sys.platform == 'win32' and hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def discover_devices():
    from backend.core.device.discovery import AndroidConnectionError, list_android_devices

    devices, errors = [], []
    try:
        for device in list_android_devices():
            devices.append(dict(serial=device['serial'], type='uiautomator2',
                                status=device['state'], model=device['model']))
    except AndroidConnectionError as exc:
        errors.append(f'adb: {exc}')
    # Discovery shares the connection helper's exact Android executable and
    # state parser. It does not perform the connection-only backend restart or
    # wait for authorization; an unauthorized phone remains selectable here.
    for tool, args, kind in [('hdc', ['list', 'targets'], 'hmdriver2')]:
        binary = shutil.which(tool)
        if not binary:
            continue
        try:
            result = subprocess.run([binary, *args], capture_output=True, text=True, timeout=8,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                errors.append(f'{tool}: {result.stderr.strip() or result.stdout.strip()}')
            for line in result.stdout.splitlines():
                fields = line.split()
                if not fields or line.startswith(('List ', '*', '[Empty]')):
                    continue
                if len(fields) == 1:
                    devices.append(dict(serial=fields[0], type=kind, status='device', model='HarmonyOS'))
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f'{tool}: {exc}')
    return dict(devices=devices, errors=errors)


class PanelLogHandler(logging.Handler):
    def __init__(self, controller):
        super().__init__(logging.INFO)
        self.controller = controller

    def emit(self, record):
        if record.name.startswith('backend'):
            self.controller.log(record.levelname, record.getMessage())


from backend.core.service.preview import PreviewWorker

class Controller(ActivityPipeline, PreviewWorker):
    def __init__(self, config=None, registry=None, app=None, activity=None):
        self.registry = registry or default_registry()
        self.registration = self.registry.get(app, activity) if self.registry.has_activities else None
        self._app_instance_plans = {}
        self.lock = threading.RLock()
        self.device = None
        self.control = RunControl()
        self.thread = None
        self.image = None
        self.sequence = 0
        self.daily = None
        self._daily_lock = threading.RLock()
        self._preview_thread = None
        self._preview_stop = threading.Event()
        self._preview_last = 0.0
        self._last_client_seen = time.monotonic()
        self._round_last_stats = {}
        self._last_failure_stamp = 0.0
        # The same dictionary remains attached to an active runner across a day
        # boundary. Its contents come from this resolved device and local date.
        self.unavailable_tasks = {}
        from backend.core.execution.task_policy import TaskPolicy
        self.task_policy = TaskPolicy()
        self.state = dict(status='disconnected', phase='idle', connected=False,
                          message='连接手机后，点击开始执行自动进入活动并识别任务', package='', source='',
                          current_task='', remaining=0, stats=asdict(RunStats()), tasks=[],
                          config=config or load_config(registry=self.registry, app=self.registration.app.id if self.registration else None, activity=self.registration.activity.id if self.registration else None), device={}, logs=[], updated_at='',
                          daily=None, task_scope='', last_failure=None, image_sequence=0, image_updated_at='',
                          app_instances=[], selected_instance='', instance_warnings=[], instance_queue=[],
                          app=self.registration.app.id if self.registration else '',
                          activity=self.registration.activity.id if self.registration else '',
                          catalog=self.registry.catalog(),
                          activity_plan=[dict(app=self.registration.app.id, activity=self.registration.activity.id)] if self.registration else [],
                          activity_pipeline=[])

    def log(self, level, message):
        with self.lock:
            self.sequence += 1
            self.state['logs'].append(dict(id=self.sequence, time=datetime.now().astimezone().isoformat(timespec='seconds'),
                                            level=level, message=message))
            self.state['logs'] = self.state['logs'][-600:]
        with self._daily_lock:
            if self.daily:
                self.daily.log(level, message)

    def update(self, **event):
        with self.lock:
            self.state.update(event)
            self.state['updated_at'] = datetime.now().astimezone().isoformat(timespec='seconds')

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.state)


    def _ensure_daily(self):
        """Called outside controller.lock; bind only after the adapter resolves serial."""
        with self._daily_lock:
            device = self.device
            if device is None:
                return False
            if isinstance(getattr(device, 'app_instances', None), list) and not device.app_instances:
                self.daily = None
                self.unavailable_tasks.clear()
                self.update(daily=None, task_scope='')
                return False
            date = local_day()
            identity = getattr(device, 'identity_serial', None)
            if not isinstance(identity, str) or not identity:
                identity = device.serial
            target = getattr(device, 'selected_instance', None)
            instance_id = target.id if isinstance(target, AppInstance) else ''
            if (self.daily is not None and self.daily.date == date
                    and self.daily.serial == identity and self.daily.device_type == device.device_type
                    and self.daily.instance_id == instance_id
                    and self.daily.app_id == (self.registration.app.id if self.registration else '')
                    and self.daily.activity_id == (self.registration.activity.id if self.registration else '')):
                self.daily.redaction_values.add(device.serial)
                return False
            self.daily = DailyState(data_dir(), identity, device.device_type, date, instance_id=instance_id, namespace=StorageNamespace(
                self.registration.app.id, self.registration.activity.id, self.registration.activity.legacy_storage,
                self.registration.app.profile.legacy_primary_instances) if self.registration else None)
            self.daily.redaction_values.add(device.serial)
            self.unavailable_tasks.clear()
            self.unavailable_tasks.update(self.daily.unavailable_tasks())
            self.task_policy.bind(self.daily)
            self.update(daily=self.daily.snapshot(), task_scope=uuid.uuid4().hex)
            if self.daily.recovery_warning:
                self.log('WARNING', self.daily.recovery_warning)
            return True

    def _summary_state(self):
        with self.lock:
            return {key: copy.deepcopy(self.state[key]) for key in
                    ('status', 'phase', 'message', 'current_task', 'remaining', 'package', 'updated_at')}

    def _persist_state(self):
        self._ensure_daily()
        with self._daily_lock:
            if self.daily:
                self.daily.save_state(self._summary_state(), self.unavailable_tasks)
                self.update(daily=self.daily.snapshot())

    def _runner_event(self, session_id, count_stats, **event):
        previous = self._round_last_stats.get(session_id, {})
        self._ensure_daily()
        self.update(**event)
        if 'stats' in event:
            with self.lock:
                for item in self.state['instance_queue']:
                    if item['status'] == 'running':
                        item['stats'] = copy.deepcopy(event['stats'])
        with self._daily_lock:
            if self.daily:
                summary = self.daily.progress(
                    session_id, event.get('stats', previous), tasks=event.get('tasks'),
                    unavailable_tasks=self.unavailable_tasks, count_stats=count_stats,
                    # A pause/diagnostic export may have switched stores before
                    # this event. The store uses its own watermark when present.
                    baseline=previous, state=self._summary_state())
                self.update(daily=summary)
        if 'stats' in event:
            self._round_last_stats[session_id] = copy.deepcopy(event['stats'])

    def on_failure(self, event=None, **details):
        """Campaign callback: retain evidence without taking the panel state lock."""
        event = dict(event or {}, **details)
        image = event.pop('image', None)
        with self.lock:
            previous = self.state['last_failure']
        if (previous and previous.get('reason') == event.get('reason')
                and time.monotonic() - self._last_failure_stamp < 3
                and (not event.get('task') or previous.get('task') == event.get('task'))):
            # The runner reports a guard with task/phase evidence before raising
            # NeedsAttention. The outer exception handler must not save it twice.
            return
        self._ensure_daily()
        if not self.daily:
            return
        if image is None and self.device is not None:
            try:
                image = self.device.screenshot()
            except Exception as exc:
                event['capture_error'] = f'{type(exc).__name__}: {exc}'
        with self._daily_lock:
            evidence = self.daily.failure(event, image)
            self.update(last_failure=evidence, daily=self.daily.snapshot())
            self._last_failure_stamp = time.monotonic()



    def diagnostics(self):
        if self.device is None:
            raise ValueError('请先连接设备，再导出当天诊断记录')
        self._persist_state()
        with self._daily_lock:
            if not self.daily:
                raise ValueError('请先连接设备，再导出当天诊断记录')
            return self.daily.diagnostics(self.snapshot())

    def _task_action(self, action, payload):
        """Change queue eligibility; never start a worker or use cached bounds."""
        self._ensure_daily()
        with self._daily_lock:
            with self.lock:
                if getattr(self, 'closed', False) or not self.device or not self.daily:
                    raise ValueError('请先连接设备并读取任务')
                if payload.get('task_scope') != self.state['task_scope']:
                    raise ValueError('设备、实例或日期已变化，请刷新任务列表')
                identity = payload.get('task_id')
                rows = [row for row in self.state['tasks'] if row.get('task_id') == identity]
                if len(rows) != 1:
                    raise ValueError('任务已更新，请重新读取任务列表')
                row = rows[0]
                if (not row.get('controllable') or row.get('status') == 'done'
                        or row.get('current', 0) >= row.get('total', 1)):
                    raise ValueError('该任务已完成或需要人工处理')
                busy = bool(self.thread and self.thread.is_alive())
                if busy and self.state['status'] not in ('running', 'paused'):
                    raise ValueError('请等待设备连接或任务识别结束')
                # Do not override disabled task types, app facts or security policy.
                if action == 'task_execute' and (row['kind'] not in self.state['config']['enabled_task_kinds']
                        or row['kind'] == 'claim' and not self.state['config']['claim_rewards']
                        or row.get('target_app') and not self.state['config']['allow_cross_app']):
                    raise ValueError('请先在任务设置中启用该任务类型')
                from backend.core.models import normalize
                unavailable_key = 'app:' + row['target_app'] if row.get('target_app') else 'task:' + normalize(row['title'])
                if action == 'task_execute' and self.unavailable_tasks.get(unavailable_key):
                    raise ValueError('该任务已确认不可用；应用安装情况变化后请重新开始识别')
            # Persist without holding the UI state lock. The daily binding stays
            # stable until this intent has been written to the right store.
            self.task_policy.choose(identity, action == 'task_skip')
            with self.lock:
                row['enabled'] = action == 'task_execute'
                if row.get('status') != 'running':
                    row.update(status='disabled' if action == 'task_skip' else 'pending',
                               reason='已跳过：用户选择今日跳过' if action == 'task_skip' else '已加入可执行队列')

    def _connection_status(self, message):
        self.control.check()
        self.update(status='connecting', phase='connecting', message=str(message))
        self.log('INFO', str(message))

    def dispatch(self, action, payload):
        with getattr(self, 'catalog_gate', nullcontext()):
            if getattr(self, 'catalog_busy', lambda: False)() and action not in ('pause', 'resume', 'stop'):
                raise ValueError('活动包正在更新，请稍后再操作设备')
            return self._dispatch(action, payload)

    def _dispatch(self, action, payload):
        if action in ('task_skip', 'task_execute'):
            return self._task_action(action, payload)
        control_action = action in ('pause', 'resume', 'stop')
        with self.lock:
            if getattr(self, 'closed', False):
                raise ValueError('设备工作区已关闭')
            if control_action:
                if not (self.thread and self.thread.is_alive()):
                    raise ValueError('当前没有正在执行的操作')
                if action == 'pause':
                    if self.state['status'] != 'running':
                        raise ValueError('只有执行任务时可以暂停')
                    self.control.paused.set()
                    self.update(status='paused', message='已暂停；可以手动操作手机，再继续执行')
                elif action == 'resume':
                    if self.state['status'] != 'paused':
                        raise ValueError('当前任务未暂停')
                    self.control.paused.clear()
                    self.update(status='running', message='继续执行')
                else:
                    self.control.stopped.set()
                    self.update(status='stopping', message='正在停止，当前设备调用返回后结束')
            else:
                if self.thread and self.thread.is_alive():
                    raise ValueError('设备正在执行操作，请先停止当前任务')
                if action == 'select_pipeline':
                    activities = self._validate_plan(payload.get('activities'))
                    self.control = RunControl()
                    self.update(status='scanning', message='正在保存活动流水线')
                    self.thread = threading.Thread(target=self._change_plan, args=(activities,), daemon=True)
                    self.thread.start()
                    return
                if action == 'select_activity':
                    registration = self.registry.get(payload.get('app'), payload.get('activity'))
                    config = load_config(registry=self.registry, app=registration.app.id, activity=registration.activity.id)
                    self.control = RunControl()
                    self.update(status='scanning', message='正在切换活动')
                    self.thread = threading.Thread(target=self._change_activity, args=(registration, config), daemon=True)
                    self.thread.start()
                    return
                if not self.registration and action not in ('configure', 'wifi_pair'):
                    raise ValueError('未注册活动，请先安装活动包')
                planned_run = action == 'run'
                if planned_run:
                    activities = self._validate_plan(payload.get('activities', self.state['activity_plan']))
                    if not activities:
                        raise ValueError('请至少选择一个已安装活动')
                    payload = dict(payload, activities=activities)
                    if len(activities) > 1 and not payload.get('auto_open', True):
                        raise ValueError('多个活动的流水线必须自动进入活动')
                if (self.registration and not planned_run and
                        (payload.get('app', self.registration.app.id) != self.registration.app.id
                         or payload.get('activity', self.registration.activity.id) != self.registration.activity.id)):
                    raise ValueError('请先选择对应应用和活动')
                if action not in ('connect', 'wifi_pair', 'wifi_connect', 'scan', 'run', 'open_app', 'capture', 'configure', 'select_instance', 'refresh_instances'):
                    raise ValueError('未知操作')
                if action in ('wifi_pair', 'wifi_connect'):
                    if self.registration is None and payload.get('connect_address'):
                        raise ValueError('请先安装并选择活动，再连接设备')
                    from backend.core.device.wifi import endpoint, pairing_code
                    address = payload.get('connect_address')
                    serial = endpoint(address) if action == 'wifi_connect' or address else None
                    payload = dict(payload, serial=serial, device_type='uiautomator2')
                    if action == 'wifi_pair':
                        payload['pair_address'] = endpoint(payload.get('pair_address'))
                        payload['pair_code'] = pairing_code(payload.get('pair_code'))
                if action not in ('connect', 'wifi_pair', 'wifi_connect', 'configure') and self.device is None:
                    raise ValueError('请先连接设备')
                config = copy.deepcopy(self.state['config'])
                overrides = payload.get('config', {})
                if not isinstance(overrides, dict) or set(overrides) - set(config):
                    raise ValueError('配置字段无效')
                config.update(overrides)
                validate_config(config)
                defer_instances = planned_run and (len(activities) > 1 or activities[0] != dict(app=self.state['app'], activity=self.state['activity']))
                if not defer_instances and action in ('run', 'scan', 'select_instance') and isinstance(getattr(self.device, 'app_instances', None), list) and not self.state['app_instances']:
                    raise ValueError('未发现可用应用实例，请安装应用或刷新实例列表后重试')
                if action in ('run', 'scan') and not isinstance(payload.get('auto_open', True), bool):
                    raise ValueError('auto_open 必须为布尔值')
                if not defer_instances and action in ('run', 'scan', 'select_instance') and self.state['app_instances']:
                    known = {item['id'] for item in self.state['app_instances']}
                    ids = payload.get('instance_ids', [payload.get('instance_id') or self.state['selected_instance']])
                    if (not isinstance(ids, list) or not 1 <= len(ids) <= 16
                            or any(not isinstance(i, str) or i not in known for i in ids)
                            or len(ids) != len(set(ids))):
                        raise ValueError('请选择有效且不重复的应用实例')
                    if action != 'run' and len(ids) != 1:
                        raise ValueError('本操作只能选择一个实例')
                    if len(ids) > 1 and not payload.get('auto_open', True):
                        raise ValueError('顺序执行多个实例必须使用自动进入活动')
                    payload = dict(payload, instance_ids=ids)
                if action == 'configure':
                    self.update(config=config)
                else:
                    self.control = RunControl()
                    self.update(config=config, status={'connect': 'connecting', 'wifi_pair': 'connecting', 'wifi_connect': 'connecting', 'scan': 'scanning',
                                                      'run': 'running', 'capture': 'scanning',
                                                      'open_app':'scanning',
                                                      'select_instance': 'scanning', 'refresh_instances': 'scanning'}[action],
                                message='正在连接手机' if action in ('connect', 'wifi_pair', 'wifi_connect') else '正在读取手机页面')
                    self.thread = threading.Thread(target=self._work, args=(action, payload, config), daemon=True,
                                                   name='miao-device-worker')
                    self.thread.start()
        # Disk and device operations must never run under the UI state lock.
        if control_action:
            self.log('INFO', {'pause': '用户暂停', 'resume': '用户继续', 'stop': '用户停止'}[action])
            self._persist_state()
        elif action == 'configure':
            self.log('INFO', '执行设置已应用')

    @device_scoped
    def _work(self, action, payload, config):
        self._last_failure_stamp = 0.0
        try:
            if action == 'wifi_pair':
                from backend.core.device.wifi import pair_wifi
                try:
                    pair_wifi(payload['pair_address'], payload.pop('pair_code'), self._connection_status)
                finally:
                    payload.pop('pair_code', None)
                self.control.check()
                self.update(wifi_paired=True)
                if not payload.get('serial'):
                    self.update(status='ready' if self.device else 'disconnected', phase='ready' if self.device else 'idle',
                                message='配对成功；关闭手机配对弹窗，填写无线调试主页面的连接地址，再点击连接')
                    return
            if action in ('connect', 'wifi_pair', 'wifi_connect'):
                from backend.core.device.adapter import DeviceAdapter
                if getattr(self, 'release_device', None):
                    self.release_device()
                # A failed attempt to choose another phone must not leave the
                # previous phone looking connected or export its diagnostics.
                self.device = None
                self._app_instance_plans.clear()
                with self._daily_lock:
                    self.daily = None
                    self.unavailable_tasks.clear()
                with self.lock:
                    self.image = None
                    self.state['image_sequence'] += 1
                    self.state['image_updated_at'] = ''
                    self.state['logs'] = []
                self.update(connected=False, device={}, daily=None, last_failure=None,
                            package='', source='', tasks=[], stats=asdict(RunStats()),
                            current_task='', remaining=0, phase='connecting', app_instances=[],
                            selected_instance='', instance_warnings=[], instance_queue=[])
                kind = payload.get('device_type', 'uiautomator2')
                serial = payload.get('serial') or None
                if kind not in ('uiautomator2', 'hmdriver2') or (serial and (not isinstance(serial, str) or len(serial) > 128)):
                    raise ValueError('设备类型或序列号无效')
                if action in ('wifi_pair', 'wifi_connect'):
                    from backend.core.device.wifi import connect_wifi
                    self.control.check()
                    serial = connect_wifi(serial, self._connection_status)
                options = dict(on_status=self._connection_status, application=self.registration.app.profile)
                if getattr(self, 'claim_device', None):
                    options['on_resolved'] = self.claim_device
                device = DeviceAdapter(kind, serial, **options)
                info = device.current_app()
                self.control.check()
                self.on_image(device.screenshot())
                width, height = device.display_size
                self.device = device
                device.preview_managed = True
                self._ensure_daily()
                self.update(connected=True, device=dict(type=kind, serial=device.serial or '自动选择'),
                            display=dict(width=width, height=height),
                            package=info.get('package', ''), status='ready', phase='ready',
                            app_instances=[i.to_dict() for i in device.app_instances],
                            selected_instance=device.selected_instance.id if device.selected_instance else '',
                            instance_warnings=device.instance_warnings,
                            tasks=[], message='设备已连接，请确认应用实例后开始自动执行')
                self._preview_stop.clear()
                self._start_preview()
                self._instance_availability('设备已连接，请确认应用实例后开始自动执行')
                self.log('INFO', f'设备连接成功：{device}')
            elif action == 'open_app':
                self.control.check()
                with self.device.instance_session():
                    self.device.resume_app(self.registration.app.profile.package_for(self.device.device_type))
                    self.control.wait(.5)
                    info = self.device.current_app()
                    self.on_image(self.device.screenshot())
                self.update(status='ready', phase='ready', package=info.get('package',''),
                            message='已唤醒所选应用；未进行登录、授权或任务操作')
            elif action == 'capture':
                self.control.check()
                self.on_image(self.device.screenshot())
                self.update(status='ready', message='手机画面已刷新')
            elif action == 'refresh_instances':
                self.control.check()
                instances = self.device.refresh_app_instances()
                self._ensure_daily()
                self.update(app_instances=instances,
                            selected_instance=self.device.selected_instance.id if self.device.selected_instance else '',
                            instance_warnings=self.device.instance_warnings, instance_queue=[],
                            status='ready', phase='ready', tasks=[], current_task='', stats=asdict(RunStats()),
                            message='实例列表已刷新；未读取任何账号登录信息')
                self._instance_availability('实例列表已刷新；未读取任何账号登录信息')
            elif action == 'select_instance':
                self._select_instance(payload['instance_ids'][0])
                self._remember_instances(payload['instance_ids'])
                self.update(status='ready', phase='ready', instance_queue=[], message='已选择实例，开始后才会打开对应应用')
            else:
                if action == 'run':
                    self._run_activity_pipeline(action, payload, config)
                else:
                    self._execute_current_activity(action, payload, config)
        except Stopped:
            self._finish_active_queue('stopped')
            self._finish_activity_pipeline('stopped')
            self.interrupt_records('已停止，本次进度尚未核验')
            self.update(status='stopped', phase='stopped', message='已停止，可从当前页面重新开始', remaining=0)
        except (NeedsAttention, InstanceMismatch) as exc:
            self._finish_active_queue('needs_attention')
            self._finish_activity_pipeline('needs_attention')
            self.interrupt_records('需要手动处理后重新核验')
            self.log('WARNING', str(exc))
            self.update(status='needs_attention', phase='needs_attention', message=str(exc), remaining=0)
            if not exc.evidence_saved:
                self.on_failure(dict(reason=str(exc), phase='needs_attention', kind=exc.kind))
            if exc.kind == 'security':
                notify_security_attention()
        except Exception as exc:
            self._finish_active_queue('error')
            self._finish_activity_pipeline('error')
            self.interrupt_records('执行中断，本次进度尚未核验')
            logger.exception('设备操作失败')
            self.update(status='error', phase='connect_failed' if action in ('connect', 'wifi_pair', 'wifi_connect') else 'error',
                        message=f'{type(exc).__name__}: {exc}', remaining=0)
            self.on_failure(dict(reason=f'{type(exc).__name__}: {exc}', phase='error', kind='exception'))
        finally:
            payload.pop('pair_code', None)
            if action in ('connect','wifi_pair','wifi_connect') and self.device is None and getattr(self, 'release_device', None):
                self.release_device()
            if action in ('run', 'scan') and self.device is not None and not self.control.stopped.is_set():
                try:
                    self.on_image(self.device.screenshot())
                except Exception:
                    logger.debug('刷新预览失败', exc_info=True)
            if self.device is not None:
                try:
                    self._persist_state()
                    self._save_report()
                except Exception:
                    logger.exception('保存本地当天记录失败')

    def _instance_availability(self, ready_message):
        warnings = list(getattr(self.device, 'instance_warnings', ()) or ())
        if not self.state['app_instances'] or not self.state['selected_instance']:
            label = self.registration.app.label if self.registration else '当前应用'
            message = (f'设备已连接，但未检测到可执行的{label}实例；请确认应用已安装及调试权限，点击「刷新实例」重试'
                       if not self.state['app_instances'] else
                       '原选择的应用实例已失效，未自动切换其他账号；请展开「应用分身」重新选择')
            if not warnings:
                warnings = ['未核验到可执行应用实例，未自动选择主应用或分身']
            self.update(status='needs_attention', phase='instance_missing', message=message,
                        selected_instance='', instance_warnings=warnings, tasks=[], task_scope='')
        else:
            self.update(status='ready', phase='ready', message=ready_message, instance_warnings=warnings)
        for warning in warnings:
            self.log('WARNING', warning)

    def _execute_current_activity(self, action, payload, config):
        auto_open = payload.get('auto_open', True)
        if not isinstance(auto_open, bool):
            raise ValueError('auto_open 必须为布尔值')
        ids = payload.get('instance_ids') or [self.state['selected_instance'] or None]
        known = {i['id']: i for i in self.state['app_instances']}
        self.update(instance_queue=[dict(id=i, label=known.get(i, {}).get('label', '当前应用'),
                                         status='pending', stats={}) for i in ids])
        for instance_id in ids:
            self.control.check()
            if instance_id:
                self._select_instance(instance_id)
            self._ensure_daily()
            self._queue_status(instance_id, 'running')
            session_id = uuid.uuid4().hex
            self._round_last_stats[session_id] = {}
            self.update(stats=asdict(RunStats()), status='running' if action == 'run' else 'scanning')
            runner = CampaignRunner(
                self.device, config, self.control,
                lambda **event: self._runner_event(session_id, action == 'run', **event), self.on_image,
                unavailable_tasks=self.unavailable_tasks, on_failure=self.on_failure,
                task_policy=self.task_policy,
                registry=self.registry, app=self.registration.app.id, activity=self.registration.activity.id)
            self.log('INFO', ('开始识别任务' if action == 'scan' else '开始执行任务') +
                     ('：' + known[instance_id]['label'] if instance_id in known else ''))
            context = self.device.instance_session() if instance_id else nullcontext()
            with context:
                if action == 'scan':
                    runner.scan(auto_open=auto_open)
                else:
                    runner.run(auto_open=auto_open)
            self._persist_state()
            self._queue_status(instance_id, 'finished')
            self._save_report()
        self.update(status='ready' if action == 'scan' else 'finished')

    def _select_instance(self, instance_id):
        self.control.check()
        target = self.device.select_app_instance(instance_id)
        self._ensure_daily()
        self.update(selected_instance=target.id, tasks=[], current_task='', remaining=0,
                    last_failure=None, stats=asdict(RunStats()))

    @device_scoped
    def _change_plan(self, activities):
        try:
            self.control.check()
            self._set_plan(activities)
        except Exception as exc:
            self.update(status='error', message=str(exc))
            self.log('WARNING', '保存活动流水线失败：' + str(exc))

    @device_scoped
    def _change_activity(self, registration, config):
        try:
            self._activate_registration(registration, config)
            self.update(activity_plan=[dict(app=registration.app.id, activity=registration.activity.id)],
                        activity_pipeline=[], status='ready' if self.device else 'disconnected',
                        phase='ready', message='已选择活动')
        except Exception:
            logger.exception('活动切换失败')
            self.device = None
            self.update(status='error', connected=False, app_instances=[], selected_instance='',
                        message='活动切换失败，请重新连接设备')

    def _queue_status(self, instance_id, status):
        with self.lock:
            for item in self.state['instance_queue']:
                if item['id'] == instance_id:
                    item['status'] = status

    def _finish_active_queue(self, status):
        with self.lock:
            for item in self.state['instance_queue']:
                if item['status'] == 'running':
                    item['status'] = status

    def interrupt_records(self, reason):
        with self.lock:
            for task in self.state['tasks']:
                if task.get('status') == 'running':
                    task.update(status='pending', reason=reason)

    def _save_report(self):
        folder = data_dir()
        report = self.snapshot()
        report.pop('config', None)
        from backend.core.storage.daily_state import _atomic_json
        _atomic_json(folder / 'last-run.json', report)


def make_handler(controller, token):
    root = controller
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def allowed_host(self):
            return allowed_host(self.headers, self.server.server_port)

        def reply(self, code, body, content_type='application/json; charset=utf-8', extra=None):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.allowed_host():
                self.reply(403, {'error': '只允许本机访问'})
                return
            path = urlsplit(self.path).path
            origin = f'http://127.0.0.1:{self.server.server_port}'
            if path == '/api/plugins' and hasattr(root, 'plugins_snapshot'):
                try:
                    refresh = parse_qs(urlsplit(self.path).query).get('refresh', ['0'])[0] == '1'
                    self.reply(200, root.plugins_snapshot(origin, refresh=refresh))
                except (ValueError, OSError) as exc:
                    self.reply(400, {'error': str(exc)})
                return
            if path == '/api/workspaces' and hasattr(root, 'summaries'):
                self.reply(200, dict(devices=root.summaries()))
                return
            try:
                controller = root.resolve(parse_qs(urlsplit(self.path).query).get('device', [None])[0]) if hasattr(root, 'resolve') else root
            except ValueError as exc:
                self.reply(404, {'error':str(exc)})
                return
            if path in ('/api/state', '/api/session/status'):
                controller._last_client_seen = time.monotonic()
                self.reply(200, dict(controller.snapshot(), token=token, version=version(),
                                     busy=bool(controller.thread and controller.thread.is_alive()),
                                     workspace_id=getattr(controller, 'workspace_id', 'default'),
                                     plugins_busy=getattr(root, 'plugin_busy', False),
                                     plugins_operation=copy.deepcopy(getattr(root, 'plugin_operation', {})),
                                     workspaces=root.summaries() if hasattr(root, 'summaries') else []))
            elif path == '/api/health':
                self.reply(200, dict(app='miao-task', version=version()))
            elif path == '/api/devices':
                self.reply(200, discover_devices())
            elif path == '/api/emulators':
                from backend.core.device.emulators import discover_emulators
                self.reply(200, discover_emulators())
            elif path == '/api/inventory':
                from backend.core.device.inventory import inventory
                self.reply(200, inventory())
            elif path == '/api/wifi/devices':
                from backend.core.device.wifi import discover_wifi
                try:
                    self.reply(200, discover_wifi())
                except ConnectionError as exc:
                    self.reply(200, dict(services=[], devices=[], errors=[str(exc)]))
            elif path in ('/api/screenshot', '/api/preview'):
                with controller.lock:
                    data = controller.image
                self.reply(200 if data else 404, data or b'', 'image/jpeg')
            elif path == '/api/report':
                self.reply(200, controller.snapshot(), extra={'Content-Disposition': 'attachment; filename="miaotask-report.json"'})
            elif path == '/api/diagnostics':
                try:
                    self.reply(200, controller.diagnostics(), 'application/zip',
                               extra={'Content-Disposition': 'attachment; filename="miao-diagnostics.zip"'})
                except ValueError as exc:
                    self.reply(400, {'error': str(exc)})
                except Exception:
                    logger.exception('导出当天诊断记录失败')
                    self.reply(500, {'error': '无法导出当天诊断记录，请检查本地数据目录是否可写'})
            elif path == '/api/stats':
                self.reply(200, dict(daily=controller.snapshot()['daily']))
            elif path == '/api/tasks':
                self.reply(200, dict(tasks=controller.snapshot()['tasks']))
            elif path == '/api/apps':
                self.reply(200, dict(apps=controller.registry.catalog()))
            else:
                result = static_response(path)
                if result:
                    self.reply(200, *result)
                else:
                    self.reply(404, {'error': '路径不存在'})

        def do_POST(self):
            if not allowed_write(self.headers, self.server.server_port, token):
                # Drain a small well-formed rejected request before closing the
                # socket. Windows may reset a connection with an unread body,
                # hiding the actionable 403 response from the browser.
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if 0 < size <= 32768:
                        self.connection.settimeout(1)
                        self.rfile.read(size)
                except (ValueError, OSError):
                    pass
                self.reply(403, {'error': '请求来源校验失败，请刷新本地控制台'})
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 32768:
                    raise ValueError('请求长度无效')
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise ValueError('请求必须为 JSON 对象')
                path = urlsplit(self.path).path
                if path == '/api/plugins/open-folder' and hasattr(root, 'open_activity_folder'):
                    folder = root.open_activity_folder(f'http://127.0.0.1:{self.server.server_port}')
                    self.reply(200, {'ok': True, 'path': folder})
                    return
                if path in ('/api/plugins/install', '/api/plugins/uninstall') and hasattr(root, 'change_plugin'):
                    root.change_plugin(path.rsplit('/', 1)[-1], payload,
                                       f'http://127.0.0.1:{self.server.server_port}')
                    self.reply(202, {'ok': True})
                    return
                key = None if path.startswith('/api/workspaces') or path == '/api/shutdown' else parse_qs(urlsplit(self.path).query).get('device', [None])[0]
                controller = root.resolve(key) if hasattr(root, 'resolve') else root
                if not path.startswith('/api/'):
                    self.reply(404, {'error': '路径不存在'})
                    return
                if path == '/api/shutdown':
                    if hasattr(root, 'stop_all'):
                        root.stop_all()
                    else:
                        controller.control.stopped.set()
                        controller._preview_stop.set()
                    self.reply(202, {'ok': True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                if hasattr(root, 'summaries') and path.startswith('/api/workspaces'):
                    if path == '/api/workspaces':
                        key = root.add(payload.get('label', ''))
                        self.reply(201, dict(id=key, devices=root.summaries()))
                    elif path == '/api/workspaces/remove':
                        root.remove(payload.get('id'))
                        self.reply(200, dict(ok=True, devices=root.summaries()))
                    elif path == '/api/workspaces/batch':
                        self.reply(202, dict(results=root.batch(payload)))
                    else:
                        self.reply(404, {'error':'路径不存在'})
                    return
                if path == '/api/export':
                    self.reply(200, controller.diagnostics(), 'application/zip')
                    return
                action = path.removeprefix('/api/')
                action = {'session/start': 'run', 'session/pause': 'pause', 'session/resume': 'resume', 'session/stop': 'stop',
                          'wifi/pair': 'wifi_pair', 'wifi/connect': 'wifi_connect'}.get(action, action)
                controller.dispatch(action, payload)
                self.reply(202, {'ok': True})
            except (ValueError, TypeError) as exc:
                self.reply(400, {'error': str(exc)})
            except Exception:
                logger.exception('控制台请求失败')
                self.reply(500, {'error': '请求失败，请查看运行日志'})
    return Handler


def serve(port=8765, open_browser=True, config=None, port_file=None, app=None, activity=None):
    from backend.core.service.devices import DeviceHub
    controller = DeviceHub(config, app=app, activity=activity)
    logging.getLogger().addHandler(PanelLogHandler(controller))
    handler = make_handler(controller, secrets.token_urlsafe(32))
    def existing_assistant():
        if not port:
            return False
        import urllib.request
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=1) as response:
                existing = json.load(response)
            return isinstance(existing, dict) and existing.get('app') == 'miao-task'
        except (OSError, ValueError):
            return False
    if existing_assistant():
        if open_browser:
            webbrowser.open(f'http://127.0.0.1:{port}')
        return controller
    try:
        server = LocalHTTPServer(('127.0.0.1', port), handler)
    except OSError as exc:
        if exc.errno not in (48, 98, 10048, 10013) and getattr(exc, 'winerror', None) not in (10048, 10013):
            raise
        # Double-clicking again opens the existing assistant, never another device worker.
        if existing_assistant():
            if open_browser:
                webbrowser.open(f'http://127.0.0.1:{port}')
            return controller
        server = LocalHTTPServer(('127.0.0.1', 0), handler)
    url = f'http://127.0.0.1:{server.server_port}'
    if port_file:
        Path(port_file).write_text(json.dumps({'url': url}), encoding='utf-8')
    if sys.stdout is not None:
        print(f'MiaoTask 已启动：{url}\n按 Ctrl+C 退出。')
    from backend.core.service.tray import WindowsTray
    def exit_service():
        controller.stop_all()
        server.shutdown()
    tray = WindowsTray(lambda: webbrowser.open(url), exit_service)
    try:
        tray_active = tray.start()
        controller.resolve().update(tray_enabled=tray_active)
        if open_browser:
            webbrowser.open(url)
        server.serve_forever(poll_interval=.3)
    except KeyboardInterrupt:
        controller.stop_all()
    finally:
        tray.stop()
        try:
            controller.close()
        except Exception:
            logger.exception('关闭控制台时保存当天记录失败')
        server.server_close()
    return controller
