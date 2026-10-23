"""Build independently downloadable, digest-pinned official activity archives."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.plugins.contracts import (ALLOWED_SUFFIXES, FRAMEWORK_API,
    OFFICIAL_REPOSITORY, PACKAGE_SCHEMA, module_id, package_version)
from backend.core.plugins.manager import validate_archive
from backend.core.registry import Registry
from scripts.check_public_files import audit_file


def build_packages(root, output, tag, version=None, *, require_clean=True, local_source=False):
    root, output = Path(root).resolve(), Path(output).resolve()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', tag):
        raise ValueError('活动发布标签无效')
    status = '' if local_source else subprocess.check_output(['git', 'status', '--porcelain'], cwd=root, text=True)
    if require_clean and status.strip():
        raise ValueError('活动发布前必须提交干净源码；本地预览可显式使用 --allow-dirty')
    commit = '' if local_source else subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    version = version or json.loads((root / 'version.json').read_text(encoding='utf-8'))['version'].split('-v')[-1]
    package_version(version)
    listing = ['git', 'ls-files', '-z']
    if not require_clean:
        listing.extend(['--cached', '--others', '--exclude-standard'])
    tracked = ([path.relative_to(root).as_posix() for path in (root / 'backend/app').rglob('*')
                if path.is_file() and '__pycache__' not in path.parts] if local_source else
               subprocess.check_output(listing + ['--', 'backend/app'], cwd=root).decode('utf-8').split('\0'))
    # A developer's installed activities must never override the source being
    # packaged. Only source manifests participate in this distribution build.
    with tempfile.TemporaryDirectory(prefix='miao-package-registry-') as empty_plugins:
        registry = Registry(root / 'backend/app', plugin_dir=empty_plugins).discover()
    output.mkdir(parents=True, exist_ok=True)
    catalog = {'schema': PACKAGE_SCHEMA, 'framework_api': FRAMEWORK_API,
               'repository': OFFICIAL_REPOSITORY, 'release': tag, 'packages': []}
    for app in registry.catalog():
        for activity in app['activities']:
            registration = registry.get(app['id'], activity['id'])
            app_module, activity_module = module_id(registration.app_dir.name), module_id(registration.activity_dir.name)
            prefix = f'backend/app/{app_module}/'
            activity_prefix = f'{prefix}activities/{activity_module}/'
            payload = {}
            for name in sorted(set(tracked)):
                if not name.startswith(prefix) or Path(name).suffix.lower() not in ALLOWED_SUFFIXES:
                    continue
                relative = name[len(prefix):]
                if relative.startswith('activities/') and relative != 'activities/__init__.py' and not name.startswith(activity_prefix):
                    continue
                path = root / name
                if path.is_symlink():
                    raise ValueError('活动源码不允许符号链接：' + name)
                content = path.read_bytes()
                if audit_file(name, content):
                    raise ValueError('活动源码未通过公开文件检查：' + name)
                payload[name] = content
            metadata = {'schema': PACKAGE_SCHEMA, 'framework_api': FRAMEWORK_API,
                        'python': f'{sys.version_info.major}.{sys.version_info.minor}',
                        'app': app['id'], 'activity': activity['id'], 'app_module': app_module,
                        'activity_module': activity_module, 'app_label': app['label'], 'label': activity['label'],
                        'version': version, 'source_commit': commit if not status.strip() else '',
                        'files': {name: hashlib.sha256(content).hexdigest() for name, content in payload.items()}}
            encoded = json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2).encode('utf-8') + b'\n'
            temporary = output / f'{app["id"]}-{activity["id"]}.zip'
            with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
                for name, content in sorted(dict(payload, **{'manifest.json': encoded}).items()):
                    info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                    info.external_attr = (0o100644 << 16)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    archive.writestr(info, content)
            digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
            validate_archive(temporary, digest, expected_app=app['id'], expected_activity=activity['id'])
            name = f'MiaoTask-activity-{app["id"]}-{activity["id"]}-{version}-{digest[:12]}.zip'
            destination = output / name
            temporary.replace(destination)
            entry = {key: metadata[key] for key in ('app', 'activity', 'app_module', 'activity_module',
                     'app_label', 'label', 'version', 'framework_api', 'python', 'source_commit')}
            entry.update(url=f'https://github.com/{OFFICIAL_REPOSITORY}/releases/download/{tag}/{name}',
                         sha256=digest, size=destination.stat().st_size, device_types=activity['device_types'])
            catalog['packages'].append(entry)
    (output / 'activity-catalog.json').write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return catalog


def main():
    parser = argparse.ArgumentParser(description='Build official independently installed activities')
    parser.add_argument('--tag', required=True, help='Immutable GitHub Release tag for the ZIP assets')
    parser.add_argument('--output', default='dist/activities')
    parser.add_argument('--version', help='Activity package semantic version, defaults to version.json')
    parser.add_argument('--allow-dirty', action='store_true', help='Local preview only; source_commit will be empty')
    args = parser.parse_args()
    catalog = build_packages(ROOT, args.output, args.tag, args.version, require_clean=not args.allow_dirty)
    print(json.dumps({'packages': len(catalog['packages']), 'catalog': str(Path(args.output) / 'activity-catalog.json')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
