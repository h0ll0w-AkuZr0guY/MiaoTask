"""京东东东农场：解析、跳过规则与核验。

坐标取自真机（2026-11-03，1080x2400）：抽屉标题在 y≈621-765，
每行是「标题(进度) 在左、说明在下方、按钮在右」。
"""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from backend.core.config.loader import load_config
from backend.core.device.screen import Screen
from backend.core.models import TextNode
from backend.core.registry import Registry

PACKAGE = 'com.jingdong.app.mall'


def make_runner():
    registry = Registry(Path(__file__).resolve().parents[1]/'backend/app').discover()
    config = load_config(app='jd', activity='dongdong_farm', registry=registry)
    runner = registry.instantiate(SimpleNamespace(device_type='uiautomator2', selected_instance=None),
                                  config, app='jd', activity='dongdong_farm')
    runner.config['ocr'] = False
    runner.update = Mock()
    runner.setup()
    return runner


def panel_screen(*rows, width=1080, height=2400, extra=()):
    """按真机布局拼一屏任务面板：rows 为 (标题, 说明, 按钮文案)。"""
    nodes = [TextNode('东东农场', (0, 0, width, height)),
             TextNode('做任务 领水滴', (0, 621, width, 765))]
    top = 801
    for title, detail, action in rows:
        nodes.append(TextNode(title, (189, top, 810, top + 63)))
        nodes.append(TextNode(detail, (189, top + 69, 768, top + 117)))
        nodes.append(TextNode(action, (837, top + 18, 1026, top + 102)))
        top += 225
    nodes.extend(TextNode(text, bounds) for text, bounds in extra)
    return Screen(nodes, width, height, PACKAGE)


def farm_home_screen(width=1080, height=2400):
    return Screen([
        TextNode('东东农场', (0, 0, width, height)),
        TextNode('这里是要浇水的大树', (138, 558, 942, 1407)),
        TextNode('水滴剩余45克 点击水壶浇水', (783, 1485, 1026, 1641)),
        TextNode('点击前往签到页面', (21, 1521, 198, 1710)),
        TextNode('点击前往做任务领水滴页面', (402, 1521, 576, 1710)),
        TextNode('领 10水滴', (30, 399, 177, 546)),
    ], width, height, PACKAGE)


