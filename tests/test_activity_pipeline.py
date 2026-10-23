"""Device-free integration contracts for sequential activities on each device."""
from contextlib import ExitStack, nullcontext
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from backend.core.config.loader import load_config
from backend.core.device.instances import AppInstance, InstanceMismatch
from backend.core.errors import NeedsAttention, Stopped
from backend.core.execution.stats import RunStats
from backend.core.registry import Registry
from backend.core.service.panel import Controller


class SyntheticDevice:
    device_type = 'uiautomator2'

    def __init__(self, profile, *, serial='synthetic-device'):
        self.serial = serial
        self.bound = []
        self.selections = []
        self.inventory = {name: [AppInstance(f'org.example.{name}', 0, name + ' main'),
                                 AppInstance(f'org.example.{name}', 999, name + ' clone')]
                          for name in ('alpha', 'beta')}
        self.bind_application(profile)

    def bind_application(self, profile):
        self.application = profile
        self.bound.append(profile.android_package)
        self.app_instances = list(self.inventory[profile.android_package.rsplit('.', 1)[-1]])
        self.selected_instance = self.app_instances[0] if self.app_instances else None

    def select_app_instance(self, identity):
        target = next((item for item in self.app_instances if item.id == identity), None)
        if target is None:
            raise InstanceMismatch('Synthetic instance is unavailable')
        self.selected_instance = target
        self.selections.append(identity)
        return target

    def instance_session(self):
        return nullcontext()

    def screenshot(self):
        return Image.new('RGB', (30, 60), 'white')


