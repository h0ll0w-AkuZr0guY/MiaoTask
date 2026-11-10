from backend.core.execution.loop import SessionLoop
"""Daily activities reuse the existing API across phone, tablet and landscape."""
from pathlib import Path
from types import SimpleNamespace
import re
import unittest
from unittest.mock import Mock, patch

from backend.core.config.loader import load_config
from backend.core.device.screen import Screen
from backend.core.models import Task, TextNode
from backend.core.registry import Registry
from backend.core.execution.session import Session
from backend.core.execution.task_policy import task_identity
from backend.core.execution.activity import Activity
from backend.core.errors import NeedsAttention


class TaobaoDailyTests(unittest.TestCase):
    def runner(self, activity='coins'):
        registry = Registry(Path(__file__).resolve().parents[1]/'backend/app').discover()
        config = load_config(app='taobao', activity=activity, registry=registry)
        return registry.instantiate(SimpleNamespace(device_type='uiautomator2', selected_instance=None),
                                    config, app='taobao', activity=activity)

    def test_each_business_has_independent_registration_and_keeps_existing_default(self):
        registry = Registry(Path(__file__).resolve().parents[1]/'backend/app').discover()
        self.assertEqual(registry.get().activity.id, 'double11')
        for name in ('coins', 'farm', 'cash-signin', 'achievements'):
            item = registry.get('taobao', name)
            self.assertEqual(item.activity.id, name)
            self.assertFalse(item.activity.legacy_storage)
            self.assertIn('uiautomator2', item.activity.device_types)

    def test_daily_activities_keep_cpu_sparse_browse_and_cuda_end_check(self):
        for activity in ('coins', 'farm', 'cash-signin', 'achievements'):
            for backend in ('cpu', 'cuda'):
                with self.subTest(activity=activity, backend=backend):
                    runner = self.runner(activity)
                    runner.origin = 'com.taobao.taobao'
                    runner.reader._engine = SimpleNamespace(engine=SimpleNamespace(miao_backend=backend))
                    clock = [0.0]
                    runner.control.now = lambda: clock[0]
                    runner.control.wait = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
                    runner.browse_swipe = lambda screen: clock.__setitem__(0, clock[0] + .5)
                    runner.guard = Mock()
                    runner.check_destination = Mock()
                    runner.config.update(scroll_browse=True, browse_recheck_rounds=3)
                    pending = Screen([TextNode('浏览商品', (10,300,300,350))], 1080,2400,runner.origin)
                    completed = Screen([TextNode('任务已完成', (10,300,300,350))],1080,2400,runner.origin)
                    runner.read = Mock(return_value=completed)
                    task = Task('synthetic', '浏览商品', '浏览15秒', '去完成', (800,400,950,460), seconds=15)
                    explicit, _ = runner.wait_for_browse(task, pending, False, 15)
                    self.assertEqual(explicit, backend == 'cuda')
                    self.assertEqual(runner.read.call_count, 1 if backend == 'cuda' else 0)
                    self.assertLess(clock[0], 16)

    def test_daily_search_grid_with_submit_still_uses_shared_search(self):
        from backend.app.taobao.utils.search import SearchPolicy
        for activity in ('coins', 'farm', 'cash-signin', 'achievements'):
            with self.subTest(activity=activity):
                runner = self.runner(activity)
                runner.search_results_ready = Mock(return_value=True)
                page = Screen([TextNode('搜索', (900,120,1050,180))],1080,2400,'com.taobao.taobao')
                with patch.object(SearchPolicy, 'handle_search', return_value=True) as submit:
                    self.assertTrue(runner.handle_search(page))
                submit.assert_called_once_with(page, None, False)

    def test_daily_search_never_accepts_results_from_another_application(self):
        runner = self.runner()
        runner.search_results_ready = Mock(return_value=True)
        page = Screen([],1080,2400,'org.example.chat')
        with self.assertRaisesRegex(NeedsAttention, '离开所选应用'):
            runner.handle_search(page)
        runner.search_results_ready.assert_not_called()

    def test_counterless_rows_and_completed_feedback_scale_across_displays(self):
        runner = self.runner()
        for width, height in ((1080, 2400), (1800, 2880), (2880, 1800)):
            with self.subTest(width=width, height=height):
                title = TextNode('浏览精选店铺', (int(width*.1),int(height*.3),int(width*.5),int(height*.32)))
                button = TextNode('去完成',(int(width*.78),int(height*.3),int(width*.93),int(height*.33)))
                before = Screen([title,button,TextNode('赚金币抵钱',(0,20,300,70))], width,height,'com.taobao.taobao')
                tasks=runner.parse_tasks(before.nodes,width,height)
                self.assertEqual(len(tasks),1)
                task=tasks[0]
                self.assertEqual(task.kind,'browse')
                self.assertEqual(task.title,'浏览精选店铺')
                self.assertFalse(runner.verify(task,before,Screen(before.nodes[:1],width,height,before.package)))
                done=Screen([title,TextNode('已完成',button.bounds),before.nodes[-1]],width,height,before.package)
                self.assertTrue(runner.verify(task,before,done))

    def test_timed_farm_reward_keeps_explicit_completed_progress(self):
        runner = self.runner('farm')
        header = TextNode('肥料明细', (400,100,600,150))
        button = TextNode('12点可领', (1400,520,1600,580))
        for current in (0,1):
            nodes = [header, TextNode(f'领肥料礼包({current}/1)', (650,500,1200,550)),
                     TextNode('7点/12点/20点/22点可领 +400', (650,560,1200,600)), button]
            task, = runner.parse_tasks(nodes,1800,2880)
            self.assertEqual(task.current,current)
            self.assertEqual(task.kind,'done' if current else 'manual')
        before = Screen([header],1800,2880,'com.taobao.taobao')
        task = Task('old','领肥料礼包','','去领取',button.bounds,kind='claim')
        # Match the parser's stable semantic identity; do not infer completion
        # from removal or from the time label alone.
        task.key = runner.parse_tasks(nodes,1800,2880)[0].key
        self.assertTrue(runner.verify(task,before,Screen(nodes,1800,2880,before.package)))

    def test_resumed_full_run_resets_modal_to_top_but_recovery_does_not(self):
        runner = self.runner('farm')
        def page(label):
            return Screen([TextNode('肥料明细',(450,100,700,150)),
                           TextNode(label+'(0/1)',(650,700,1100,750)),
                           TextNode('去完成',(1400,720,1600,780)),
                           TextNode('浏览另一商品(0/1)',(650,1600,1100,1650)),
                           TextNode('去完成',(1400,1620,1600,1680))],1800,2880,'com.taobao.taobao')
        lower, top = page('浏览底部商品'),page('浏览顶部商品')
        runner.navigate = Mock(return_value=lower)
        runner.read_page = Mock(return_value=top)
        runner.device.swipe = Mock()
        runner.control.wait = Mock()
        self.assertIs(runner.ensure_list(auto_open=True),top)
        self.assertEqual(runner.device.swipe.call_count,2) # move then prove unchanged
        x,y1,_,y2 = runner.device.swipe.call_args.args
        # 滑动只要求落在列表列宽内、方向朝下（内容往上回）；具体 x 不必绑定
        # 任务行的中心——屏上只剩一两行时那个中心会跑到按钮上。
        self.assertTrue(0 < x < 1800)
        self.assertLess(y1,y2)
        runner.device.swipe.reset_mock()
        self.assertIs(runner.recover(),lower)
        runner.device.swipe.assert_not_called()
        runner.ensure_list(auto_open=False)
        runner.device.swipe.assert_not_called()

    def test_reward_label_does_not_allow_payment_or_invitation(self):
        runner=self.runner()
        for title in ('支付一笔领金币','下单得奖励','邀请好友领肥料','邀30天没来农场的朋友','抽奖一次','施肥一次'):
            result=runner.classify_daily(title,'',[title],'领取奖励',0,1)
            task=Task('policy',title,'','领取奖励',(1,2,3,4),kind=result[1])
            self.assertIn('常驻关键词',runner.availability_reason(task))

    def test_signin_button_may_be_centered_but_withdrawal_is_not_a_task(self):
        runner=self.runner('cash-signin')
        nodes=[TextNode('累计签到奖励(1/7)',(70,590,500,650)),
               TextNode('立即签到',(310,940,1490,1160)),
               TextNode('立即提现',(1200,200,1700,550))]
        tasks=runner.parse_tasks(nodes,1800,2880)
        self.assertEqual([t.title for t in tasks],['每日红包签到'])
        self.assertEqual(tasks[0].kind,'claim')

    def test_homepage_uses_whole_icon_not_only_its_decorative_caption(self):
        runner=self.runner()
        outer=TextNode('领淘金币',(300,200,450,350))
        caption=TextNode('领淘金币',(300,350,450,390))
        self.assertIs(runner.entry_node(Screen([caption,outer],1800,2880,'com.taobao.taobao'),['领淘金币']),outer)

    def test_homepage_fallback_rejects_merged_neighboring_icons(self):
        runner = self.runner()
        tabs = [TextNode('购物车', (650, 2200, 780, 2300)),
                TextNode('我的淘宝', (800, 2200, 1000, 2300))]
        for caption, accepted in [('领淘金币抵现金', True), ('领淘金币红包签到', False)]:
            with self.subTest(caption=caption):
                node = TextNode(caption, (100, 300, 500, 360))
                screen = Screen([*tabs, node], 1080, 2400, 'com.taobao.taobao', 'ocr')
                self.assertEqual(runner.homepage_entry(screen) is node, accepted)
        self.assertIsNone(runner.homepage_entry(Screen([node], 1080, 2400, screen.package)))

    def test_confirmed_native_homepage_opens_icon_before_visual_read(self):
        runner = self.runner()
        entry = TextNode('领淘金币', (300, 200, 450, 350), 'ui')
        home = Screen([entry, TextNode('芭芭农场', (500, 200, 700, 350)),
                       TextNode('购物车', (650, 2200, 780, 2300)),
                       TextNode('我的淘宝', (800, 2200, 1000, 2300))],
                      1080, 2400, 'com.taobao.taobao', 'ui')
        panel = Screen([TextNode('今日速赚', (50, 500, 400, 600))],
                       1080, 2400, home.package, 'ocr')
        runner.device.current_app = Mock(return_value={'package': home.package})
        runner.read_page = Mock(side_effect=[home, panel])
        runner.click = Mock()
        runner.control.wait = Mock()
        self.assertIs(runner.navigate(auto_open=True), panel)
        runner.click.assert_called_once_with(entry)
        self.assertEqual(runner.read_page.call_args_list, [unittest.mock.call(), unittest.mock.call()])

    def test_post_campaign_coin_signin_entry_and_end_description(self):
        runner = self.runner()
        self.assertTrue({'赚更多金币', '去赚金币', '回日常版', '日常版'}.issubset(
            runner.config['task_entry_labels']))
        landing = Screen([TextNode('淘金币', (80, 100, 350, 170)),
                          TextNode('签到领金币', (250, 600, 800, 760))],
                         1080, 2400, 'com.taobao.taobao', 'ocr')
        self.assertTrue(landing.find(runner.page['landing_markers']))
        self.assertEqual(runner.entry_node(landing, runner.config['task_entry_labels']).text, '签到领金币')
        registry = Registry(Path(__file__).resolve().parents[1] / 'backend/app').discover()
        self.assertIn('已结束', registry.get('taobao', 'double11').description)
        achievements = self.runner('achievements')
        for caption in ('点此设置默认地址', '请先选择地址', '免费领正版周边'):
            self.assertRegex(caption, achievements.page['manual_entry_markers'])

    def test_all_channels_keeps_its_entry_without_homepage_bottom_tabs(self):
        runner = self.runner('farm')
        runner.config['ocr'] = False
        entry = TextNode('芭芭农场', (100, 300, 300, 400))
        channels = Screen([TextNode('全部频道', (100, 100, 400, 200)), entry],
                          1080, 2400, 'com.taobao.taobao', 'ui')
        panel = Screen([TextNode('肥料明细', (50, 500, 400, 600))],
                       1080, 2400, channels.package, 'ui')
        runner.device.current_app = Mock(return_value={'package': channels.package})
        runner.read_page = Mock(side_effect=[channels, panel])
        runner.click = Mock()
        runner.control.wait = Mock()
        self.assertIs(runner.navigate(auto_open=True), panel)
        runner.click.assert_called_once_with(entry)

    def test_cash_promotion_close_can_overlap_background_product_text(self):
        for width, height in ((1080, 2400), (1800, 2880), (2560, 1600)):
            with self.subTest(size=(width, height)):
                runner = self.runner('cash-signin')
                def node(text, x, y):
                    return TextNode(text, (int(width*x), int(height*y),
                                           int(width*(x+.06)), int(height*(y+.02))))
                close = node('X', .47, .72)
                screen = Screen([node('累计签到奖励', .05, .2),
                                 node('送你限时折扣', .3, .3), close,
                                 node('背景商品', .47, .745)], width, height,
                                'com.taobao.taobao', 'ocr')
                runner.click = Mock()
                runner.control.wait = Mock()
                runner.device.go_back = Mock()
                self.assertIs(runner.dismiss_overlay(screen), close)
                runner.click.assert_called_once_with(close)
                runner.device.go_back.assert_not_called()
                screen.nodes.pop(1)
                self.assertFalse(runner.lonely_close(screen, close))

    def test_coin_task_entry_priority_beats_taller_version_switch(self):
        runner = self.runner()
        direct = TextNode('去赚金币',(30,1300,270,1370))
        version = TextNode('日常版>',(1360,600,1430,810))
        screen = Screen([version,direct],1440,3200,'com.taobao.taobao','ocr')
        self.assertIs(runner.entry_node(screen,runner.config['task_entry_labels']),direct)

    def test_activity_menu_uses_current_native_field_when_painted_caption_is_split(self):
        runner = self.runner('farm')
        menu = TextNode('集肥料',(1000,2300,1200,2440),'ui')
        native = Screen([menu],1440,3200,'com.taobao.taobao','ui')
        painted = Screen([TextNode('芭芭农场|免费领水果',(180,180,900,250)),
                          TextNode('集',(1000,2390,1060,2440)),
                          TextNode('肥料',(1060,2390,1200,2440))],1440,3200,native.package,'ocr')
        panel = Screen([TextNode('肥料明细',(70,600,400,700))],1440,3200,native.package,'ocr')
        runner.device.current_app = Mock(return_value={'package':native.package})
        runner.read_page = Mock(side_effect=[native,painted,panel])
        runner.click = Mock(); runner.control.wait = Mock()
        self.assertIs(runner.navigate(auto_open=True),panel)
        runner.click.assert_called_once_with(menu)

    def test_farm_promotion_dismissal_does_not_close_task_sheet_behind_it(self):
        runner = self.runner('farm')
        behind = TextNode('X',(1320,650,1400,740))
        front = TextNode('X',(670,2400,770,2500))
        screen = Screen([TextNode('肥料明细',(50,650,400,730)),behind,
                         TextNode('品质农场上新季',(350,800,1100,930)),front],
                        1440,3200,'com.taobao.taobao','ocr')
        runner.click = Mock(); runner.control.wait = Mock(); runner.device.go_back = Mock()
        self.assertTrue(runner.dismiss_overlay(screen))
        runner.click.assert_called_once_with(front)
        runner.device.go_back.assert_not_called()
        screen.nodes.remove(front); runner.click.reset_mock()
        self.assertTrue(runner.dismiss_overlay(screen))
        runner.click.assert_not_called()
        runner.device.go_back.assert_called_once()
        screen.nodes[2] = TextNode('明日7点后记得来领',(350,800,1100,930))
        screen.nodes.append(front)
        self.assertFalse(runner.is_task_list(screen.nodes))
        runner.click.reset_mock()
        self.assertTrue(runner.dismiss_overlay(screen))
        runner.click.assert_called_once_with(front)

    def test_confirmed_home_uses_native_icon_when_ocr_merges_adjacent_captions(self):
        runner=self.runner('farm')
        icon=TextNode('芭芭农场',(1100,440,1390,680),'ui')
        native=Screen([icon],1440,3200,'com.taobao.taobao','ui')
        painted=Screen([TextNode('红包签到菜鸟驿站芭芭农场',(750,620,1400,680),'ocr'),
                        TextNode('购物车',(940,3060,1070,3120),'ocr'),
                        TextNode('我的淘宝',(1210,3060,1380,3120),'ocr')],1440,3200,native.package,'ocr')
        panel=Screen([TextNode('肥料明细',(100,400,400,500))],1440,3200,native.package,'ocr')
        runner.read_page=Mock(side_effect=[native,painted,panel])
        runner.device.current_app=Mock(return_value={'package':native.package})
        runner.click=Mock();runner.control.wait=Mock()
        self.assertIs(runner.navigate(auto_open=True),panel)
        runner.click.assert_called_once_with(icon)

    def test_search_query_is_not_an_official_result(self):
        runner=self.runner('farm')
        edit=TextNode('芭芭农场',(150,80,1200,160))
        self.assertIsNone(runner.official_activity_entry(Screen([edit],1800,2880,'com.taobao.taobao')))
        suggestion=TextNode('芭芭农场',(150,600,800,680))
        history=TextNode('历史搜索',(50,350,400,420))
        self.assertIsNone(runner.official_activity_entry(Screen([edit,history,suggestion],1800,2880,'com.taobao.taobao')))

    def test_phone_official_search_shortcut_uses_its_aligned_action(self):
        runner=self.runner()
        query=TextNode('淘金币',(180,170,370,260))
        submit=TextNode('搜索',(1240,175,1370,250))
        title=TextNode('淘金币',(120,335,300,410))
        action=TextNode('去查看>',(1220,340,1400,410))
        # Accessibility splits highlighted words out of suggestion sentences.
        fragment=TextNode('淘金币',(120,680,300,750))
        page=Screen([query,submit,title,action,fragment],1440,3200,'com.taobao.taobao','ocr')
        self.assertIs(runner.official_activity_entry(page),action)
        page.nodes.remove(action)
        self.assertIsNone(runner.official_activity_entry(page))
        page.nodes.append(TextNode('去看看>',(1220,1200,1400,1270)))
        self.assertIsNone(runner.official_activity_entry(page))

    def test_product_coin_discount_badge_is_not_an_activity_entry(self):
        runner=self.runner()
        product=Screen([TextNode('淘金币',(30,700,300,850)),
                        TextNode('加入购物车',(500,2850,900,3100)),
                        TextNode('领券购买',(900,2850,1400,3100))],1440,3200,'com.taobao.taobao','ocr')
        self.assertIsNone(runner.official_activity_entry(product))

    def test_coins_reward_circles_and_cards_are_panel_relative(self):
        runner = self.runner()
        for width, height in ((1080,2400), (1800,2880), (2880,1800)):
            def node(text, x, y, right, bottom):
                return TextNode(text, tuple(round(v*s) for v,s in zip((x,y,right,bottom),(width,height,width,height))), 'ocr')
            card = node('精选清单', .34,.27,.47,.35)
            nodes = [node('今日速赚510淘金币',.28,.15,.55,.18),
                     node('逛清单，每15秒30金币(0/2)',.34,.22,.62,.24), card,
                     node('+30',.7,.23,.75,.25),
                     node('发现精选好物(0/2)',.34,.39,.62,.41),
                     node('浏览15秒',.34,.41,.46,.43), node('+60',.7,.40,.75,.42),
                     node('去支付宝农场逛逛哟',.34,.51,.63,.53),
                     node('点击去逛',.34,.53,.47,.55), node('+50',.7,.52,.75,.54),
                     node('淘金币趣味课堂',.34,.63,.6,.65),
                     node('参与答题',.34,.65,.47,.67), node('+30',.7,.64,.75,.66),
                     # Unrelated product promotion outside the central panel.
                     node('去购买(0/1)',.01,.39,.23,.41)]
            tasks = runner.parse_tasks(nodes,width,height)
            self.assertEqual(len(tasks),4)
            self.assertEqual(tasks[0].bounds,card.bounds)
            self.assertEqual((tasks[1].kind,tasks[1].seconds),('browse',15))
            self.assertEqual((tasks[2].kind,tasks[2].target_app),('visit','支付宝'))
            # 趣味课堂答题每题选 A 即可，交给 quiz 流程执行，不再留给人工。
            self.assertEqual(tasks[3].kind,'visit')

    def test_coin_quick_and_normal_sections_merge_mirrors_and_keep_extra_tasks(self):
        runner=self.runner()
        nodes=[TextNode('今日速赚240淘金币',(50,350,820,450)),
               TextNode('任务到访得金币',(260,870,670,940)),TextNode('+10',(1200,920,1310,990)),
               TextNode('更多专享福利',(50,2680,350,2740)),
               TextNode('任务到访得金币(0/1)',(250,2810,780,2880)),
               TextNode('领取奖励',(1160,2850,1360,2920)),
               TextNode('浏览精选商品(0/1)',(250,3000,780,3060)),
               TextNode('去完成',(1160,3030,1360,3100))]
        tasks=runner.parse_tasks(nodes,1440,3200)
        self.assertEqual([t.title for t in tasks],['任务到访得金币','浏览精选商品'])
        # 行以右侧按钮为锚点，bounds 即按钮落点；奖励徽章只参与判断不再作为坐标。
        self.assertEqual(tasks[0].bounds,(1160,2850,1360,2920))
        # The modal title stays fixed while the section separator scrolls away.
        scrolled=[nodes[0],TextNode('浏览精选商品(0/1)',(250,800,780,860)),
                  TextNode('去完成',(1160,830,1360,900))]
        self.assertEqual([t.title for t in runner.parse_tasks(scrolled,1440,3200)],['浏览精选商品'])

    def test_coin_header_integer_balance_is_scoped_to_the_channel_header(self):
        runner=self.runner()
        header=TextNode('淘金币',(160,170,420,260))
        task=Task('c','任务到访得金币','每日来任务面板 +10','领取奖励',(1200,920,1310,990),kind='claim')
        before=Screen([header,TextNode('165100',(500,180,680,245))],1440,3200,'com.taobao.taobao','ocr')
        after=Screen([header,TextNode('165110',(500,180,680,245))],1440,3200,before.package,'ocr')
        self.assertTrue(runner.coin_balance_increased(task,before,after))
        after.nodes[-1]=TextNode('165999',(500,1800,680,1860))
        self.assertFalse(runner.coin_balance_increased(task,before,after))

    def test_retired_coin_search_row_requires_exact_reward_balance_delta(self):
        runner=self.runner()
        header=TextNode('淘金币',(160,170,420,260))
        panel=TextNode('今日速赚240淘金币',(50,350,820,450))
        before=Screen([header,panel,TextNode('165100',(500,180,680,245))],1440,3200,'com.taobao.taobao','ocr')
        task=Task('search','搜一搜你心仪的宝贝','浏览15秒 +30','去完成',(1200,920,1310,990),seconds=15,kind='search')
        after=Screen([header,panel,TextNode('165130',(500,180,680,245))],1440,3200,before.package,'ocr')
        self.assertTrue(runner.verify(task,before,after))
        self.assertFalse(runner.verify(task,before,before))
        after.nodes[-1]=TextNode('165200',(500,180,680,245))
        self.assertFalse(runner.verify(task,before,after))

    def test_passive_followed_product_browse_is_not_a_request_to_follow(self):
        runner=self.runner()
        for title in ('你关注的商品都可抵','看看你关注的新品动态'):
            self.assertEqual(runner.classify_daily(title,'浏览5秒 +20',[title,'浏览5秒 +20'],'去完成',0,1)[1],'browse')
            self.assertIn('常驻关键词',runner.availability_reason(Task('policy',title,'关注后浏览5秒 +20','去完成',(1,2,3,4))))
        self.assertIn('常驻关键词',runner.availability_reason(Task('policy','关注店铺','浏览5秒','去完成',(1,2,3,4))))

    def test_destination_waits_for_content_instead_of_status_bar_only(self):
        runner=self.runner();runner.origin='com.taobao.taobao'
        before=Screen([TextNode('今日速赚240淘金币',(50,350,820,450))],1440,3200,runner.origin,'ocr')
        blank=Screen([TextNode('15:33',(80,30,220,100)),TextNode('100',(1200,30,1350,100))],1440,3200,runner.origin,'ocr')
        loaded=Screen([TextNode('浏览好物',(80,350,500,450)),TextNode('¥20',(100,1200,300,1280))],1440,3200,runner.origin,'ocr')
        self.assertFalse(runner.destination_ready(blank))
        self.assertTrue(runner.destination_ready(loaded))
        runner.device.click=Mock();runner.guard=Mock();runner.check_destination=Mock()
        runner.control=SimpleNamespace(now=lambda:0,wait=Mock(),check=Mock())
        runner.read=Mock(side_effect=[blank,loaded])
        runner.browse_destination=Mock(return_value=(loaded,False,False))
        task=Task('b','浏览好物','','去完成',(1000,800,1200,900),kind='browse')
        runner.execute(task,before)
        self.assertEqual(runner.read.call_count,2)
        runner.browse_destination.assert_called_once_with(task,loaded)

    def test_system_launch_prompt_chooses_one_time_and_rejects_other_permissions(self):
        runner=self.runner('farm')
        once=TextNode('本次允许',(200,2200,1240,2380))
        always=TextNode('始终允许',(200,2420,1240,2600))
        prompt=Screen([TextNode('启动应用',(500,1700,900,1800)),
                       TextNode('淘宝 想要打开 美团，是否允许？',(100,1900,1320,2000)),once,always],
                      1440,3200,'com.miui.securitycenter','ocr')
        self.assertIs(runner.launch_confirmation(prompt),once)
        prompt.nodes[0]=TextNode('位置权限使用说明',(500,1700,900,1800))
        self.assertIsNone(runner.launch_confirmation(prompt))
        prompt.nodes[0]=TextNode('启动应用',(500,1700,900,1800))
        prompt.nodes[1]=TextNode('其他应用 想要打开 美团，是否允许？',(100,1900,1320,2000))
        self.assertIs(runner.launch_confirmation(prompt),once)
        for caption in ('允许本次','允许','确认','确认打开','打开'):
            prompt.nodes=[prompt.nodes[0],prompt.nodes[1],TextNode(caption,once.bounds),always]
            self.assertEqual(runner.launch_confirmation(prompt).text,caption)
        prompt.nodes=[prompt.nodes[0],prompt.nodes[1],always]
        self.assertIsNone(runner.launch_confirmation(prompt))
        runner.config['jump_words']={'allow':['好，继续跳转']}
        prompt.nodes.append(TextNode('好，继续跳转',once.bounds))
        self.assertEqual(runner.launch_confirmation(prompt).text,'好，继续跳转')
        runner.config['jump_words']['request']=['即刻转至.{1,12}客户端']
        prompt.nodes[0]=TextNode('链接提示',(500,1700,900,1800))
        prompt.nodes[1]=TextNode('即刻转至示例客户端',(100,1900,1320,2000))
        self.assertEqual(runner.launch_confirmation(prompt).text,'好，继续跳转')
        runner.config['jump_words']['request']=[]
        self.assertIsNone(runner.launch_confirmation(prompt))

    def test_leaving_app_confirmation_matches_by_scene_at_different_sizes(self):
        runner = self.runner('farm')
        for width,height in ((1080,2400),(1800,2880),(2880,1800)):
            def node(text,x,y,w,h):
                return TextNode(text,(int(width*x),int(height*y),int(width*(x+w)),int(height*(y+h))))
            confirm=node('确认',.62,.54,.15,.04)
            page=Screen([node('正在离开手机淘宝，打开菜鸟',.2,.47,.6,.04),
                         node('取消',.25,.54,.15,.04),confirm],width,height,'com.taobao.taobao','ocr')
            self.assertIs(runner.launch_confirmation(page),confirm)
            page.nodes[0]=node('正在离开手机淘宝，打开相机权限设置',.2,.47,.6,.04)
            self.assertIsNone(runner.launch_confirmation(page))
            page.nodes[0]=node('订单确认',.2,.47,.6,.04)
            self.assertIsNone(runner.launch_confirmation(page))

    def test_unresponsive_launch_skips_only_current_task_after_two_confirmations(self):
        runner=self.runner('farm');runner.origin='com.taobao.taobao'
        prompt=Screen([TextNode('启动应用',(500,1700,900,1800)),
                       TextNode('是否打开支付宝应用？',(100,1900,1320,2000)),
                       TextNode('确认',(200,2200,1240,2380)),
                       TextNode('肥料明细',(100,400,400,500))],1440,3200,runner.origin,'ocr')
        before=Screen([TextNode('肥料明细',(100,400,400,500))],1440,3200,runner.origin,'ocr')
        task=Task('jump','访问支付宝活动','','去完成',(1000,800,1200,900),kind='visit',target_app='支付宝')
        runner.device.click=Mock();runner.guard=Mock();runner.check_destination=Mock()
        runner.read=Mock(return_value=prompt);runner.recover=Mock(return_value=before)
        runner.control=SimpleNamespace(now=lambda:0,wait=Mock(),check=Mock())
        after,success,_=runner.execute(task,before)
        self.assertIs(after,before);self.assertFalse(success)
        self.assertEqual(runner.device.click.call_count,3) # task + two confirms
        self.assertTrue(runner.availability_reason(task))
        self.assertNotIn('app:支付宝',runner.unavailable_tasks)
        other=Task('other','另一个支付宝任务','','去完成',(1000,800,1200,900),kind='visit',target_app='支付宝')
        self.assertFalse(runner.availability_reason(other))

    def test_external_recovery_backs_before_launching_selected_app(self):
        from unittest.mock import MagicMock
        runner=self.runner('farm');runner.origin='com.taobao.taobao'
        foreign=Screen([TextNode('启动应用',(500,1700,900,1800))],1440,3200,'com.miui.securitycenter','ocr')
        target=Screen([TextNode('肥料明细',(100,400,400,500))],1440,3200,runner.origin,'ocr')
        device=MagicMock(device_type='uiautomator2')
        foreground={'package':foreign.package}
        device.current_app.side_effect=lambda:foreground
        device.go_back.side_effect=lambda:foreground.update(package=runner.origin)
        runner.device=device;runner.read_page=Mock(side_effect=[foreign,target,target])
        runner.control.wait=Mock()
        self.assertIs(runner.recover(),target)
        device.go_back.assert_called_once()
        device.start_app.assert_not_called()
        device.go_home.assert_not_called()

    def test_question_button_anchors_its_own_row(self):
        runner = self.runner('farm')
        tasks = runner.parse_tasks([
            TextNode('肥料明细',(100,100,300,140)),
            TextNode('浏览推荐商品(0/4)',(100,500,600,550)),
            TextNode('浏览15秒',(100,555,400,600)),
            TextNode('去完成',(1400,520,1600,580)),
            TextNode('农场百科问答(0/1)',(100,650,600,700)),
            TextNode('答题领肥料',(100,710,400,750)),
            TextNode('去答题',(1400,670,1600,730)),
        ],1800,2880)
        # 答题行同样归入可执行的 visit，选项与提交由答题流程处理。
        self.assertEqual([t.kind for t in tasks],['browse','visit'])

    def test_completed_coin_row_moves_to_bottom_without_reward_circle(self):
        runner = self.runner()
        header=TextNode('今日速赚510淘金币',(460,200,950,270),'ocr')
        before=Screen([header,TextNode('逛清单，每15秒30金币(1/2)',(600,530,1070,580),'ocr'),
                       TextNode('+30',(1210,560,1280,610),'ocr')],1800,2880,'com.taobao.taobao','ocr')
        task=runner.tasks(before)[0]
        after=Screen([header,TextNode('逛清单，每15秒30金币(2/2)',(600,1810,1070,1860),'ocr')],
                     1800,2880,before.package,'ocr')
        current=runner.tasks(after)[0]
        self.assertEqual((current.key,current.current,current.kind),(task.key,2,'done'))
        self.assertTrue(runner.verify(task,before,after))

    def test_cash_timed_tile_cannot_match_unrelated_coupon(self):
        runner = self.runner('cash-signin')
        nodes = [TextNode('累计签到奖励(1/7)',(70,590,500,650)),
                 TextNode('继续领钱',(310,940,1490,1160)),
                 TextNode('点击领取',(1500,1060,1730,1130)),
                 TextNode('+1280',(1590,910,1740,970)),
                 TextNode('点击领取',(730,1490,945,1730)),
                 TextNode('立即提现',(1200,200,1700,550))]
        tasks = runner.parse_tasks(nodes,1800,2880)
        self.assertEqual([t.title for t in tasks],['签到定时奖励'])
        self.assertEqual(tasks[0].bounds,nodes[2].bounds)
        self.assertEqual(runner.parse_tasks([n for n in nodes if n.text!='+1280'],1800,2880),[])

    def test_positive_balance_delta_requires_same_reward_amount(self):
        runner = self.runner()
        def screen(text):
            return Screen([TextNode(text,(100,100,600,150))],1800,2880,'com.taobao.taobao')
        task = SimpleNamespace(detail='+10')
        self.assertTrue(runner.coin_balance_increased(task,screen('3182抵31.82元'),screen('3192抵31.92元')))
        self.assertFalse(runner.coin_balance_increased(task,screen('3182抵31.82元'),screen('3182抵31.82元')))
        self.assertFalse(runner.coin_balance_increased(task,screen('抵31.82元'),screen('未知')))
        self.assertFalse(runner.coin_balance_increased(task,screen('抵31.82元'),screen('抵31.83元')))

    def test_accessibility_overflow_falls_back_without_swallowing_other_errors(self):
        runner = self.runner()
        overflow = type('RPCStackOverflowError',(Exception,),{})
        result = Screen([],1800,2880,'com.taobao.taobao')
        with patch.object(Session,'read',side_effect=[overflow(),result]) as read:
            self.assertIs(runner.read(),result)
            self.assertEqual(read.call_args_list[-1].kwargs,{'force_ocr':True})
        with patch.object(Session,'read',side_effect=ValueError('different failure')):
            with self.assertRaises(ValueError): runner.read()

    def test_claim_never_closes_the_task_list_to_find_success(self):
        runner = self.runner()
        before = Screen([TextNode('今日速赚',(300,200,600,250))],1800,2880,'com.taobao.taobao')
        task = SimpleNamespace(key='claim',title='任务到访得金币',kind='claim',action='领取奖励',
                               bounds=(1000,500,1200,600),current=0,detail='+10')
        ticks = iter([0,0,20])
        runner.control = SimpleNamespace(now=lambda:next(ticks),wait=Mock())
        runner.click=Mock(); runner.read_page=Mock(return_value=before)
        runner.tasks=Mock(return_value=[task]); runner.dismiss_overlay=Mock()
        runner.recover=Mock(return_value=before)
        runner.execute(task,before)
        runner.dismiss_overlay.assert_not_called()

    def test_cash_claim_requires_reward_evidence_not_ocr_label_changes(self):
        runner = self.runner('cash-signin')
        header = TextNode('累计签到奖励(1/7)',(70,590,500,650))
        before = Screen([header,TextNode('再攒80000元宝提现',(70,220,1190,280)),
                         TextNode('+1280元宝，点击领取',(1507,942,1730,1160))],1800,2880,'com.taobao.taobao')
        task = runner.tasks(before)[0]
        after = Screen([header,TextNode('再攒80000提现',(70,220,1190,280)),
                        TextNode('+1280',(1590,910,1740,970)),
                        TextNode('点击领取',(1500,1060,1730,1130))],1800,2880,before.package,'ocr')
        self.assertNotEqual(task.action,runner.tasks(after)[0].action)
        self.assertFalse(runner.verify(task,before,after))
        credited = Screen([header,TextNode('再攒78720提现',(70,220,1190,280)),
                           *after.nodes[2:]],1800,2880,before.package,'ocr')
        self.assertTrue(runner.verify(task,before,credited))
        self.assertIn('本轮已核验',runner.availability_reason(task))

    def test_cash_balance_accepts_split_same_row_but_not_other_rows(self):
        runner=self.runner('cash-signin')
        header=TextNode('累计签到奖励(1/7)',(70,714,520,774))
        def screen(value,y=314):
            return Screen([header,TextNode('再攒',(74,314,177,368)),
                           TextNode(f'{value}提现',(151,y,476,y+55))],1440,3200,'com.taobao.taobao','ocr')
        task=Task('cash','签到定时奖励','+40','点击领取',(550,1800,740,1870),kind='claim')
        self.assertTrue(runner.reward_balance_increased(task,screen(100000),screen(99960)))
        self.assertFalse(runner.reward_balance_increased(task,screen(100000),screen(99961)))
        self.assertFalse(runner.reward_balance_increased(task,screen(100000),screen(99960,500)))
        self.assertFalse(runner.reward_balance_increased(task,screen(100000),screen(99960,2700)))

    def test_cash_video_tile_next_to_games_is_not_the_timed_claim(self):
        runner=self.runner('cash-signin')
        header=TextNode('累计签到奖励(1/7)',(70,714,520,774))
        action=TextNode('点击领取',(815,1850,1003,1911))
        amount=TextNode('+40',(918,1718,996,1766))
        game=TextNode('游戏乐园',(554,1850,742,1911))
        self.assertEqual(runner.parse_tasks([header,game,amount,action],1440,3200),[])
        self.assertEqual(len(runner.parse_tasks([header,amount,action],1440,3200)),1)

    def test_cash_navigation_restores_main_balance_before_claiming(self):
        runner=self.runner('cash-signin')
        title=TextNode('红包签到·领现金',(191,174,654,244),'ocr')
        lower=Screen([title,TextNode('¥20',(50,1290,220,1380)),
                      TextNode('¥30',(1230,1480,1380,1560))],1440,3200,'com.taobao.taobao','ocr')
        main=Screen([title,TextNode('累计签到奖励(1/7)',(74,714,518,774),'ocr')],
                    1440,3200,lower.package,'ocr')
        self.assertFalse(runner.is_task_list(lower.nodes))
        runner.read_page=Mock(side_effect=[lower,main]);runner.device.swipe=Mock();runner.control.wait=Mock()
        self.assertIs(runner.navigate(auto_open=False),main)
        runner.device.swipe.assert_called_once()
        _,y1,_,y2=runner.device.swipe.call_args.args
        self.assertLess(y1,y2)

    def test_cash_home_icon_and_product_prices_do_not_trigger_activity_scroll_restore(self):
        for width, height in ((1080,2400),(1800,2880),(2880,1800)):
            with self.subTest(size=(width,height)):
                runner = self.runner('cash-signin')
                def text(label,x,y,w=.16,h=.03):
                    return TextNode(label,(round(width*x),round(height*y),
                                           round(width*(x+w)),round(height*(y+h))),'ocr')
                icon = text('红包签到',.48,.18)
                home = Screen([icon,text('购物车',.65,.92),text('我的淘宝',.81,.92),
                               text('¥20',.1,.45),text('¥30',.6,.6)],
                              width,height,'com.taobao.taobao','ocr')
                panel = Screen([text('红包签到·领现金',.1,.08,.45),
                                text('累计签到奖励(2/7)',.05,.26,.5)],
                               width,height,home.package,'ocr')
                self.assertIsNone(home.find(runner.page['landing_markers']))
                # Older installed activity configs can still contain this
                # broad label. The navigator must also protect a proved home.
                runner.page['landing_markers'] += '|红包签到'
                runner.device.current_app = Mock(return_value={'package':home.package})
                runner.device.swipe = Mock()
                runner.device.go_back = Mock()
                runner.read_page = Mock(side_effect=[home,panel])
                runner.click = Mock()
                runner.control.wait = Mock()
                self.assertIs(runner.navigate(auto_open=True),panel)
                runner.click.assert_called_once_with(icon)
                runner.device.swipe.assert_not_called()
                runner.device.go_back.assert_not_called()

    def test_cash_main_view_restoration_stops_after_four_attempts(self):
        runner=self.runner('cash-signin')
        lower=Screen([TextNode('红包签到·领现金',(191,174,654,244),'ocr'),
                      TextNode('¥20',(50,1290,220,1380)),TextNode('¥30',(1230,1480,1380,1560))],
                     1440,3200,'com.taobao.taobao','ocr')
        runner.read_page=Mock(return_value=lower);runner.device.swipe=Mock();runner.control.wait=Mock()
        with self.assertRaises(NeedsAttention):
            runner.navigate(auto_open=False)
        self.assertEqual(runner.device.swipe.call_count,4)

    def test_cash_overlay_cannot_expose_background_task_controls(self):
        runner = self.runner('cash-signin')
        header = TextNode('累计签到奖励(1/7)',(70,590,500,650))
        close = TextNode('X',(850,2070,945,2160),'ocr')
        self.assertFalse(runner.is_task_list([header,close]))
        screen = Screen([header,close],1800,2880,'com.taobao.taobao','ocr')
        runner.click=Mock()
        self.assertTrue(runner.promotion_visible(screen))
        self.assertTrue(runner.dismiss_overlay(screen))
        runner.click.assert_called_once_with(close)

    def test_cash_reward_browse_prompt_reuses_destination_without_counting_click(self):
        runner = self.runner('cash-signin')
        before = Screen([TextNode('累计签到奖励(1/7)',(70,590,500,650))],1800,2880,'com.taobao.taobao','ocr')
        overlay = Screen([*before.nodes,TextNode('浏览商品30秒',(400,850,900,920)),
                          TextNode('去领元宝',(450,1800,1300,1900))],1800,2880,before.package,'ocr')
        destination = Screen([TextNode('精选好货',(100,100,700,150)),
                              TextNode('浏览30秒完成任务',(900,100,1700,150))],1800,2880,before.package,'ocr')
        task = Task('timed','签到定时奖励','+1280','点击领取',(1500,1000,1700,1100),kind='claim')
        runner.read_page=Mock(side_effect=[before,overlay,destination])
        runner.click=Mock(); runner.control=SimpleNamespace(now=lambda:0,wait=Mock())
        runner.browse_destination=Mock(return_value=(before,True,False))
        after,explicit,_ = runner.execute(task,before)
        self.assertIs(after,before)
        self.assertFalse(explicit)  # completion banner alone is not a credited reward
        sample = runner.browse_destination.call_args.args[0]
        self.assertEqual((sample.kind,sample.seconds),('browse',30))
        self.assertEqual(runner.click.call_args.args[0].text,'去领元宝')

    def test_cash_navigation_uses_painted_tasks_when_native_tree_has_only_header(self):
        runner = self.runner('cash-signin')
        header = TextNode('累计签到奖励',(100,300,600,360))
        native = Screen([header],1800,2880,'com.taobao.taobao','ui')
        painted = Screen([header,TextNode('立即签到',(350,600,1450,750),'ocr')],
                         1800,2880,native.package,'ocr')
        runner.read_page = Mock(side_effect=[native,painted])
        self.assertIs(runner.ensure_list(),painted)
        # Prefer a meaningful native tile when it retains the complete amount
        # and action in one control, after the painted page was confirmed.
        native.nodes.append(TextNode('+1280元宝，点击领取',(1200,900,1700,1100)))
        runner.read_page = Mock(side_effect=[native,painted])
        self.assertIs(runner.ensure_list(),native)

    def test_achievement_search_query_is_not_the_task_page(self):
        runner = self.runner('achievements')
        query = TextNode('淘宝成就',(130,90,300,140),'ocr')
        self.assertFalse(runner.is_task_list([query]))
        self.assertIsNone(runner.official_activity_entry(Screen([query],1800,2880,'com.taobao.taobao','ocr')))
        banner = TextNode('开启我的淘宝成就',(1300,190,1640,240),'ocr')
        button = TextNode('点击进入>',(1490,300,1630,340),'ocr')
        page = Screen([query,banner,button],1800,2880,'com.taobao.taobao','ocr')
        self.assertIs(runner.official_activity_entry(page),button)

    def test_final_navigation_observation_is_checked_after_slow_ocr(self):
        runner = self.runner()
        clock = SimpleNamespace(value=0)
        runner.control=SimpleNamespace(now=lambda:clock.value,check=Mock())
        screen=Screen([TextNode('今日速赚510淘金币',(300,200,600,250))],1800,2880,'com.taobao.taobao','ocr')
        def slow_read():
            clock.value=200
            return screen
        runner.read_page=Mock(side_effect=slow_read)
        self.assertIs(runner.ensure_list(),screen)
        clock.value=0
        runner.read_page=Mock(side_effect=lambda:(setattr(clock,'value',200) or Screen([],1800,2880,screen.package,'ocr')))
        with self.assertRaises(NeedsAttention): runner.ensure_list()
        self.assertEqual(runner.read_page.call_count,1)

    def test_confirmed_jump_gets_bounded_time_to_load_its_destination(self):
        runner=self.runner('farm');runner.origin='com.taobao.taobao'
        clock=SimpleNamespace(value=0)
        runner.control=SimpleNamespace(now=lambda:clock.value,wait=Mock(),check=Mock())
        runner.device.click=Mock();runner.guard=Mock();runner.check_destination=Mock()
        before=Screen([],1440,3200,runner.origin,'ocr')
        prompt=Screen([TextNode('是否打开示例应用？',(200,1500,1200,1600)),
                       TextNode('本次允许',(850,1700,1100,1800))],1440,3200,runner.origin,'ocr')
        destination=Screen([TextNode('示例首页',(200,600,1200,700)),
                            TextNode('推荐内容',(200,800,1200,900))],1440,3200,'org.example.app','ocr')
        pages=iter([prompt,destination])
        def read(**kwargs):
            clock.value+=runner.config['page_timeout']
            return next(pages)
        runner.read=Mock(side_effect=read);runner.recover=Mock()
        runner.browse_destination=Mock(return_value=(before,False,True))
        task=Task('sample','浏览示例应用','','去完成',(100,800,200,900),kind='visit')
        self.assertEqual(runner.execute(task,before),(before,False,True))
        runner.browse_destination.assert_called_once_with(task,destination)
        runner.recover.assert_not_called()

    def test_activity_entry_ignores_offscreen_and_system_region_duplicates(self):
        from backend.core.device.geometry import DisplayGeometry
        runner=self.runner('achievements')
        visible=TextNode('成就中心',(37,438,264,684),'ui')
        stale=TextNode('成就中心',(37,-240,264,60),'ui')
        bar=TextNode('成就中心',(37,0,264,100),'ui')
        page=Screen([stale,bar,visible],1440,3200,'com.taobao.taobao','ui',
                    geometry=DisplayGeometry(1440,3200,safe_insets=(0,138,0,56)))
        self.assertIs(runner.entry_node(page,['成就中心']),visible)
        page.nodes=[stale,bar]
        self.assertIsNone(runner.entry_node(page,['成就中心']))

    def test_navigation_unwinds_old_launch_overlay_before_accepting_task_list(self):
        runner=self.runner('farm')
        header=TextNode('肥料明细',(100,500,400,600),'ocr')
        panel=Screen([header],1440,3200,'com.taobao.taobao','ocr')
        popup=Screen([header,TextNode('正在离开手机淘宝，打开菜鸟',(200,1500,1200,1600),'ocr'),
                      TextNode('确认',(850,1700,1100,1800),'ocr')],1440,3200,panel.package,'ocr')
        runner.read_page=Mock(side_effect=[popup,panel]);runner.device.go_back=Mock()
        runner.control.wait=Mock();runner.click=Mock()
        self.assertIs(runner.navigate(auto_open=False),panel)
        runner.device.go_back.assert_called_once();runner.click.assert_not_called()

    def test_visible_next_entry_can_advance_after_soft_navigation_budget(self):
        runner=self.runner('farm')
        clock=SimpleNamespace(value=0)
        runner.control=SimpleNamespace(now=lambda:clock.value,check=Mock(),wait=Mock())
        runner.device.current_app=Mock(return_value={'package':'com.taobao.taobao'})
        button=TextNode('芭芭农场',(1250,500,1400,600),'ocr')
        home=Screen([button,TextNode('我的淘宝',(1200,3000,1400,3100),'ocr'),
                     TextNode('购物车',(950,3000,1100,3100),'ocr')],1440,3200,'com.taobao.taobao','ocr')
        panel=Screen([TextNode('肥料明细',(100,500,400,600),'ocr')],1440,3200,home.package,'ocr')
        pages=iter([home,panel])
        def delayed():
            clock.value=runner.config['return_timeout']+runner.config['page_timeout']*3+1
            return next(pages)
        runner.read_page=Mock(side_effect=delayed);runner.click=Mock()
        self.assertIs(runner.navigate(auto_open=True),panel)
        runner.click.assert_called_once_with(button)

    def test_achievement_points_panel_requires_tasks_not_only_the_profile_tab(self):
        runner = self.runner('achievements')
        header = TextNode('淘宝成就',(180,100,560,210))
        menu = TextNode('做任务领成就',(1100,950,1600,1080))
        profile = [header,menu,TextNode('我的成就点0',(300,480,700,570))]
        self.assertFalse(runner.is_task_list(profile))
        nodes = profile + [TextNode('成就每日签到(1/1)',(420,1460,970,1540)),
                           TextNode('领取奖励',(1360,1500,1650,1590)),
                           TextNode('每日登陆领 +3',(420,1560,950,1650))]
        self.assertTrue(runner.is_task_list(nodes))
        task, = runner.parse_tasks(nodes,1800,2880)
        self.assertEqual(task.kind,'claim') # 1/1 is still claimable, not received
        self.assertEqual(task.title,'成就每日签到')
        self.assertEqual(runner.entry_node(Screen(profile,1800,2880,'com.taobao.taobao'),
                                          runner.config['task_entry_labels']),menu)
        # Scrolling away from the first sign-in row must not lose the panel.
        lower = profile + [TextNode('浏览推荐好物(0/2)',(420,1460,970,1540)),
                           TextNode('去完成',(1360,1500,1650,1590))]
        self.assertTrue(runner.is_task_list(lower))

    def test_achievement_navigation_opens_profile_then_points_tasks(self):
        runner = self.runner('achievements')
        title = TextNode('淘宝成就',(180,100,560,210),'ocr')
        tab = TextNode('我的成就',(790,2650,1000,2720),'ocr')
        menu = TextNode('做任务领成就',(1100,950,1600,1080),'ocr')
        mall = Screen([title,tab],1800,2880,'com.taobao.taobao','ocr')
        profile = Screen([title,tab,menu],1800,2880,mall.package,'ocr')
        panel = Screen([title,TextNode('成就每日签到(1/1)',(420,1460,970,1540),'ocr'),
                        TextNode('领取奖励',(1360,1500,1650,1590),'ocr')],1800,2880,mall.package,'ocr')
        runner.read_page = Mock(side_effect=[mall,profile,panel])
        runner.click = Mock(); runner.control.wait = Mock()
        self.assertIs(runner.ensure_list(),panel)
        self.assertEqual([c.args[0] for c in runner.click.call_args_list],[tab,menu])

    def test_achievement_removed_claim_requires_expected_points_increase(self):
        runner = self.runner('achievements')
        header = TextNode('淘宝成就',(180,100,560,210))
        next_row = TextNode('浏览推荐好物(0/1)',(420,1780,970,1860))
        before = Screen([header,next_row,TextNode('我的成就点0',(300,480,700,570))],
                        1800,2880,'com.taobao.taobao','ocr')
        task = Task('claimed','成就每日签到','每日登陆领 +3','领取奖励',
                    (1360,1500,1650,1590),current=1,kind='claim')
        after = Screen([header,next_row,TextNode('我的成就点3',(300,480,700,570))],
                       1800,2880,before.package,'ocr')
        self.assertTrue(runner.verify(task,before,after))
        self.assertFalse(runner.verify(task,before,before))
        after.nodes[-1] = TextNode('我的成就点2',(300,480,700,570))
        self.assertFalse(runner.verify(task,before,after))
        after.nodes[-1] = TextNode('我的成就点300',(300,2480,700,2570))
        self.assertFalse(runner.verify(task,before,after))

    def test_optional_merchandise_signup_does_not_block_points_profile(self):
        runner = self.runner('achievements')
        tab = TextNode('我的成就',(790,2650,1000,2720))
        screen = Screen([TextNode('淘宝成就',(180,100,560,210)),
                         TextNode('未开启签到抽奖快去开启吧~',(50,560,860,630)),tab],
                        1800,2880,'com.taobao.taobao','ocr')
        runner.read = Mock(return_value=screen); runner.click = Mock()
        self.assertIs(runner.read_page(visual=True),screen)
        self.assertFalse(runner.is_task_list(screen.nodes))
        self.assertIs(runner.entry_node(screen,runner.config['task_entry_labels']),tab)
        runner.click.assert_not_called()

    def test_achievement_initialization_never_selects_an_address(self):
        runner = self.runner('achievements')
        screen=Screen([TextNode('淘宝成就',(100,100,500,150)),
                       TextNode('点此设置默认地址',(100,500,800,550))],1800,2880,'com.taobao.taobao')
        runner.read=Mock(return_value=screen);runner.click=Mock()
        with self.assertRaisesRegex(NeedsAttention,'本人先设置收货地址'):
            runner.read_page()
        runner.click.assert_not_called()

    def test_direct_reward_grid_does_not_type_into_hidden_search_field(self):
        runner = self.runner()
        page=Screen([TextNode('浏览15秒可领30',(900,110,1610,210)),
                     TextNode('¥8.25',(50,1150,230,1235)),TextNode('金币已抵',(360,1130,570,1195))],
                    1800,2880,'com.taobao.taobao','ocr')
        runner.device.input_text=Mock()
        self.assertTrue(runner.search_results_ready(page))
        self.assertTrue(runner.handle_search(page))
        runner.device.input_text.assert_not_called()
        page.nodes.append(TextNode('历史搜索',(300,400,600,460)))
        self.assertFalse(runner.search_results_ready(page))

    def test_daily_search_reward_grid_without_sort_toolbar_or_numeric_countdown(self):
        runner = self.runner('achievements')
        nodes = [TextNode('搜索有惊喜',(150,130,550,220)),
                 TextNode('浏览得奖励',(1240,130,1560,220)),
                 TextNode('123',(170,360,280,440)),TextNode('搜索',(1570,360,1710,440)),
                 TextNode('¥199',(50,1290,220,1380)),TextNode('¥119',(1230,1480,1380,1560))]
        page = Screen(nodes,1800,2880,'com.taobao.taobao','ocr')
        self.assertTrue(runner.search_results_ready(page))
        page.nodes = nodes[:4]
        self.assertFalse(runner.search_results_ready(page))
        page.nodes = nodes + [TextNode('历史搜索',(50,580,370,670))]
        self.assertFalse(runner.search_results_ready(page))
        page.nodes = [n for n in nodes if n.text!='搜索有惊喜']
        self.assertFalse(runner.search_results_ready(page))

    def test_separate_countdown_number_requires_same_top_reward_badge(self):
        runner=self.runner('achievements')
        for width,height in ((1080,2400),(1800,2880),(2880,1800)):
            def node(text,x,y,w,h):
                return TextNode(text,(int(width*x),int(height*y),int(width*(x+w)),int(height*(y+h))))
            caption=node('浏览得奖励 +5',.7,.08,.27,.03)
            number=node('14',.63,.08,.055,.03)
            page=Screen([number,caption],width,height,'com.taobao.taobao','ocr')
            self.assertEqual(runner.browse_countdown(page),14)
            page.nodes=[number,node('购物车',.7,.08,.27,.03)]
            self.assertIsNone(runner.browse_countdown(page))
            page.nodes=[number,node('浏览得奖励 +5',.7,.7,.27,.03)]
            self.assertIsNone(runner.browse_countdown(page))
            page.nodes=[node('99',.1,.08,.055,.03),caption]
            self.assertIsNone(runner.browse_countdown(page))

    def test_search_grid_wording_can_be_extended_without_device_branches(self):
        runner=self.runner('farm')
        nodes=[TextNode('搜索有福利',(150,130,550,220)),
               TextNode('滑动浏览得肥料 +500',(1000,130,1650,220)),
               TextNode('123',(170,360,280,440)),TextNode('搜索',(1570,360,1710,440)),
               TextNode('¥109.9',(50,1290,220,1380)),TextNode('¥219',(1230,1480,1380,1560))]
        page=Screen(nodes,1800,2880,'com.taobao.taobao','ocr')
        self.assertTrue(runner.search_results_ready(page))
        page.nodes=nodes+[TextNode('历史搜索',(50,580,370,670))]
        self.assertFalse(runner.search_results_ready(page))
        page.nodes=nodes[:4]
        self.assertFalse(runner.search_results_ready(page))
        page.nodes=[TextNode('搜索有新礼',nodes[0].bounds),*nodes[1:]]
        self.assertFalse(runner.search_results_ready(page))
        runner.config['daily_page']['search_page_markers']+='|^搜索有新礼$'
        self.assertTrue(runner.search_results_ready(page))
        page.nodes=[TextNode('搜索有新礼',(150,2000,550,2090)),*nodes[1:]]
        self.assertFalse(runner.search_results_ready(page))

    def test_countdown_crop_preserves_current_frame_geometry(self):
        runner = self.runner()
        runner._active_task=Task('x','浏览推荐商品','','去完成',(100,100,200,200),kind='browse')
        runner._phase='browsing'
        geometry=object()
        base=Screen([],1800,2880,'com.taobao.taobao','ocr',geometry=geometry)
        enriched=Screen([TextNode('浏览12秒可领30',(900,110,1610,210))],1800,2880,base.package,'ocr+regions')
        runner.reader.enrich_regions=Mock(return_value=enriched)
        with patch.object(Session,'read',return_value=base) as read:
            current=runner.read()
        self.assertIs(current.geometry,geometry)
        self.assertEqual(runner.browse_countdown(current),12)
        read.assert_called_once_with(force_ocr=True)

    def test_search_uses_focused_setter_without_counting_all_edit_nodes(self):
        from unittest.mock import MagicMock
        runner = self.runner()
        runner.origin='com.taobao.taobao'
        overflow=type('RPCStackOverflowError',(Exception,),{})
        device=MagicMock(device_type='uiautomator2')
        device.current_app.return_value = {'package': runner.origin}
        device.input_ascii=None
        device.input_text.side_effect=overflow()
        runner.device=device
        runner.read_page=Mock(return_value=Screen([TextNode('历史搜索',(100,600,400,650)),
                                                  TextNode('搜索',(1510,350,1760,500))],1800,2880,runner.origin,'ocr'))
        runner.click=Mock();runner.control.wait=Mock()
        runner.fill_search_field('手机壳',{'bounds':(140,350,1500,500)})
        self.assertEqual(runner.click.call_args.args[0].bounds,(140,350,1500,500))
        device.assert_called_once_with(className='android.widget.EditText',focused=True)
        device.return_value.set_text.assert_called_once_with('手机壳',timeout=.25)
        device.input_text.assert_not_called()

    def test_ascii_task_search_keeps_unicode_activity_navigation(self):
        from unittest.mock import MagicMock
        runner=self.runner();runner.origin='com.taobao.taobao'
        runner.config['task_search_keywords'] = []
        device=MagicMock(device_type='uiautomator2');runner.device=device
        device.current_app.return_value = {'package': runner.origin}
        runner.read_page=Mock(return_value=Screen([TextNode('历史搜索',(100,600,400,650)),
                                                  TextNode('搜索',(1510,350,1760,500))],1800,2880,runner.origin,'ocr'))
        runner.click=Mock();runner.control.wait=Mock()
        edit={'bounds':(140,350,1500,500)}
        runner.fill_search_field('手机壳',edit)
        device.input_ascii.assert_called_once_with('123')
        device.assert_not_called()
        device.reset_mock()
        runner.fill_search_field('phone',edit)
        device.input_ascii.assert_called_once_with('123')
        device.reset_mock()
        runner.fill_search_field('2026',edit)
        device.input_ascii.assert_called_once_with('2026')
        device.reset_mock()
        runner.fill_search_field(runner.config['search_keyword'],edit)
        device.input_ascii.assert_not_called()
        device.return_value.set_text.assert_called_once_with(runner.config['search_keyword'],timeout=.25)

    def test_ime_x_keys_and_search_page_glyphs_are_never_popup_closers(self):
        runner = self.runner()
        for glyph in ('x', 'X'):
            for reward_text in ('', '恭喜获得奖励'):
                with self.subTest(glyph=glyph, reward=reward_text):
                    page = Screen([TextNode('历史搜索',(40,350,350,410)),
                                   TextNode('搜索',(850,100,980,170)),
                                   TextNode(glyph,(200,1500,260,1560)),
                                   TextNode('中/英',(600,1750,730,1810)),
                                   TextNode('符',(30,1750,130,1810)),
                                   TextNode(reward_text,(200,900,700,960))],
                                  1000,2000,'com.taobao.taobao','ocr')
                    runner.click = Mock()
                    self.assertIsNone(runner.popup_close_node(page))
                    self.assertIsNone(runner.dismiss_popup(page))
                    self.assertFalse(runner.dismiss_overlay(page))
                    runner.click.assert_not_called()

    def test_daily_input_relocates_native_field_after_keyboard_rebuild(self):
        from unittest.mock import MagicMock
        runner = self.runner()
        runner.origin = 'com.taobao.taobao'
        runner.config['task_search_keywords'] = []
        runner.device = MagicMock(device_type='uiautomator2')
        runner.device.current_app.return_value = {'package': runner.origin}
        runner.read_page = Mock(return_value=Screen([
            TextNode('历史搜索',(100,600,400,650)),TextNode('搜索',(1510,350,1760,500))],
            1800,2880,runner.origin,'ocr'))
        current = (140,350,1500,500)
        runner.search_tree = Mock(return_value={'children':[
            dict(type='android.widget.EditText',focused='true',bounds=current)]})
        runner.click = Mock()
        runner.control.wait = Mock()
        runner.fill_search_field('2026', {'bounds': (0,9000,1000,10000)})
        self.assertEqual(runner.click.call_args.args[0].bounds, current)
        runner.device.input_ascii.assert_called_once_with('2026')

    def test_expand_is_scoped_to_coin_task_panel_and_retries_until_painted(self):
        runner=self.runner()
        header=TextNode('今日速赚510淘金币',(460,200,950,270),'ocr')
        button=TextNode('展开',(1190,2540,1300,2590),'ocr')
        context=TextNode('更多金币等你赚',(490,2540,870,2590),'ocr')
        page=Screen([header,context,button],1800,2880,'com.taobao.taobao','ocr')
        runner.read_page=Mock(return_value=page)
        runner.control=SimpleNamespace(now=lambda:0,check=Mock(),wait=Mock())
        runner.click=Mock()
        self.assertIs(runner.ensure_list(),page)
        # 这张假页面永远停在折叠态（标题和「展开」一直都在），等于每次点完都
        # 没展开成功：重试到上限就停，不会在一个按钮上无限点。
        self.assertEqual(runner.click.call_count,runner.EXPAND_TRIES)
        runner.click.assert_called_with(button)
        runner.click.reset_mock()
        runner.read_page=Mock(return_value=Screen([header,button],1800,2880,page.package,'ocr'))
        runner.ensure_list()
        runner.click.assert_not_called()

    def test_expand_stops_after_one_click_when_queue_is_painted(self):
        runner=self.runner()
        header=TextNode('今日速赚510淘金币',(460,200,950,270),'ocr')
        button=TextNode('展开',(1190,2540,1300,2590),'ocr')
        context=TextNode('更多金币等你赚',(490,2540,870,2590),'ocr')
        collapsed=Screen([header,context,button],1800,2880,'com.taobao.taobao','ocr')
        # 展开后「更多金币等你赚」整行会被任务行替换掉——标题和「展开」都不见了。
        opened=Screen([header,TextNode('逛支付宝会员频道(0/1)',(190,2620,700,2680),'ocr'),
                       TextNode('去完成',(1420,2620,1690,2700),'ocr')],1800,2880,'com.taobao.taobao','ocr')
        runner.control=SimpleNamespace(now=lambda:0,check=Mock(),wait=Mock())
        runner.click=Mock()
        runner.read_page=Mock(side_effect=[opened,opened,opened])
        self.assertIs(runner.ensure_expanded(collapsed),opened)
        runner.click.assert_called_once_with(button)

    def test_farm_delayed_promotion_is_closed_without_shopping_action(self):
        runner=self.runner('farm')
        close=TextNode('X',(850,1940,950,2040),'ocr')
        page=Screen([TextNode('芭芭农场|免费领水果',(120,80,580,150)),
                     TextNode('品质农场上新季',(600,920,1200,1000)),
                     TextNode('¥0.01抢',(1020,1200,1200,1280)),
                     TextNode('看看更多新品',(580,1750,1250,1840)),close],1800,2880,'com.taobao.taobao','ocr')
        runner.click=Mock()
        self.assertTrue(runner.promotion_visible(page))
        self.assertTrue(runner.dismiss_overlay(page))
        runner.click.assert_called_once_with(close)

    def test_navigation_ignores_native_channel_nodes_behind_farm_popup(self):
        runner=self.runner('farm')
        stale=Screen([TextNode('全部频道',(100,100,300,160)),
                      TextNode('芭芭农场',(100,900,400,960))],1800,2880,'com.taobao.taobao','ui')
        close=TextNode('X',(850,1940,950,2040),'ocr')
        popup=Screen([TextNode('昨日施肥奖励待领取',(500,1000,1300,1070)),close],1800,2880,stale.package,'ocr')
        task_list=Screen([TextNode('肥料明细',(100,100,400,170))],1800,2880,stale.package,'ocr')
        runner.read_page=Mock(side_effect=[stale,popup,task_list])
        runner.click=Mock()
        runner.control=SimpleNamespace(now=lambda:0,check=Mock(),wait=Mock())
        self.assertIs(runner.ensure_list(),task_list)
        runner.click.assert_called_once_with(close)

    def test_sidebar_order_ad_cannot_disable_visible_farm_browse_task(self):
        runner=self.runner('farm')
        nodes=[TextNode('浏览全场好物(0/1)',(640,500,1050,550)),
               TextNode('浏览15秒',(640,560,900,600)),TextNode('去完成',(1160,520,1300,580)),
               TextNode('下单得肥料最高+90000',(80,540,400,590)),
               TextNode('下单一次(0/1)',(640,720,1050,770)),TextNode('去完成',(1160,740,1300,800))]
        tasks=runner.parse_tasks(nodes,1800,2880)
        self.assertEqual([t.kind for t in tasks],['browse','browse'])
        self.assertIn('常驻关键词',runner.availability_reason(tasks[1]))
        self.assertNotIn('下单',tasks[0].detail)

    def test_unlabelled_reward_amount_is_not_a_synthetic_task(self):
        runner=self.runner('farm')
        tasks=runner.parse_tasks([TextNode('x500',(700,500,900,550)),
                                  TextNode('已领取',(1160,520,1300,580))],1800,2880)
        self.assertEqual(tasks,[])

    def test_optional_order_bonus_does_not_disable_an_explicit_browse_reward(self):
        runner=self.runner('farm')
        title='浏览全场1元淘工厂'
        detail='先浏览赚肥料，下单再得肥料 +150'
        self.assertEqual(runner.classify_daily(title,detail,[title,detail],'去完成',0,1)[1],'browse')
        self.assertEqual(runner.classify_daily(title,detail,[title,'先浏览赚肥料,','下单再得肥料 +150'],'去完成',0,1)[1],'browse')
        required=detail+'，必须下单'
        self.assertIn('常驻关键词',runner.availability_reason(Task('policy',title,required,'去完成',(1,2,3,4))))
        self.assertIn('常驻关键词',runner.availability_reason(Task('policy','下单一次',detail,'去完成',(1,2,3,4))))