class PanelParsingTests(unittest.TestCase):
    def setUp(self):
        self.runner = make_runner()

    def test_rows_become_browse_claim_and_explicit_skips(self):
        screen = panel_screen(
            ('养猪猪抽1888现金(0/1)', '浏览6秒，奖励+20g', '去完成'),
            ('精选活动推荐(1/7)', '浏览6秒，奖励+10g', '去领奖'),
            ('【农场专供】品质保证(0/2)', '下1单奖励+3000g', '去下单'),
            ('去小程序邀请好友助力(0/20)', '邀请1人奖励+180g', '去完成'),
            ('玩消消乐得红包(0/1)', '通过1关，奖励+10g', '去完成'),
            ('去快手极速版领金币(0/1)', '完成任务奖励+150g', '去完成'),
            ('添加并访问农场小组件(0/1)', '完成任务奖励+100g', '去完成'),
            ('至高领100元话费(0/1)', '完成任务奖励+100g', '去完成'),
            ('逛PLUS会员中心(0/1)', '浏览6秒，奖励+10g', '逛一逛'),
            ('去开启定时领水通知(0/1)', '完成任务奖励+50g', '去完成'),
        )
        tasks = self.runner.parse_tasks(screen.nodes, screen.width, screen.height)
        by_title = {task.title: task for task in tasks}
        self.assertEqual(by_title['养猪猪抽1888现金'].kind, 'browse')
        self.assertEqual(by_title['养猪猪抽1888现金'].seconds, 6)
        self.assertEqual(by_title['精选活动推荐'].kind, 'claim')
        self.assertEqual(by_title['精选活动推荐'].current, 1)
        self.assertEqual(by_title['精选活动推荐'].total, 7)
        for title, word in (('【农场专供】品质保证', '下单'),
                            ('去小程序邀请好友助力', '邀请'),
                            ('玩消消乐得红包', '通关'),
                            ('添加并访问农场小组件', '小组件'),
                            ('至高领100元话费', '金融'),
                            ('去开启定时领水通知', '通知')):
            with self.subTest(title=title):
                task = by_title[title]
                self.assertEqual(task.kind, 'manual')
                self.assertIn(word, task.skip_reason)

    def test_external_app_tasks_target_the_right_package(self):
        """站外任务不跳过：带 target_app/target_packages，交给跨应用流程。"""
        screen = panel_screen(
            ('去快手极速版领金币(0/1)', '完成任务奖励+150g', '去完成'),
            ('去百度APP领现金(0/1)', '访问并登录奖励+50g', '去完成'),
            ('去百度极速版领现金(0/1)', '访问并登录奖励+10g', '去完成'),
            ('逛PLUS会员中心(0/1)', '浏览6秒，奖励+10g', '逛一逛'),
        )
        tasks = self.runner.parse_tasks(screen.nodes, screen.width, screen.height)
        by_title = {task.title: task for task in tasks}
        self.assertEqual(by_title['去快手极速版领金币'].target_app, '快手极速版')
        self.assertIn('com.kuaishou.nebula', by_title['去快手极速版领金币'].target_packages)
        self.assertEqual(by_title['去百度APP领现金'].target_app, '百度')
        self.assertEqual(by_title['去百度极速版领现金'].target_app, '百度极速版')
        self.assertIn('com.baidu.searchbox.lite',
                      by_title['去百度极速版领现金'].target_packages)
        # 站外任务给足停留时间；站内的 PLUS 中心不算站外，按普通浏览计 6 秒
        self.assertEqual(by_title['去快手极速版领金币'].seconds,
                         self.runner.config['cross_app_seconds'])
        self.assertEqual(by_title['逛PLUS会员中心'].target_app, '')
        self.assertEqual(by_title['逛PLUS会员中心'].kind, 'browse')

    def test_reward_and_continue_buttons_are_recognised(self):
        """领奖按钮还叫「领奖励」；(1/1) 上的「继续逛」是已做完，不能再点。"""
        screen = panel_screen(
            ('读一本小说最高赚20(1/1)', '读6s书得奖励+10g', '领奖励'),
            ('逛家电家居领京豆(1/1)', '浏览6秒，奖励+10g', '继续逛'),
            ('精选活动推荐(1/7)', '浏览6秒，奖励+10g', '继续逛'),
        )
        tasks = {task.title: task for task in
                 self.runner.parse_tasks(screen.nodes, screen.width, screen.height)}
        self.assertEqual(tasks['读一本小说最高赚20'].kind, 'claim')
        # 进度已经满了，再点「继续逛」不会有新进度，只会把失败次数耗光
        self.assertEqual(tasks['逛家电家居领京豆'].kind, 'done')
        self.assertEqual(tasks['精选活动推荐'].kind, 'browse')

    def test_watering_row_is_a_visit_and_stops_at_the_run_cap(self):
        screen = panel_screen(('每日累计浇水20次(1/20)', '完成任务，奖励+40g', '去浇水'))
        task = self.runner.parse_tasks(screen.nodes, screen.width, screen.height)[0]
        self.assertEqual(task.kind, 'visit')
        self.assertEqual(task.action, 'browse')
        self.runner._water_actions = self.runner.config['water_times_per_run']
        capped = self.runner.parse_tasks(screen.nodes, screen.width, screen.height)[0]
        self.assertEqual(capped.kind, 'manual')
        self.assertIn('浇水次数', capped.skip_reason)

    def test_farm_home_is_not_a_task_list(self):
        self.assertFalse(self.runner.is_task_list(farm_home_screen().nodes))
        self.assertTrue(self.runner.is_task_list(
            panel_screen(('养猪猪抽1888现金(0/1)', '浏览6秒，奖励+20g', '去完成')).nodes))

    def test_home_entry_ignores_the_full_screen_container(self):
        home = Screen([
            TextNode('东东农场', (0, 0, 1080, 2400)),            # 农场上铺满全屏的容器
            TextNode('东东农场', (194, 455, 388, 616)),           # 首页金刚区图标
            TextNode('我的', (846, 2311, 1018, 2352)),
        ], 1080, 2400, PACKAGE)
        node = self.runner.home_entry_node(home)
        self.assertIsNotNone(node)
        self.assertEqual(node.center, (291, 535))
        self.assertTrue(self.runner.homepage_visible(home))


