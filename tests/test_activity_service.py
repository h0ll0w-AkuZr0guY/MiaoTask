"""Loopback installation lifecycle, authentication and all-device update gate."""
import hashlib
import importlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from backend.core.plugins.manager import PluginManager
from backend.core.registry import Registry, default_registry
from backend.core.config.loader import load_config
from backend.core.device.screen import Screen
from backend.core.models import TextNode
from backend.core.service.devices import DeviceHub
from backend.core.service.panel import make_handler
from test_activity_distribution import package_fixture


class ActivityServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.source = self.folder / 'source'
        self.source.mkdir()
        archive, digest = package_fixture(self.source)
        self.digest = digest
        catalog = dict(schema=1, framework_api=1, repository='h0ll0w-AkuZr0guY/MiaoTask', packages=[dict(
            app='sample', activity='daily', app_module='sample', activity_module='daily',
            app_label='Sample', label='Daily', version='0.1.1', framework_api=1,
            python=f'{sys.version_info.major}.{sys.version_info.minor}',
            source_commit='a'*40, url='https://example.invalid/daily.zip', sha256=digest,
            size=archive.stat().st_size, device_types=['uiautomator2'])])
        (self.source / 'activity-catalog.json').write_text(json.dumps(catalog), encoding='utf-8')
        self.app_package = importlib.import_module('backend.app')
        self.old_path = list(self.app_package.__path__)
        self.old_marker = getattr(self.app_package, '_miaotask_plugin_paths', ())
        self.asset_patch = patch('backend.core.registry.assets_dir', return_value=self.folder / 'empty')
        self.asset_patch.start()
        registry = Registry(plugin_dir=self.folder / 'plugins/apps').discover()
        self.hub = DeviceHub(registry=registry)
        base_handler = make_handler(self.hub, 'test-token')
        fixture = self
        class FixtureHandler(base_handler):
            def do_GET(self):
                if self.path == '/fixture/catalog':
                    self.reply(200, fixture.catalog)
                elif self.path == '/fixture/daily.zip':
                    self.reply(200, (fixture.source / 'daily.zip').read_bytes(), 'application/zip')
                else:
                    super().do_GET()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.catalog = catalog
        self.catalog['packages'][0]['url'] = self.base + '/fixture/daily.zip'
        self.hub.plugin_manager = PluginManager(self.folder / 'plugins',
            catalog_url=self.base+'/fixture/catalog', allow_local=True)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def tearDown(self):
        if self.hub.plugin_thread:
            self.hub.plugin_thread.join(5)
        self.server.shutdown()
        self.server.server_close()
        self.asset_patch.stop()
        default_registry.cache_clear()
        for name in list(sys.modules):
            if name == 'backend.app.sample' or name.startswith('backend.app.sample.'):
                sys.modules.pop(name, None)
        self.app_package.__path__ = self.old_path
        self.app_package._miaotask_plugin_paths = self.old_marker
        importlib.invalidate_caches()
        self.temp.cleanup()

    def get(self, path):
        with urlopen(self.base+path, timeout=4) as response:
            return json.load(response)

    def post(self, path, payload, token='test-token'):
        request = Request(self.base+path, data=json.dumps(payload).encode(),
                          headers={'Content-Type':'application/json', 'X-Miao-Token':token})
        with urlopen(request, timeout=4) as response:
            return response.status, json.load(response)

    def test_empty_framework_installs_and_uninstalls_through_authenticated_http(self):
        self.assertEqual(self.get('/api/apps')['apps'], [])
        snapshot = self.get('/api/plugins?refresh=1')
        self.assertTrue(snapshot['test_source'])
        entry = self.get('/fixture/catalog')['packages'][0]
        self.assertTrue(entry['url'].startswith(self.base+'/fixture/'))
        with urlopen(entry['url']) as response:
            self.assertEqual(hashlib.sha256(response.read()).hexdigest(), self.digest)
        for action in ('install', 'uninstall'):
            self.assertEqual(self.post('/api/plugins/'+action, dict(app='sample', activity='daily'))[0], 202)
            self.hub.plugin_thread.join(5)
            self.assertFalse(self.hub.plugin_busy)
            self.assertEqual(self.hub.plugin_operation['status'], 'finished')
            state = self.get('/api/state')
            self.assertEqual(bool(state['catalog']), action == 'install')
            self.assertEqual(state['activity_plan'], [dict(app='sample', activity='daily')] if action == 'install' else [])
        self.assertFalse(self.hub.resolve().registry.has_activities)

    def test_update_requires_token_and_every_device_idle(self):
        with self.assertRaises(HTTPError) as denied:
            self.post('/api/plugins/install', dict(app='sample', activity='daily'), token='bad')
        self.assertEqual(denied.exception.code, 403)
        second = self.hub.add('Second')
        self.hub.resolve(second).thread = Mock(is_alive=lambda: True)
        with self.assertRaises(HTTPError) as busy:
            self.post('/api/plugins/install', dict(app='sample', activity='daily'))
        self.assertEqual(busy.exception.code, 400)
        self.assertFalse(self.hub.plugin_busy)
        self.hub.resolve(second).thread = None
        self.hub.plugin_busy = True
        with self.assertRaisesRegex(ValueError, '正在更新'):
            self.hub.resolve().dispatch('configure', {})
        with self.assertRaisesRegex(ValueError, '正在更新'):
            self.hub.add('Blocked')
        self.hub.plugin_busy = False

    def test_activity_folder_is_visible_and_opening_requires_token_and_ignores_client_path(self):
        snapshot = self.get('/api/plugins')
        self.assertEqual(Path(snapshot['activities_dir']), self.hub.plugin_manager.root)
        with patch('os.startfile', create=True) as opener:
            with self.assertRaises(HTTPError) as denied:
                self.post('/api/plugins/open-folder', {}, token='bad')
            self.assertEqual(denied.exception.code, 403)
            opener.assert_not_called()
            status, result = self.post('/api/plugins/open-folder', {'path': 'C:/unrelated'})
            self.assertEqual(status, 200)
            opener.assert_called_once_with(str(self.hub.plugin_manager.root))
            self.assertEqual(result['path'], str(self.hub.plugin_manager.root))

    def test_normal_polling_never_requests_catalog_and_test_server_cannot_read_other_files(self):
        with patch.object(self.hub.plugin_manager, 'snapshot', side_effect=AssertionError('unexpected network')):
            self.get('/api/state')
            self.get('/api/apps')
            self.get('/api/workspaces')
            self.post('/api/configure', dict(config={'max_tasks': 3}))
        for path in ('/api/plugins/test/files/activity-catalog.json', '/api/plugins/test/files/..%2Fsecret.zip'):
            with self.assertRaises(HTTPError) as missing:
                self.get(path)
            self.assertEqual(missing.exception.code, 404)