def make_daily_runner(activity='coins'):
    """构造一个不连接设备的日常活动实例，用于补扫逻辑的单测。"""
    registry = Registry(Path(__file__).resolve().parents[1]/'backend/app').discover()
    config = load_config(app='taobao', activity=activity, registry=registry)
    runner = registry.instantiate(SimpleNamespace(device_type='uiautomator2', selected_instance=None),
                                  config, app='taobao', activity=activity)
    runner.pause = lambda seconds, jitter=None: None
    runner.update = Mock()
    runner.config['ocr'] = False
    return runner


def screen_of(*texts):
    nodes = [TextNode(t, (40, 300 + i*90, 900, 340 + i*90)) for i, t in enumerate(texts)]
    return Screen(nodes, 1080, 2400, 'com.taobao.taobao')


class RescanVerdictTests(unittest.TestCase):
    """补扫三态：只有“翻到底且没有可执行行”才算真的做完。"""

    def test_unknown_when_list_cannot_be_opened(self):
        runner = make_daily_runner()
        runner.ensure_list = Mock(side_effect=RuntimeError('导航失败'))
        self.assertEqual(runner.rescan(), runner.SCAN_UNKNOWN)

    def test_unknown_when_page_leaves_task_list(self):
        runner = make_daily_runner()
        first = screen_of('更多金币等你赚', '浏览精选店铺')
        runner.ensure_list = Mock(return_value=first)
        runner.ensure_expanded = Mock(side_effect=lambda screen, force=False: screen)
        runner.runnable_rows = Mock(return_value=[])
        runner.tasks = Mock(return_value=[SimpleNamespace(key='a')])
        runner.is_task_list = Mock(return_value=False)
        runner.read = Mock(return_value=screen_of('直播间', '立即购买'))
        runner.device = SimpleNamespace(swipe=Mock())
        self.assertEqual(runner.rescan(), runner.SCAN_UNKNOWN)

    def test_empty_when_scroll_produces_nothing_new(self):
        runner = make_daily_runner()
        first = screen_of('更多金币等你赚', '浏览精选店铺')
        runner.ensure_list = Mock(return_value=first)
        runner.ensure_expanded = Mock(side_effect=lambda screen, force=False: screen)
        runner.runnable_rows = Mock(return_value=[])
        runner.tasks = Mock(return_value=[SimpleNamespace(key='a')])
        runner.is_task_list = Mock(return_value=True)
        runner.read = Mock(return_value=first)
        runner.device = SimpleNamespace(swipe=Mock())
        self.assertEqual(runner.rescan(), runner.SCAN_EMPTY)

    def test_empty_when_task_rows_repeat(self):
        runner = make_daily_runner()
        page1 = screen_of('更多金币等你赚', '浏览精选店铺')
        page2 = screen_of('更多金币等你赚', '逛一逛得金币')
        runner.ensure_list = Mock(return_value=page1)
        runner.ensure_expanded = Mock(side_effect=lambda screen, force=False: screen)
        runner.runnable_rows = Mock(return_value=[])
        runner.tasks = Mock(return_value=[SimpleNamespace(key='a')])
        runner.is_task_list = Mock(return_value=True)
        runner.read = Mock(side_effect=[page2, page1])
        runner.device = SimpleNamespace(swipe=Mock())
        self.assertEqual(runner.rescan(), runner.SCAN_EMPTY)

    def test_unknown_when_page_budget_runs_out(self):
        runner = make_daily_runner()
        runner.ensure_list = Mock(return_value=screen_of('第1屏'))
        runner.ensure_expanded = Mock(side_effect=lambda screen, force=False: screen)
        runner.runnable_rows = Mock(return_value=[])
        runner.tasks = Mock(side_effect=lambda screen: [SimpleNamespace(key=screen.text[:8])])
        runner.is_task_list = Mock(return_value=True)
        runner.read = Mock(side_effect=[screen_of('第%d屏' % i) for i in range(2, 20)])
        runner.device = SimpleNamespace(swipe=Mock())
        self.assertEqual(runner.rescan(pages=3), runner.SCAN_UNKNOWN)

    def test_found_when_a_runnable_row_exists(self):
        runner = make_daily_runner()
        # 真实列表屏：标题 + 任务行 + 动作词，补扫才会认为“停在了列表上”
        runner.ensure_list = Mock(return_value=screen_of('更多金币等你赚', '浏览精选店铺', '去完成'))
        runner.ensure_expanded = Mock(side_effect=lambda screen, force=False: screen)
        runner.runnable_rows = Mock(return_value=[SimpleNamespace(title='浏览精选店铺')])
        self.assertEqual(runner.rescan(), runner.SCAN_FOUND)

    def test_collapsed_panel_is_opened_before_scanning(self):
        """折叠态下「更多金币等你赚」右侧还挂着「展开」，队列根本没画出来。

        这种页面同样满足 ``is_task_list``（分区标题都在），翻一屏也不会变——
        不先展开就会判成“翻到底、没有任务”。这里确认补扫会先把它点开。
        """
        runner = make_daily_runner()
        collapsed = screen_of('更多金币等你赚', '展开')
        opened = screen_of('更多金币等你赚', '逛支付宝会员频道(0/1)', '去完成')
        runner.ensure_list = Mock(return_value=collapsed)
        runner.ensure_expanded = Mock(return_value=opened)
        runner.runnable_rows = Mock(return_value=[SimpleNamespace(title='逛支付宝会员频道')])
        runner.is_task_list = Mock(return_value=True)
        self.assertEqual(runner.rescan(), runner.SCAN_FOUND)
        runner.ensure_expanded.assert_called_once_with(collapsed)

    def test_unknown_when_collapsed_panel_cannot_be_opened(self):
        """展不开的折叠段不能算“没有任务”：那等于把整段未读队列当成空的。"""
        runner = make_daily_runner()
        collapsed = screen_of('更多金币等你赚', '展开')
        runner.ensure_list = Mock(return_value=collapsed)
        runner.ensure_expanded = Mock(return_value=collapsed)
        runner.runnable_rows = Mock(return_value=[])
        runner.tasks = Mock(return_value=[])
        runner.is_task_list = Mock(return_value=True)
        self.assertEqual(runner.rescan(), runner.SCAN_UNKNOWN)


