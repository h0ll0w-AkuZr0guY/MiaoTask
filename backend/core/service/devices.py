"""One loopback service, independent device workers and exclusive physical leases."""
import copy
import threading
import time
import uuid

from backend.core.context import workspace_scope
from backend.core.service.activity_hub import ActivityCatalogHub


class DeviceHub(ActivityCatalogHub):
    limit = 8

    def __init__(self, config=None, registry=None, app=None, activity=None):
        self.lock = threading.RLock()
        self.controllers = {}
        self.leases = {}
        self.closing = False
        self._init_activity_catalog()
        self.defaults = dict(config=config, registry=registry, app=app, activity=activity)
        self.add('设备 1', key='default')

    def add(self, label='', key=None):
        from backend.core.service.panel import Controller
        if not isinstance(label, str) or len(label) > 60:
            raise ValueError('设备名称应不超过 60 个字符')
        with self.catalog_gate, self.lock:
            if self.plugin_busy:
                raise ValueError('活动包正在更新，请稍后添加设备')
            if self.closing or len(self.controllers) >= self.limit:
                raise ValueError('工作区已关闭或达到设备数量上限（8 台）')
            key = key or uuid.uuid4().hex
            controller = Controller(**copy.deepcopy({k:v for k,v in self.defaults.items() if k != 'registry'}),
                                    registry=self.defaults.get('registry'))
            controller.workspace_id = key
            controller.workspace_label = label.strip() or f'设备 {len(self.controllers)+1}'
            controller.claim_device = lambda kind, serial: self.claim(key, kind, serial)
            controller.release_device = lambda: self.release(key)
            controller.catalog_gate = self.catalog_gate
            controller.catalog_busy = lambda: self.plugin_busy
            self.controllers[key] = controller
            return key

    def resolve(self, key=None):
        with self.lock:
            key = key or next(iter(self.controllers))
            if key not in self.controllers:
                raise ValueError('设备工作区不存在，请重新选择')
            return self.controllers[key]

    @property
    def thread(self):
        # Compatibility for callers that inspect the original default workspace.
        return self.resolve().thread

    def claim(self, owner, kind, serial):
        from backend.core.device.discovery import android_identity
        identity = android_identity(serial) if kind == 'uiautomator2' else serial
        key = (kind, identity or serial)
        with self.lock:
            if self.closing:
                raise ValueError('助手正在退出')
            held = self.leases.get(key)
            if held and held != owner:
                raise ValueError('同一台设备已在其他工作区连接，请先移除原工作区；USB、Wi-Fi 和模拟器别名不能重复控制')
            self.leases[key] = owner
        return identity

    def release(self, owner):
        with self.lock:
            self.leases = {key:value for key,value in self.leases.items() if value != owner}

    def summaries(self):
        with self.lock:
            rows = list(self.controllers.items())
        result = []
        for key, controller in rows:
            state = controller.snapshot()
            daily_stats = (state.get('daily') or {}).get('stats') or state['stats']
            result.append(dict(id=key, label=controller.workspace_label, status=state['status'],
                               connected=state['connected'], busy=bool(controller.thread and controller.thread.is_alive()),
                               message=state['message'], stats=daily_stats, round_stats=state['stats'], display=state.get('display'),
                               app=state['app'], activity=state['activity']))
        return result

    def remove(self, key):
        if not isinstance(key,str) or not key:
            raise ValueError('请选择要移除的设备工作区')
        with self.catalog_gate, self.lock:
            if self.plugin_busy:
                raise ValueError('活动包正在更新，请稍后移除设备')
            controller = self.resolve(key)
            if len(self.controllers) == 1:
                raise ValueError('请至少保留一个设备工作区')
            if controller.thread and controller.thread.is_alive():
                raise ValueError('请先停止该设备的操作，再移除')
            # A request that resolved this workspace before removal must not start it.
            with controller.lock:
                controller.closed = True
                controller._preview_stop.set()
            del self.controllers[key]
            self.release(key)
        controller._persist_state()

    def batch(self, payload):
        keys, action = payload.get('devices'), payload.get('action', 'run')
        if (not isinstance(keys, list) or not keys or len(keys) > self.limit or
                any(not isinstance(key,str) for key in keys) or len(set(keys)) != len(keys)):
            raise ValueError('请选择不重复的设备工作区')
        if action not in ('run','scan','open_app','pause','resume','stop'):
            raise ValueError('不支持的批量操作')
        # Resolve every ID first. Offline/busy devices return their own failures;
        # successful workers start immediately and do not wait for other devices.
        targets = [(key,self.resolve(key)) for key in keys]
        results = []
        for key, controller in targets:
            try:
                controller.dispatch(action, dict(config=payload.get('config', {}),
                                                 auto_open=payload.get('auto_open', True),
                                                 app=payload.get('app', controller.state['app']),
                                                 activity=payload.get('activity', controller.state['activity']),
                                                 **({'activities':payload['activities']} if 'activities' in payload else {})))
                results.append(dict(id=key, ok=True))
            except (ValueError, TypeError) as exc:
                results.append(dict(id=key, ok=False, error=str(exc)))
        return results

    def log(self, level, message):
        key = workspace_scope.get()
        with self.lock:
            controller = self.controllers.get(key)
        if controller:
            controller.log(level, message)

    def stop_all(self):
        with self.lock:
            self.closing = True
            controllers = list(self.controllers.values())
        for controller in controllers:
            with controller.lock:
                controller.closed = True
                controller.control.stopped.set()
                controller._preview_stop.set()
                controller.update(status='stopping', message='正在停止任务并退出助手')

    def close(self, timeout=25):
        self.stop_all()
        deadline = time.monotonic() + timeout
        if self.plugin_thread and self.plugin_thread is not threading.current_thread():
            self.plugin_thread.join(max(0, deadline-time.monotonic()))
        for controller in list(self.controllers.values()):
            if controller.thread and controller.thread is not threading.current_thread():
                controller.thread.join(max(0, deadline-time.monotonic()))
            controller._persist_state()
