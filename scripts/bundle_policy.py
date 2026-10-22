"""Describe the explicit framework/application boundary used by PyInstaller.

Importing this helper must not import application manifests.  Build tools can
therefore inspect optional adapters without loading their executable code.
"""
import json
from pathlib import Path


def include_module(name, include_activities=False):
    if not name.startswith('backend.app.'):
        return True
    return include_activities or name == 'backend.app.common' or name.startswith('backend.app.common.')


def bundle_policy(root, include_activities=False):
    from backend.core.plugins.contracts import FRAMEWORK_API
    root = Path(root)
    app_root = root / 'backend/app'
    applications = sorted(path.name for path in app_root.iterdir()
                          if path.is_dir() and (path / 'manifest.py').is_file()) if app_root.exists() else []
    embedded = []
    if include_activities:
        for app in applications:
            for manifest in sorted((app_root / app / 'activities').glob('*/manifest.py')):
                # Public IDs may differ from module directories. Registry supplies
                # the canonical IDs after the frozen application starts.
                embedded.append({'app_module': app, 'activity_module': manifest.parent.name})
    return dict(schema=1, bundle_mode='full' if include_activities else 'framework',
                framework_api=FRAMEWORK_API, embedded_activity_modules=embedded,
                excluded_app_modules=[] if include_activities else ['backend.app.' + app for app in applications])


def application_data(root, include_activities=False):
    """Return data tuples relative to root; framework builds retain common only."""
    root = Path(root)
    result = []
    for path in (root / 'backend/app').rglob('*'):
        if not path.is_file():
            continue
        relative = path.relative_to(root / 'backend/app')
        if not include_activities and relative.parts[0] != 'common':
            continue
        if path.name == 'manifest.py' or path.suffix in ('.json', '.yaml', '.yml'):
            result.append((str(path), str(path.parent.relative_to(root))))
    return result


def write_policy(path, root, include_activities=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bundle_policy(root, include_activities), ensure_ascii=False, indent=2),
                    encoding='utf-8')
    return path