class RescanRestartTests(unittest.TestCase):
    """补扫前必须先退出客户端再重新进入：WebView 缓存不刷新会漏掉任务。"""

    class Device:
        """只记录“重开客户端”的调用顺序，配合补扫断言用。"""

        def __init__(self, events, fail_on=None):
            self.device_type = 'uiautomator2'
            self.events = events
            self.fail_on = fail_on
            self.swipe = Mock()

        def go_home(self):
            self.events.append('home')

        def go_back(self):
            self.events.append('back')

        def stop_app(self, package):
            if self.fail_on == 'stop':
                raise RuntimeError('force-stop 不被允许')
            self.events.append(('stop', package))

        def start_app(self, package):
            self.events.append(('start', package))

    def runner(self, **overrides):
        events = []
        runner = make_daily_runner()
        runner.origin = 'com.taobao.taobao'
        runner.device = self.Device(events)
        page = screen_of('更多金币等你赚', '浏览精选店铺', '去完成')
        # leave_list 先看到列表、退出后看到活动首页，才算“真的离开了列表”。
        runner.read_page = Mock(side_effect=[page, screen_of('去赚金币')])
        runner.ensure_list = Mock(side_effect=lambda **kw: events.append('list') or page)
        runner.ensure_expanded = Mock(side_effect=lambda screen, force=False: screen)
        runner.runnable_rows = Mock(return_value=[SimpleNamespace(title='浏览精选店铺')])
        runner.config['daily_page'] = dict(runner.config['daily_page'], **dict({'rescan_restart_app': True}, **overrides))
        return runner, events

    def test_client_is_restarted_before_the_list_is_reopened(self):
        runner, events = self.runner()
        self.assertEqual(runner.rescan(), runner.SCAN_FOUND)
        # 顺序必须是：回桌面 → 强停 → 冷启动 → 再进活动列表
        self.assertEqual(events, ['home', ('stop', 'com.taobao.taobao'),
                                  ('start', 'com.taobao.taobao'), 'list'])

    def test_restart_can_be_switched_off(self):
        """关掉客户端重启不等于不做“重新进入”：仍要先退出列表再重开。

        新版本默认就是关着的，而 navigate 看到当前已在列表会直接返回，所谓
        「重新进入列表确认没有可执行任务」会变成把旧页面再读一遍，服务端
        这一分钟刷新出来的进度一条都拿不到——11-05 真机上就是这样收工的。
        """
        runner, events = self.runner(rescan_restart_app=False)
        self.assertEqual(runner.rescan(), runner.SCAN_FOUND)
        self.assertEqual(events, ['back', 'list'])

    def test_restart_budget_limits_repeated_cold_starts(self):
        runner, events = self.runner(rescan_restart_max=1)
        runner.rescan()
        first = events.count(('start', 'com.taobao.taobao'))
        runner.rescan()
        self.assertEqual(first, 1)
        # 第二次不再重启，但扫描照旧进行，不会因为预算用尽就跳过检查
        self.assertEqual(events.count(('start', 'com.taobao.taobao')), 1)
        self.assertEqual(events.count('list'), 2)

    def test_restart_failure_still_scans_current_page(self):
        runner, events = self.runner()
        runner.device.fail_on = 'stop'
        # 强停失败不能把整轮补扫搞崩：退出列表后重进，结论仍然严格判定
        self.assertEqual(runner.rescan(), runner.SCAN_FOUND)
        self.assertEqual(events, ['home', 'back', 'list'])

    def test_run_resets_the_restart_budget(self):
        runner, events = self.runner(rescan_restart_max=1)
        runner.rescan()
        runner.rescan()
        self.assertEqual(events.count(('start', 'com.taobao.taobao')), 1)
        runner._rescan_restarts = 0
        runner.rescan()
        self.assertEqual(events.count(('start', 'com.taobao.taobao')), 2)