# activity_description
"""Optional informational text never changes activity execution or registration."""
import json
from pathlib import Path
import tempfile
import unittest

from backend.core.registry import Registry
from test_activity_pipeline import write_registry


class ActivityDescriptionTests(unittest.TestCase):
    def test_missing_blank_and_non_text_descriptions_are_omitted(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);registry=write_registry(root)
            path=root/'alpha/activities/first/config.json'
            for value in (None,'', '  ', {'html':'<b>unsupported</b>'}):
                config=json.loads(path.read_text(encoding='utf-8'))
                config['description']=value
                path.write_text(json.dumps(config),encoding='utf-8')
                row=registry.catalog()[0]['activities'][0]
                self.assertNotIn('description',row)

    def test_multiline_description_is_plain_text(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);registry=write_registry(root)
            path=root/'alpha/activities/first/config.json'
            config=json.loads(path.read_text(encoding='utf-8'))
            description='🎁 活动介绍\n\n使用方法：\n<script>plain text</script>'
            config['description']=description
            path.write_text(json.dumps(config),encoding='utf-8')
            row=registry.catalog()[0]['activities'][0]
            self.assertEqual(row['description'],description)

    def test_only_requested_activity_has_provided_description(self):
        registry=Registry(Path(__file__).resolve().parents[1]/'backend/app').discover()
        described=[(app['id'],activity['id'],activity['description']) for app in registry.catalog()
                   for activity in app['activities'] if activity.get('description')]
        self.assertEqual([(a,b) for a,b,_ in described],
                         [('jd','dongdong_farm'),('jd','plant_bean'),('taobao','double11'),
                          ('zfb','ant_forest'),('zfb','baba_farm'),('zfb','gold_beans'),('zfb','yueyuezhuan')])
        ended=next(text for a,b,text in described if (a,b)==('taobao','double11'))
        self.assertIn('已结束',ended)
        self.assertIn('淘金币日常',ended)
        transfer=next(text for a,b,text in described if (a,b)==('zfb','yueyuezhuan'))
        self.assertIn('这是一款转账红包（非支付减免红包）',transfer)
        self.assertIn('积少成多',transfer)
        farm=next(text for a,b,text in described if (a,b)==('jd','dongdong_farm'))
        self.assertIn('东东农场',farm)


