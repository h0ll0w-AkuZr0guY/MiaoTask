"""Regression checks for persisted settings, browsing contracts and ADB ownership."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from backend.core.device.adb_lifecycle import OwnedAdb
from backend.core.device.comfort import device_comfort
from backend.core.execution.browse_contract import apply_browse_contract
from backend.core.execution.task_policy import TaskPolicy
from backend.core.models import Task
from backend.core.storage.daily_state import DailyState
from backend.core.storage.namespace import StorageNamespace
from backend.core.storage.preferences import read_preferences, save_preferences


class FrameworkPreferencesTests(unittest.TestCase):
    def test_preferences_merge_and_survive_new_reader(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, MIAO_DATA_DIR=folder):
            save_preferences(config={'minimum_volume': True})
            save_preferences(activity_branch='development')
            self.assertEqual(read_preferences(), {'config': {'minimum_volume': True}, 'activity_branch': 'development'})
            self.assertTrue((Path(folder) / 'preferences.json').is_file())

    def test_bad_preferences_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, MIAO_DATA_DIR=folder):
            path = Path(folder) / 'preferences.json'
            path.write_text('broken', encoding='utf-8')
            self.assertEqual(read_preferences(), {})
            self.assertEqual(path.read_text(encoding='utf-8'), 'broken')

    def task(self, title='答题领金币', detail='浏览15秒', **kwargs):
        return Task('example', title, detail, '去完成', (1, 2, 3, 4), kind='manual', skip_reason='答题', **kwargs)

    def test_explicit_browse_instruction_beats_quiz_title(self):
        task = apply_browse_contract(self.task())
        self.assertEqual((task.kind, task.seconds, task.skip_reason), ('browse', 15, ''))
        self.assertFalse(task.browse_once)

    def test_untimed_browse_is_once_and_durable(self):
        task = apply_browse_contract(self.task(detail='浏览页面领取'))
        self.assertTrue(task.browse_once)
        with tempfile.TemporaryDirectory() as folder:
            state = DailyState(folder, 'synthetic', 'uiautomator2')
            policy = TaskPolicy()
            policy.bind(state)
            policy.mark_browse_once(task)
            policy.bind(DailyState(folder, 'synthetic', 'uiautomator2'))
            self.assertIn('今日已尝试一次', policy.reason(task))

    def test_browse_instruction_does_not_override_required_actions(self):
        for title, detail in [('答题', '浏览15秒后完成答题'), ('支付订单', '浏览15秒'), ('邀请好友', '浏览页面'), ('答题', '提交答案')]:
            with self.subTest(title=title, detail=detail):
                self.assertEqual(apply_browse_contract(self.task(title, detail)).kind, 'manual')

    def test_comfort_disabled_makes_no_device_calls(self):
        device = Mock(device_type='uiautomator2', io_lock=None)
        with device_comfort(device, {}):
            pass
        device._checked_shell.assert_not_called()

    def test_comfort_restores_on_exception(self):
        device = Mock(device_type='uiautomator2', io_lock=None)
        device._checked_shell.side_effect = lambda command, **kw: ('[v] volume is 7 in range [0..15]' if '--get' in command else '1' if command[-1] == 'screen_brightness_mode' else '128' if command[-1] == 'screen_brightness' else '')
        with self.assertRaises(RuntimeError):
            with device_comfort(device, {'minimum_volume': True, 'minimum_brightness': True}):
                raise RuntimeError('synthetic stop')
        commands = [call.args[0] for call in device._checked_shell.call_args_list]
        self.assertEqual(commands[-3:], [['settings','put','system','screen_brightness','128'], ['settings','put','system','screen_brightness_mode','1'], ['media','volume','--stream','3','--set','7']])

    def test_adb_cleanup_only_new_matching_process(self):
        with patch.object(OwnedAdb, '_processes', side_effect=[{(1, '10')}, {(1, '10'), (2, '20')}]), patch.object(OwnedAdb, '_powershell') as execute:
            OwnedAdb('adb.exe').close()
            self.assertEqual(execute.call_count, 1)
            self.assertIn('ProcessId=2', execute.call_args.args[0])
            self.assertIn("-eq '20'", execute.call_args.args[0])

    def test_unknown_adb_ownership_never_kills(self):
        with patch.object(OwnedAdb, '_processes', side_effect=[None, {(2, '20')}]), patch.object(OwnedAdb, '_powershell') as execute:
            OwnedAdb('adb.exe').close()
            execute.assert_not_called()

    def test_finished_marker_scoped_to_day_instance_and_activity(self):
        with tempfile.TemporaryDirectory() as folder:
            namespace = StorageNamespace('synthetic', 'daily', False, ())
            def state(day='2026-01-02', instance='org.example@0'):
                return DailyState(folder, 'synthetic', 'uiautomator2', day, instance, namespace)
            original = state()
            self.assertFalse(original.activity_finished())
            original.mark_activity_finished()
            self.assertTrue(state().activity_finished())
            self.assertFalse(state(instance='org.example@999').activity_finished())
            self.assertFalse(state(day='2026-01-03').activity_finished())
            original.save_task_decisions({})
            self.assertFalse(state().activity_finished())