class RescanLoopTests(unittest.TestCase):
    """收工条件：连续两次完整扫描都确认没有可执行任务。"""

    def runner_with(self, verdicts, progress=True, watch=0):
        runner = make_daily_runner()
        # 收工守望（默认还要额外等 60 秒再确认几次）在这里关掉：这组用例只验证
        # “确认几次才收工”，守望本身由 RescanWatchTests 单独覆盖。
        runner.config['rescan'] = dict(runner.config.get('rescan') or {},
                                       rescan_settle_end=0, rescan_watch=watch)
        runner.rescan = Mock(side_effect=verdicts)

        def fake_run(auto_open=False):
            if progress:
                # progress_mark() 只看“定案记录”，不看尝试次数；每轮真的做完
                # 一条任务就要留下一条记录，否则会被当成“轮轮空转”。
                runner.records[f'k{runner.stats.attempts}'] = {'status': 'done', 'title': 't'}
                runner.stats.attempts += 1
            return None
        runner._fake_run = fake_run
        return runner

    def test_one_empty_scan_after_a_round_finishes_the_run(self):
        # 每轮结束只重进一次：这一眼是空的就收工，不再反复重进确认。
        runner = self.runner_with(['found', 'empty'])
        with patch.object(Activity, 'run', side_effect=runner._fake_run):
            runner.run()
        self.assertEqual(runner.rescan.call_count, 2)

    def test_keeps_working_when_scan_finds_more_tasks(self):
        runner = self.runner_with(['found', 'found', 'found', 'empty', 'empty'])
        with patch.object(Activity, 'run', side_effect=runner._fake_run):
            runner.run()
        self.assertGreaterEqual(runner.stats.attempts, 3)

    def test_unknown_scans_never_count_as_finished(self):
        runner = self.runner_with(['unknown'] * 12)
        with patch.object(Activity, 'run', side_effect=runner._fake_run):
            runner.run()
        self.assertEqual(runner.stats.attempts, 1)
        self.assertEqual(runner.rescan.call_count, 2)

    def test_stops_after_three_rounds_without_progress(self):
        runner = self.runner_with(['found'] * 10, progress=False)
        with patch.object(Activity, 'run', side_effect=runner._fake_run):
            runner.run()
        self.assertEqual(runner.rescan.call_count, 2)
        self.assertEqual(runner.stats.attempts, 0)

    def test_first_round_runs_before_any_scan(self):
        runner = self.runner_with(['empty', 'empty'])
        with patch.object(Activity, 'run', side_effect=runner._fake_run):
            runner.run()
        self.assertEqual(runner.stats.attempts, 1)

    def test_keeps_going_past_the_round_budget_while_rounds_still_progress(self):
        # 淘宝的任务是一批一批刷出来的：只要每轮都真的推得动，就说明列表里还有
        # 活可干。按“到 3 轮就收工”硬停，界面上会留下一条条还能做的任务。
        runner = self.runner_with(['found'] * 40, progress=True)
        runner.config['daily_page'] = dict(runner.config['daily_page'], rescan_rounds=3)
        with patch.object(Activity, 'run', side_effect=runner._fake_run):
            runner.run()
        self.assertGreaterEqual(runner.stats.attempts, 5)


