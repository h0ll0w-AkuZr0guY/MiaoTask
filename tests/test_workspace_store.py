"""Persistent workspace store and DeviceHub restore behaviour."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend.core.service.devices import DeviceHub
from backend.core.storage.workspaces import WORKSPACES_FILE, WorkspaceStore


def _valid_config(**overrides):
    config = {
        'default_browse_seconds': 8, 'visit_seconds': 5, 'browse_extra_seconds': 5,
        'max_task_seconds': 90, 'settle_seconds': 2, 'page_timeout': 12,
        'return_timeout': 20, 'max_back_steps': 3, 'max_attempts': 2,
        'max_tasks': 12, 'max_scroll_pages': 4,
        'allow_cross_app': False, 'ocr': True, 'scroll_browse': True, 'claim_rewards': False,
        'enabled_task_kinds': ['browse', 'search', 'visit', 'claim'],
        'search_keyword': '', 'task_search_keyword': '手机壳',
        'entry_labels': ['活动'], 'task_entry_labels': ['任务'],
    }
    config.update(overrides)
    return config


class WorkspaceStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / WORKSPACES_FILE
        self.store = WorkspaceStore(self.path)

    def test_update_get_and_reload(self):
        self.store.update('default', label='主力机', device_type='uiautomator2',
                          serial='192.168.1.8:5555', auto_instances=True,
                          config=_valid_config(visit_seconds=9))
        again = WorkspaceStore(self.path)
        entry = again.get('default')
        self.assertEqual(entry['label'], '主力机')
        self.assertEqual(entry['serial'], '192.168.1.8:5555')
        self.assertIs(entry['auto_instances'], True)
        self.assertEqual(entry['config']['visit_seconds'], 9)

    def test_partial_update_keeps_other_fields(self):
        self.store.update('default', serial='abc123')
        self.store.update('default', label='改名')
        entry = self.store.get('default')
        self.assertEqual(entry['serial'], 'abc123')
        self.assertEqual(entry['label'], '改名')

    def test_empty_serial_clears_only_serial(self):
        self.store.update('default', serial='abc', label='保留')
        self.store.update('default', serial=None)
        entry = self.store.get('default')
        self.assertNotIn('serial', entry)
        self.assertEqual(entry['label'], '保留')

    def test_remove(self):
        self.store.update('abc', label='x')
        self.store.remove('abc')
        self.assertIsNone(self.store.get('abc'))

    def test_rejects_unknown_fields_and_pairing_secrets(self):
        # Pairing codes / credentials must never be storable here.
        with self.assertRaises(ValueError):
            self.store.update('default', pair_code='482913')
        with self.assertRaises(ValueError):
            self.store.update('default', password='secret')

    def test_rejects_bad_key_serial_label_and_config(self):
        with self.assertRaises(ValueError):
            self.store.update('../escape', label='x')
        with self.assertRaises(ValueError):
            self.store.update('default', serial='x' * 129)
        with self.assertRaises(ValueError):
            self.store.update('default', label=' ')
        with self.assertRaises(ValueError):
            self.store.update('default', device_type='bluetooth')
        with self.assertRaises(ValueError):
            self.store.update('default', config=_valid_config(visit_seconds=999))

    def test_corrupt_file_falls_back_to_empty(self):
        self.path.write_text('{broken', encoding='utf-8')
        store = WorkspaceStore(self.path)
        self.assertEqual(store.all(), [])
        store.update('default', label='恢复后可写')
        self.assertEqual(WorkspaceStore(self.path).get('default')['label'], '恢复后可写')

    def test_skips_invalid_entries_on_load(self):
        payload = {'version': 1, 'workspaces': [
            {'id': 'default', 'label': '正常'},
            {'id': '../bad', 'label': '非法标识'},
            {'id': 'deadbeef', 'config': _valid_config(visit_seconds=-1)},
        ]}
        self.path.write_text(json.dumps(payload), encoding='utf-8')
        store = WorkspaceStore(self.path)
        entries = {entry['id']: entry for entry in store.all()}
        self.assertEqual(set(entries), {'default', 'deadbeef'})
        # The valid workspace survives with its caption; the entry with only a
        # broken config keeps the workspace but falls back to default settings.
        self.assertEqual(entries['default']['label'], '正常')
        self.assertNotIn('config', entries['deadbeef'])

    def test_caps_restored_workspaces(self):
        payload = {'version': 1, 'workspaces': [
            {'id': f'{index:032x}', 'label': str(index)} for index in range(20)]}
        self.path.write_text(json.dumps(payload), encoding='utf-8')
        self.assertEqual(WorkspaceStore(self.path).all(), [])


class _FakeDevice:
    device_type = 'uiautomator2'
    serial = 'fake-wifi-phone:5555'
    display_size = (360, 800)
    app_instances = []
    selected_instance = None
    instance_warnings = []

    def current_app(self):
        return {'package': 'org.example.app'}

    def screenshot(self):
        from PIL import Image
        return Image.new('RGB', (360, 800), 'white')


class HubRestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'MIAO_DATA_DIR': self.tmp.name}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_configure_is_persisted_and_restored(self):
        hub = DeviceHub()
        hub.resolve().dispatch('configure', {'config': {'visit_seconds': 11}, 'all_instances': True})
        first = hub.resolve().snapshot()
        self.assertEqual(first['config']['visit_seconds'], 11)

        rebuilt = DeviceHub()
        state = rebuilt.resolve().snapshot()
        self.assertEqual(state['config']['visit_seconds'], 11)
        self.assertIs(state['all_instances'], True)

    def test_rename_and_extra_workspace_restore(self):
        hub = DeviceHub()
        hub.rename('default', '主力机')
        second = hub.add('备用机')
        hub.resolve(second).dispatch('configure', {'config': {'max_tasks': 7}})

        rebuilt = DeviceHub()
        rows = {row_id: controller for row_id, controller in rebuilt.controllers.items()}
        self.assertEqual(set(rows), {'default', second})
        self.assertEqual(rebuilt.resolve('default').workspace_label, '主力机')
        self.assertEqual(rebuilt.resolve(second).snapshot()['config']['max_tasks'], 7)

    def test_removed_workspace_is_not_restored(self):
        hub = DeviceHub()
        second = hub.add('备用机')
        hub.remove(second)
        rebuilt = DeviceHub()
        self.assertEqual(list(rebuilt.controllers), ['default'])

    def test_last_connection_is_saved_but_device_is_not_auto_connected(self):
        hub = DeviceHub()
        controller = hub.resolve()
        fake = _FakeDevice()
        with patch('backend.core.device.adapter.DeviceAdapter', return_value=fake), \
                patch.object(controller, '_start_preview'):
            controller.dispatch('connect', {'device_type': 'uiautomator2', 'serial': fake.serial})
            controller.thread.join(5)
        self.assertFalse(controller.thread.is_alive())
        self.assertEqual(controller.snapshot()['last_connection'],
                         {'type': 'uiautomator2', 'serial': fake.serial})

        rebuilt = DeviceHub()
        state = rebuilt.resolve().snapshot()
        # The address is prefilled for one click, but no connection is attempted.
        self.assertEqual(state['last_connection']['serial'], fake.serial)
        self.assertFalse(state['connected'])
        self.assertIsNone(rebuilt.resolve().device)

    def test_persisted_file_holds_no_runtime_or_secret_fields(self):
        hub = DeviceHub()
        hub.resolve().dispatch('configure', {'config': {'visit_seconds': 6}})
        raw = json.loads((Path(self.tmp.name) / WORKSPACES_FILE).read_text(encoding='utf-8'))
        entry = raw['workspaces'][0]
        self.assertEqual(set(entry) - {'id'}, {'config'})
        forbidden = {'pair_code', 'pairing_code', 'password', 'logs', 'tasks', 'device',
                     'image', 'stats', 'daily', 'token'}
        keys = set(entry) | set(entry['config'])
        self.assertTrue(keys.isdisjoint(forbidden), keys & forbidden)


if __name__ == '__main__':
    unittest.main()