class CatalogOpeningLatencyTests(unittest.TestCase):
    def test_slow_remote_refresh_never_blocks_cached_opening_or_duplicates_worker(self):
        from backend.core.service.activity_hub import ActivityCatalogHub
        from backend.core.plugins.manager import PluginManager
        from backend.core.plugins.repository import GitHubSourceProvider
        from test_activity_repository import SourceFixture
        import threading
        import tempfile
        entered, release = threading.Event(), threading.Event()
        with tempfile.TemporaryDirectory() as folder:
            fixture = SourceFixture(Path(folder))
            provider = GitHubSourceProvider(fixture.fetch)
            manager = PluginManager(Path(folder) / 'installed', source_provider=provider)
            value, _ = manager.catalog()
            def slow_catalog():
                entered.set()
                release.wait(3)
                return value
            provider.catalog = Mock(side_effect=slow_catalog)
            hub = ActivityCatalogHub()
            hub._init_activity_catalog()
            hub.plugin_manager = manager
            try:
                first = hub.plugins_snapshot('', refresh=True)
                self.assertTrue(first['catalog'])
                self.assertTrue(entered.wait(1))
                replies = []
                reader = threading.Thread(target=lambda: replies.append(hub.plugins_snapshot('', refresh=True)))
                reader.start(); reader.join(1)
                self.assertFalse(reader.is_alive(), 'cached display waited for remote refresh')
                self.assertTrue(replies[0]['catalog'])
                self.assertTrue(replies[0]['refreshing'])
                self.assertEqual(provider.catalog.call_count, 1)
            finally:
                release.set()
                hub.catalog_refresh_thread.join(3)


