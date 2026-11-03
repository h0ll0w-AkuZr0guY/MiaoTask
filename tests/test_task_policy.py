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



class ResetPolicyTests(unittest.TestCase):
    def test_reset_preserves_cumulative_statistics_and_other_instance(self):
        import tempfile
        from backend.core.storage.daily_state import DailyState
        from backend.core.execution.task_policy import TaskPolicy
        with tempfile.TemporaryDirectory() as folder:
            store = DailyState(folder, 'synthetic-reset', 'uiautomator2', instance_id='sample@0')
            other = DailyState(folder, 'synthetic-reset', 'uiautomator2', instance_id='sample@999')
            store.progress('first', {'completed': 7, 'attempts': 9})
            decisions = {'a'*24: {'skip': True, 'failures': 2, 'exhausted': True}}
            store.save_task_decisions(decisions)
            other.save_task_decisions(decisions)
            policy = TaskPolicy(); policy.bind(store); policy.reset()
            self.assertEqual(store.task_decisions(), {})
            self.assertEqual(store.snapshot()['stats']['completed'], 7)
            self.assertEqual(other.task_decisions(), decisions)

class ProgressBudgetTests(unittest.TestCase):
    def test_verified_progress_resets_failures_but_keeps_skip(self):
        from backend.core.execution.task_policy import TaskPolicy, task_identity
        from backend.core.models import Task
        t=Task('synthetic','浏览','浏览15秒','去完成',(1,2,3,4),total=10)
        policy=TaskPolicy();policy.fail(t,2);policy.choose(task_identity(t),True)
        policy.progress(t)
        self.assertEqual(policy.failures(t),0)
        self.assertIn('用户选择',policy.reason(t))


class DefaultHeadlinePolicyTests(unittest.TestCase):
    def test_headline_skip_is_owned_by_editable_words_and_queue_cannot_bypass_it(self):
        task=Task('headline','头条刷热点领现金','点击去逛','去完成',(10,20,100,80),kind='visit',target_app='头条')
        runner=CampaignRunner(Mock(device_type='uiautomator2',selected_instance=None))
        self.assertIn('常驻关键词',runner.availability_reason(task))
        runner.task_policy.choose(task_identity(task),False)
        self.assertIn('常驻关键词',runner.availability_reason(task))
        runner.config['skip_task_keywords'].remove('头条')
        self.assertEqual(runner.availability_reason(task),'')
        runner.task_policy.reset()
        self.assertEqual(runner.availability_reason(task),'')

    def test_explicit_enable_intent_survives_daily_reload(self):
        with tempfile.TemporaryDirectory() as folder:
            store=DailyState(Path(folder),'synthetic-device','uiautomator2','2026-01-01')
            policy=TaskPolicy();policy.bind(store)
            task=Task('test','headline','','',(10,20,100,80),kind='visit')
            policy.choose(task_identity(task),False)
            restored=TaskPolicy();restored.bind(DailyState(Path(folder),'synthetic-device','uiautomator2','2026-01-01'))
            self.assertTrue(restored.explicitly_enabled(task))