class NavigationTests(unittest.TestCase):
    """恢复失败的真机现场：京东小程序/H5 盖住了农场，返回键又退不出来。"""

    def setUp(self):
        self.runner = make_runner()

    def test_mini_program_page_is_foreign_and_has_close_button(self):
        manto = Screen([
            TextNode('更多', (867, 147, 951, 231)),
            TextNode('关闭', (951, 147, 1035, 231)),
            TextNode('点击钉子回收到盒子里', (156, 519, 924, 585)),
        ], 1080, 2400, PACKAGE)
        self.assertTrue(self.runner.foreign_page(manto))
        node = self.runner.page_close_node(manto)
        self.assertIsNotNone(node)
        self.assertEqual(node.center, (993, 189))
        # 恢复流程会把「入口」指向这个关闭控件：点一下就能退出小程序
        self.assertEqual(self.runner.task_panel_entry(manto).center, (993, 189))

    def test_farm_home_and_panel_are_not_foreign(self):
        self.assertFalse(self.runner.foreign_page(farm_home_screen()))
        panel = panel_screen(('养猪猪抽1888现金(0/1)', '浏览6秒，奖励+20g', '去完成'))
        self.assertFalse(self.runner.foreign_page(panel))
        self.assertIsNone(self.runner.page_close_node(panel))

    def test_other_app_is_not_foreign_page(self):
        kuaishou = Screen([TextNode('返回', (33, 120, 153, 240))], 1080, 2400,
                          'com.kuaishou.nebula')
        self.assertFalse(self.runner.foreign_page(kuaishou))


