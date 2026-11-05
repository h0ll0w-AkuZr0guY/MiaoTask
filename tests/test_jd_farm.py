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

    def test_loading_farm_page_is_not_foreign(self):
        """刚点进农场的那一两帧只有「返回」，判成外站会把农场立刻退掉。

        真机 2026-11-05 08:35：点完入口 1 秒内就判外站、去点返回，接着再点入口，
        最后整个导航预算耗尽，报「未能自动进入应用活动任务列表」。
        """
        loading = Screen([TextNode('返回', (33, 120, 153, 240))], 1080, 2400, PACKAGE)
        self.assertFalse(self.runner.foreign_page(loading))

    def test_offscreen_close_button_is_not_offered(self):
        """农场容器会把「返回」报到屏幕外（屏宽 1080 却给 x=1098~1218）。

        这个坐标拿去点会被框架的越界保护拦下，抛「目标位于系统栏、屏幕裁切
        或不可操作区域」，整轮直接中断 —— 所以入口定位必须跳过它。
        """
        loading = Screen([
            TextNode('返回', (1098, 114, 1218, 261)),
            TextNode('东东农场', (0, 0, 1080, 2400)),
        ], 1080, 2400, PACKAGE)
        self.assertIsNone(self.runner.page_close_node(loading))
        self.assertIsNone(self.runner.home_entry_node(loading))

    def test_offscreen_node_is_never_tapped(self):
        runner = self.runner
        runner.device = Mock()
        runner.reader = SimpleNamespace(last_screen=Screen([], 1080, 2400, PACKAGE))
        self.assertFalse(runner._tap(TextNode('返回', (1098, 114, 1218, 261))))
        runner.device.click.assert_not_called()


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

    def test_alt_swipe_reopens_the_drawer_instead_of_closing_it(self):
        """京东抽屉不认反向拖动：往上拖会被识别成「下拉关闭」，抽屉直接消失。

        真机上连滑三次就退回农场首页，导航随后全部扑空。所以反向检索必须走
        「收起 → 重新打开」，语义仍是回到列表顶部。
        """
        panel = panel_screen(('逛家电家居领京豆(1/1)', '浏览6秒，奖励+10g', '继续逛'),
                             extra=[('关闭任务面板', (957, 363, 1032, 438))])
        farm = farm_home_screen()
        self.play([farm, panel])
        self.runner._tap = self.runner.click
        self.assertTrue(self.runner.alt_swipe(panel))
        self.assertEqual([node.text for node in self.runner.clicked],
                         ['关闭任务面板', '点击前往做任务领水滴页面'])

    def test_panel_reopens_near_the_last_finished_row(self):
        """抽屉每次重开都回到顶部，主循环就得重滑三屏才够到刚才那一行。

        一轮下来滑动预算全花在「回到原位」上，列表下半截的浏览任务永远轮不到。
        """
        full = panel_screen(
            ('逛PLUS会员中心(0/1)', '浏览6秒，奖励+10g', '逛一逛'),
            ('去浏览直播挑战赛(0/1)', '浏览6秒，奖励+10g', '逛一逛'),
            ('读一本小说最高赚20(0/1)', '读6s书奖励+10g', '逛一逛'),
        )
        tasks = self.runner.parse_tasks(full.nodes, full.width, full.height)
        first = next(t for t in tasks if t.title == '逛PLUS会员中心')
        self.runner._remember_anchor(full, first)
        self.assertEqual(self.runner._anchor_titles, ('去浏览直播挑战赛', '读一本小说最高赚20'))

        top = panel_screen(('逛PLUS会员中心(1/1)', '浏览6秒，奖励+10g', '继续逛'))
        deeper = panel_screen(('去浏览直播挑战赛(0/1)', '浏览6秒，奖励+10g', '逛一逛'))
        self.runner.browse_swipe = Mock()
        self.runner.read = lambda force_ocr=False: deeper
        screen = self.runner._restore_panel_position(top)
        self.runner.browse_swipe.assert_called_once()
        self.assertIn('去浏览直播挑战赛(0/1)', [node.text for node in screen.nodes])

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


