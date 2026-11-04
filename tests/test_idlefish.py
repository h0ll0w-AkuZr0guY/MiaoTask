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
