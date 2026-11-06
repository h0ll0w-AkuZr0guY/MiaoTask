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
        r._swipe_once = Mock()
        return r

    def panel(self, progress='0/3', action='去完成'):
        return Screen([TextNode('浏览好物', (100,190,250,210)),
            TextNode(progress, (280,190,330,210)), TextNode(action, (450,190,550,210)),
            TextNode('+100完成3次',(100,80,300,100))],600,1000,'com.taobao.idlefish')

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

    def test_claim_ready_row_removed_returns_with_evidence(self):
        r = self.runner()
        task = r.parse_tasks(self.panel().nodes, 600, 1000)[0]
        screen = Screen([TextNode('+100完成3次', (100, 80, 300, 100))], 600, 1000, r.origin)
        r._claim_ready(task, screen)
        self.assertIn(task.key, r._completed_evidence)
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