class AttentionSettleTests(unittest.TestCase):
    """登录/授权浮层不该一出现就把整轮停下来等人。

    真机事实：跨应用任务落到快手广告页时，页面自带「立即登录」横幅，京东这边
    任务其实照做；以前这里判成「页面要求登录」，整轮就此结束且要人处理。
    """

    def setUp(self):
        self.runner = make_runner()
        self.runner.config['attention_settle_rounds'] = 2
        self.runner.config['attention_settle_seconds'] = 0
        self.runner.control.wait = lambda seconds=0: None

    def test_external_app_login_banner_does_not_stop_the_run(self):
        kuaishou = Screen([TextNode('立即登录', (10, 300, 200, 400))], 1080, 2400,
                          'com.kuaishou.nebula')
        self.runner.read = lambda force_ocr=False: kuaishou
        self.runner.guard(kuaishou)

    def test_page_that_settles_itself_continues(self):
        login = Screen([TextNode('立即登录', (10, 300, 200, 400))], 1080, 2400, PACKAGE)
        queue = [login, farm_home_screen()]
        self.runner.read = lambda force_ocr=False: queue.pop(0) if queue else farm_home_screen()
        self.runner.guard(login)

    def test_real_login_page_still_stops_after_waiting(self):
        from backend.core.errors import NeedsAttention
        login = Screen([TextNode('短信登录', (10, 300, 200, 400))], 1080, 2400, PACKAGE)
        self.runner.read = lambda force_ocr=False: login
        with self.assertRaises(NeedsAttention):
            self.runner.guard(login)

    def test_old_core_without_attention_reason_still_skips_external_banner(self):
        """桌面版把 core 打进 exe，可能没有 attention_reason：不能因此导入失败。"""
        from unittest.mock import patch
        from backend.app.jd.activities.dongdong_farm import activity as module
        kuaishou = Screen([TextNode('立即登录', (10, 300, 200, 400))], 1080, 2400,
                          'com.kuaishou.nebula')
        self.runner.read = lambda force_ocr=False: kuaishou
        with patch.object(module, '_attention_reason', None):
            self.runner.guard(kuaishou)

    def test_old_core_still_stops_on_real_login_page(self):
        from unittest.mock import patch
        from backend.core.errors import NeedsAttention
        from backend.app.jd.activities.dongdong_farm import activity as module
        login = Screen([TextNode('短信登录', (10, 300, 200, 400))], 1080, 2400, PACKAGE)
        self.runner.read = lambda force_ocr=False: login
        with patch.object(module, '_attention_reason', None):
            with self.assertRaises(NeedsAttention):
                self.runner.guard(login)

    def test_storefront_words_do_not_stop_the_round_on_old_core(self):
        """落地页商品名“导轨滑块”、主图“需要验证码?不上号”不是验证页。

        旧版程序的核心词表把这两个裸词都算验证，整轮会停在一个没有验证的
        商品页上；净化层的职责就是让旧 core 也能放行。
        """
        from unittest.mock import patch
        from backend.app.jd.activities.dongdong_farm import activity as module
        feed = Screen([TextNode('浏览6秒，奖励+10g', (10, 120, 700, 180)),
                       TextNode('HG系列导轨滑块', (10, 900, 700, 960)),
                       TextNode('需要验证码?不上号', (10, 1000, 700, 1060))],
                      1080, 2400, PACKAGE)
        self.runner.read = lambda force_ocr=False: feed
        with patch.object(module, '_attention_reason', None):
            self.runner.guard(feed)

    def test_storefront_clear_keeps_real_challenge_untouched(self):
        """净化只摘货架文案：拖动滑块、输入验证码这些页面必须原样交给判定。"""
        from backend.app.jd.activities.dongdong_farm.activity import storefront_clear
        for label in ('请按住滑块，拖动到最右边', '请输入验证码', '拖动下方滑块完成拼图'):
            page = Screen([TextNode(label, (10, 900, 700, 960))], 1080, 2400, PACKAGE)
            self.assertIs(storefront_clear(page), page)


