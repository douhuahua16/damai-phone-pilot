import json
import threading
import unittest
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from unittest.mock import patch
from unittest.mock import Mock
from types import SimpleNamespace
from pathlib import Path
import tempfile
import sys
import engine
import server

def cfg():
    return dict(serial='test-device',title_contains=['北京·测试演出'],date_text='2030.01.02 18:30',
                price='158',quantity=1,attendee='测试观演人',price_index=0,
                calibrated_price_cards=['[10,100][200,180]'],session_layout='')

def xml(stage='review',price='158',selected=True,other=False):
    r=ET.Element('hierarchy')
    def node(parent=r,text='',rid='',**attrs):
        return ET.SubElement(parent,'node',{'text':text,'resource-id':engine.PREFIX+rid if rid else '',
                                           'bounds':'[10,10][100,60]',**attrs})
    node(text='【北京】测试演出')
    if stage=='review':
        node(text='2030.01.02 18:30');node(text='￥'+price+'.00票档');node(text='¥ '+price+'.00')
        row=node();node(row,text='测试观演人',rid='text_name');node(row,rid='checkbox',checked=str(selected).lower())
        row=node();node(row,text='另一个人',rid='text_name');node(row,rid='checkbox',checked=str(other).lower())
        node(text='立即提交')
    else:
        node(text=price,rid='tv_price');node(text='1张',rid='tv_num');node(rid='btn_buy_view')
        panel=node(rid='project_detail_perform_price_flowlayout')
        n=node(panel,rid='ll_perform_item');n.set('bounds','[10,100][200,180]')
    return r

class PlannerTests(unittest.TestCase):
    def test_normalized_title_and_ready(self):
        self.assertEqual(engine.plan(xml(),cfg(),'review')[0],'ready')
    def test_wrong_price_stops(self):
        with self.assertRaises(engine.Stop):engine.plan(xml(price='188'),cfg(),'review')
    def test_wrong_date_stops(self):
        c=cfg();c['date_text']='2030.01.03 18:30'
        with self.assertRaises(engine.Stop):engine.plan(xml(),c,'review')
    def test_wrong_attendee_stops(self):
        with self.assertRaises(engine.Stop):engine.plan(xml(other=True),cfg(),'review')
    def test_unselected_person_selects_without_submit(self):
        self.assertEqual(engine.plan(xml(selected=False),cfg(),'review')[0],'click')
    def test_new_event_configuration(self):
        c=cfg();c['price']='188';c['title_contains']=['上海测试音乐会']
        r=xml(price='188');next(r.iter('node')).set('text','上海·测试音乐会')
        self.assertEqual(engine.plan(r,c,'review')[0],'ready')
    def test_layout_change_stops(self):
        c=cfg();c['calibrated_price_cards']=['[0,0][1,1]']
        r=xml('sku',price='0')
        with self.assertRaises(engine.Stop):engine.plan(r,c,'sku')
    def test_captcha_stops(self):
        r=xml();ET.SubElement(r,'node',text='完成验证后可继续购票')
        with self.assertRaises(engine.Stop):engine.plan(r,cfg(),'review')
    def test_quantity_stops(self):
        r=xml('sku');engine.by_id(r,'tv_num')[0].set('text','2张')
        with self.assertRaises(engine.Stop):engine.plan(r,cfg(),'sku')
    def test_config_validation(self):
        c=cfg();c['price']='158.00';engine.validate_config(c);self.assertEqual(c['price'],'158')
        for value in ['NaN','Infinity','-1','0','1.001']:
            c=cfg();c['price']=value
            with self.assertRaises(Exception):engine.validate_config(c)
    def test_missing_person_stops(self):
        c=cfg();c['attendee']='不存在'
        with self.assertRaises(engine.Stop):engine.plan(xml(),c,'review')

class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        cls.thread=threading.Thread(target=cls.http.serve_forever,daemon=True);cls.thread.start()
        cls.url='http://127.0.0.1:'+str(cls.http.server_port)
    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown();cls.http.server_close();cls.thread.join()
    def request(self,path,data=None,headers=None):
        req=urllib.request.Request(self.url+path,data=json.dumps(data).encode() if data is not None else None,headers=headers or {})
        return urllib.request.urlopen(req)
    def test_panel_and_status(self):
        with self.request('/') as r:self.assertIn('Phone Pilot',r.read().decode())
        with self.request('/api/status') as r:self.assertFalse(json.load(r)['running'])
    def test_csrf_blocked(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:self.request('/api/stop',{})
        self.assertEqual(ctx.exception.code,403)
    def test_wrong_origin_blocked(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request('/api/stop',{}, {'X-Pilot-Token':server.TOKEN,'Origin':'https://example.com'})
        self.assertEqual(ctx.exception.code,403)
    def test_stop_works(self):
        with self.request('/api/stop',{}, {'X-Pilot-Token':server.TOKEN}) as r:self.assertTrue(json.load(r)['ok'])
        server.CANCEL.clear()
    def test_start_requires_capture(self):
        with patch.object(server,'CAPTURE',None):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.request('/api/start',{}, {'X-Pilot-Token':server.TOKEN})
            self.assertEqual(ctx.exception.code,400)

class WorkflowTests(unittest.TestCase):
    def exercise(self, submit=False, cancelled=False):
        frames=[('sku',xml('sku','0')),('sku',xml('sku')),('review',xml(selected=False)),('review',xml())]
        class Device:
            def __init__(self):
                self.index=0;self.clicks=[];self.settings={};self.jsonrpc=Mock()
                self.jsonrpc.getConfigurator.return_value={'waitForIdleTimeout':10000,'waitForSelectorTimeout':0}
            def app_current(self):
                stage=frames[min(self.index,3)][0]
                return {'package':'cn.damai','activity':'NcovSkuActivity' if stage=='sku' else 'DmOrderActivity'}
            def dump_hierarchy(self):return ET.tostring(frames[min(self.index,3)][1],encoding='unicode')
            def click(self,*pos):self.clicks.append(pos);self.index+=1
        d=Device();stop=threading.Event()
        if cancelled:stop.set()
        with tempfile.TemporaryDirectory() as tmp, patch.object(engine,'ROOT',Path(tmp)), patch.dict(sys.modules,{'android_compat':Mock(),'uiautomator2':SimpleNamespace(connect=lambda serial:d)}):
            args=SimpleNamespace(at=None,submit=submit,timeout=45)
            if cancelled:
                with self.assertRaises(engine.Stop):engine.run(args,emit=lambda _:None,stop_event=stop,config=cfg())
                self.assertEqual(d.clicks,[])
            else:
                result=engine.run(args,emit=lambda _:None,stop_event=stop,config=cfg())
                self.assertEqual(len(d.clicks),4 if submit else 3)
                self.assertEqual(result['submitted_click'],submit)
                self.assertEqual(result['outcome'],'submission_attempted_check_official_order' if submit else 'ready_before_submit')
            self.assertEqual(d.jsonrpc.setConfigurator.call_args.args[0]['waitForIdleTimeout'],10000)
    def test_rehearsal_end_to_end(self):self.exercise()
    def test_submit_exactly_once(self):self.exercise(submit=True)
    def test_cancel_before_click(self):self.exercise(cancelled=True)

if __name__=='__main__':unittest.main()
