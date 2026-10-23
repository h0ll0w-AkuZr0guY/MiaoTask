"""Discover declarative application manifests; no platform imports in core."""
from dataclasses import dataclass, field
from functools import lru_cache
import importlib
import importlib.util
from pathlib import Path
import re

from backend.core.device.profile import ApplicationProfile
from backend.core.runtime import assets_dir, activities_dir


def checked_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', value):
        raise ValueError('应用或活动 ID 无效')
    return value


@dataclass(frozen=True)
class AppMeta:
    id: str
    label: str
    profile: ApplicationProfile
    config: str = 'configs/base.json'
    default: bool = False


@dataclass(frozen=True)
class ActivityMeta:
    app: str
    id: str
    label: str
    factory: str
    config: str = 'config.json'
    device_types: tuple = ('uiautomator2', 'hmdriver2')
    default: bool = False
    legacy_storage: bool = False
    rules_version: str = '1'


@dataclass(frozen=True)
class Registration:
    app: AppMeta
    activity: ActivityMeta
    app_dir: Path
    activity_dir: Path

    def factory(self):
        module, name = self.activity.factory.rsplit(':', 1)
        return getattr(importlib.import_module(module), name)


class Registry:
    def __init__(self, app_dir=None, *, plugin_dir=None):
        self.root = Path(app_dir) if app_dir is not None else assets_dir() / 'backend/app'
        self.plugin_root = (Path(plugin_dir) if plugin_dir is not None else
                            activities_dir() / 'apps' if app_dir is None else None)
        self._apps = {}
        self._activities = {}

    @staticmethod
    def _manifest(path):
        spec = importlib.util.spec_from_file_location('_miao_manifest_' + str(abs(hash(str(path)))), path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def discover(self):
        apps, activities = {}, {}
        directories = {}
        for root in (self.root, self.plugin_root):
            if root is not None and root.is_dir():
                for directory in sorted(root.iterdir()):
                    if directory.is_dir() and (directory / 'manifest.py').is_file():
                        directories[directory.name] = directory
        if self.plugin_root is not None:
            # Add writable reviewed activities to the same namespace in source
            # and PyInstaller's PYZ. Framework/common modules remain bundled.
            package = importlib.import_module('backend.app')
            previous = getattr(package, '_miaotask_plugin_paths', ())
            paths = [path for path in package.__path__ if path not in previous]
            plugin_path = str(self.plugin_root.resolve())
            package.__path__ = [plugin_path] + [path for path in paths if path != plugin_path]
            package._miaotask_plugin_paths = (plugin_path,)
            importlib.invalidate_caches()
        for directory in directories.values():
            manifest = directory / 'manifest.py'
            if not directory.is_dir() or not manifest.is_file():
                continue
            app = self._manifest(manifest).APP
            checked_id(app.id)
            if app.id in apps:
                raise ValueError('重复应用 ID：' + app.id)
            apps[app.id] = app
            activity_root = directory / 'activities'
            if not activity_root.is_dir():
                continue
            for activity_dir in sorted(activity_root.iterdir()):
                if not (activity_dir / 'manifest.py').is_file():
                    continue
                activity = self._manifest(activity_dir / 'manifest.py').META
                checked_id(activity.id)
                if activity.app != app.id or (app.id, activity.id) in activities:
                    raise ValueError('活动归属不符或重复活动 ID')
                activities[app.id, activity.id] = Registration(app, activity, directory, activity_dir)
        self._apps, self._activities = apps, activities
        return self

    def default_app(self):
        if not self._apps:
            raise ValueError('未注册可用应用')
        choices = [a for a in self._apps.values() if any(r.app.id == a.id for r in self._activities.values())]
        choices = choices or list(self._apps.values())
        return next((a for a in choices if a.default), choices[0])

    @property
    def has_activities(self):
        return bool(self._activities)

    def get(self, app=None, activity=None):
        app = checked_id(app) if app else self.default_app().id
        choices = [r for (a, _), r in self._activities.items() if a == app]
        if activity is not None:
            checked_id(activity)
            if (app, activity) not in self._activities:
                raise ValueError('未注册的应用或活动')
            return self._activities[app, activity]
        if not choices:
            raise ValueError('此应用尚无活动')
        return next((r for r in choices if r.activity.default), choices[0])

    def catalog(self):
        return [dict(id=a.id, label=a.label, package=a.profile.android_package, activities=[
            dict(id=r.activity.id, label=r.activity.label, device_types=list(r.activity.device_types))
            for r in self._activities.values() if r.app.id == a.id]) for a in self._apps.values()]

    def instantiate(self, device, config=None, *args, app=None, activity=None, **kwargs):
        registration = self.get(app, activity)
        if device.device_type not in registration.activity.device_types:
            raise ValueError('此活动不支持当前设备驱动')
        from backend.core.device.instances import AppInstance, InstanceMismatch
        target = getattr(device, 'selected_instance', None)
        if isinstance(target, AppInstance) and not registration.app.profile.matches(target.package):
            raise InstanceMismatch('设备所选实例不属于当前应用，请先切换应用并重新选择实例')
        instance = registration.factory()(device, config, *args, **kwargs)
        instance.registration = registration
        return instance


@lru_cache(maxsize=1)
def default_registry():
    return Registry().discover()


def create_runner(device, config=None, *args, registry=None, **kwargs):
    return (registry or default_registry()).instantiate(device, config, *args, **kwargs)