class PlainBrowseTaskTests(unittest.TestCase):
    """描述里只写「浏览」、没写秒数的任务，必须照常做、照常重试。

    淘宝新版把这些任务的时长省掉了（「发现精选好物」下方只有「浏览 +40」）。
    早先的判定是“有浏览要求、没有时长 = 没法判断做没做成”，于是只允许今天试
    一次：第一次没推进就整天不再做，任务直接漏掉。
    """

    @staticmethod
    def row(title, detail, amount=None):
        nodes = [TextNode(title, (60, 900, 620, 950)),
                 TextNode(detail, (60, 955, 300, 995))]
        if amount:
            nodes.append(TextNode(amount, (330, 955, 420, 995)))
        nodes.append(TextNode('去完成', (820, 915, 980, 975)))
        return Screen(nodes, 1080, 2400, 'com.taobao.taobao')

    def test_task_without_any_duration_is_not_locked_to_one_try(self):
        runner = make_daily_runner()
        rows = runner.tasks(self.row('发现精选好物 (4/6)', '浏览', '+40'))
        self.assertEqual(len(rows), 1)
        task = rows[0]
        self.assertEqual(task.title, '发现精选好物')
        self.assertEqual(task.kind, 'browse')
        self.assertFalse(getattr(task, 'browse_once', False))

    def test_task_with_a_stated_duration_keeps_its_seconds(self):
        runner = make_daily_runner()
        rows = runner.tasks(self.row('看看你关注的新品动态 (0/1)', '浏览5秒', '+20'))
        self.assertEqual(rows[0].seconds, 5)
        self.assertFalse(getattr(rows[0], 'browse_once', False))

    def test_title_noun_does_not_skip_a_plain_browse_task(self):
        # 标题里的“关注”只是“你关注的新品”这个名词的一部分，描述写的是浏览。
        runner = make_daily_runner()
        task = Task('a', '看看你关注的新品动态', '浏览5秒 +20', '去完成', (820, 915, 980, 975),
                    0, 1, 5, 'browse')
        self.assertEqual(runner.availability_reason(task), '')

    def test_a_real_action_word_in_the_detail_still_skips(self):
        runner = make_daily_runner()
        for detail in ('下单得 +300', '完成带图评价得 +100', '浏览后下单再得 +50'):
            with self.subTest(detail=detail):
                task = Task('b', '买限时折扣好物得奖励', detail, '去完成', (820, 915, 980, 975),
                            0, 1, 0, 'browse')
                self.assertIn('命中常驻关键词', runner.availability_reason(task))

    def test_action_style_title_still_skips_even_with_a_browse_detail(self):
        # 「关注店铺」是真的要你去关注，跟「你关注的新品」这种名词不是一回事。
        runner = make_daily_runner()
        task = Task('d', '关注店铺', '浏览5秒', '去完成', (820, 915, 980, 975),
                    0, 1, 5, 'browse')
        self.assertIn('命中常驻关键词', runner.availability_reason(task))

    def test_policy_choice_still_wins_over_the_browse_exception(self):
        runner = make_daily_runner()
        task = Task('c', '看看你关注的新品动态', '浏览5秒 +20', '去完成', (820, 915, 980, 975),
                    0, 1, 5, 'browse')
        runner.task_policy.choose(task_identity(task), skip=True)
        self.assertIn('用户选择', runner.availability_reason(task))


