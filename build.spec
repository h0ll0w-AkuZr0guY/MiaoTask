# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import os
import shutil
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata

root = Path(SPECPATH)
sys.path.insert(0, str(root))
from scripts.bundle_policy import application_data, bundle_policy, include_module, write_policy

include_activities = os.environ.get('MIAO_INCLUDE_ACTIVITIES') == '1'
policy = bundle_policy(root, include_activities)
policy_path = write_policy(Path(workpath) / 'bundle-policy.json', root, include_activities)
datas = [(str(root / 'version.json'), '.'), (str(root / 'frontend'), 'frontend'), (str(root / 'backend/core/config/defaults.json'), 'backend/core/config')]
datas.append((str(policy_path), '.'))
datas += application_data(root, include_activities)
binaries = collect_dynamic_libs('onnxruntime')
for package in ('uiautomator2', 'hmdriver2', 'rapidocr'):
    datas += collect_data_files(package)
for package in ('uiautomator2', 'hmdriver2', 'rapidocr', 'onnxruntime', 'numpy', 'Pillow'):
    datas += copy_metadata(package, recursive=True)

for tool in ('adb', 'hdc'):
    supplied = os.environ.get(f'MIAO_{tool.upper()}_DIR')
    executable = str(Path(supplied) / f'{tool}.exe') if supplied else shutil.which(tool)
    if executable and Path(executable).is_file():
        folder = Path(executable).parent
        binaries.append((executable, 'shared/tools'))
        binaries.extend((str(dll), 'shared/tools') for dll in folder.glob('*.dll'))
    elif tool == 'adb':
        raise RuntimeError('ADB is required for this release build')

a = Analysis([str(root / 'main.py')], pathex=[str(root)], binaries=binaries, datas=datas,
             hiddenimports=collect_submodules('backend', filter=lambda name: include_module(name, include_activities)) + collect_submodules('rapidocr'),
             hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=['torch', 'tensorflow', 'paddle', 'matplotlib', 'pytest', 'IPython'] + policy['excluded_app_modules'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='MiaoTask',
          icon=str(root / 'frontend/assets/miaotask.ico'),
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='MiaoTask')