class PersistentKeywordTests(unittest.TestCase):
    def test_keyword_validation_separators_limits_and_literal_metacharacters(self):
        from backend.core.config.schema import keyword_list
        self.assertEqual(keyword_list('微博；快手; 头条\nA+B 微博', 'skip'), ['微博','快手','头条','A+B'])
        self.assertEqual(keyword_list('； 微博； ', 'skip'), ['微博'])
        for value in (None, 2, [''], ['x'*41], ['two words'], ['x']*101):
            with self.subTest(value=value), self.assertRaises(ValueError):
                keyword_list(value, 'skip')

    def test_keyword_preferences_survive_activity_switch_and_task_mark_reset(self):
        from backend.core.storage.preferences import save_preferences, read_preferences
        from backend.core.config.loader import load_config, framework_overrides
        import os
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, MIAO_DATA_DIR=folder):
            config = load_config()
            config.update(task_search_keywords=['手机壳','2026'], skip_task_keywords=['weibo','A+B'])
            save_preferences(config=framework_overrides(config))
            restored = load_config(app='zfb', activity='yueyuezhuan', overrides=read_preferences()['config'])
            self.assertEqual(restored['skip_task_keywords'], ['weibo','A+B'])
            runner = CampaignRunner(Mock(device_type='uiautomator2', selected_instance=None), config)
            task = Task('test', '浏览 Weibo', '浏览15秒', '去完成', (1,2,3,4))
            self.assertIn('常驻关键词', runner.availability_reason(task))
            runner.task_policy.choose(task_identity(task), False)
            runner.reset_task_marks()
            self.assertIn('常驻关键词', runner.availability_reason(task))
            runner.config['skip_task_keywords'] = []
            self.assertEqual(runner.availability_reason(task), '')
            self.assertEqual(read_preferences()['config']['skip_task_keywords'], ['weibo','A+B'])

    def test_skip_is_literal_and_covers_details_without_reclassifying_done(self):
        from backend.core.execution.task_policy import keyword_skip_reason
        task = Task('test', '看活动', '访问A+B频道', '去完成', (1,2,3,4))
        self.assertTrue(keyword_skip_reason(task, ['a+b']))
        self.assertEqual(keyword_skip_reason(task, ['a.b']), '')
        self.assertEqual(keyword_skip_reason(replace(task, kind='done'), ['a+b']), '')

    def test_search_words_are_random_choices_without_replacing_activity_entry(self):
        runner = CampaignRunner(Mock(device_type='uiautomator2', selected_instance=None))
        entry = runner.config['search_keyword']
        runner.config['task_search_keywords'] = ['手机壳','2026']
        with patch('backend.core.execution.session.random.choice',side_effect=['2026','2026','手机壳']) as choose:
            self.assertEqual([runner.next_search_keyword() for _ in range(3)], ['2026','2026','手机壳'])
            self.assertEqual(choose.call_count,3)
            choose.assert_called_with(['手机壳','2026'])
        self.assertEqual(runner.config['search_keyword'], entry)
        runner.config['task_search_keywords'] = []
        self.assertEqual(runner.next_search_keyword(), runner.config.get('task_search_keyword','123'))

    def test_all_registered_activities_share_keyword_skip_contract(self):
        from backend.core.registry import Registry
        from backend.core.config.loader import load_config
        registry=Registry(Path(__file__).resolve().parents[1]/'backend/app').discover()
        for app in registry.catalog():
            for activity in app['activities']:
                with self.subTest(app=app['id'],activity=activity['id']):
                    config=load_config(app=app['id'],activity=activity['id'],registry=registry,
                                       overrides={'skip_task_keywords':['测试频道']})
                    runner=registry.instantiate(Mock(device_type='uiautomator2',selected_instance=None),
                        config,app=app['id'],activity=activity['id'])
                    task=Task('test','浏览测试频道','浏览15秒','去完成',(1,2,3,4))
                    self.assertIn('常驻关键词',runner.availability_reason(task))

    def test_queue_button_cannot_override_permanent_skip(self):
        helper=TaskPolicyTests()
        with tempfile.TemporaryDirectory() as folder:
            helper.root=Path(folder);helper.policy=TaskPolicy()
            with patch('backend.core.service.panel.data_dir',return_value=helper.root):
                controller=helper.controller()
                controller.state['config']['skip_task_keywords']=['随心']
                row=controller.state['tasks'][0]
                with self.assertRaisesRegex(ValueError,'常驻关键词'):
                    controller.dispatch('task_execute',dict(task_id=row['task_id'],task_scope=controller.state['task_scope']))
                self.assertIsNone(controller.thread)
                controller.device.click.assert_not_called()


if __name__ == '__main__':
    unittest.main()


