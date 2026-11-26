# -*- mode: python ; coding: utf-8 -*-
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
import os
import shutil
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH)
sys.path.insert(0, str(root))
from scripts.bundle_policy import application_data, bundle_policy, include_module, write_policy

include_activities = os.environ.get('MIAO_INCLUDE_ACTIVITIES') == '1'
policy = bundle_policy(root, include_activities)
policy_path = write_policy(Path(workpath) / 'bundle-policy.json', root, include_activities)
datas = [
    (str(root / 'version.json'), '.'),
    (str(root / 'frontend'), 'frontend'),
    (str(root / 'backend/core/config/defaults.json'), 'backend/core/config'),
    (str(policy_path), '.'),
]
datas += application_data(root, include_activities)
datas += collect_data_files('uiautomator2')
binaries = []

for package in ('uiautomator2', 'Pillow', 'PyYAML',
                'pyobjc-core', 'pyobjc-framework-Cocoa',
                'pyobjc-framework-Vision', 'pyobjc-framework-Quartz'):
    try:
        distribution(package)
        datas += copy_metadata(package, recursive=True)
    except PackageNotFoundError:
        if package in ('uiautomator2', 'Pillow', 'PyYAML'):
            raise

supplied_adb_dir = os.environ.get('MIAO_ADB_DIR')
adb_executable = str(Path(supplied_adb_dir) / 'adb') if supplied_adb_dir else shutil.which('adb')
if adb_executable and Path(adb_executable).is_file():
    binaries.append((adb_executable, 'shared/tools'))
else:
    raise RuntimeError('macOS ADB binary is required for this release build')

hiddenimports = (
    collect_submodules('backend', filter=lambda name: include_module(name, include_activities))
    + ['objc', 'Foundation', 'Vision', 'Quartz']
)

a = Analysis(
    [str(root / 'main.py')],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['torch', 'tensorflow', 'paddle', 'rapidocr', 'onnxruntime', 'hmdriver2',
              'matplotlib', 'pytest', 'IPython'] + policy['excluded_app_modules'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='MiaoTask',
    icon=str(root / 'frontend/assets/miaotask-512.png'),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    target_arch='arm64',
    console=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='MiaoTask')