class RescanReidentifyTests(unittest.TestCase):
    """每轮补扫重新进入时，必须把上一轮的任务清单丢掉，按新页面重新识别。

    不这么做，扫描读到的就是上一轮的缓存结论：一个“今日已试一次”的浏览任务、
    一条 disabled 记录，都会把还能做的任务挡在候选之外，连续两次判空就收工。
    """

    def runner(self):
        runner = make_daily_runner()
        runner.origin = 'com.taobao.taobao'
        runner.device = SimpleNamespace(device_type='uiautomator2', swipe=Mock(),
                                        go_home=Mock(), stop_app=Mock(), start_app=Mock())
        page = screen_of('更多金币等你赚', '浏览精选店铺', '去完成')
        runner.ensure_list = Mock(return_value=page)
        runner.ensure_expanded = Mock(side_effect=lambda screen, force=False: screen)
        runner.runnable_rows = Mock(return_value=[])
        runner.tasks = Mock(return_value=[])
        runner.is_task_list = Mock(return_value=True)
        runner.read = Mock(return_value=page)
        return runner

    def test_stale_records_are_dropped_before_the_page_is_parsed(self):
        runner = self.runner()
        runner.records = {'k1': {'status': 'disabled', 'reason': '已跳过：无明确浏览时长，今日已尝试一次'}}
        runner._known_tasks = {'k1': SimpleNamespace(key='k1')}
        runner._verified_progress = {('id', 5)}
        seen = []
        runner.ensure_list = Mock(side_effect=lambda **kw: seen.append(dict(runner.records))
                                  or screen_of('更多金币等你赚', '去完成'))
        runner.rescan()
        # 重新进列表那一刻，上一轮的清单已经清空（不是扫描完才清）
        self.assertEqual(seen, [{}])
        self.assertEqual(runner._known_tasks, {})
        self.assertEqual(runner._verified_progress, set())

    def test_reentry_gives_the_stuck_browse_task_one_more_chance(self):
        runner = self.runner()
        task = Task('k', '好物沉浸看', '浏览', '去完成', (0, 0, 10, 10),
                    kind='browse', current=5, total=6)
        runner.task_policy.mark_browse_once(task)
        self.assertIn('今日已尝试一次', runner.task_policy.reason(task))
        runner.rescan()
        self.assertEqual(runner.task_policy.reason(task), '')

    def test_old_framework_without_retry_transient_keeps_its_public_contract(self):
        """旧框架没有公开接口时不修改私有状态，保留原有额度与选择。"""
        class LegacyPolicy:
            def __init__(self):
                self.entries = {'id1': {'browse_once_attempted': True},
                                'id2': {'skip': True},
                                'id3': {'failures': 2, 'exhausted': True}}
                self.saved = 0

            def _save(self):
                self.saved += 1

        runner = self.runner()
        policy = LegacyPolicy()
        runner.task_policy = policy
        granted = set()
        self.assertEqual(runner.unlock_once_only(policy, granted), 0)
        self.assertIn('id1', policy.entries)                 # 锁没了，空记录一并回收
        self.assertTrue(policy.entries['id2']['skip'])         # 用户手动跳过不动
        self.assertTrue(policy.entries['id3']['exhausted'])    # 当日失败额度不动
        self.assertEqual(policy.saved, 0)
        policy.entries['id1'] = {'browse_once_attempted': True}
        self.assertEqual(runner.unlock_once_only(policy, granted), 0)

    def test_finished_panel_keeps_the_last_parsed_list(self):
        """清空后这一轮没跑出新记录时，收工前要把快照放回去，面板不能是空表。"""
        runner = self.runner()
        runner.records = {'k1': {'status': 'disabled', 'reason': 'x'}}
        runner.rescan()
        self.assertEqual(runner.records, {})
        runner.restore_list_state()
        self.assertEqual(list(runner.records), ['k1'])


