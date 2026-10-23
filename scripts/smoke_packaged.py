"""Verify an exe with Python/ADB removed from PATH, without touching a phone."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import zipfile
from urllib.request import Request, urlopen


def synthetic_activity_package(path):
    """Create a minimal, explicitly synthetic adapter for frozen-import checks.

    No device methods are called.  The activity imports a shared business model
    as well as framework contracts, catching missing common modules in PYZ.
    """
    prefix = 'backend/app/smokefixture/'
    content = {
        prefix + '__init__.py': '',
        prefix + 'manifest.py': (
            'from backend.core.registry import AppMeta\n'
            'from backend.core.device.profile import ApplicationProfile\n'
            "PROFILE = ApplicationProfile('Offline fixture', 'org.example.smoke', r'org\\.example\\.smoke')\n"
            "APP = AppMeta('smokefixture', 'Offline fixture', PROFILE)\n"),
        prefix + 'configs/base.json': json.dumps({'search_keyword': 'example', 'task_search_keyword': 'case'}),
        prefix + 'activities/__init__.py': '',
        prefix + 'activities/daily/__init__.py': '',
        prefix + 'activities/daily/manifest.py': (
            'from backend.core.registry import ActivityMeta\n'
            "META = ActivityMeta('smokefixture', 'daily', 'Offline import check', "
            "'backend.app.smokefixture.activities.daily.activity:OfflineActivity')\n"),
        prefix + 'activities/daily/config.json': json.dumps(
            {'name': 'Offline import check', 'entry_labels': ['Example'], 'task_entry_labels': ['Tasks']}),
        prefix + 'activities/daily/activity.py': (
            'from backend.core.execution.activity import Activity\n'
            'from backend.core.config.loader import load_config\n'
            'from backend.app.common.models.task import TaskRules\n'
            'from backend.app.smokefixture.manifest import PROFILE\n'
            'class OfflineActivity(Activity):\n'
            '    application = PROFILE\n'
            '    def default_config(self):\n'
            "        return load_config(app='smokefixture', activity='daily')\n"
            '    def parse_tasks(self, nodes, width, height): return []\n'
            '    def is_task_list(self, nodes): return False\n'
            '    def execute(self, task, before): raise RuntimeError("Synthetic activity must not execute")\n'
            '    def verify(self, task, before, after, explicit=False): return False\n'),
    }
    payload = {name: value.encode('utf-8') for name, value in content.items()}
    manifest = dict(schema=1, framework_api=1, app='smokefixture', activity='daily',
                    app_module='smokefixture', activity_module='daily', version='0.1.0',
                    python=f'{sys.version_info.major}.{sys.version_info.minor}',
                    files={name: hashlib.sha256(data).hexdigest() for name, data in payload.items()})
    path = Path(path)
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json', json.dumps(manifest))
        for name, data in payload.items():
            archive.writestr(name, data)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def smoke(executable, output=None, *, activity_packages=()):
    executable = Path(executable).resolve()
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    result = {'exe': executable.name}
    with tempfile.TemporaryDirectory(prefix='miao-package-check-') as work:
        folder = Path(work)
        env = os.environ.copy()
        windows = env.get('SystemRoot', r'C:\Windows')
        env['PATH'] = os.pathsep.join([str(Path(windows) / 'System32'), windows])
        env['MIAO_DATA_DIR'] = str(folder / 'data')
        for key in ('PYTHONPATH', 'PYTHONHOME', 'ADBUTILS_ADB_PATH', 'MIAO_ACTIVITIES_DIR'):
            env.pop(key, None)
        report = folder / 'self-test.json'
        self_check = subprocess.run([str(executable), '--self-test', '--output', str(report)], cwd=work,
                                    env=env, creationflags=flags, timeout=120)
        if self_check.returncode:
            details = report.read_text(encoding='utf-8') if report.exists() else 'No self-test report was written'
            raise RuntimeError(f'Packaged self-test failed ({self_check.returncode}): {details}')
        result['offline'] = json.loads(report.read_text(encoding='utf-8'))
        if not result['offline']['ok'] or not result['offline']['frozen']:
            raise RuntimeError(f'Packaged self-test failed: {result}')
        port_file = folder / 'port.json'
        process = subprocess.Popen([str(executable), '--no-browser', '--port', '0', '--port-file', str(port_file)],
                                   cwd=work, env=env, creationflags=flags)
        try:
            deadline = time.monotonic() + 30
            while not port_file.exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError('The packaged web service did not start')
                time.sleep(.2)
            base = json.loads(port_file.read_text(encoding='utf-8'))['url']
            with urlopen(base + '/api/health', timeout=5) as response:
                result['health'] = json.load(response)
            for name, expected in [('/', 'MiaoTask'), ('/app.js', 'browse_extra_seconds'), ('/style.css', '.topbar')]:
                with urlopen(base + name, timeout=5) as response:
                    if expected not in response.read().decode('utf-8'):
                        raise RuntimeError(f'Unexpected packaged response: {name}')
            with urlopen(base + '/api/state', timeout=5) as response:
                state = json.load(response)
            with urlopen(base + '/api/apps', timeout=5) as response:
                catalog = json.load(response)['apps']
            if state.get('catalog') != catalog:
                raise RuntimeError('Panel catalog does not match the registry API')
            result['activity_catalog'] = True
            if result['offline']['checks'].get('bundle_mode') == 'framework':
                if catalog:
                    raise RuntimeError('Framework-only executable unexpectedly contains an activity')
                result['empty_activity_catalog_starts'] = True
            for asset in ('/assets/miaotask-32.png', '/assets/miaotask-128.png', '/favicon.ico'):
                with urlopen(base + asset, timeout=5) as response:
                    expected = b'\x00\x00\x01\x00' if asset.endswith('.ico') else b'\x89PNG\r\n\x1a\n'
                    if not response.read().startswith(expected):
                        raise RuntimeError(f'Packaged icon missing or invalid: {asset}')
            result['brand_icons'] = True
            if state['connected']:
                raise RuntimeError('Package should not connect to a device automatically')
            if state.get('version') != result['health']['version']:
                raise RuntimeError('Panel version does not match the running executable')
            # Include real browser security headers, not just a headerless CLI POST.
            request = Request(base + '/api/configure', data=b'{}', method='POST', headers={
                'Content-Type': 'application/json', 'X-Miao-Token': state['token'],
                'Origin': base, 'Sec-Fetch-Site': 'same-origin'})
            with urlopen(request, timeout=5) as response:
                result['browser_headers_post'] = response.status == 202
            def post(path, payload):
                request = Request(base+path, data=json.dumps(payload).encode(), headers={
                    'Content-Type':'application/json','X-Miao-Token':state['token'],
                    'Origin':base,'Sec-Fetch-Site':'same-origin'})
                with urlopen(request,timeout=5) as response:
                    return json.load(response)
            added=post('/api/workspaces',{'label':'offline check'})['id']
            post('/api/configure?device='+added,{'config':{'max_tasks':3}})
            with urlopen(base+'/api/state?device='+added,timeout=5) as response:
                child=json.load(response)
            with urlopen(base+'/api/state',timeout=5) as response:
                original=json.load(response)
            if child['config']['max_tasks']!=3 or original['config']['max_tasks']!=state['config']['max_tasks']:
                raise RuntimeError('Packaged workspaces do not isolate configuration')
            post('/api/workspaces/remove',{'id':added})
            for path in ['/js/components/device-manager.js','/js/components/wifi-connect.js']:
                with urlopen(base+path,timeout=5) as response:
                    if not response.read():
                        raise RuntimeError('Packaged frontend component missing: '+path)
            result['multi_device_isolation']=True
            # Launching twice should reuse the existing service.
            port = base.rsplit(':', 1)[1]
            subprocess.run([str(executable), '--no-browser', '--port', port], cwd=work, env=env,
                           creationflags=flags, check=True, timeout=20)
            result['second_launch_reuses_service'] = True
            request = Request(base + '/api/shutdown', data=b'{}', method='POST',
                              headers={'Content-Type': 'application/json', 'X-Miao-Token': state['token']})
            with urlopen(request, timeout=5) as response:
                result['shutdown'] = response.status == 202
            process.wait(timeout=10)
            if process.returncode != 0:
                raise RuntimeError(f'Unexpected exit code: {process.returncode}')
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
        # A second real frozen process installs trusted synthetic source from a
        # local ZIP.  This verifies that external namespace imports work even
        # though no platform implementation exists in the frozen module graph.
        activity_zip = folder / 'offline-activity.zip'
        activity_hash = synthetic_activity_package(activity_zip)
        installed_report = folder / 'installed-self-test.json'
        install_check = subprocess.run(
            [str(executable), '--self-test', '--output', str(installed_report),
             '--activity-package', str(activity_zip), '--activity-package-sha256', activity_hash],
            cwd=work, env=env, creationflags=flags, timeout=120)
        details = json.loads(installed_report.read_text(encoding='utf-8')) if installed_report.exists() else {}
        factories = details.get('checks', {}).get('downloaded_activity_factories', [])
        expected_factory = 'backend.app.smokefixture.activities.daily.activity:OfflineActivity'
        if install_check.returncode or not details.get('ok') or expected_factory not in factories:
            raise RuntimeError(f'Frozen activity installation/import failed: {details}')
        result['dynamic_activity_factory'] = True
        installed_port = folder / 'installed-port.json'
        installed_process = subprocess.Popen(
            [str(executable), '--no-browser', '--port', '0', '--port-file', str(installed_port)],
            cwd=work, env=env, creationflags=flags)
        try:
            deadline = time.monotonic() + 30
            while not installed_port.exists():
                if installed_process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError('Service could not restart with installed activity')
                time.sleep(.2)
            installed_base = json.loads(installed_port.read_text(encoding='utf-8'))['url']
            with urlopen(installed_base + '/api/apps', timeout=5) as response:
                installed_catalog = json.load(response)['apps']
            app = next((item for item in installed_catalog if item['id'] == 'smokefixture'), None)
            if not app or not any(activity['id'] == 'daily' for activity in app['activities']):
                raise RuntimeError('Installed activity is missing after executable restart')
            with urlopen(installed_base + '/api/state', timeout=5) as response:
                installed_state = json.load(response)
            if not installed_state.get('config', {}).get('max_tasks'):
                raise RuntimeError('Installed activity configuration did not load')
            result['installed_activity_survives_restart'] = True
            request = Request(installed_base + '/api/shutdown', data=b'{}', method='POST',
                              headers={'Content-Type': 'application/json', 'X-Miao-Token': installed_state['token']})
            with urlopen(request, timeout=5) as response:
                if response.status != 202:
                    raise RuntimeError('Installed activity service shutdown failed')
            installed_process.wait(timeout=10)
            if installed_process.returncode != 0:
                raise RuntimeError('Installed activity service exited abnormally')
        finally:
            if installed_process.poll() is None:
                installed_process.terminate()
                installed_process.wait(timeout=10)
        result['actual_activity_imports'] = []
        for index, (archive, digest) in enumerate(activity_packages):
            real_report = folder / f'activity-{index}.json'
            real_check = subprocess.run(
                [str(executable), '--self-test', '--output', str(real_report),
                 '--activity-package', str(Path(archive).resolve()), '--activity-package-sha256', digest],
                cwd=work, env=env, creationflags=flags, timeout=120)
            details = json.loads(real_report.read_text(encoding='utf-8')) if real_report.exists() else {}
            record = details.get('checks', {}).get('local_activity_install', {})
            factory_prefix = f"backend.app.{record.get('app_module')}.activities.{record.get('activity_module')}."
            factories = details.get('checks', {}).get('downloaded_activity_factories', [])
            if (real_check.returncode or not details.get('ok') or record.get('sha256') != digest
                    or not any(factory.startswith(factory_prefix) for factory in factories)):
                raise RuntimeError(f'Reviewed activity could not import in the framework executable: {details}')
            result['actual_activity_imports'].append({'app': record['app'], 'activity': record['activity']})
    result['ok'] = True
    if output:
        Path(output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('exe')
    parser.add_argument('--output')
    parser.add_argument('--activity-package', action='append', default=[], help='Also verify this trusted activity ZIP')
    parser.add_argument('--activity-package-sha256', action='append', default=[], help='Expected digest, in ZIP argument order')
    args = parser.parse_args()
    if len(args.activity_package) != len(args.activity_package_sha256):
        parser.error('Each activity ZIP requires one --activity-package-sha256')
    print(json.dumps(smoke(args.exe, args.output,
                           activity_packages=list(zip(args.activity_package, args.activity_package_sha256))),
                     ensure_ascii=False, indent=2))
