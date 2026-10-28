"""Real worker concurrency with synthetic devices; no external connections."""
from contextlib import ExitStack
import json
import logging
import threading
import unittest
from unittest.mock import Mock, patch
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from PIL import Image

from backend.core.context import workspace_scope
from backend.core.service.devices import DeviceHub
from backend.core.service.panel import PanelLogHandler, make_handler
from backend.core.device.displays import capture_display, focused_display, focused_window_dump
from backend.core.device.geometry import geometry_from_window, ocr_tiles
from backend.core.device.screen import ScreenReader, Screen
from backend.core.models import TextNode
from backend.core.errors import NeedsAttention
from backend.core.execution.guard import check_attention
from backend.core.device.adapter import DeviceAdapter
from backend.app.taobao.activities.double11.entry import EntryPolicy
from backend.app.taobao.utils.task_rules import classify


class HubTests(unittest.TestCase):
    def test_automatic_instances_uses_current_list_and_off_clears_remembered_queue(self):
        hub=DeviceHub();c=hub.resolve();main={'id':'main','user_id':0};clone={'id':'clone','user_id':999}
        c.update(app_instances=[main,clone],selected_instance='main')
        plan=[{'app':c.state['app'],'activity':c.state['activity']}]
        with patch.object(c,'_execute_current_activity') as execute:
            c._run_activity_pipeline('run',{'activities':plan,'all_instances':True},c.state['config'])
            self.assertEqual(execute.call_args.args[1]['instance_ids'],['main','clone'])
            c._run_activity_pipeline('run',{'activities':plan,'all_instances':False},c.state['config'])
            self.assertEqual(execute.call_args.args[1]['instance_ids'],['main'])
        c.dispatch('configure',{'all_instances':True})
        self.assertTrue(c.snapshot()['all_instances'])
        with self.assertRaises(ValueError):c.dispatch('configure',{'all_instances':'true'})

    def test_rename_changes_only_workspace_label_and_accepts_original_device(self):
        hub=DeviceHub();second=hub.add('Other');controller=hub.resolve('default')
        controller.thread=Mock(is_alive=lambda:True)
        before=controller.snapshot()
        hub.rename('default','  My tablet  ')
        self.assertEqual(controller.workspace_label,'My tablet')
        self.assertEqual(controller.snapshot(),before)
        self.assertEqual(hub.resolve(second).workspace_label,'Other')
        for key,label in [('missing','New'),(None,'New'),('default',''),('default',' '*4),('default','a'*61),('default',3)]:
            with self.assertRaises(ValueError):hub.rename(key,label)
        hub.closing=True
        with self.assertRaises(ValueError):hub.rename('default','New')

    def test_preview_failure_status_is_observational_and_recovers_on_image(self):
        hub=DeviceHub();c=hub.resolve();device=object();c.device=device;c.update(connected=True)
        before=c.snapshot()['stats']
        for _ in range(2):c._preview_failed(device)
        self.assertFalse(hub.summaries()[0]['connection_issue'])
        c._preview_failed(device)
        self.assertTrue(hub.summaries()[0]['connection_issue'])
        self.assertTrue(c.snapshot()['connected'])
        self.assertEqual(c.snapshot()['stats'],before)
        c.on_image(Image.new('RGB',(30,60)))
        self.assertFalse(hub.summaries()[0]['connection_issue'])
        for _ in range(3):c._preview_failed(object())
        self.assertFalse(hub.summaries()[0]['connection_issue'])

    def test_three_workers_overlap_and_stopping_one_does_not_stop_others(self):
        hub=DeviceHub();keys=['default',hub.add('second'),hub.add('third')]
        entered=threading.Barrier(4);release=threading.Event()
        scopes=[];runner_calls=[]
        class Runner:
            def __init__(self,device,config,control,emit,on_image,**kwargs):
                self.control,self.emit=control,emit
                runner_calls.append(config['max_tasks'])
            def run(self,auto_open):
                key=workspace_scope.get();scopes.append(key)
                logging.getLogger('backend.synthetic').warning('log:'+key)
                entered.wait(3);release.wait(3);self.control.check()
                self.emit(stats=dict(completed=1,failed=0,skipped=0,claims=0,cross_app=0,attempts=1,elapsed=1))
        handler=PanelLogHandler(hub);logging.getLogger().addHandler(handler)
        try:
            with ExitStack() as stack:
                stack.enter_context(patch('backend.core.service.panel.CampaignRunner',Runner))
                for key in keys:
                    c=hub.resolve(key);c.device=Mock(device_type='uiautomator2')
                    c.device.screenshot.return_value=Image.new('RGB',(30,60))
                    c.update(connected=True)
                    for method in ['_ensure_daily','_persist_state','_save_report']:
                        stack.enter_context(patch.object(c,method))
                results=hub.batch(dict(devices=keys,action='run',config={'max_tasks':2}))
                self.assertTrue(all(row['ok'] for row in results))
                entered.wait(3)
                hub.resolve(keys[0]).dispatch('pause',{})
                self.assertTrue(hub.resolve(keys[0]).control.paused.is_set())
                self.assertFalse(hub.resolve(keys[1]).control.paused.is_set())
                hub.resolve(keys[0]).dispatch('stop',{})
                release.set()
                for key in keys:hub.resolve(key).thread.join(3)
            self.assertCountEqual(scopes,keys)
            self.assertEqual(runner_calls,[2,2,2])
            self.assertEqual(hub.resolve(keys[0]).state['status'],'stopped')
            for key in keys[1:]:self.assertEqual(hub.resolve(key).state['stats']['completed'],1)
            for key in keys:
                messages=[row['message'] for row in hub.resolve(key).state['logs']]
                self.assertIn('log:'+key,messages)
                self.assertFalse(any('log:'+other in messages for other in keys if other!=key))
        finally:
            release.set();logging.getLogger().removeHandler(handler)

    def test_usb_wifi_and_emulator_aliases_share_one_physical_lease(self):
        hub=DeviceHub();second=hub.add()
        with patch('backend.core.device.discovery.android_identity',return_value='synthetic-handset'):
            hub.claim('default','uiautomator2','usb')
            with self.assertRaisesRegex(ValueError,'同一台设备'):hub.claim(second,'uiautomator2','192.0.2.1:5555')
            hub.release('default');hub.claim(second,'uiautomator2','emulator-5554')

    def test_remove_running_workspace_is_rejected_and_late_dispatch_stays_closed(self):
        hub=DeviceHub();second=hub.add();c=hub.resolve(second)
        c.thread=Mock(is_alive=lambda:True)
        with self.assertRaises(ValueError):hub.remove(second)
        c.thread=None;hub.remove(second)
        with self.assertRaisesRegex(ValueError,'已关闭'):c.dispatch('configure',{})
        with self.assertRaises(ValueError):hub.remove('default')
        with self.assertRaises(ValueError):hub.remove(None)

    def test_one_disconnected_device_does_not_prevent_other_batch_start(self):
        hub=DeviceHub();second=hub.add()
        with patch.object(hub.resolve('default'),'dispatch') as accepted:
            result=hub.batch(dict(devices=['default',second],action='run'))
        self.assertTrue(result[0]['ok']);self.assertFalse(result[1]['ok']);accepted.assert_called_once()
        with self.assertRaises(ValueError):hub.batch(dict(devices=[second,second]))

    def test_shutdown_blocks_new_workers_and_workspace_creation(self):
        hub=DeviceHub();hub.stop_all()
        with self.assertRaises(ValueError):hub.add()
        with self.assertRaises(ValueError):hub.resolve().dispatch('configure',{})

    def test_scoped_http_never_controls_another_device_on_unknown_id(self):
        hub=DeviceHub();second=hub.add('second')
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(hub,'test-token'))
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        base=f'http://127.0.0.1:{server.server_port}'
        def post(path,body,token='test-token'):
            return urlopen(Request(base+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json','X-Miao-Token':token}))
        try:
            with post('/api/workspaces/rename',{'id':'default','label':'Renamed original'}):pass
            self.assertEqual(hub.resolve('default').workspace_label,'Renamed original')
            self.assertEqual(hub.resolve(second).workspace_label,'second')
            with post('/api/configure?device='+second,{'config':{'max_tasks':3}}):pass
            self.assertEqual(hub.resolve(second).state['config']['max_tasks'],3)
            self.assertEqual(hub.resolve('default').state['config']['max_tasks'],80)
            with self.assertRaises(HTTPError):post('/api/configure?device=missing',{})
            with self.assertRaises(HTTPError) as error:post('/api/workspaces',{},token='wrong')
            self.assertEqual(error.exception.code,403)
            hub.remove('default')
            with urlopen(base+'/api/workspaces?device=default') as response:
                self.assertEqual(json.load(response)['devices'][0]['id'],second)
        finally:server.shutdown();server.server_close();worker.join(3)


class GeometryTests(unittest.TestCase):
    def test_nonprimary_input_targets_logical_display_and_capture_uses_unique_id(self):
        import io
        d=DeviceAdapter.__new__(DeviceAdapter);d.device_type='uiautomator2';d.serial='synthetic';d.io_lock=threading.RLock();d._display_id=7
        d._device=Mock();d._device.shell.return_value=Mock(exit_code=0,output='mDisplayId=7\n mPrimaryDisplayDevice=screen006(local:123456789)')
        d.click(100,200)
        self.assertEqual(d._device.shell.call_args.args[0],['input','-d','7','tap','100','200'])
        d._device.click.assert_not_called()
        raw=io.BytesIO();Image.new('RGB',(1920,1080)).save(raw,format='PNG')
        transport=Mock();transport.shell.return_value=raw.getvalue()
        with patch('adbutils.adb.device',return_value=transport):
            self.assertEqual(d.screenshot().size,(1920,1080))
        self.assertEqual(transport.shell.call_args.args[0],['screencap','-p','-d','123456789'])

    def test_black_protected_page_reads_login_text_without_click_or_ocr(self):
        device=Mock(display_size=(900,1600));device.current_app.return_value={'package':'sample'}
        device.screenshot.return_value=Image.new('RGB',(900,1600))
        device.dump_hierarchy.return_value={'package':'sample','text':'亲，欢迎登录','bounds':[100,100,700,200]}
        reader=ScreenReader(device);reader._ocr=Mock()
        screen=reader.read(force_ocr=True)
        self.assertTrue(screen.obscured);self.assertIn('欢迎登录',screen.text);reader._ocr.assert_not_called()
        session=Mock();session.passive_login_banner=lambda s:False
        with self.assertRaises(NeedsAttention):check_attention(session,screen)
        device.click.assert_not_called()

    def test_mumu_discovery_reads_running_instance_ports_only(self):
        from backend.core.device import emulators
        output={'1':dict(is_android_started=True,adb_host_ip='127.0.0.1',adb_port=12345,name='synthetic'),
                '2':dict(is_android_started=False,adb_host_ip='127.0.0.1',adb_port=12346),
                '3':dict(is_android_started=True,adb_host_ip='192.0.2.10',adb_port=5555)}
        with patch.object(emulators,'find_mumu_cli',return_value='vendor-cli'),patch.object(
                emulators.subprocess,'run',return_value=Mock(returncode=0,stdout=json.dumps(output))) as run:
            rows=emulators.discover_emulators()
        self.assertEqual(run.call_args.args[0],['vendor-cli','info','--vmindex','all'])
        self.assertEqual([d['serial'] for d in rows['devices']],['127.0.0.1:12345'])

    def test_sparse_logical_display_maps_unique_id_not_hwc_port_or_list_index(self):
        dump='Display 7:\n mDisplayId=7\n mPrimaryDisplayDevice=screen006(local:123456789)\n mHasContent=true\n'
        self.assertEqual(capture_display(dump,7),'123456789')
        self.assertEqual(focused_display('mTopFocusedDisplayId=7'),7)
        with self.assertRaises(RuntimeError):capture_display('Display 888 (HWC display 7)',7)
        windows='  Display: mDisplayId=0\n mCurrentFocus=launcher\n  Display: mDisplayId=7\n mCurrentFocus=app\n'
        self.assertNotIn('mCurrentFocus=app',focused_window_dump(windows,0))

    def test_wide_ocr_tiles_cover_edges_without_stretching(self):
        image=Image.new('RGB',(1920,1080));tiles=ocr_tiles(image)
        self.assertEqual(tiles[0][1],0)
        self.assertEqual(tiles[-1][1]+tiles[-1][0].width,1920)
        self.assertTrue(all(tile.height==1080 for tile,x,y in tiles))
        phone=Image.new('RGB',(1440,3200));self.assertIs(ocr_tiles(phone)[0][0],phone)

    def test_rotation_changes_screen_size_on_every_observation(self):
        device=Mock(display_size=(900,1600));device.current_app.return_value={'package':'sample'}
        device.dump_hierarchy.return_value={}
        reader=ScreenReader(device,ocr=False)
        self.assertEqual(reader.read().height,1600)
        device.display_size=(1920,1080)
        result=reader.read();self.assertEqual((result.width,result.height),(1920,1080))
        adapter=DeviceAdapter.__new__(DeviceAdapter);adapter.device_type='uiautomator2';adapter._display_id=7
        adapter._active_image_size=(1920,1080);adapter._device=Mock()
        adapter._device.shell.return_value=Mock(exit_code=0,output='Display: mDisplayId=7\n cur=900x1600 app=900x1600')
        self.assertEqual(adapter.display_size,(900,1600))

    def test_background_windows_are_not_used_as_foreground_controls(self):
        tree={'children':[{'package':'launcher','text':'领淘金币','bounds':[0,0,100,50]},
                          {'package':'sample','text':'real control','bounds':[0,50,100,100]}]}
        nodes=ScreenReader(None)._flatten(tree,'sample')
        self.assertEqual([node.text for node in nodes],['real control'])

    def test_safe_insets_follow_reported_bars_and_cutouts(self):
        output='type=statusBars frame=[0,0][900,40] visible=true\ntype=navigationBars frame=[0,1550][900,1600] visible=true'
        geometry=geometry_from_window(900,1600,output)
        self.assertEqual(geometry.content_bounds,(0,40,900,1550))
        self.assertFalse(geometry.contains(30,20));self.assertTrue(geometry.contains(450,800))
        cutout=geometry_from_window(900,1600,'boundingRect={Bounds=[Rect(400, 0 - 500, 70)]}')
        self.assertFalse(cutout.contains(450,50))

    def test_passive_homepage_login_banner_is_ignored_but_real_login_stops(self):
        session=Mock();session.report_failure.return_value=False
        session.passive_login_banner=EntryPolicy.passive_login_banner
        nodes=[TextNode(label,(0,100+i*50,300,140+i*50)) for i,label in enumerate(['领淘金币','淘宝秒杀','飞猪旅行'])]
        nodes.append(TextNode('立即登录',(600,1400,880,1480)))
        check_attention(session,Screen(nodes,900,1600,'com.taobao.taobao'))
        with self.assertRaises(NeedsAttention):check_attention(session,Screen([TextNode('立即登录',(10,300,200,400))],900,1600,'sample'))
        with self.assertRaises(NeedsAttention):check_attention(session,Screen(nodes+[TextNode('验证码登录',(0,300,500,400))],900,1600,'sample'))

    def test_existing_followed_goods_browse_does_not_authorize_following(self):
        self.assertEqual(classify('你关注的商品都可抵','浏览15秒 +400',['你关注的商品都可抵','浏览15秒 +400'],'去完成',0,1)[1],'browse')
        self.assertEqual(classify('去关注店铺','浏览15秒',['去关注店铺','浏览15秒'],'去完成',0,1)[1],'manual')


if __name__=='__main__':unittest.main()
