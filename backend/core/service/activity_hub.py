"""Service-wide gate for installing reviewed activities from the repository."""
import copy
import threading


class ActivityCatalogHub:
    def _init_activity_catalog(self):
        self.catalog_gate = threading.RLock()
        self.plugin_busy = False
        self.plugin_thread = None
        self.plugin_operation = {}
        self.plugin_manager = None
        self._plugin_snapshot = {}

    def _manager(self, origin):
        with self.catalog_gate:
            if self.plugin_manager is None:
                from backend.core.plugins.manager import PluginManager
                self.plugin_manager = PluginManager()
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
        source_commit = payload.get('source_commit')
        if source_commit is not None:
            import re
            if not isinstance(source_commit, str) or not re.fullmatch(r'[0-9a-f]{40}', source_commit):
                raise ValueError('活动源码提交标识无效，请刷新目录')
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
                                                  args=(manager, action, app, activity, source_commit),
                                                  name='miao-activity-installer', daemon=False)
            self.plugin_thread.start()

    def open_activity_folder(self, origin):
        import os
        # Never accept a path from the browser; only open the resolved installation.
        folder = self._manager(origin).root
        if not hasattr(os, 'startfile'):
            raise ValueError('此系统不支持打开文件夹，请按活动中心显示的路径访问')
        os.startfile(str(folder))
        return str(folder)

    def _plugin_work(self, manager, action, app, activity, source_commit=None):
        try:
            if action == 'install':
                manager.install(app, activity, source_commit=source_commit)
            else:
                manager.uninstall(app, activity)
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
