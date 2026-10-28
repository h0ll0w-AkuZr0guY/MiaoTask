"""Daily activities reuse the existing API across phone, tablet and landscape."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from backend.core.config.loader import load_config
from backend.core.device.screen import Screen
from backend.core.models import Task, TextNode
from backend.core.registry import Registry
from backend.core.execution.session import Session
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
        self.assertGreater(x,1800*.55)
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
            self.assertEqual(result[1],'manual')

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
            self.assertEqual(tasks[3].kind,'manual')

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
        self.assertEqual(tasks[0].bounds,(1200,920,1310,990))
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
            self.assertEqual(runner.classify_daily(title,'关注后浏览5秒 +20',[title,'关注后浏览5秒 +20'],'去完成',0,1)[1],'manual')
        self.assertEqual(runner.classify_daily('关注店铺','浏览5秒',['关注店铺','浏览5秒'],'去完成',0,1)[1],'manual')

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
        self.assertEqual([t.kind for t in tasks],['browse','manual'])

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
        device=MagicMock(device_type='uiautomator2');runner.device=device
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

    def test_expand_is_scoped_to_coin_task_panel_and_only_clicked_once(self):
        runner=self.runner()
        header=TextNode('今日速赚510淘金币',(460,200,950,270),'ocr')
        button=TextNode('展开',(1190,2540,1300,2590),'ocr')
        context=TextNode('更多金币等你赚',(490,2540,870,2590),'ocr')
        page=Screen([header,context,button],1800,2880,'com.taobao.taobao','ocr')
        runner.read_page=Mock(return_value=page)
        runner.control=SimpleNamespace(now=lambda:0,check=Mock(),wait=Mock())
        runner.click=Mock()
        self.assertIs(runner.ensure_list(),page)
        runner.click.assert_called_once_with(button)
        runner.click.reset_mock()
        runner.read_page=Mock(return_value=Screen([header,button],1800,2880,page.package,'ocr'))
        runner.ensure_list()
        runner.click.assert_not_called()

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
        self.assertEqual([t.kind for t in tasks],['browse','manual'])
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
        self.assertEqual(runner.classify_daily(title,required,[title,required],'去完成',0,1)[1],'manual')
        self.assertEqual(runner.classify_daily('下单一次',detail,['下单一次',detail],'去完成',0,1)[1],'manual')


if __name__=='__main__':
    unittest.main()
