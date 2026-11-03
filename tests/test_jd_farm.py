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
                            ('去快手极速版领金币', '其他应用'),
                            ('添加并访问农场小组件', '小组件'),
                            ('至高领100元话费', '金融'),
                            ('逛PLUS会员中心', '站外'),
                            ('去开启定时领水通知', '通知')):
            with self.subTest(title=title):
                task = by_title[title]
                self.assertEqual(task.kind, 'manual')
                self.assertIn(word, task.skip_reason)

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