class ClaimSweepTests(unittest.TestCase):
    """领奖必须一次领干净：漏一条，对应的任务行就卡在「去领奖」不动。"""

    class FakeControl:
        def __init__(self):
            self.t = 0.0

        def check(self):
            pass

        def wait(self, seconds=0):
            self.t += max(0.0, seconds or 0)
            return self.t

        def now(self):
            return self.t

    def setUp(self):
        self.runner = make_runner()

    def screen(self, *rows):
        return panel_screen(*rows)

    def play(self, screens):
        """让 read() 依次返回给定屏幕（末尾重复），并记录点击。"""
        queue = list(screens)
        self.runner.read = lambda force_ocr=False: queue.pop(0) if len(queue) > 1 else queue[0]
        self.runner.control = self.FakeControl()
        self.runner.clicked = []
        self.runner.click = lambda node: self.runner.clicked.append(node)

    def test_claim_row_without_progress_is_still_parsed(self):
        """「抢20元外卖金券」这类行不带 (x/y)，也必须认成待领奖。"""
        screen = self.screen(
            ('抢20元外卖金券', '已完成，点击领取', '领奖励'),
            ('去百度APP领现金(1/1)', '浏览6秒，奖励+50g', '去领奖'),
        )
        tasks = {t.title: t for t in
                 self.runner.parse_tasks(screen.nodes, screen.width, screen.height)}
        self.assertEqual(tasks['抢20元外卖金券'].kind, 'claim')
        self.assertEqual(tasks['去百度APP领现金'].kind, 'claim')

    def test_same_title_rows_are_not_collapsed(self):
        screen = panel_screen(
            ('抢20元外卖金券(1/1)', '已完成，点击领取', '领奖励'),
            ('抢20元外卖金券(1/1)', '已完成，点击领取', '领奖励'),
        )
        tasks = self.runner.parse_tasks(screen.nodes, screen.width, screen.height)
        self.assertEqual(len(tasks), 2)

    def test_sweep_claims_every_visible_reward(self):
        first = self.screen(
            ('抢20元外卖金券(1/1)', '已完成，点击领取', '领奖励'),
            ('读小说最高赚20(1/1)', '读6s书得奖励+10g', '去领奖'),
            ('养猪猪抽1888现金(0/1)', '浏览6秒，奖励+20g', '去完成'),
        )
        second = self.screen(
            ('读小说最高赚20(1/1)', '读6s书得奖励+10g', '去领奖'),
            ('养猪猪抽1888现金(0/1)', '浏览6秒，奖励+20g', '去完成'),
        )
        third = self.screen(
            ('养猪猪抽1888现金(0/1)', '浏览6秒，奖励+20g', '去完成'),
        )
        self.play([first, second, third])
        screen = self.runner.sweep_claims(first)
        self.assertEqual(len(self.runner.clicked), 2)
        self.assertEqual(self.runner.stats.claims, 2)
        self.assertEqual([t.title for t in self.runner.tasks(screen)],
                         ['养猪猪抽1888现金'])

    def test_claim_that_does_not_change_is_retried_once(self):
        stuck = self.screen(('抢20元外卖金券(1/1)', '已完成，点击领取', '领奖励'))
        self.play([stuck])
        task = self.runner.parse_tasks(stuck.nodes, 1080, 2400)[0]
        screen, ok = self.runner.claim_row(stuck, task)
        self.assertFalse(ok)
        self.assertEqual(len(self.runner.clicked), 2)   # claim_retry 的两次
        self.assertEqual(self.runner.stats.claims, 0)

    def test_running_flag_is_not_reset_by_setup(self):
        """setup() 在 run() 之后被调用；它把 _running 抹掉会让签到/浇水整段不执行。"""
        runner = make_runner()
        runner.setup()
        self.assertFalse(runner._running)
        runner._running = True
        runner.setup()
        self.assertTrue(runner._running)

    def test_claim_records_that_left_the_panel_are_closed(self):
        self.runner.records = {
            'gone#claim': dict(kind='claim', title='抢20元外卖金券', status='pending', current=1),
            'live#claim': dict(kind='claim', title='养猪猪抽1888现金', status='pending', current=1),
        }
        self.runner._swept_titles = {'养猪猪抽1888现金'}
        self.runner._drop_stale_claims()
        self.assertEqual(self.runner.records['gone#claim']['status'], 'done')
        self.assertEqual(self.runner.records['live#claim']['status'], 'pending')


class FarmVerificationTests(unittest.TestCase):
    def setUp(self):
        self.runner = make_runner()
        self.before = panel_screen(('养猪猪抽1888现金(0/1)', '浏览6秒，奖励+20g', '去完成'))
        self.task = self.runner.parse_tasks(self.before.nodes, 1080, 2400)[0]

    def test_progress_growth_is_success(self):
        after = panel_screen(('养猪猪抽1888现金(1/1)', '浏览6秒，奖励+20g', '去领奖'))
        self.assertTrue(self.runner.verify(self.task, self.before, after))

    def test_missing_row_means_claimed(self):
        after = panel_screen(('精选活动推荐(0/7)', '浏览6秒，奖励+10g', '去完成'))
        self.assertTrue(self.runner.verify(self.task, self.before, after))

    def test_reward_popup_counts_even_outside_the_panel(self):
        popup = Screen([TextNode('恭喜获得奖励', (114, 909, 966, 999)),
                        TextNode('开心收下', (162, 1407, 918, 1539))], 1080, 2400, PACKAGE)
        self.assertTrue(self.runner.verify(self.task, self.before, popup))

    def test_unchanged_row_is_not_success(self):
        self.assertFalse(self.runner.verify(self.task, self.before, self.before))

    def test_leaving_the_panel_without_evidence_is_not_success(self):
        landing = Screen([TextNode('商品详情', (100, 300, 900, 360))], 1080, 2400, PACKAGE)
        self.assertFalse(self.runner.verify(self.task, self.before, landing))


if __name__ == '__main__':
    unittest.main()
