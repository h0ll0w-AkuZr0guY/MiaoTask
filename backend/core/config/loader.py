"""Layered config with immutable snapshots and backward-compatible dictionaries."""
from collections.abc import Mapping
import copy
from dataclasses import dataclass
import json
from pathlib import Path
from types import MappingProxyType

from backend.core.config.schema import validate_config
from backend.core.runtime import assets_dir


def read_config(path):
    path = Path(path)
    if path.suffix.lower() in ('.yaml', '.yml'):
        import yaml
        value = yaml.safe_load(path.read_text(encoding='utf-8-sig'))
    else:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError('配置必须为对象')
    return value


def resource(directory, relative):
    base = Path(directory).resolve()
    path = (base / relative).resolve()
    if not path.is_relative_to(base):
        raise ValueError('配置路径超出活动目录')
    return path


def freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(freeze(item) for item in value)
    return copy.deepcopy(value)


def thaw(value):
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    return copy.deepcopy(value)


@dataclass(frozen=True)
class ConfigSnapshot(Mapping):
    _values: Mapping
    sources: Mapping

    def __getitem__(self, key):
        return thaw(self._values[key])

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def mutable(self):
        return {key: self[key] for key in self}


def load_resolved(registration, overrides=None):
    values, sources = {}, {}
    layers = [('core', assets_dir() / 'backend/core/config/defaults.json'),
              ('app', resource(registration.app_dir, registration.app.config)),
              ('activity', resource(registration.activity_dir, registration.activity.config))]
    for source, path in layers:
        content = read_config(path)
        values.update(content)
        sources.update({key: source for key in content})
    if overrides:
        values.update(copy.deepcopy(overrides))
        sources.update({key: 'user' for key in overrides})
    validate_config(values)
    return ConfigSnapshot(freeze(values), MappingProxyType(sources))


def load_framework_config(overrides=None):
    """Usable generic controls before any app is installed, never an activity."""
    values = read_config(assets_dir() / 'backend/core/config/defaults.json')
    values.update(search_keyword='', entry_labels=['活动'], task_entry_labels=['任务'])
    values.update(copy.deepcopy(overrides or {}))
    validate_config(values)
    return values


def load_config(path=None, *, app=None, activity=None, registry=None, overrides=None):
    if registry is None:
        from backend.core.registry import default_registry
        registry = default_registry()
    user = read_config(path) if path else {}
    user.update(overrides or {})
    if not registry.has_activities and app is None and activity is None:
        return load_framework_config(user)
    return load_resolved(registry.get(app, activity), user).mutable()