class RescanOneShotTests(unittest.TestCase):
    """每轮结束只重进一次客户端，判空即收工。

    旧的收工前“守望确认”会在列表明明已经空了的时候继续退出重进，看起来像
    死循环；现在只允许重进一次，没看全才在原地重试（不重进）。
    """

    def runner(self, verdicts, **rescan):
        runner = make_daily_runner()
        runner.config['rescan'] = dict({'rescan_retries': 2, 'rescan_pause': 0}, **rescan)
        runner.rescan = Mock(side_effect=verdicts)

        def fake_run(auto_open=False):
            runner.stats.attempts += 1
            return None
        runner._fake_run = fake_run
        return runner

    def test_empty_verdict_finishes_without_a_second_reentry(self):
        runner = self.runner(['empty', 'empty', 'empty'])
        with patch.object(Activity, 'run', side_effect=runner._fake_run):
            runner.run()
        self.assertEqual(runner.rescan.call_count, 1)

    def test_retry_after_unknown_reuses_the_open_list(self):
        runner = self.runner(['unknown', 'found', 'empty'])
        with patch.object(Activity, 'run', side_effect=runner._fake_run):
            runner.run()
        restarts = [c.kwargs.get('restart') for c in runner.rescan.call_args_list]
        self.assertEqual(restarts[:2], [True, False])


class BrowseCreditTests(unittest.TestCase):
    """浏览落地页报「已得 N」时，即便列表进度还没刷新也要认这一条已完成。

    真机现场：好物沉浸看 0/6 执行完回到列表，读到的仍是 0，被判失败并锁上
    「今日只试一次」；而落地页上明明浮着「已得50」，进度后来涨到了 3/6。
    """

    def runner(self):
        runner = make_daily_runner()
        runner.tasks = Mock(return_value=[SimpleNamespace(
            key='k1', title='好物沉浸看', kind='browse', current=0, total=6, action='去完成')])
        return runner

    def task(self):
        return SimpleNamespace(key='k1', title='好物沉浸看', kind='browse',
                               current=0, total=6, action='去完成')

    def test_credited_browse_is_success_even_when_progress_lags(self):
        runner = self.runner()
        screen = screen_of('更多金币等你赚', '好物沉浸看', '去完成')
        runner._browse_credited = 'k1'
        self.assertFalse(runner.verify(self.task(), screen, screen))

    def test_credit_only_counts_once_and_only_for_that_task(self):
        runner = self.runner()
        screen = screen_of('更多金币等你赚', '好物沉浸看', '去完成')
        runner._browse_credited = 'k1'
        runner.verify(self.task(), screen, screen)
        # 标记已被消费：同一任务再判一次要回到普通证据，不能一直算成功。
        self.assertFalse(runner.verify(self.task(), screen, screen))
        runner._browse_credited = 'other'
        self.assertFalse(runner.verify(self.task(), screen, screen))


if __name__=='__main__':
    unittest.main()

class PaintedDismissalTests(unittest.TestCase):
    def test_hidden_skip_never_beats_visible_close(self):
        runner=TaobaoDailyTests().runner();runner.click=Mock();runner.control.wait=Mock()
        native=Screen([TextNode('跳过',(300,400,400,440))],1000,1600,'com.taobao.taobao','ui')
        close=TextNode('X',(470,1000,520,1050),'ocr')
        visual=Screen([TextNode('送你能量',(300,400,700,450),'ocr'),close],1000,1600,native.package,'ocr')
        after=Screen([TextNode('日常版',(800,200,950,300),'ocr')],1000,1600,native.package,'ocr')
        with patch.object(Session,'read',side_effect=[visual,after]):
            self.assertIs(runner.dismiss_popup(native),after)
        runner.click.assert_called_once_with(close)

    def test_invisible_skip_without_painted_popup_does_nothing(self):
        runner=TaobaoDailyTests().runner();runner.click=Mock()
        native=Screen([TextNode('跳过',(300,400,400,440))],1000,1600,'com.taobao.taobao','ui')
        visual=Screen([TextNode('日常版',(800,200,950,300),'ocr')],1000,1600,native.package,'ocr')
        with patch.object(Session,'read',return_value=visual):
            self.assertIsNone(runner.dismiss_popup(native))
        runner.click.assert_not_called()


class KeywordAndSearchPopupTests(unittest.TestCase):
    def runner(self):
        return TaobaoDailyTests().runner()

    def test_x_in_native_edit_is_not_a_popup_or_dismissal_target(self):
        runner = self.runner()
        edit = {'class':'android.widget.EditText','text':'x','bounds':(100,160,850,240)}
        field = TextNode('X', (200,170,260,230))
        page = Screen([field,TextNode('历史搜索',(100,400,350,450))],1000,2000,
                      'com.taobao.taobao','ui',tree={'children':[edit]})
        runner.click = Mock()
        with patch.object(Session,'read') as read:
            self.assertIsNone(runner.dismiss_popup(page))
        self.assertFalse(runner.dismiss_overlay(page))
        runner.click.assert_not_called()
        read.assert_not_called()

    def test_ocr_query_and_clear_cross_are_not_popup_buttons_even_without_submit(self):
        runner = self.runner()
        for value in ('x','X','×'):
            with self.subTest(value=value):
                page = Screen([TextNode(value,(780,170,820,220)),
                               TextNode('历史搜索',(100,400,350,450))],1000,2000,
                              'com.taobao.taobao','ocr')
                self.assertIsNone(runner.popup_close_node(page))
                self.assertIsNone(runner.dismiss_popup(page))

    def test_real_reward_close_below_search_field_still_dismisses(self):
        runner = self.runner()
        close = TextNode('X',(470,1500,520,1550),'ocr')
        page = Screen([TextNode('历史搜索',(100,400,350,450)),
                       TextNode('恭喜获得奖励',(200,1000,700,1100)),close],1000,2000,
                      'com.taobao.taobao','ocr')
        fresh = Screen([],1000,2000,page.package,'ocr')
        runner.click = Mock();runner.control.wait = Mock()
        with patch.object(Session,'read',return_value=fresh):
            self.assertIs(runner.dismiss_popup(page), fresh)
        runner.click.assert_called_once_with(close)

    def test_configured_unicode_query_uses_focused_field_then_bounded_numeric_fallback(self):
        from unittest.mock import MagicMock
        runner=self.runner();runner.origin='com.taobao.taobao'
        runner.config['task_search_keywords']=['雨伞']
        device=MagicMock(device_type='uiautomator2');runner.device=device
        device.current_app.return_value={'package':runner.origin}
        runner.read_page=Mock(return_value=Screen([TextNode('历史搜索',(100,600,400,650)),
                                                  TextNode('搜索',(1510,350,1760,500))],1800,2880,runner.origin,'ocr'))
        runner.click=Mock();runner.control.wait=Mock()
        edit={'bounds':(140,350,1500,500)}
        runner.fill_search_field('雨伞',edit)
        device.return_value.set_text.assert_called_once_with('雨伞',timeout=.25)
        device.input_ascii.assert_not_called()
        device.return_value.set_text.side_effect=TimeoutError('synthetic native input timeout')
        runner.fill_search_field('雨伞',edit)
        device.input_ascii.assert_called_once_with('123')
        self.assertEqual(runner.config['task_search_keywords'],['雨伞'])

    def test_failed_custom_input_cannot_fall_back_into_another_application(self):
        from unittest.mock import MagicMock
        runner=self.runner();runner.origin='com.taobao.taobao'
        runner.config['task_search_keywords']=['雨伞']
        device=MagicMock(device_type='uiautomator2');runner.device=device
        device.return_value.set_text.side_effect=TimeoutError('slow field')
        before=Screen([TextNode('历史搜索',(100,600,400,650)),
                       TextNode('搜索',(1510,350,1760,500))],1800,2880,runner.origin,'ocr')
        runner.read_page=Mock(side_effect=[before,Screen([],1800,2880,'org.example.other','ocr')])
        runner.click=Mock();runner.control.wait=Mock()
        with self.assertRaises(NeedsAttention):
            runner.fill_search_field('雨伞',{'bounds':(140,350,1500,500)})
        device.input_ascii.assert_not_called()


if __name__=='__main__':
    unittest.main()
class LandingCloseTests(unittest.TestCase):
    """落地页自己的 × 不能被当成弹窗关掉。

    拍立淘逛感兴趣的宝贝：点「去完成」打开的是站内「找同款」结果页，右上角
    有一个 ×（关闭该页）。旧逻辑在导航途中把它当浮层点掉，页面退回任务列表，
    任务被判「没有成功离开列表」并计一次失败，两次就锁成「今日失败已达上限」。
    """

    def runner(self, phase):
        runner = make_daily_runner()
        runner._active_task = Task('x', '拍立淘逛感兴趣的宝贝', '', '去完成',
                                   (100, 100, 200, 200), kind='browse')
        runner._phase = phase
        runner.dismiss_popup = Mock(return_value=None)
        return runner

    def test_close_glyph_on_the_landing_page_is_left_alone(self):
        runner = self.runner('opening')
        page = Screen([TextNode('找同款', (100, 300, 400, 360)),
                       TextNode('×', (1040, 180, 1080, 220))],
                      1080, 2400, 'com.taobao.taobao', 'ui')
        with patch.object(Session, 'read', return_value=page):
            runner.read()
        runner.dismiss_popup.assert_not_called()

    def test_popup_is_still_dismissed_outside_task_phases(self):
        runner = self.runner(None)
        page = Screen([TextNode('领券中心', (100, 300, 400, 360))],
                      1080, 2400, 'com.taobao.taobao', 'ui')
        with patch.object(Session, 'read', return_value=page):
            runner.read()
        runner.dismiss_popup.assert_called_once()

    def test_landing_page_is_protected_in_every_phase(self):
        """判据是“还在不在清单上”，不是某一个阶段：回来核验、退货、翻页读数
        都可能停在落地页上，那时同样不能点它顶栏的 ×。"""
        runner = self.runner('verifying')
        runner._task_page = True
        page = Screen([TextNode('找同款', (100, 300, 400, 360)),
                       TextNode('×', (1040, 180, 1080, 220))],
                      1080, 2400, 'com.taobao.taobao', 'ui')
        with patch.object(Session, 'read', return_value=page):
            runner.read()
        runner.dismiss_popup.assert_not_called()

    def test_other_app_pages_are_never_auto_dismissed(self):
        """头条/闲鱼/支付宝里的 × 关的是那篇文章或那件商品，不归我们管。"""
        runner = self.runner(None)
        runner.origin = 'com.taobao.taobao'
        page = Screen([TextNode('推荐', (100, 300, 400, 360)),
                       TextNode('×', (1040, 180, 1080, 220))],
                      1080, 2400, 'com.taobao.idlefish', 'ui')
        with patch.object(Session, 'read', return_value=page):
            runner.read()
        runner.dismiss_popup.assert_not_called()

    def test_back_on_the_task_list_releases_the_protection(self):
        """回到清单以后必须恢复自动关浮层，否则领券浮层会一直挡着任务行。"""
        runner = make_daily_runner()
        runner._task_page = True
        runner.is_task_list = Mock(return_value=True)
        runner.dismiss_popup = Mock(return_value=None)
        page = Screen([TextNode('浏览精选店铺', (100, 300, 400, 360)),
                       TextNode('去完成', (800, 300, 1000, 360))],
                      1080, 2400, 'com.taobao.taobao', 'ui')
        with patch.object(Session, 'read', return_value=page):
            runner.read()
        self.assertFalse(runner._task_page)
        runner.dismiss_popup.assert_called_once()

    def test_guard_still_closes_a_real_overlay_on_a_landing_page(self):
        """救场动作不能一起废掉：挡在页面中段的浮层仍然要关。"""
        runner = make_daily_runner()
        runner._task_page = True
        runner.dismiss_popup = Mock(return_value=None)
        page = Screen([TextNode('恭喜抢到 9.5 折直播折扣券', (100, 900, 900, 960)),
                       TextNode('×', (1000, 1200, 1040, 1240))],
                      1080, 2400, 'com.taobao.taobao', 'ui')
        with patch('backend.core.execution.guard.check_attention',
                   side_effect=NeedsAttention('页面出现支付提示')):
            runner.guard(page)
        runner.dismiss_popup.assert_called_once()

    def test_a_real_overlay_still_closes_while_a_task_page_is_open(self):
        """落在落地页上不等于完全不管：盖在页面中段、写着奖励内容的浮层照旧关。"""
        runner = make_daily_runner()
        runner._task_page = True
        runner.dismiss_popup = Mock(return_value=None)
        page = Screen([TextNode('恭喜获得 50 金币', (100, 900, 900, 960)),
                       TextNode('×', (1000, 1200, 1040, 1240))],
                      1080, 2400, 'com.taobao.taobao', 'ui')
        with patch.object(Session, 'read', return_value=page):
            runner.read()
        runner.dismiss_popup.assert_called_once()

    def test_guard_leaves_the_landing_page_chrome_close_alone(self):
        runner = make_daily_runner()
        runner._task_page = True
        runner.dismiss_popup = Mock(return_value=None)
        page = Screen([TextNode('找同款', (100, 300, 400, 360)),
                       TextNode('×', (1040, 180, 1080, 220))],
                      1080, 2400, 'com.taobao.taobao', 'ui')
        with patch('backend.core.execution.guard.check_attention',
                   side_effect=NeedsAttention('页面出现支付提示')):
            runner.guard(page)
        runner.dismiss_popup.assert_not_called()


