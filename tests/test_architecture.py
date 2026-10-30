"""Offline migration contracts. All devices, identifiers and screens are synthetic."""
import ast
import copy
from dataclasses import replace
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image

from backend.core.config.loader import load_config, load_resolved
from backend.core.device.geometry import (CoordinateTransform, DisplayGeometry, IdentityImageProcessor, PreparedImage)
from backend.core.device.endpoints import ConfiguredEmulatorProvider
from backend.core.device.contracts import DriverFactory
from backend.core.device.profile import ApplicationProfile
from backend.core.device.instances import AppInstance
from backend.core.device.screen import Screen, ScreenReader
from backend.core.execution.activity import Activity
from backend.core.models import Task, TextNode
from backend.core.registry import Registry, default_registry
from backend.core.service.panel import Controller
from backend.core.storage.daily_state import DailyState
from backend.core.storage.namespace import StorageNamespace
from backend.core.runtime import ROOT
from backend.app.taobao.activities.double11.tasks import parse_tasks, is_task_list, RULES
from backend.app.common.models.task import parse_tasks as pair_tasks


class SampleActivity(Activity):
    """Test-only second application; never registered in the real catalog."""
    application = ApplicationProfile('Sample', 'org.example.sample', r'org\.example\.sample')

    @staticmethod
    def default_config():
        return load_config()

    def ensure_list(self, auto_open=False):
        return Screen([], 100, 200, 'org.example.sample', user_id=0)

    def parse_tasks(self, nodes, width, height):
        return [Task('sample', '浏览任务', '', '执行', (70, 70, 90, 90), self.stats.completed, 1,
                     kind='done' if self.stats.completed else 'browse')]

    def is_task_list(self, nodes):
        return True

    def execute(self, task, before):
        return before, True, False

    def verify(self, task, before, after, explicit=False):
        return explicit


def manifest_tree(root):
    app = root / 'sample'
    activity = app / 'activities/sample_activity'
    activity.mkdir(parents=True)
    (app / 'configs').mkdir()
    (app / 'manifest.py').write_text(
        "from backend.core.registry import AppMeta\nfrom backend.core.device.profile import ApplicationProfile\n"
        "APP=AppMeta('sample','示例',ApplicationProfile('示例','org.example.sample',r'org\\.example\\.sample'))\n", encoding='utf-8')
    (activity / 'manifest.py').write_text(
        "from backend.core.registry import ActivityMeta\nMETA=ActivityMeta('sample','sample-activity','示例活动','test_architecture:SampleActivity')\n", encoding='utf-8')
    config = load_config()
    (app / 'configs/base.json').write_text(json.dumps({'task_search_keyword': 'base'}), encoding='utf-8')
    (activity / 'config.json').write_text(json.dumps(config), encoding='utf-8')
    return app, activity


