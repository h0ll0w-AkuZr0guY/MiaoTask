"""Synthetic queue decisions must survive restarts without operating stale rows."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from backend.core.errors import NeedsAttention, Stopped
from backend.core.models import Task, TextNode
from backend.core.device.screen import Screen
from backend.core.execution.task_policy import TaskPolicy, task_identity
from backend.core.storage.daily_state import DailyState
from backend.core.service.panel import Controller
from backend.app.taobao.activities.double11.activity import CampaignRunner


def screen():
    return Screen([TextNode('做任务赚能量', (20, 200, 600, 260)),
                   TextNode('累计任务奖励', (20, 300, 600, 360)),
                   TextNode('好物随心逛(0/10)', (200, 650, 800, 710)),
                   TextNode('浏览15秒 +400', (200, 730, 800, 780)),
                   TextNode('去完成', (950, 680, 1100, 740))],
                  1200, 2400, 'com.taobao.taobao')


class TaskPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.make_store()
        self.policy = TaskPolicy()
        self.policy.bind(self.store)

    def make_store(self, **kwargs):
        return DailyState(self.root, kwargs.pop('serial', 'synthetic-phone'),
                          'uiautomator2', kwargs.pop('day', '2026-01-01'), **kwargs)

    def runner(self, policy=None, emit=None):
        device = Mock(device_type='uiautomator2', selected_instance=None)
        runner = CampaignRunner(device, task_policy=policy or self.policy, emit=emit)
        runner.config.update(max_attempts=2, max_tasks=3, max_scroll_pages=0)
        # 这些用例测的是队列决策本身（一轮内的选择/跳过/计数），补扫会把同一屏
        # 再跑几轮，把计数搅乱，所以显式关掉。
        runner.config['rescan'] = {'rescan_enabled': False}
        runner.control.wait = Mock()
        runner.ensure_list = Mock(return_value=screen())
        runner.read = Mock(return_value=screen())
        runner.execute = Mock(side_effect=lambda task, before: (before, False, False))
        return runner

    def test_identity_ignores_position_progress_and_ui_button(self):
        task = Task('temporary', '合成任务', '', '去完成', (0, 0, 40, 40), total=5)
        moved = replace(task, key='other', current=2, bounds=(0, 200, 40, 240), action='领取')
        self.assertEqual(task_identity(task), task_identity(moved.to_dict()))
        for changed in (replace(task, title='替换任务'), replace(task, total=10),
                        replace(task, seconds=20), replace(task, target_app='示例应用'),
                        replace(task, kind='search')):
            self.assertNotEqual(task_identity(task), task_identity(changed))

    def test_failures_survive_runner_and_store_restarts(self):
        first = self.runner()
        self.assertEqual(first.run()['attempts'], 2)
        policy = TaskPolicy()
        policy.bind(self.make_store())
        second = self.runner(policy)
        self.assertEqual(second.run()['attempts'], 0)
        second.execute.assert_not_called()
        self.assertTrue(policy.reason(second.tasks(screen())[0]))

    def test_manual_execute_reenables_queue_without_starting_anything(self):
        runner = self.runner()
        runner.run()
        task = runner.tasks(screen())[0]
        self.policy.choose(task_identity(task), False)
        runner.stats.attempts = 0
        runner.config['max_tasks'] = 1
        runner.run()
        self.assertEqual(runner.stats.attempts, 1)

    def test_skip_persists_and_isolated_by_day_device_and_instance(self):
        task = self.runner().tasks(screen())[0]
        self.policy.choose(task_identity(task), True)
        for options, expected in (({}, True), ({'day': '2026-01-02'}, False),
                                  ({'serial': 'synthetic-other'}, False),
                                  ({'instance_id': 'synthetic-instance'}, False)):
            policy = TaskPolicy()
            policy.bind(self.make_store(**options))
            self.assertEqual(bool(policy.reason(task)), expected)

    def test_live_skip_after_observation_prevents_next_click(self):
        once = []
        def event(**data):
            rows = data.get('tasks', [])
            if rows and not once:
                once.append(True)
                self.policy.choose(rows[0]['task_id'], True)
        runner = self.runner(emit=event)
        self.assertEqual(runner.run()['attempts'], 0)
        runner.execute.assert_not_called()

    def test_rotating_row_reuses_runtime_slot_but_not_old_user_choice(self):
        runner = self.runner()
        first = runner.tasks(screen())[0]
        self.policy.choose(task_identity(first), True)
        replacement = screen()
        replacement.nodes[2].text = '另一条浏览任务(0/10)'
        changed = runner.tasks(replacement)[0]
        self.assertEqual(first.key, changed.key)
        self.assertNotEqual(task_identity(first), task_identity(changed))
        runner.remember([changed])
        self.assertTrue(runner.records[changed.key]['enabled'])

    def test_unknown_external_offer_and_search_share_generic_failure_limit(self):
        for title in ('去示例商店逛一逛(0/10)', '搜一搜你喜欢的宝贝(0/10)'):
            with self.subTest(title=title):
                current = screen()
                current.nodes[2].text = title
                first = self.runner()
                first.ensure_list.return_value = current
                first.read.return_value = current
                self.assertEqual(first.run()['attempts'], 2)
                new = self.runner()
                new.ensure_list.return_value = current
                self.assertEqual(new.run()['attempts'], 0)
                new.execute.assert_not_called()

    def test_stopping_or_authentication_does_not_exhaust_retry_budget(self):
        for error in (Stopped(), NeedsAttention('请本人认证')):
            runner = self.runner()
            runner.execute.side_effect = error
            with self.assertRaises(type(error)):
                runner.run()
            self.assertFalse(self.store.task_decisions())

    def test_terminal_recovery_failure_charges_once_per_attempt(self):
        runner = self.runner()
        runner.execute.side_effect = NeedsAttention('返回失败', kind='navigation')
        runner.recover = Mock(side_effect=NeedsAttention('恢复失败', kind='navigation'))
        with self.assertRaises(NeedsAttention):
            runner.run()
        self.assertEqual(self.policy.failures(runner.tasks(screen())[0]), 1)

    def test_legacy_daily_record_without_decisions_remains_loadable(self):
        data = json.loads(self.store.stats_path.read_text())
        data.pop('task_decisions')
        self.store.stats_path.write_text(json.dumps(data))
        restored = self.make_store()
        self.assertFalse(restored.recovery_warning)
        self.assertEqual(restored.task_decisions(), {})

    def test_malformed_decision_is_quarantined(self):
        data = json.loads(self.store.stats_path.read_text())
        data['task_decisions'] = {'bad': {'failures': 'wrong'}}
        self.store.stats_path.write_text(json.dumps(data))
        self.assertTrue(self.make_store().recovery_warning)

    def controller(self):
        controller = Controller()
        controller.device = Mock(serial='synthetic-phone', identity_serial='synthetic-phone',
                                 selected_instance=None, device_type='uiautomator2')
        controller.device.io_lock = threading.RLock()
        controller._ensure_daily()
        runner = self.runner()
        runner.remember(runner.tasks(screen()))
        controller.update(tasks=list(runner.records.values()), status='ready', connected=True)
        return controller

    def test_buttons_only_change_queue_and_reject_stale_scope_or_manual_row(self):
        with patch('backend.core.service.panel.data_dir', return_value=self.root):
            controller = self.controller()
            row = controller.state['tasks'][0]
            payload = dict(task_id=row['task_id'], task_scope=controller.state['task_scope'])
            controller.dispatch('task_skip', payload)
            self.assertFalse(row['enabled'])
            controller.dispatch('task_execute', payload)
            self.assertTrue(row['enabled'])
            self.assertIsNone(controller.thread)
            controller.device.click.assert_not_called()
            with self.assertRaises(ValueError):
                controller.dispatch('task_skip', dict(payload, task_scope='stale'))
            row['controllable'] = False
            with self.assertRaises(ValueError):
                controller.dispatch('task_execute', payload)

    def test_running_selection_is_deferred_and_does_not_stop_worker(self):
        with patch('backend.core.service.panel.data_dir', return_value=self.root):
            controller = self.controller()
            row = controller.state['tasks'][0]
            controller.thread = Mock()
            controller.thread.is_alive.return_value = True
            controller.update(status='running')
            row['status'] = 'running'
            controller.dispatch('task_skip', dict(task_id=row['task_id'], task_scope=controller.state['task_scope']))
            self.assertEqual(row['status'], 'running')
            self.assertFalse(row['enabled'])
            self.assertFalse(controller.control.stopped.is_set())

    def test_day_rollover_rejects_prior_scope(self):
        with patch('backend.core.service.panel.data_dir', return_value=self.root):
            with patch('backend.core.service.panel.local_day', return_value='2026-01-01'):
                controller = self.controller()
                row = controller.state['tasks'][0]
                payload = dict(task_id=row['task_id'], task_scope=controller.state['task_scope'])
            with patch('backend.core.service.panel.local_day', return_value='2026-01-02'):
                with self.assertRaisesRegex(ValueError, '日期已变化'):
                    controller.dispatch('task_skip', payload)


if __name__ == '__main__':
    unittest.main()
