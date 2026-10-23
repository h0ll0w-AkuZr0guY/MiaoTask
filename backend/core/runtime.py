"""Asset and writable-data paths for source and frozen distributions."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[2]
_PATH_LOCK = threading.RLock()


def assets_dir():
    return Path(getattr(sys, '_MEIPASS', ROOT))


def data_dir():
    override = os.environ.get('MIAO_DATA_DIR')
    if override:
        path = Path(override).expanduser().resolve()
    elif getattr(sys, 'frozen', False):
        path = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'MiaoTask'
    else:
        path = ROOT / 'runtime'
    path.mkdir(parents=True, exist_ok=True)
    return path


def _seed_activities(source, target):
    """Copy an older installation once, leaving its original files untouched."""
    if target.exists() or not (source / 'apps').is_dir():
        return
    # Do not follow links out of an installation while migrating its source.
    selected = [source / name for name in ('apps', 'installed.json', 'catalog.json')
                if (source / name).exists()]
    linked = lambda path: path.is_symlink() or path.is_junction()
    if linked(source) or any(linked(item) or (item.is_dir() and any(linked(p) for p in item.rglob('*')))
           for item in selected):
        raise ValueError('旧活动目录含符号链接，请保留现场并从活动中心重新安装')
    with tempfile.TemporaryDirectory(prefix='.activity-migration-', dir=target.parent) as temporary:
        staged = Path(temporary) / 'activities'
        staged.mkdir()
        for item in selected:
            if item.is_dir():
                shutil.copytree(item, staged / item.name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            else:
                shutil.copy2(item, staged / item.name)
        if not target.exists():
            os.replace(staged, target)


def _writable_directory(path):
    path.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=path):
        pass


def activities_dir():
    """Visible activity source root, separate from private daily/device data.

    Explicit paths are strict. MIAO_DATA_DIR also isolates activities for tests.
    A read-only portable folder falls back to the user's writable data root.
    """
    with _PATH_LOCK:
        explicit = os.environ.get('MIAO_ACTIVITIES_DIR')
        if explicit:
            target = Path(explicit).expanduser().resolve()
            _writable_directory(target)
            return target
        private = data_dir()
        isolated = bool(os.environ.get('MIAO_DATA_DIR'))
        portable = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else ROOT
        preferred = private / 'activities' if isolated else portable / 'activities'
        target = preferred
        try:
            # Probe the parent before creating the target so first migration is atomic.
            _writable_directory(target if target.exists() else target.parent)
        except OSError:
            target = private / 'activities'
            _writable_directory(target if target.exists() else target.parent)
        if not target.exists():
            previous = preferred if target != preferred and preferred.exists() else private / 'plugins'
            _seed_activities(previous, target)
        target.mkdir(parents=True, exist_ok=True)
        return target


def version():
    return json.loads((assets_dir() / 'version.json').read_text(encoding='utf-8'))['version']