class DurableDeferralTests(unittest.TestCase):
    def test_real_daily_record_reopens_without_losing_counts_or_choices(self):
        with tempfile.TemporaryDirectory() as folder:
            def reopen():
                return DailyState(folder,'synthetic-reopen','uiautomator2','2026-01-01')
            store=reopen();policy=TaskPolicy();policy.bind(store)
            task=Task('x','browse','15s','go',(1,2,3,4),kind='browse')
            other=replace(task,title='other')
            store.progress('run',{'completed':7,'attempts':9})
            policy.choose(task_identity(other),True)
            self.assertTrue(policy.defer(task,'settlement'))
            restored=reopen()
            self.assertFalse(restored.recovery_warning)
            self.assertEqual(restored.snapshot()['stats']['completed'],7)
            again=TaskPolicy();again.bind(restored)
            self.assertFalse(again.defer(task,'settlement'))
            self.assertIn('用户选择',again.reason(other))
            again.fail(task,2)
            again.bind(reopen())
            self.assertEqual(again.failures(task),1)
            self.assertFalse(again.defer(task,'settlement'))

    def test_malformed_deferred_values_are_rejected(self):
        for value in ({'settlement':True},{'unknown':1},{'navigation':-1},['settlement']):
            with self.subTest(value=value),tempfile.TemporaryDirectory() as folder:
                store=DailyState(folder,'synthetic-invalid','uiautomator2','2026-01-01')
                store.save_task_decisions({'a'*24:{'deferred':value}})
                reopened=DailyState(folder,'synthetic-invalid','uiautomator2','2026-01-01')
                self.assertTrue(reopened.recovery_warning)


class WordlistMigrationTests(unittest.TestCase):
    def test_legacy_empty_lists_migrate_once_and_keep_other_preferences(self):
        from backend.core.storage.preferences import read_preferences,save_preferences
        with tempfile.TemporaryDirectory() as folder,patch.dict('os.environ',{'MIAO_DATA_DIR':folder}):
            path=Path(folder)/'preferences.json'
            path.write_text(json.dumps({'config':{'task_search_keywords':[],'skip_task_keywords':[],
                'default_skip_task_patterns':['头条'],'minimum_volume':True},'activity_branch':'dev'}),encoding='utf-8')
            prefs=read_preferences()
            self.assertTrue(prefs['config']['minimum_volume'])
            self.assertEqual(prefs['activity_branch'],'dev')
            self.assertIn('头条',prefs['config']['skip_task_keywords'])
            self.assertNotIn('default_skip_task_patterns',prefs['config'])
            self.assertEqual(prefs['task_wordlists_schema'],1)
            config=prefs['config'];config.update(task_search_keywords=[],skip_task_keywords=[])
            save_preferences(config=config)
            self.assertEqual(read_preferences()['config']['skip_task_keywords'],[])
            self.assertEqual(read_preferences()['config']['task_search_keywords'],[])

    def test_legacy_custom_lists_remain_exactly_as_entered(self):
        from backend.core.storage.preferences import read_preferences
        with tempfile.TemporaryDirectory() as folder,patch.dict('os.environ',{'MIAO_DATA_DIR':folder}):
            config={'task_search_keywords':['custom'],'skip_task_keywords':['微博']}
            (Path(folder)/'preferences.json').write_text(json.dumps({'config':config}),encoding='utf-8')
            self.assertEqual(read_preferences()['config'],config)

    def test_removing_default_words_really_enables_parsed_tasks(self):
        from backend.app.taobao.activities.double11.tasks import parse_tasks
        for title in ('玩游戏','淘金币等级考试','邀请好友','去关注店铺','抽奖一次'):
            page=screen()
            page.nodes[2].text=title+'(0/1)'
            task=parse_tasks(page.nodes,1200,2400)[0]
            runner=CampaignRunner(Mock(device_type='uiautomator2',selected_instance=None))
            self.assertIn('常驻关键词',runner.availability_reason(task))
            runner.config['skip_task_keywords']=[]
            runner.remember([task])
            self.assertEqual(runner.records[task.key]['status'],'pending')
            self.assertTrue(runner.records[task.key]['controllable'])

    def test_shared_words_apply_to_daily_and_claim_rows_too(self):
        from backend.core.registry import Registry
        registry=Registry(Path(__file__).resolve().parents[1]/'backend/app').discover()
        runner=registry.instantiate(Mock(device_type='uiautomator2',selected_instance=None),app='taobao',activity='coins')
        title='邀请好友'
        result=runner.classify_daily(title,'点击去逛',[title,'点击去逛'],'领取奖励',0,1)
        task=Task('sample',title,'点击去逛','领取奖励',(1,2,3,4),kind=result[1])
        self.assertIn('常驻关键词',runner.availability_reason(task))
        runner.config['skip_task_keywords']=[]
        self.assertEqual(runner.availability_reason(task),'')