class AlipayTaskPanelTests(unittest.TestCase):
    package = 'com.eg.android.AlipayGphone'

    def runner(self, activity):
        from types import SimpleNamespace
        registry = Registry().discover()
        config = load_config(app='zfb', activity=activity, registry=registry)
        return registry.instantiate(SimpleNamespace(device_type='uiautomator2', selected_instance=None),
                                    config, app='zfb', activity=activity)

    def test_gold_beans_split_progress_requires_an_increase(self):
        runner = self.runner('gold_beans')

        def panel(progress):
            return Screen([
                TextNode('逛精选页面得大额金豆', (220, 980, 650, 1040)),
                TextNode(progress, (655, 980, 750, 1040)),
                TextNode('浏览15秒即得', (220, 1050, 470, 1100)),
                TextNode('去完成', (805, 995, 1015, 1080)),
            ], 1080, 2400, self.package)

        before = panel('0/10')
        task, = runner.tasks(before)
        self.assertEqual((task.title, task.kind, task.seconds, task.total),
                         ('逛精选页面得大额金豆', 'browse', 15, 10))
        self.assertFalse(runner.verify(task, before, panel('0/10')))
        self.assertTrue(runner.verify(task, before, panel('1/10')))
        outside = panel('1/10')
        outside.package = 'com.example.foreign'
        self.assertFalse(runner.verify(task, before, outside))

    def test_farm_combined_row_separates_timed_browse_from_game(self):
        runner = self.runner('baba_farm')
        nodes = [
            TextNode('做任务集肥料', (50, 500, 1000, 580)),
            TextNode('看精选商品得肥料当前进度0，总进度3 每浏览15s最高得2000肥',
                     (60, 800, 800, 930)),
            TextNode('去完成', (800, 830, 1010, 910)),
            TextNode('镇邪人招募1次镇邪人当前进度0，总进度1 可得1000肥',
                     (60, 1010, 800, 1140)),
            TextNode('去完成', (800, 1040, 1010, 1120)),
        ]
        tasks = runner.tasks(Screen(nodes, 1080, 2400, self.package))
        self.assertEqual([(t.kind, t.total, t.seconds) for t in tasks],
                         [('browse', 3, 15), ('manual', 1, 0)])

    def test_farm_promotion_uses_overlay_close_and_leaves_app_close(self):
        runner = self.runner('baba_farm')
        runner.origin = self.package
        app_close = TextNode('X', (970, 140, 1020, 185))
        promo_close = TextNode('X', (950, 530, 1000, 590))
        scene = Screen([app_close, promo_close,
                        TextNode('来玩农场抽抽乐', (60, 1320, 550, 1380))],
                       1080, 2400, self.package, source='ocr')
        runner._read = Mock(return_value=scene)
        runner.click = Mock()
        self.assertTrue(runner._close_landing_overlay())
        runner.click.assert_called_once_with(promo_close)
        runner.click.reset_mock()
        scene.nodes.pop()
        self.assertFalse(runner._close_landing_overlay())
        runner.click.assert_not_called()

    def test_forest_claim_requires_same_row_done(self):
        runner = self.runner('ant_forest')

        def panel(action):
            return Screen([
                TextNode('森林任务', (60, 600, 420, 650)),
                TextNode('每日奖励', (120, 968, 550, 1000)),
                TextNode(action, (810, 970, 1010, 1040)),
            ], 1080, 2400, self.package)

        before = panel('立即领取')
        task, = runner.tasks(before)
        self.assertEqual(task.kind, 'claim')
        self.assertTrue(runner.verify(task, before, panel('已领取')))
        self.assertFalse(runner.verify(task, before, panel('立即领取')))

    def test_forest_scrolled_rewards_panel_keeps_its_identity(self):
        runner = self.runner('ant_forest')
        runner.origin = self.package
        native = Screen([
            TextNode('蚂蚁森林', (100, 132, 290, 201)),
            TextNode('关闭奖励弹窗', (950, 300, 1030, 380)),
            TextNode('逛一逛神奇海洋', (247, 1140, 726, 1210)),
            TextNode('去看看', (760, 1143, 1003, 1211)),
        ], 1080, 2400, self.package)
        runner._read = Mock(side_effect=AssertionError('面板已打开，不应再次读屏或导航'))
        runner.click = Mock()
        self.assertIs(runner._locate(native, auto_open=True), native)
        runner.click.assert_not_called()
        task, = runner.tasks(native)
        self.assertEqual(task.title, '逛一逛神奇海洋')
        self.assertEqual((task.kind, task.seconds, task.browse_once), ('browse', 0, True))

        painted = [TextNode('蚂蚁森林', (100, 132, 290, 201), 'ocr'),
                   TextNode('奖励', (490, 330, 584, 370), 'ocr'),
                   TextNode('今日继续赚能量 获胜得奖励', (260, 458, 820, 510), 'ocr')]
        self.assertTrue(runner.is_task_list(painted))
        self.assertFalse(runner.is_task_list(painted[:2]))
        self.assertFalse(runner.is_task_list(native.nodes[1:]))

    def test_untimed_forest_browse_waits_15_seconds_and_remains_controllable(self):
        runner = self.runner('ant_forest')
        runner.origin = self.package
        before = Screen([
            TextNode('蚂蚁森林', (100, 132, 290, 201)),
            TextNode('关闭奖励弹窗', (950, 300, 1030, 380)),
            TextNode('逛一逛神奇海洋', (247, 1140, 726, 1210)),
            TextNode('去看看', (760, 1143, 1003, 1211)),
        ], 1080, 2400, self.package)
        landing = Screen([TextNode(text, (100, 100+i*100, 500, 160+i*100))
                          for i, text in enumerate(('神奇海洋', '海洋首页', '海洋图鉴', '今日动态'))],
                         1080, 2400, self.package)
        task, = runner.tasks(before)
        runner.remember([task])
        self.assertTrue(runner.records[task.key]['enabled'])
        runner._read = Mock(side_effect=[before, landing, landing])
        runner.control.wait = Mock()
        runner.click = Mock()
        runner._return_to_panel = Mock(return_value=before)
        with patch.object(runner, 'verify', return_value=True):
            runner.execute(task, before)
        runner.control.wait.assert_any_call(15)
        runner.click.assert_called_once()
        runner.task_policy.mark_browse_once(task)
        runner.remember([task])
        self.assertFalse(runner.records[task.key]['enabled'])
        self.assertTrue(runner.records[task.key]['controllable'])
        runner.task_policy.choose(runner.records[task.key]['task_id'], False)
        runner.remember([task])
        self.assertTrue(runner.records[task.key]['enabled'])

    def test_untimed_browse_fallback_keeps_protected_actions_out(self):
        for activity in ('gold_beans', 'baba_farm', 'ant_forest'):
            with self.subTest(activity=activity):
                runner = self.runner(activity)
                result = runner._classify('逛精选页面', '完成可得奖励', [], '去完成', 0, 1)
                self.assertEqual(result[:2], (0, 'browse'))
                for title in ('玩一玩疯狂水世界', '搜“意外险”选百万保障', '邀请好友', '开启巡护玩法消息提醒'):
                    result = runner._classify(title, '浏览15秒', [], '去看看', 0, 1)
                    self.assertEqual(result[1], 'manual')

    def test_browse_fallback_declines_external_launch_confirmation(self):
        from backend.core.errors import TaskUnavailable
        runner = self.runner('ant_forest')
        runner.origin = self.package
        runner.click = Mock()
        decline = TextNode('拒绝', (100, 1500, 320, 1560))
        popup = Screen([
            TextNode('支付宝 想要打开 闲鱼，是否允许？', (110, 1200, 980, 1280)),
            TextNode('本次允许', (400, 1500, 620, 1560)),
            decline,
        ], 1080, 2400, 'com.example.system')
        with self.assertRaisesRegex(TaskUnavailable, '外部应用跳转'):
            runner._check_browse_destination(popup)
        runner.click.assert_called_once_with(decline)

    def test_forest_checkin_and_member_visit_use_distinct_rows(self):
        runner = self.runner('ant_forest')

        def panel(sign):
            return Screen([
                TextNode('关闭奖励弹窗', (950, 300, 1030, 380)),
                TextNode('我的活力值', (65, 432, 340, 480)),
                TextNode('今天', (105, 890, 170, 945)),
                TextNode('第二天', (240, 890, 320, 945)),
                TextNode(sign, (834, 845, 1012, 920)),
                TextNode('逛支付宝会员中心', (247, 1398, 725, 1476)),
                TextNode('逛一逛', (761, 1400, 1004, 1475)),
            ], 1080, 2400, self.package)

        before = panel('领取')
        signup, visit = runner.tasks(before)
        self.assertEqual((signup.key, signup.kind), ('daily-sign', 'claim'))
        self.assertEqual((visit.title, visit.kind, visit.browse_once), ('逛支付宝会员中心', 'browse', True))
        self.assertTrue(runner.verify(signup, before, panel('已领取')))

    def test_forest_claim_replacement_requires_neighbor_rows(self):
        runner = self.runner('ant_forest')

        def panel(rows):
            nodes = [TextNode('我的活力值', (65, 432, 340, 480)),
                     TextNode('关闭奖励弹窗', (950, 300, 1030, 380))]
            for index, (title, action) in enumerate(rows):
                y = 840 + 200 * index
                nodes.extend([TextNode(title, (245, y, 720, y + 72)),
                              TextNode(action, (760, y + 3, 1000, y + 76))])
            return Screen(nodes, 1080, 2400, self.package)

        head = ('去神奇鱼塘得能量', '去看看')
        tail = ('去淘宝签到领红包', '逛一逛')
        target = ('逛支付宝会员中心', '立即领取')
        before = panel([head, target, tail])
        task = runner.tasks(before)[1]
        self.assertEqual(task.kind, 'claim')
        self.assertTrue(runner.verify(task, before, panel([head, ('去庄园喂小鸡', '去看看'), tail])))
        self.assertFalse(runner.verify(task, before, panel([tail])))
        self.assertFalse(runner.verify(task, before, panel([head, target, tail])))


class IdlePreviewBudgetTests(unittest.TestCase):
    def test_attention_view_does_not_recapture_twice_per_second(self):
        from backend.core.service.preview import PreviewWorker
        worker = PreviewWorker()
        worker._preview_stop = Mock(); worker._preview_stop.wait.side_effect = [False, True]
        worker._last_client_seen = 100; worker._preview_last = 98
        worker.state = {'status': 'needs_attention'}
        worker.device = Mock(); worker.lock = threading.RLock()
        with patch('backend.core.service.preview.time.monotonic', return_value=100):
            worker._preview_loop()
        worker.device.screenshot.assert_not_called()