class MigrationContracts(unittest.TestCase):
    def test_original_config_and_synthetic_parser_results_are_identical(self):
        fixture = json.loads((ROOT / 'tests/fixtures/v009_contract.json').read_text(encoding='utf-8'))
        configured = load_config()
        for key in ('minimum_volume', 'minimum_brightness'):
            self.assertIs(configured.pop(key), False)
        self.assertEqual(configured, fixture['config'])
        for case in fixture['cases']:
            nodes = [TextNode(n['text'], tuple(n['bounds'])) for n in case['nodes']]
            # JSON normalizes tuples to arrays on both sides.
            actual = json.loads(json.dumps([t.to_dict() for t in parse_tasks(nodes)]))
            self.assertEqual(actual, case['tasks'])
            self.assertEqual(is_task_list(nodes), case['is_task_list'])

    def test_core_does_not_import_platform_code_or_embed_platform_names(self):
        for path in (ROOT / 'backend/core').rglob('*.py'):
            text = path.read_text(encoding='utf-8')
            self.assertNotRegex(text.lower(), r'taobao|double11', str(path))
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.ImportFrom):
                    self.assertFalse((node.module or '').startswith('backend.app'), str(path))

    def test_custom_vocabulary_uses_shared_pairing_without_platform_changes(self):
        rules = replace(RULES, action_re=re.compile('执行'), classify=lambda *args: (0, 'browse', '', '', ()))
        nodes = [TextNode('自定义任务(0/1)', (30, 700, 600, 760)), TextNode('执行', (1100, 700, 1300, 760))]
        task = pair_tasks(nodes, rules=rules)[0]
        self.assertEqual((task.title, task.kind), ('自定义任务', 'browse'))

    def test_config_provenance_and_nested_values_cannot_mutate_snapshot(self):
        registration = default_registry().get()
        config = load_resolved(registration, {'max_tasks': 2})
        self.assertEqual(config.sources['max_tasks'], 'user')
        self.assertEqual(config.sources['entry_labels'], 'activity')
        labels = config['entry_labels']; labels.clear()
        self.assertTrue(config['entry_labels'])
        with self.assertRaises(TypeError):
            config.sources['name'] = 'user'

    def test_original_flat_json_override_is_still_supported(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'override.json'
            path.write_text('{"max_tasks": 2}', encoding='utf-8')
            self.assertEqual(load_config(path)['max_tasks'], 2)

    def test_registry_adds_second_app_with_hyphenated_public_activity_id(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest_tree(Path(folder))
            registry = Registry(folder).discover()
            self.assertEqual(registry.catalog()[0]['activities'][0]['id'], 'sample-activity')
            runner = registry.instantiate(Mock(device_type='uiautomator2'), load_config(overrides={'max_tasks': 1}))
            self.assertEqual(runner.run()['completed'], 1)

    def test_registry_rejects_missing_and_path_traversal_ids(self):
        registry = default_registry()
        for value in ('../sample', 'abs/path', 'missing'):
            with self.assertRaises(ValueError):
                registry.get(value)

    def test_activity_factory_rejects_instance_of_another_application(self):
        device = Mock(device_type='uiautomator2')
        device.selected_instance = AppInstance('org.example.other', 0, 'other')
        with self.assertRaisesRegex(RuntimeError, '不属于当前应用'):
            default_registry().instantiate(device)

    def test_empty_registry_starts_control_panel_without_activity_import(self):
        with tempfile.TemporaryDirectory() as folder:
            registry = Registry(folder).discover()
            controller = Controller(registry=registry)
            self.assertEqual(controller.snapshot()['catalog'], [])
            with self.assertRaisesRegex(ValueError, '未注册活动'):
                controller.dispatch('connect', {})

    def test_activity_switch_changes_config_and_namespace_without_device_io(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest_tree(Path(folder))
            registry = Registry(folder).discover()
            controller = Controller(registry=registry)
            controller.dispatch('select_activity', {'app': 'sample', 'activity': 'sample-activity'})
            controller.thread.join(3)
            self.assertEqual(controller.snapshot()['activity'], 'sample-activity')
            self.assertIsNone(controller.device)

    def test_busy_session_cannot_switch_activity(self):
        c = Controller(); c.thread = Mock(); c.thread.is_alive.return_value = True
        with self.assertRaises(ValueError):
            c.dispatch('select_activity', {'app': 'sample', 'activity': 'sample-activity'})

    def test_registered_platform_without_activity_does_not_prevent_panel_start(self):
        with tempfile.TemporaryDirectory() as folder:
            _, activity = manifest_tree(Path(folder))
            (activity / 'manifest.py').unlink()
            registry = Registry(folder).discover()
            self.assertFalse(registry.has_activities)
            self.assertEqual(Controller(registry=registry).snapshot()['activity'], '')

    def test_lifecycle_teardown_runs_on_stop_or_exception(self):
        runner = SampleActivity(Mock(), load_config())
        trace = []
        runner.setup = lambda: trace.append('setup')
        runner.teardown = lambda: trace.append('teardown')
        runner.ensure_list = Mock(side_effect=ValueError('entry failed'))
        with self.assertRaises(ValueError):
            runner.run()
        self.assertEqual(trace, ['setup', 'teardown'])


class NamespaceContracts(unittest.TestCase):
    def test_legacy_state_and_evidence_adopt_once_without_changing_old_store(self):
        with tempfile.TemporaryDirectory() as root:
            registration = default_registry().get()
            ns = StorageNamespace(registration.app.id, registration.activity.id, True,
                                  registration.app.profile.legacy_primary_instances)
            identity = registration.app.profile.legacy_primary_instances[0]
            old = DailyState(root, 'synthetic-handset', 'uiautomator2', '2026-10-21', identity)
            old.progress('old', {'completed': 3, 'attempts': 3}, unavailable_tasks={'app:sample': '未安装'})
            old.failure({'reason': 'sample'}, Image.new('RGB', (4, 4)))
            original = old.stats_path.read_bytes()
            new = DailyState(root, 'synthetic-handset', 'uiautomator2', '2026-10-21', identity, ns)
            self.assertNotEqual(old.folder, new.folder)
            self.assertEqual(new.snapshot()['stats']['completed'], 3)
            self.assertEqual(len(list(new.failures_dir.glob('*.png'))), 1)
            new.progress('new', {'completed': 2})
            reopened = DailyState(root, 'synthetic-handset', 'uiautomator2', '2026-10-21', identity, ns)
            self.assertEqual(reopened.snapshot()['stats']['completed'], 5)
            self.assertEqual(old.stats_path.read_bytes(), original)

    def test_app_activity_instance_and_date_are_independent(self):
        with tempfile.TemporaryDirectory() as root:
            stores = [DailyState(root, 'synthetic-device', 'uiautomator2', day, instance, StorageNamespace(app, activity))
                      for app, activity, day, instance in [('one','daily','2026-10-21','pkg@0'),
                          ('one','daily','2026-10-21','pkg@999'),('one','other','2026-10-21','pkg@0'),
                          ('two','daily','2026-10-21','pkg@0'),('one','daily','2026-10-22','pkg@0')]]
            stores[0].progress('first', {'completed': 4}, unavailable_tasks={'sample': 'missing'})
            self.assertEqual(len({s.folder for s in stores}), 5)
            for store in stores[1:]:
                self.assertEqual(store.snapshot()['stats']['completed'], 0)
                self.assertFalse(store.unavailable_tasks())

    def test_namespace_rejects_path_escape(self):
        with self.assertRaises(ValueError):
            StorageNamespace('../invalid', 'daily')

    def test_corrupt_legacy_file_is_preserved_and_recovered_in_new_namespace(self):
        with tempfile.TemporaryDirectory() as root:
            registration = default_registry().get()
            identity = registration.app.profile.legacy_primary_instances[0]
            old = DailyState(root, 'synthetic-device', 'uiautomator2', '2026-10-21', identity)
            old.stats_path.write_text('{broken', encoding='utf-8')
            ns = StorageNamespace(registration.app.id, registration.activity.id, True,
                                  registration.app.profile.legacy_primary_instances)
            new = DailyState(root, 'synthetic-device', 'uiautomator2', '2026-10-21', identity, ns)
            self.assertTrue(new.recovery_warning)
            self.assertEqual(old.stats_path.read_text(encoding='utf-8'), '{broken')
            self.assertEqual(len(list(new.folder.glob('state.corrupt-*.json'))), 1)


class DeviceExtensionContracts(unittest.TestCase):
    def test_identity_processor_preserves_pixels_and_coordinates(self):
        image = Image.new('RGB', (40, 80))
        prepared = IdentityImageProcessor().prepare(image)
        self.assertIs(prepared.image, image)
        self.assertEqual(prepared.to_device.bounds((2, 4, 20, 40)), (2, 4, 20, 40))

    def test_ocr_maps_injected_crop_coordinates_back_to_original_screen(self):
        class Crop:
            cache_key = 'crop'
            def prepare(self, image):
                return PreparedImage(image.crop((10, 20, 60, 100)), CoordinateTransform(offset_x=10, offset_y=20))
        reader = ScreenReader(None, image_processor=Crop())
        reader._engine = Mock(return_value=Mock(boxes=[[(1,2),(11,2),(11,12),(1,12)]], txts=['label'], scores=[.99]))
        result = reader._ocr(Image.new('RGB', (100, 200)))
        self.assertEqual(result[0].bounds, (11, 22, 21, 32))

    def test_processor_change_invalidates_full_frame_cache(self):
        reader = ScreenReader(None); reader._ocr = Mock(return_value=[])
        image = Image.new('RGB', (4, 8))
        reader._image_nodes(image)
        reader.image_processor = Mock(cache_key='different')
        reader._image_nodes(image)
        self.assertEqual(reader._ocr.call_count, 2)

    def test_display_metadata_validates_safe_area_without_changing_gestures(self):
        self.assertEqual(DisplayGeometry(1080, 2400, safe_insets=(0,80,0,40)).rotation, 0)
        with self.assertRaises(ValueError):
            DisplayGeometry(1080, 2400, safe_insets=(600,0,600,0))

    def test_emulator_provider_uses_configured_endpoint_without_adb_or_port_guess(self):
        provider = ConfiguredEmulatorProvider(['127.0.0.1:16384'], 'MuMu')
        endpoint = provider.discover()[0]
        self.assertEqual((endpoint.serial, endpoint.transport), ('127.0.0.1:16384', 'tcp'))
        with self.assertRaises(ValueError):
            ConfiguredEmulatorProvider(['127.0.0.1:99999'])

    def test_custom_driver_factory_does_not_change_existing_adapter(self):
        factory = DriverFactory(); instance = Mock()
        factory.register('sample', lambda **kwargs: instance)
        self.assertIs(factory.create('sample'), instance)
        with self.assertRaises(ValueError):
            factory.register('sample', Mock())


if __name__ == '__main__':
    unittest.main()
