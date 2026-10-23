"""An unverified application instance must never look executable or use old data."""
from contextlib import ExitStack
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from backend.core.device.instances import AppInstance
from backend.core.service.panel import Controller


class InstanceAvailabilityTests(unittest.TestCase):
    def test_refresh_empty_retains_phone_but_shows_recovery_reason_and_no_daily_store(self):
        controller = Controller()
        device = controller.device = Mock(device_type='uiautomator2', app_instances=[], selected_instance=None,
                                           instance_warnings=['用户空间 999 无法核验'])
        device.refresh_app_instances.return_value = []
        controller.update(connected=True, app_instances=[], selected_instance='')
        with ExitStack() as stack:
            stack.enter_context(patch('backend.core.service.panel.DailyState'))
            stack.enter_context(patch.object(controller, '_save_report'))
            controller.dispatch('refresh_instances', {})
            controller.thread.join(3)
        state = controller.snapshot()
        self.assertTrue(state['connected'])
        self.assertIs(controller.device, device)
        self.assertEqual(state['status'], 'needs_attention')
        self.assertEqual(state['phase'], 'instance_missing')
        self.assertIn('刷新实例', state['message'])
        self.assertIn('999', state['instance_warnings'][0])
        self.assertIsNone(state['daily'])
        with self.assertRaisesRegex(ValueError, '未发现可用应用实例'):
            controller.dispatch('run', {})

    def test_verified_instance_refresh_recovers_to_ready(self):
        controller = Controller()
        target = AppInstance('com.taobao.taobao', 0, 'Synthetic primary')
        device = controller.device = Mock(app_instances=[target], selected_instance=target,
                                           instance_warnings=[])
        device.refresh_app_instances.return_value = [target.to_dict()]
        controller.update(connected=True, status='needs_attention', app_instances=[], selected_instance='')
        with patch.object(controller, '_ensure_daily'), patch.object(controller, '_persist_state'), patch.object(controller, '_save_report'):
            controller.dispatch('refresh_instances', {})
            controller.thread.join(3)
        state = controller.snapshot()
        self.assertEqual(state['status'], 'ready')
        self.assertEqual(state['selected_instance'], target.id)
        self.assertEqual(state['instance_warnings'], [])

    def test_missing_selection_does_not_claim_ready_even_if_other_instances_exist(self):
        controller = Controller()
        controller.device = Mock(instance_warnings=[])
        controller.update(connected=True, app_instances=[dict(id='synthetic@0')], selected_instance='')
        controller._instance_availability('Ready')
        self.assertEqual(controller.state['status'], 'needs_attention')
        self.assertIn('未自动切换', controller.state['message'])
        self.assertEqual(controller.state['selected_instance'], '')

    def test_empty_enumeration_cannot_bind_or_overwrite_a_primary_daily_record(self):
        controller = Controller()
        controller.device = Mock(app_instances=[], selected_instance=None)
        controller.daily = Mock()
        with patch('backend.core.service.panel.DailyState') as store:
            self.assertFalse(controller._ensure_daily())
        store.assert_not_called()
        self.assertIsNone(controller.daily)
        self.assertEqual(controller.state['task_scope'], '')


if __name__ == '__main__':
    unittest.main()