class OutOfBoundsClickTests(unittest.TestCase):
    def test_click_outside_content_bounds_is_skipped_not_fatal(self):
        """农场把顶部「返回」报到 x≈1098（屏宽 1080）：跳过这一帧，不是报废整轮。"""
        from types import SimpleNamespace
        from backend.core.execution.session import Session
        session = Session.__new__(Session)
        session.control = Mock()
        session.device = Mock()
        geometry = SimpleNamespace(contains=lambda x, y: 0 <= x < 1080 and 0 <= y < 2400)
        session.reader = SimpleNamespace(last_screen=SimpleNamespace(geometry=geometry))
        self.assertFalse(session.click(SimpleNamespace(center=(1158, 187), text='返回')))
        session.device.click.assert_not_called()
        self.assertTrue(session.click(SimpleNamespace(center=(540, 1200), text='去完成')))
        session.device.click.assert_called_once_with(540, 1200)

    def test_execute_skips_task_when_old_core_raises_out_of_bounds(self):
        """打包版 core 换不了：越界抛出来也只丢这一条，不让整轮停下。"""
        from backend.core.errors import NeedsAttention
        runner = make_runner()
        runner.read = lambda force_ocr=False: farm_home_screen()
        runner._run_task = Mock(side_effect=NeedsAttention(
            '目标位于系统栏、屏幕裁切或不可操作区域，请检查窗口布局'))
        screen, explicit, cross = runner.execute(Mock(), farm_home_screen())
        self.assertFalse(explicit)
        self.assertFalse(cross)
        self.assertIsInstance(screen, Screen)

    def test_execute_still_raises_other_attention(self):
        from backend.core.errors import NeedsAttention
        runner = make_runner()
        runner._run_task = Mock(side_effect=NeedsAttention('页面要求京东安全验证', kind='security'))
        with self.assertRaises(NeedsAttention):
            runner.execute(Mock(), farm_home_screen())


class CatalogLoopTests(unittest.TestCase):
    """新主循环：整张抽屉扫一遍，逐条滚过去做，而不是只盯当前一屏。"""

    def setUp(self):
        self.runner = make_runner()
        self.runner.control.wait = lambda seconds=0: None
        self.runner.browse_swipe = Mock()
        self.runner._panel_scroll_to_top = lambda screen, limit=4: screen

    def test_scan_catalog_merges_rows_from_several_screens(self):
        first = panel_screen(
            ('精选活动推荐(4/7)', '浏览6秒，奖励+10g', '去领奖'),
            ('去快手极速版领金币(0/1)', '完成任务奖励+150g', '去完成'))
        second = panel_screen(
            ('逛PLUS会员中心(0/1)', '浏览6秒，奖励+10g', '逛一逛'),
            ('去百度APP领现金(0/1)', '完成任务奖励+20g', '去完成'))
        queue = [first, second, second]
        self.runner.read = lambda force_ocr=False: queue.pop(0) if len(queue) > 1 else queue[0]
        catalog, screen = self.runner.scan_catalog(first)
        self.assertEqual(len(catalog), 4)
        self.assertIn('逛PLUS会员中心#browse', catalog)
        self.assertIn('去百度APP领现金#browse', catalog)

    def test_actionable_filters_manual_done_and_exhausted_rows(self):
        screen = panel_screen(
            ('精选活动推荐(4/7)', '浏览6秒，奖励+10g', '去领奖'),
            ('玩消消乐得红包(0/1)', '通过1关，奖励+10g', '去完成'),
            ('双十一抽1888现金(1/1)', '浏览6秒，奖励+20g', '继续逛'))
        tasks = self.runner.parse_tasks(screen.nodes, 1080, 2400)
        by_title = {t.title: t for t in tasks}
        self.assertTrue(self.runner._actionable(by_title['精选活动推荐'].key,
                                                by_title['精选活动推荐']))
        self.assertFalse(self.runner._actionable(by_title['玩消消乐得红包'].key,
                                                 by_title['玩消消乐得红包']))
        self.assertFalse(self.runner._actionable(by_title['双十一抽1888现金'].key,
                                                 by_title['双十一抽1888现金']))
        self.runner._own_failures[by_title['精选活动推荐'].key] = 2
        self.assertFalse(self.runner._actionable(by_title['精选活动推荐'].key,
                                                 by_title['精选活动推荐']))

    def test_claim_node_never_returns_the_new_browse_button(self):
        task = self.runner.parse_tasks(panel_screen(
            ('精选活动推荐(4/7)', '浏览6秒，奖励+10g', '去领奖')).nodes, 1080, 2400)[0]
        claim = panel_screen(('精选活动推荐(4/7)', '浏览6秒，奖励+10g', '去领奖'))
        self.assertIsNotNone(self.runner._claim_node(claim, task))
        settled = panel_screen(('精选活动推荐(4/7)', '浏览6秒，奖励+10g', '去完成'))
        self.assertIsNone(self.runner._claim_node(settled, task),
                          '领完后按钮变成「去完成」，不能再当成领奖按钮补点')


if __name__ == '__main__':
    unittest.main()
