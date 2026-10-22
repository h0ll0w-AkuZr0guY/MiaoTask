"""Service-wide activity installation gate and explicit loopback test source."""
import copy
import json
from pathlib import Path
import re
import threading
from urllib.parse import quote, unquote, urlsplit

from backend.core.plugins.contracts import MAX_CATALOG_BYTES


class LocalActivitySource:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        if not self.directory.is_dir() or not (self.directory / 'activity-catalog.json').is_file():
            raise ValueError('本地测试活动源必须包含 activity-catalog.json')

    def _catalog(self):
        path = self.directory / 'activity-catalog.json'
        if path.is_symlink() or path.stat().st_size > MAX_CATALOG_BYTES:
            raise ValueError('本地测试活动目录无效')
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(value, dict) or not isinstance(value.get('packages'), list):
            raise ValueError('本地测试活动目录格式无效')
        return value

    @staticmethod
    def _filename(item):
        if not isinstance(item, dict) or not isinstance(item.get('url'), str):
            raise ValueError('本地测试活动包条目无效')
        name = unquote(urlsplit(item.get('url', '')).path.rsplit('/', 1)[-1])
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,180}\.zip', name):
            raise ValueError('本地测试活动包文件名无效')
        return name

    def response(self, path, origin):
        catalog = self._catalog()
        if path == '/api/plugins/test/catalog':
            for item in catalog['packages']:
                name = self._filename(item)
                item['url'] = origin + '/api/plugins/test/files/' + quote(name)
            return json.dumps(catalog, ensure_ascii=False).encode('utf-8'), 'application/json; charset=utf-8'
        if not path.startswith('/api/plugins/test/files/'):
            raise ValueError('测试活动资源不存在')
        name = unquote(path.removeprefix('/api/plugins/test/files/'))
        allowed = {self._filename(item) for item in catalog['packages']}
        if name not in allowed:
            raise ValueError('测试活动资源不存在')
        file = self.directory / name
        if file.is_symlink() or not file.resolve().is_relative_to(self.directory) or not file.is_file():
            raise ValueError('测试活动资源不存在')
        from backend.core.plugins.contracts import MAX_ARCHIVE_BYTES
        if file.stat().st_size > MAX_ARCHIVE_BYTES:
            raise ValueError('测试活动包过大')
        return file.read_bytes(), 'application/zip'


class ActivityCatalogHub:
    def _init_activity_catalog(self, test_activity_source_dir=None):
        self.catalog_gate = threading.RLock()
        self.plugin_busy = False
        self.plugin_thread = None
        self.plugin_operation = {}
        self.plugin_manager = None
        self.plugin_source = LocalActivitySource(test_activity_source_dir) if test_activity_source_dir else None
        self._plugin_snapshot = {}

    def _manager(self, origin):
        with self.catalog_gate:
            if self.plugin_manager is None:
                from backend.core.plugins.manager import PluginManager
                self.plugin_manager = (PluginManager(catalog_url=origin + '/api/plugins/test/catalog', allow_local=True)
                                       if self.plugin_source else PluginManager())
            return self.plugin_manager

    def plugins_snapshot(self, origin, refresh=False):
        with self.catalog_gate:
            busy = self.plugin_busy
            cached = copy.deepcopy(self._plugin_snapshot)
            operation = copy.deepcopy(self.plugin_operation)
        if not busy:
            cached = self._manager(origin).snapshot(refresh=refresh)
            with self.catalog_gate:
                self._plugin_snapshot = copy.deepcopy(cached)
        return dict(cached, busy=busy, operation=operation)

    def change_plugin(self, action, payload, origin):
        from backend.core.registry import checked_id
        app, activity = checked_id(payload.get('app')), checked_id(payload.get('activity'))
        if action not in ('install', 'uninstall'):
            raise ValueError('活动操作无效')
        with self.catalog_gate:
            if self.closing or self.plugin_busy:
                raise ValueError('活动包正在更新或服务正在退出')
            with self.lock:
                if any(c.thread and c.thread.is_alive() for c in self.controllers.values()):
                    raise ValueError('请先停止所有设备的操作，再安装或卸载活动')
            manager = self._manager(origin)
            self.plugin_busy = True
            self.plugin_operation = dict(action=action, app=app, activity=activity, status='running',
                                         message='正在下载并检查活动包' if action == 'install' else '正在卸载活动')
            self.plugin_thread = threading.Thread(target=self._plugin_work,
                                                  args=(manager, action, app, activity),
                                                  name='miao-activity-installer', daemon=False)
            self.plugin_thread.start()

    def _plugin_work(self, manager, action, app, activity):
        try:
            getattr(manager, action)(app, activity)
            from backend.core.plugins.manager import reload_plugin_registry
            from backend.core.registry import Registry
            reload_plugin_registry()
            registry = Registry(plugin_dir=manager.apps_dir).discover()
            with self.catalog_gate:
                with self.lock:
                    for controller in self.controllers.values():
                        controller.rebind_registry(registry)
                    self.defaults.update(registry=registry, app=None, activity=None, config=None)
                self._plugin_snapshot = manager.snapshot(registry=registry)
                self.plugin_operation.update(status='finished', message='活动已安装，可选择后连接设备' if action == 'install' else '活动已卸载，本地任务记录保留')
        except Exception as exc:
            with self.catalog_gate:
                self.plugin_operation.update(status='error', message=str(exc))
        finally:
            with self.catalog_gate:
                self.plugin_busy = False
