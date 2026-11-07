"""Offline activity contracts; no phone or account is controlled."""
import unittest
from unittest.mock import Mock
from backend.app.idlefish.activities.coins.activity import IdleFishCoinsActivity
from backend.core.device.screen import Screen
from backend.core.models import TextNode, Task
from backend.core.errors import TaskUnavailable

class IdlefishContracts(unittest.TestCase):
    def runner(self):
        r = IdleFishCoinsActivity.__new__(IdleFishCoinsActivity)
        r.config = IdleFishCoinsActivity.default_config()
        r.origin = 'com.taobao.idlefish'
        r.control = Mock()
        r.click = Mock()
        r.read = Mock()
        r.device = Mock()
        r.update = Mock()
        r._swipe_once = Mock()
        return r

    def panel(self, progress='0/3', action='去完成'):
        return Screen([TextNode('浏览好物', (100,190,250,210)),
            TextNode(progress, (280,190,330,210)), TextNode(action, (450,190,550,210)),
            TextNode('+100完成3次',(100,80,300,100))],600,1000,'com.taobao.idlefish')

    def search_page(self):
        return Screen([TextNode('搜索',(400,80,500,120)),TextNode('搜索发现',(0,200,100,240)),
                       TextNode('手机壳',(100,400,200,440))],600,1000,'com.taobao.idlefish')

    def test_identity_stays_stable_when_progress_changes(self):
        r=self.runner(); a=self.panel(); b=self.panel('1/3')
        x=r.parse_tasks(a.nodes,a.width,a.height)[0]; y=r.parse_tasks(b.nodes,b.width,b.height)[0]
        self.assertEqual(x.key,y.key); self.assertEqual((x.current,y.current),(0,1))
        self.assertTrue(r.verify(x,a,b))
        self.assertFalse(r.verify(x,a,a))
        r.config['verify_enabled']=False
        self.assertFalse(r.verify(x,a,a))
        self.assertFalse(r.verify(x,a,Screen([],600,1000,r.origin)))

    def test_no_navigation_for_trade_task(self):
        r=self.runner(); task=Task('t','购买商品','','去完成',(0,0,1,1),kind='trade')
        with self.assertRaises(TaskUnavailable):r.execute(task,self.panel())
        r.click.assert_not_called()

    def test_search_returns_contract_tuple(self):
        r=self.runner(); r.config['search_browse_swipes']=1
        s=Screen([TextNode('搜索',(0,0,100,40)),TextNode('搜索发现',(0,200,100,240)),
                  TextNode('手机壳',(100,400,200,440))],600,1000,r.origin)
        r.read.return_value=s
        result=r._search_task(s)
        self.assertIs(result[0],s); self.assertIsInstance(result[1],bool)
        r._swipe_once.assert_called_once()

    def test_search_without_submission_does_not_swipe(self):
        r=self.runner(); s=Screen([],600,1000,r.origin);r.read.return_value=s
        with self.assertRaises(TaskUnavailable):r._search_task(s)
        r._swipe_once.assert_not_called()

    def test_search_inputs_keyword_before_swiping(self):
        r=self.runner(); r.config['search_browse_swipes']=1
        s=self.search_page()
        r.read.return_value=s
        r._search_task(s)
        r.device.input_text.assert_called_once_with(r.config['task_search_keyword'])
        r._swipe_once.assert_called_once()

    def test_search_falls_back_to_suggestion_when_input_unavailable(self):
        r=self.runner(); r.config['search_browse_swipes']=0
        r.device.input_text.side_effect=TimeoutError('synthetic ime failure')
        s=self.search_page()
        r.read.return_value=s
        r._search_task(s)
        # 搜索框一次 + 推荐词一次；输入不可用时不能直接放弃整个任务
        self.assertEqual(r.click.call_count,2)

    def test_browse_keeps_result_when_final_read_overflows(self):
        r=self.runner(); r.control.now.side_effect=[0,0,99999]
        first=self.panel()
        r.read.side_effect=[first,RuntimeError('synthetic StackOverflowError')]
        screen,explicit=r._browse(1)
        self.assertIs(screen,first); self.assertFalse(explicit)

    def test_video_task_waits_for_page_before_finishing(self):
        r=self.runner(); r.config['browse']['video_seconds']=1
        r.config['browse_extra_seconds']=1
        panel=self.panel()
        video=Screen([TextNode('广告',(100,300,200,320))],600,1000,r.origin)
        # 第一屏仍是任务列表（H5 还没跳转），第二屏才是视频页
        r.control.now.side_effect=[0,0,5]
        r.read.side_effect=[panel,video]
        screen,explicit=r._video_browse(1)
        self.assertIs(screen,video); self.assertFalse(explicit)

    def test_restart_client_runs_once_per_round(self):
        r=self.runner(); r._client_restarted=False
        self.assertTrue(r._restart_client())
        self.assertFalse(r._restart_client())
        r.device.go_home.assert_called_once()
        r.device.stop_app.assert_called_once_with(r.origin)
        r.device.start_app.assert_called_once_with(r.origin)

    def test_setup_resets_restart_budget(self):
        r=self.runner(); r._client_restarted=True
        r.setup()
        self.assertFalse(r._client_restarted)

    def test_task_panel_is_not_treated_as_landing(self):
        r=self.runner()
        landing=Screen([TextNode('再走4步完成1圈',(100,800,500,840))],600,1000,r.origin)
        self.assertTrue(r.activity_landing(landing))
        self.assertFalse(r.activity_landing(self.panel()))

    def test_overlay_without_close_text_is_tapped_by_title(self):
        r=self.runner()
        s=Screen([TextNode('前进获得闲鱼币',(100,150,700,200))],600,1000,r.origin)
        self.assertTrue(r._close_overlay(s))
        r.click.assert_called_once()

    def test_unknown_teardown_does_not_claim_or_walk(self):
        r=self.runner();r.read.side_effect=RuntimeError('synthetic disconnected')
        r._claim_top_rewards=Mock();r._walk_steps=Mock()
        r.teardown();r._claim_top_rewards.assert_not_called();r._walk_steps.assert_not_called()

    def test_claim_row_removed_is_verified(self):
        r = self.runner()
        claim = Task('c', '每日签到', '', '领取奖励', (450, 190, 550, 210), kind='claim')
        browse = Task('b', '浏览好物', '', '去完成', (450, 190, 550, 210), kind='browse')
        after = Screen([TextNode('+100完成3次', (100, 80, 300, 100))], 600, 1000, r.origin)
        self.assertTrue(r.verify(claim, self.panel(), after))
        self.assertFalse(r.verify(browse, self.panel(), after))

    def test_claim_ready_does_not_wait_when_not_loading(self):
        r = self.runner()
        task = r.parse_tasks(self.panel().nodes, 600, 1000)[0]
        r._claim_ready(task, self.panel())
        r.read.assert_not_called()
        r.control.wait.assert_not_called()

    def test_claim_ready_allows_one_extra_beat_while_loading(self):
        r = self.runner()
        task = r.parse_tasks(self.panel().nodes, 600, 1000)[0]
        loading = Screen([TextNode('浏览好物', (100, 190, 250, 210)),
                          TextNode('0/3', (280, 190, 330, 210)),
                          TextNode('去完成', (450, 190, 550, 210)),
                          TextNode('加载中', (100, 400, 300, 420)),
                          TextNode('+100完成3次', (100, 80, 300, 100))], 600, 1000, r.origin)
        r.read.return_value = self.panel()
        r._claim_ready(task, loading)
        self.assertEqual(r.read.call_count, 1)
        self.assertEqual(r.control.wait.call_count, 1)

    def test_claim_ready_clicks_reward_immediately(self):
        r = self.runner()
        task = r.parse_tasks(self.panel().nodes, 600, 1000)[0]
        ready = self.panel(action='领取奖励')
        r.read.return_value = ready
        r._claim_ready(task, ready)
        r.click.assert_called_once()

    def test_claim_ready_row_removed_without_receipt_is_not_success(self):
        r = self.runner()
        task = r.parse_tasks(self.panel().nodes, 600, 1000)[0]
        screen = Screen([TextNode('+100完成3次', (100, 80, 300, 100))], 600, 1000, r.origin)
        r._claim_ready(task, screen)
        self.assertNotIn(task.key, getattr(r, '_completed_evidence', set()))
        r.read.assert_not_called()

    def test_exit_dialog_second_confirmation_is_clicked(self):
        r = self.runner()
        first = Screen([TextNode('跳过', (100, 190, 250, 210))], 600, 1000, r.origin)
        second = Screen([TextNode('坚持退出', (100, 190, 250, 210))], 600, 1000, r.origin)
        done = Screen([TextNode('去完成', (450, 190, 550, 210))], 600, 1000, r.origin)
        r.read.side_effect = [second, done]
        r._dismiss_exit_dialogs(first)
        self.assertEqual(r.click.call_count, 2)

    def test_return_to_list_uses_page_back_button_once(self):
        r = self.runner()
        r.device = Mock()
        r.recover = Mock()
        page = Screen([TextNode('返回', (450, 190, 550, 210))], 600, 1000, r.origin)
        r.read.side_effect = [self.panel()]
        r._return_to_list(page)
        r.click.assert_called_once()
        r.device.go_back.assert_not_called()
        r.recover.assert_not_called()
        r.control.wait.assert_called_once_with(1.5)


class ClaimReceiptTests(unittest.TestCase):
    runner = IdlefishContracts.runner
    panel = IdlefishContracts.panel
    def test_click_with_unchanged_claim_button_is_not_success(self):
        r = self.runner(); screen = self.panel(action='领取奖励')
        task = r.parse_tasks(screen.nodes, screen.width, screen.height)[0]
        r._stable_target = Mock(return_value=(screen, task)); r.update = Mock(); r.guard = Mock()
        r._dismiss_popups = Mock(return_value=False); r.read.return_value = screen
        after, explicit, _ = r.execute(task, screen)
        self.assertFalse(explicit)
        self.assertFalse(r.verify(task, screen, after, explicit=explicit))