def write_registry(root):
    for app, activities in (('alpha', ('first', 'second')), ('beta', ('first',))):
        app_root = root / app
        (app_root / 'configs').mkdir(parents=True)
        (app_root / 'manifest.py').write_text(
            'from backend.core.registry import AppMeta\n'
            'from backend.core.device.profile import ApplicationProfile\n'
            f"APP = AppMeta('{app}', '{app}', ApplicationProfile('{app}', 'org.example.{app}', "
            f"r'org\\.example\\.{app}'), default={app == 'alpha'})\n", encoding='utf-8')
        (app_root / 'configs/base.json').write_text(json.dumps(
            {'search_keyword': app + '-entry', 'task_search_keyword': app + '-query'}), encoding='utf-8')
        for activity in activities:
            folder = app_root / 'activities' / activity
            folder.mkdir(parents=True)
            (folder / 'manifest.py').write_text(
                'from backend.core.registry import ActivityMeta\n'
                f"META = ActivityMeta('{app}', '{activity}', '{app}-{activity}', "
                f"'test_activity_pipeline:SyntheticRunner', default={activity == 'first'})\n", encoding='utf-8')
            (folder / 'config.json').write_text(json.dumps({
                'name': f'{app}-{activity}', 'entry_labels': [f'{app}-{activity}-entry'],
                'task_entry_labels': [f'{app}-{activity}-tasks']}), encoding='utf-8')
    return Registry(root).discover()


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.registry = write_registry(Path(self.temporary.name))
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('backend.core.service.panel.notify_security_attention'))

    def controller(self, *, serial='synthetic-device'):
        controller = Controller(registry=self.registry)
        controller.device = SyntheticDevice(controller.registration.app.profile, serial=serial)
        controller.update(connected=True,
                          app_instances=[item.to_dict() for item in controller.device.app_instances],
                          selected_instance=controller.device.selected_instance.id)
        for name in ('_ensure_daily', '_persist_state', '_save_report', 'on_failure', 'on_image'):
            self.stack.enter_context(patch.object(controller, name))
        return controller

    def select(self, controller, *items):
        controller.dispatch('select_pipeline', {'activities': [dict(app=app, activity=activity)
                                                               for app, activity in items]})
        if controller.thread:
            controller.thread.join(3)
            self.assertFalse(controller.thread.is_alive(), 'Activity selection did not finish')

    def run_controller(self, controller, payload=None):
        controller.dispatch('run', dict(auto_open=True, **(payload or {})))
        controller.thread.join(3)
        self.assertFalse(controller.thread.is_alive(), 'Synthetic worker did not finish')
        return controller.snapshot()

    @staticmethod
    def runner_factory(calls, *, fail=None, rendezvous=None):
        class SyntheticRunner:
            def __init__(self, device, config, control, emit, on_image, **kwargs):
                self.device, self.config, self.control, self.emit = device, config, control, emit

            def run(self, auto_open=False):
                calls.append(dict(serial=self.device.serial, name=self.config['name'],
                                  instance=self.device.selected_instance.id,
                                  entry=list(self.config['entry_labels']),
                                  task_entry=list(self.config['task_entry_labels']),
                                  max_tasks=self.config['max_tasks']))
                if rendezvous:
                    rendezvous(self)
                if fail:
                    fail(self)
                self.control.check()
                stats = asdict(RunStats())
                stats.update(completed=1, attempts=1)
                self.emit(stats=stats)
                return stats
        return SyntheticRunner

    def test_same_app_activities_keep_instance_and_load_distinct_entry_rules(self):
        controller = self.controller()
        self.select(controller, ('alpha', 'first'), ('alpha', 'second'))
        calls = []
        with patch('backend.core.service.panel.CampaignRunner', self.runner_factory(calls)):
            state = self.run_controller(controller, {'config': {'max_tasks': 3},
                                          'instance_ids': ['org.example.alpha@999']})
        self.assertEqual([row['name'] for row in calls], ['alpha-first', 'alpha-second'])
        self.assertEqual([row['instance'] for row in calls], ['org.example.alpha@999'] * 2)
        self.assertEqual([row['entry'] for row in calls], [['alpha-first-entry'], ['alpha-second-entry']])
        self.assertEqual([row['task_entry'] for row in calls], [['alpha-first-tasks'], ['alpha-second-tasks']])
        self.assertEqual([row['max_tasks'] for row in calls], [3, 3])
        self.assertEqual(controller.device.bound, ['org.example.alpha'])
        self.assertEqual([row['status'] for row in state['activity_pipeline']], ['finished', 'finished'])
        self.assertEqual([row['stats']['completed'] for row in state['activity_pipeline']], [1, 1])

    def test_cross_app_round_trip_restores_each_apps_explicit_instance(self):
        controller = self.controller()
        controller._app_instance_plans['beta'] = ['org.example.beta@999']
        self.select(controller, ('alpha', 'first'), ('beta', 'first'), ('alpha', 'second'))
        calls = []
        with patch('backend.core.service.panel.CampaignRunner', self.runner_factory(calls)):
            state = self.run_controller(controller, {'instance_ids': ['org.example.alpha@999']})
        self.assertEqual([row['instance'] for row in calls],
                         ['org.example.alpha@999', 'org.example.beta@999', 'org.example.alpha@999'])
        self.assertEqual([row['name'] for row in calls], ['alpha-first', 'beta-first', 'alpha-second'])
        self.assertEqual(controller.device.bound, ['org.example.alpha', 'org.example.beta', 'org.example.alpha'])
        self.assertEqual(state['status'], 'finished')

    def test_reconnecting_different_phone_never_reuses_previous_phones_clone_plan(self):
        controller = self.controller(serial='synthetic-phone-a')
        calls = []
        with patch('backend.core.service.panel.CampaignRunner', self.runner_factory(calls)):
            self.run_controller(controller, {'instance_ids': ['org.example.alpha@999']})
            self.assertEqual(controller._app_instance_plans['alpha'], ['org.example.alpha@999'])
            replacement = SyntheticDevice(controller.registration.app.profile, serial='synthetic-phone-b')
            replacement.current_app = Mock(return_value={'package': 'org.example.alpha'})
            replacement.display_size = (30, 60)
            replacement.instance_warnings = []
            with patch('backend.core.device.adapter.DeviceAdapter', return_value=replacement), \
                    patch.object(controller, '_start_preview'):
                controller.dispatch('connect', {'device_type': 'uiautomator2', 'serial': replacement.serial})
                controller.thread.join(3)
                self.assertFalse(controller.thread.is_alive())
            self.assertEqual(controller.state['status'], 'ready')
            self.assertEqual(controller._app_instance_plans, {})
            self.assertEqual(controller.state['selected_instance'], 'org.example.alpha@0')
            state = self.run_controller(controller)
        self.assertEqual([row['serial'] for row in calls], ['synthetic-phone-a', 'synthetic-phone-b'])
        self.assertEqual([row['instance'] for row in calls], ['org.example.alpha@999', 'org.example.alpha@0'])
        self.assertNotIn('org.example.alpha@999', replacement.selections)
        self.assertEqual(state['status'], 'finished')

    def test_missing_remembered_instance_stops_without_account_fallback(self):
        controller = self.controller()
        controller._app_instance_plans['beta'] = ['org.example.beta@123']
        self.select(controller, ('alpha', 'first'), ('beta', 'first'))
        original_device = controller.device
        calls = []
        with patch('backend.core.service.panel.CampaignRunner', self.runner_factory(calls)):
            state = self.run_controller(controller)
        self.assertEqual([row['name'] for row in calls], ['alpha-first'])
        self.assertEqual(state['status'], 'needs_attention')
        self.assertNotIn('org.example.beta@0', original_device.selections)
        self.assertIsNone(controller.device)

    def test_new_app_with_only_clone_requires_explicit_selection(self):
        controller = self.controller()
        controller.device.inventory['beta'] = [AppInstance('org.example.beta', 999, 'clone')]
        self.select(controller, ('alpha', 'first'), ('beta', 'first'))
        calls = []
        with patch('backend.core.service.panel.CampaignRunner', self.runner_factory(calls)):
            state = self.run_controller(controller)
        self.assertEqual([row['name'] for row in calls], ['alpha-first'])
        self.assertEqual(state['status'], 'needs_attention')

    def test_stop_authentication_and_unhandled_error_never_advance_pipeline(self):
        cases = [(Stopped(), 'stopped'),
                 (NeedsAttention('Synthetic manual verification', kind='security', evidence_saved=True), 'needs_attention'),
                 (RuntimeError('Synthetic adapter failure'), 'error')]
        for error, status in cases:
            with self.subTest(status=status):
                controller = self.controller()
                self.select(controller, ('alpha', 'first'), ('beta', 'first'))
                calls = []
                def fail(runner):
                    raise error
                with patch('backend.core.service.panel.CampaignRunner', self.runner_factory(calls, fail=fail)), \
                        patch('backend.core.service.panel.logger.exception'):
                    state = self.run_controller(controller)
                self.assertEqual([row['name'] for row in calls], ['alpha-first'])
                self.assertEqual(state['status'], status)
                self.assertEqual([row['status'] for row in state['activity_pipeline']], [status, 'pending'])

    def test_plan_rejects_duplicate_unknown_and_malformed_entries(self):
        controller = self.controller()
        item = dict(app='alpha', activity='first')
        for plan in ([item, item], [dict(app='missing', activity='first')],
                     [dict(app='alpha')], [dict(item, instance='extra')], 'alpha', [item] * 17,
                     [dict(app=None, activity='first')], [dict(app='alpha', activity='')],
                     [dict(app=0, activity=None)]):
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                controller._validate_plan(plan)
        self.assertEqual(controller._validate_plan([]), [])
        with self.assertRaisesRegex(ValueError, '至少选择'):
            controller._run_activity_pipeline('run', {'activities': []}, controller.state['config'])
        with self.assertRaisesRegex(ValueError, '自动进入'):
            controller._run_activity_pipeline('run', {'activities': [item, dict(app='beta', activity='first')],
                                                       'auto_open': False}, controller.state['config'])

    def test_two_devices_execute_independent_pipelines_in_parallel(self):
        first, second = self.controller(serial='synthetic-one'), self.controller(serial='synthetic-two')
        self.select(first, ('alpha', 'first'), ('alpha', 'second'))
        self.select(second, ('beta', 'first'))
        barrier, release = threading.Barrier(3), threading.Event()
        def rendezvous(runner):
            if runner.config['name'] in ('alpha-first', 'beta-first'):
                barrier.wait(3)
                release.wait(3)
        calls = []
        try:
            with patch('backend.core.service.panel.CampaignRunner', self.runner_factory(calls, rendezvous=rendezvous)):
                first.dispatch('run', {'auto_open': True})
                second.dispatch('run', {'auto_open': True})
                barrier.wait(3)
                self.assertEqual(first.state['activity_pipeline'][0]['status'], 'running')
                self.assertEqual(second.state['activity_pipeline'][0]['status'], 'running')
                first.dispatch('stop', {})
                release.set()
                first.thread.join(3)
                second.thread.join(3)
            self.assertEqual(first.state['status'], 'stopped')
            self.assertEqual(second.state['status'], 'finished')
            self.assertEqual([row['name'] for row in calls if row['serial'] == 'synthetic-one'], ['alpha-first'])
            self.assertEqual([row['name'] for row in calls if row['serial'] == 'synthetic-two'], ['beta-first'])
            self.assertEqual(first.state['activity_plan'], [dict(app='alpha', activity='first'), dict(app='alpha', activity='second')])
            self.assertEqual(second.state['activity_plan'], [dict(app='beta', activity='first')])
        finally:
            release.set()
            for controller in (first, second):
                if controller.thread:
                    controller.thread.join(3)

    def test_explicit_instance_selection_cannot_be_silently_replaced(self):
        invalid = ('org.example.alpha@0', [], [None], [''],
                   ['org.example.alpha@0', 'org.example.alpha@0'], ['org.example.beta@0'])
        for identities in invalid:
            with self.subTest(identities=identities):
                controller = self.controller()
                self.select(controller, ('alpha', 'first'), ('beta', 'first'))
                calls = []
                with patch('backend.core.service.panel.CampaignRunner', self.runner_factory(calls)), \
                        patch('backend.core.service.panel.logger.exception'):
                    try:
                        controller.dispatch('run', {'instance_ids': identities})
                    except (ValueError, InstanceMismatch):
                        pass
                    else:
                        controller.thread.join(3)
                        self.assertFalse(controller.thread.is_alive())
                        self.assertIn(controller.state['status'], ('error', 'needs_attention'))
                self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
