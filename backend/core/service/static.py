"""Explicit public frontend routes; never expose arbitrary project files."""
from backend.core.runtime import assets_dir
from backend.core.service.branding import PUBLIC_ICON_FILES


STATIC = {
    '/js/components/themes.js': ('js/components/themes.js', 'text/javascript; charset=utf-8'),
    '/css/themes.css': ('css/themes.css', 'text/css; charset=utf-8'),
    '/js/components/rankings.js': ('js/components/rankings.js', 'text/javascript; charset=utf-8'),
    '/js/components/dialogs.js': ('js/components/dialogs.js', 'text/javascript; charset=utf-8'),
    '/js/components/preview-controls.js': ('js/components/preview-controls.js', 'text/javascript; charset=utf-8'),
    '/js/components/activity-center.js': ('js/components/activity-center.js', 'text/javascript; charset=utf-8'),
    '/js/components/device-manager.js': ('js/components/device-manager.js', 'text/javascript; charset=utf-8'),
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/app.js': ('js/app.js', 'text/javascript; charset=utf-8'),
    '/style.css': ('css/base.css', 'text/css; charset=utf-8'),
    '/js/app.js': ('js/app.js', 'text/javascript; charset=utf-8'),
    '/js/api.js': ('js/api.js', 'text/javascript; charset=utf-8'),
    '/js/components/activity-selector.js': ('js/components/activity-selector.js', 'text/javascript; charset=utf-8'),
    '/js/components/wifi-connect.js': ('js/components/wifi-connect.js', 'text/javascript; charset=utf-8'),
    '/css/base.css': ('css/base.css', 'text/css; charset=utf-8'),
}


def static_response(path):
    if path == '/favicon.ico':
        path = '/assets/miaotask.ico'
    if path.startswith('/assets/') and path.removeprefix('/assets/') in PUBLIC_ICON_FILES:
        name = path.removeprefix('/assets/')
        return ((assets_dir() / 'frontend/assets' / name).read_bytes(),
                'image/x-icon' if name.endswith('.ico') else 'image/png')
    if path in STATIC:
        name, mime = STATIC[path]
        return (assets_dir() / 'frontend' / name).read_bytes(), mime
    return None