class SoftMissTests(unittest.TestCase):
    """页面打开了、停留也够了，只是面板还没刷新：这次不算失败。

    淘金币面板是 WebView，服务端入账后要一两分钟才把新数字刷出来。11-03 的真机
    记录里，判「发现精选好物 2/6 失败」之后 7 分钟，列表上已经是 3/6；搜一搜同理。
    旧逻辑两次这样的误判就写 exhausted，任务整天不再执行。
    """

    def runner(self):
        runner = make_daily_runner()
        runner.config['max_attempts'] = 2
        runner._attempt_browsed = True
        return runner

    def task(self):
        return Task('x', '发现精选好物', '', '去完成', (100, 100, 200, 200), kind='browse')

    def miss(self, runner, task):
        runner._task_budget_counted = False
        return runner.record_task_failure(task)

    def test_first_misses_do_not_consume_the_daily_quota(self):
        runner, task = self.runner(), self.task()
        for _ in range(1):
            self.miss(runner, task)
        self.assertEqual(runner.task_policy.failures(task), 0)
        self.assertEqual(runner.task_policy.reason(task), '')

    def test_a_streak_beyond_the_limit_counts_as_a_real_failure(self):
        runner, task = self.runner(), self.task()
        for _ in range(2):
            self.miss(runner, task)
        self.assertEqual(runner.task_policy.failures(task), 1)

    def test_verified_progress_clears_the_miss_streak(self):
        runner, task = self.runner(), self.task()
        self.miss(runner, task)
        self.miss(runner, task)
        runner.task_policy.progress(task)
        self.assertNotIn('deferred', runner.task_policy.entries.get(task_identity(task), {}))

    def test_progress_after_a_miss_no_longer_counts_towards_the_limit(self):
        runner, task = self.runner(), self.task()
        for _ in range(1):
            self.miss(runner, task)
        runner.task_policy.progress(task)
        for _ in range(1):
            self.miss(runner, task)
        self.assertEqual(runner.task_policy.failures(task), 0)

    def test_a_real_browse_does_not_set_the_once_only_lock(self):
        """软失败之后再套「今日只试一次」，等于换了句话把任务按住。"""
        runner, task = self.runner(), self.task()
        runner.task_policy.mark_browse_once(task)
        self.assertIn('今日已尝试一次', runner.task_policy.reason(task))

    def test_the_once_only_lock_still_applies_when_nothing_was_browsed(self):
        runner = self.runner()
        runner._attempt_browsed = False
        task = self.task()
        runner.task_policy.mark_browse_once(task)
        self.assertIn('今日已尝试一次', runner.task_policy.reason(task))


class NavigationFailureBudgetTests(unittest.TestCase):
    """“点了去完成但没确认离开清单”不该扣当日额度。

    这类失败属于导航层（落地页被关、跳转没确认、加载慢），任务本身并没有做过。
    旧逻辑两次就写 exhausted，任务整天不再执行——拍立淘正是被锁在 2/5。
    """

    def runner(self):
        runner = make_daily_runner()
        runner.config['max_attempts'] = 2
        runner._attempt_nav_failure = True
        return runner

    def task(self):
        return Task('x', '拍立淘逛感兴趣的宝贝', '', '去完成', (100, 100, 200, 200),
                    kind='browse')

    def test_navigation_failure_does_not_consume_the_daily_quota(self):
        runner = self.runner()
        task = self.task()
        runner.record_task_failure(task)
        runner._task_budget_counted = False
        runner.record_task_failure(task)
        self.assertEqual(runner.task_policy.failures(task), 1)
        self.assertEqual(runner.task_policy.reason(task), '')

    def test_the_in_run_budget_still_stops_an_endless_retry(self):
        runner = self.runner()
        task = self.task()
        for _ in range(4):
            runner._task_budget_counted = False
            runner.record_task_failure(task)
        self.assertEqual(runner.task_policy.failures(task), 3)
        self.assertTrue(runner.task_policy.reason(task))

    def test_a_real_progress_failure_still_counts(self):
        runner = self.runner()
        runner._attempt_nav_failure = False
        task = self.task()
        runner.record_task_failure(task)
        self.assertEqual(runner.task_policy.failures(task), 1)



class MaintainerSafetyTests(unittest.TestCase):
    def test_real_security_challenge_requires_manual_handling_without_input(self):
        runner = make_daily_runner()
        page = screen_of('安全验证', '拖动滑块完成验证')
        runner.device.swipe = Mock()
        runner.click = Mock()
        with self.assertRaises(NeedsAttention):
            runner.guard(page)
        runner.device.swipe.assert_not_called()
        runner.click.assert_not_called()

    def test_storefront_poster_code_word_is_not_a_security_challenge(self):
        """搜索页商品主图印着“需要验证码?不上号”，不是安全验证。

        真机 2026-11-05 12:02：「搜一搜你心仪的宝贝」做到 4/6 时整轮停止，
        失败截图是一张商品图，图上就印着这句话（OCR 读到的，控件树里没有）。
        桌面版把 core 打进 exe，词表换不掉，只能靠活动包先把它摘出去。
        """
        old_core = re.compile(r'滑块|拖动.{0,8}验证|安全验证|请完成验证|验证码|人机验证|'
                              r'操作过于频繁|账号存在风险|访问被拒绝|'
                              r'将满足描述的所有图片拖放到指定区域')
        page = screen_of('浏览10秒可领 20金币', '小红书薯币充值', '需要验证码?不上号',
                         '24小时自动秒充秒到！')
        self.assertTrue(old_core.search(page.text))
        runner = make_daily_runner()
        with patch('backend.core.execution.guard._SECURITY', old_core):
            runner.guard(page)

    def test_old_core_still_stops_on_a_real_code_page(self):
        """净化只摘货架文案：带“输入/获取”动作的真验证页必须仍然停。"""
        old_core = re.compile(r'滑块|拖动.{0,8}验证|安全验证|请完成验证|验证码|人机验证|'
                              r'操作过于频繁|账号存在风险|访问被拒绝|'
                              r'将满足描述的所有图片拖放到指定区域')
        runner = make_daily_runner()
        with patch('backend.core.execution.guard._SECURITY', old_core):
            for label in ('请输入验证码', '获取验证码', '验证码错误，请重新输入'):
                with self.assertRaises(NeedsAttention):
                    runner.guard(screen_of(label))

    def test_horizontal_swipe_hint_is_not_a_challenge(self):
        """商品页/内容页的“向右滑动查看”是引导语，不是滑块验证。

        之前词表里只写了“向右滑动”，撞上详情页的引导语就整轮停；真验证页
        一定还带着“验证/拼图”，所以现在要求两者共现。
        """
        runner = make_daily_runner()
        for text in ('向右滑动查看', '向右滑动查看更多商品', '左右滑动切换'):
            runner.guard(screen_of(text, '当前页下单再得 500 金币'))
        with self.assertRaises(NeedsAttention):
            runner.guard(screen_of('向右滑动验证'))
        with self.assertRaises(NeedsAttention):
            runner.guard(screen_of('亲，请拖动下方滑块完成验证', '通过验证以确保正常访问'))


    def test_rescan_propagates_stop_auth_and_identity_errors(self):
        from backend.core.errors import Stopped
        from backend.core.device.instances import InstanceMismatch
        for exc in (Stopped(), NeedsAttention('需要本人验证'), InstanceMismatch('身份不符')):
            runner = make_daily_runner()
            runner.ensure_list = Mock(side_effect=exc)
            with self.assertRaises(type(exc)):
                runner.rescan(restart=False)

    def test_rescan_grants_once_only_one_time_and_keeps_unavailable_tasks(self):
        runner = make_daily_runner()
        task = Task('x', '浏览好物', '', '去完成', (10,10,20,20), kind='browse')
        runner.unavailable_tasks = {'task:missing': '今日已确认不可用'}
        for index in range(2):
            runner.task_policy.mark_browse_once(task)
            runner.reset_list_state()
            self.assertEqual(bool(runner.task_policy.reason(task)), index == 1)
        self.assertIn('task:missing', runner.unavailable_tasks)

    def test_deferral_survives_policy_rebind_without_modifying_methods(self):
        from backend.core.execution.task_policy import TaskPolicy
        runner = make_daily_runner()
        store = Mock()
        store.task_decisions.return_value = {}
        runner.task_policy.bind(store)
        task = Task('x', '浏览好物', '', '去完成', (10,10,20,20), kind='browse')
        self.assertTrue(runner.task_policy.defer(task, 'settlement'))
        saved = store.save_task_decisions.call_args.args[0]
        other = TaskPolicy()
        store.task_decisions.return_value = saved
        other.bind(store)
        self.assertFalse(other.defer(task, 'settlement'))
        self.assertIs(other.progress.__func__, TaskPolicy.progress)
        other.progress(task)
        self.assertTrue(other.defer(task, 'settlement'))


class RescanPersistenceBoundaryTests(unittest.TestCase):
    def test_unknown_scan_cannot_persist_activity_finished(self):
        runner = make_daily_runner()
        runner.stats.attempts = 0
        runner.confirm_scan = Mock(return_value=runner.SCAN_UNKNOWN)
        def one_round(*args):
            runner.stats.attempts += 1
            runner.daily_exhausted = True
            return runner.stats
        with patch.object(SessionLoop, 'run', side_effect=one_round):
            runner.run()
        self.assertFalse(runner.daily_exhausted)

    def test_fresh_rescan_respects_existing_unavailable_reason(self):
        runner = make_daily_runner()
        task = Task('sample','missing app','','去完成',(1,2,3,4),kind='browse')
        runner.tasks = Mock(return_value=[task])
        runner.availability_reason = Mock(return_value='今日已确认不可用')
        self.assertEqual(runner.runnable_rows(screen_of('更多金币等你赚')), [])


class CoinLandingStrayCrossTests(unittest.TestCase):
    """真机 2026-11-05 11:38：淘金币首页上「去赚金币」就在屏幕里，却报「未识别到
    任务页」。现场是定位阶段一分钟里点了 8 次「关闭弹窗：X」——工具栏的音效图标
    和商品标题里的乘号都被读成 X，定位预算全耗在它们身上，入口一次都没点到。"""

    PACKAGE = 'com.taobao.taobao'

    def runner(self):
        return TaobaoDailyTests().runner()

    def landing(self):
        return Screen([
            TextNode('升级赛车拿钱', (213, 894, 867, 1041), 'ocr'),
            TextNode('回日常版', (1017, 453, 1080, 618), 'ocr'),
            TextNode('去赚金币', (0, 894, 216, 1041), 'ocr'),
            TextNode('关闭音效', (870, 138, 954, 222), 'ocr'),
            TextNode('更多', (969, 138, 1053, 222), 'ocr'),
            # 音效图标被 OCR 打成一个 X，右侧紧挨着「更多」
            TextNode('X', (912, 150, 952, 210), 'ocr'),
            # 商品标题「运动户外×抵扣券」中间的乘号，左右都是正文
            TextNode('运动户外', (300, 1700, 520, 1760), 'ocr'),
            TextNode('×', (540, 1700, 580, 1760), 'ocr'),
            TextNode('抵扣券', (600, 1700, 900, 1760), 'ocr'),
        ], 1080, 2400, self.PACKAGE, 'ocr')

    def panel(self):
        return Screen([TextNode('今日速赚210淘金币', (60, 250, 800, 330), 'ocr'),
                       TextNode('更多金币等你赚', (100, 2000, 600, 2060), 'ocr'),
                       TextNode('去完成', (900, 700, 1050, 780), 'ocr')],
                      1080, 2400, self.PACKAGE, 'ocr')

    def test_cross_inside_product_text_and_toolbar_icon_are_not_closers(self):
        runner = self.runner()
        page = self.landing()
        self.assertTrue(page.find('升级赛车拿钱'))
        self.assertIsNone(runner.popup_close_node(page))
        runner.click = Mock()
        self.assertIsNone(runner.dismiss_popup(page))
        self.assertFalse(runner.dismiss_overlay(page))
        runner.click.assert_not_called()

    def test_navigation_walks_past_the_stray_cross_and_opens_the_entry(self):
        runner = self.runner()
        landing, panel = self.landing(), self.panel()
        runner.device.current_app = Mock(return_value={'package': self.PACKAGE})
        runner.read_page = Mock(side_effect=[landing, panel])
        runner.click = Mock()
        runner.control.wait = Mock()
        self.assertIs(runner.navigate(auto_open=True), panel)
        clicked = [call.args[0] for call in runner.click.call_args_list]
        self.assertEqual([n.text for n in clicked], ['去赚金币'])

    def test_close_that_changes_nothing_is_never_clicked_twice(self):
        runner = self.runner()
        close = TextNode('X', (900, 300, 960, 360), 'ocr')
        page = Screen([TextNode('恭喜获得奖励', (200, 900, 800, 980), 'ocr'), close],
                      1080, 2400, self.PACKAGE, 'ocr')
        # 只有金币数字在跳，内容其实一点没变：同一个 X 不能一直点。
        same = Screen([TextNode('恭喜获得奖励', (200, 900, 800, 980), 'ocr'),
                       TextNode('X', (900, 300, 960, 360), 'ocr'),
                       TextNode('22555', (40, 250, 200, 300), 'ocr')],
                      1080, 2400, self.PACKAGE, 'ocr')
        runner.click = Mock()
        runner.control.wait = Mock()
        from unittest.mock import patch as _patch
        with _patch.object(Session, 'read', return_value=same):
            self.assertIsNone(runner.dismiss_popup(page))
            self.assertEqual(runner.click.call_count, 1)
            self.assertIsNone(runner.dismiss_popup(page))
        self.assertEqual(runner.click.call_count, 1)

    def test_dismiss_budget_stops_the_loop_and_leaves_room_for_the_entry(self):
        runner = self.runner()
        page = Screen([TextNode('恭喜获得奖励', (200, 900, 800, 980), 'ocr'),
                       TextNode('X', (900, 300, 960, 360), 'ocr')],
                      1080, 2400, self.PACKAGE, 'ocr')
        runner.click = Mock()
        runner._dismiss_count = runner.DISMISS_LIMIT
        self.assertIsNone(runner.dismiss_popup(page))
        self.assertFalse(runner.dismiss_overlay(page))
        runner.click.assert_not_called()

    def test_entry_label_survives_trailing_ocr_symbols(self):
        runner = self.runner()
        for label in ('去赚金币', '去赚金币|', '去赚金币>', '去赚金币,'):
            with self.subTest(label=label):
                node = TextNode(label, (0, 894, 216, 1041), 'ocr')
                screen = Screen([node], 1080, 2400, self.PACKAGE, 'ocr')
                self.assertIs(runner.entry_node(screen, runner.config['task_entry_labels']), node)

    def test_entry_falls_back_to_native_tree_when_ocr_drops_the_label(self):
        runner = self.runner()
        # 画面上只认出「升级赛车拿钱」，入口文字被 OCR 整块丢掉
        landing = Screen([TextNode('升级赛车拿钱', (213, 894, 867, 1041), 'ocr')],
                         1080, 2400, self.PACKAGE, 'ocr')
        native = Screen([TextNode('去赚金币', (0, 894, 216, 1041), 'ui')],
                        1080, 2400, self.PACKAGE, 'ui')
        panel = self.panel()
        runner.device.current_app = Mock(return_value={'package': self.PACKAGE})
        runner.read_page = Mock(side_effect=[landing, panel])
        runner.click = Mock()
        runner.control.wait = Mock()
        with patch.object(Session, 'read', return_value=native):
            self.assertIs(runner.navigate(auto_open=True), panel)
        self.assertEqual([c.args[0].text for c in runner.click.call_args_list], ['去赚金币'])
