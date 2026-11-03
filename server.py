"""Local control panel: only loopback, token-protected mutation endpoints."""
from pathlib import Path
import argparse
import base64
from datetime import datetime
import io
import json
import secrets
import threading
import time
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import xml.etree.ElementTree as ET
import engine

ROOT=Path(__file__).resolve().parent
TOKEN=secrets.token_urlsafe(32)
LOCK=threading.Lock()
DEVICE_LOCK=threading.Lock()
STATE={'running':False,'messages':[],'result':None}
CANCEL=threading.Event()
CAPTURE=None

def log(message):
    with LOCK:
        STATE['messages']=(STATE['messages']+[str(message)])[-150:]

def capture(serial):
    import android_compat
    import uiautomator2 as u2
    import adbutils
    devices=adbutils.adb.device_list()
    if not serial:
        if len(devices)!=1:
            raise engine.Stop('请连接一台安卓手机，或填写设备序列号')
        serial=devices[0].serial
    d=u2.connect(serial)
    current=d.app_current()
    if current['package']!='cn.damai' or 'NcovSku' not in current['activity']:
        raise engine.Stop('请在手机上打开目标演出的票档选购页，选好场次，再读取')
    xml=d.dump_hierarchy()
    root=ET.fromstring(xml)
    title=engine.one(engine.by_id(root,'tv_title'),'演出标题').get('text')
    panel=engine.one(engine.by_id(root,'project_detail_perform_price_flowlayout'),'票档区域')
    cards=engine.by_id(panel,'ll_perform_item')
    if not title or not cards:
        raise engine.Stop('此选购界面暂不支持，未找到演出或票档卡片')
    session=engine.by_id(root,'project_detail_perform_flowlayout')
    prices=engine.by_id(root,'tv_price')
    screenshot=d.screenshot()
    stream=io.BytesIO()
    screenshot.save(stream,format='PNG')
    return {'serial':serial,'title':title,'price':prices[0].get('text','0') if prices else '0',
            'cards':[{'bounds':n.get('bounds'),'label':' / '.join(engine.texts(n))} for n in cards],
            'session_layout':ET.tostring(session[0],encoding='unicode') if session else '',
            'image':'data:image/png;base64,'+base64.b64encode(stream.getvalue()).decode(),
            'width':screenshot.width,'height':screenshot.height,'captured_at':time.time()}

def task(cfg, at, submit):
    try:
        with DEVICE_LOCK:
            args=SimpleNamespace(at=at or None,submit=submit,timeout=45)
            result=engine.run(args,emit=log,stop_event=CANCEL,config=cfg)
        with LOCK:
            STATE['result']=result
    except Exception as exc:
        log('已停止：'+str(exc))
        with LOCK:
            STATE['result']={'outcome':'stopped','reason':str(exc)}
    finally:
        with LOCK:
            STATE['running']=False

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass
    def send(self,value,status=200,ctype='application/json; charset=utf-8'):
        data=value.encode() if isinstance(value,str) else json.dumps(value,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type',ctype)
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.end_headers()
        self.wfile.write(data)
    def allowed(self,mutation=False):
        expected=f'127.0.0.1:{self.server.server_port}'
        if self.headers.get('Host')!=expected:
            return False
        origin=self.headers.get('Origin')
        if origin and origin!='http://'+expected:
            return False
        return not mutation or secrets.compare_digest(self.headers.get('X-Pilot-Token',''),TOKEN)
    def do_GET(self):
        if not self.allowed():
            return self.send({'error':'禁止访问'},403)
        if self.path=='/':
            return self.send((ROOT/'index.html').read_text(encoding='utf-8').replace('__TOKEN__',TOKEN),ctype='text/html; charset=utf-8')
        if self.path=='/api/status':
            with LOCK:
                value=json.loads(json.dumps(STATE))
            return self.send(value)
        if self.path=='/api/profile':
            path=ROOT/'local'/'profile.json'
            return self.send(json.loads(path.read_text(encoding='utf-8')) if path.exists() else {})
        return self.send({'error':'不存在'},404)
    def do_POST(self):
        global CAPTURE
        if not self.allowed(True):
            return self.send({'error':'请求令牌或来源无效'},403)
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<65536:
                raise ValueError('请求大小无效')
            data=json.loads(self.rfile.read(length))
            if self.path=='/api/stop':
                CANCEL.set()
                return self.send({'ok':True})
            if self.path=='/api/capture':
                if not DEVICE_LOCK.acquire(blocking=False):
                    raise engine.Stop('任务运行中，请先停止')
                try:
                    with LOCK:
                        if STATE['running']:
                            raise engine.Stop('任务运行中，请先停止')
                    CAPTURE=capture(data.get('serial','').strip())
                finally:
                    DEVICE_LOCK.release()
                return self.send(CAPTURE)
            if self.path=='/api/start':
                if CAPTURE is None:
                    raise engine.Stop('请先读取当前选购页')
                cfg={'serial':CAPTURE['serial'],'title_contains':[CAPTURE['title']],
                     'date_text':data['date'].strip(),'price':data['price'], 'quantity':1,
                     'attendee':data['attendee'].strip(),'price_index':int(data['index']),
                     'calibrated_price_cards':[c['bounds'] for c in CAPTURE['cards']],
                     'session_layout':CAPTURE['session_layout']}
                engine.validate_config(cfg)
                submit=data.get('submit') is True
                if submit and data.get('ack')!='提交一次':
                    raise engine.Stop('真实模式需输入“提交一次”确认')
                at=data.get('at','')
                if at:
                    dt=datetime.fromisoformat(at)
                    if dt.tzinfo is None or dt.timestamp()<=time.time():
                        raise engine.Stop('开售时间必须是未来时间且包含时区')
                with LOCK:
                    if STATE['running']:
                        raise engine.Stop('已有任务在运行')
                    STATE.update(running=True,messages=[],result=None)
                    CANCEL.clear()
                local=ROOT/'local'
                local.mkdir(exist_ok=True)
                try:
                    (local/'profile.json').write_text(json.dumps({'attendee':cfg['attendee'],'price':cfg['price'],'date':cfg['date_text']},ensure_ascii=False),encoding='utf-8')
                    threading.Thread(target=task,args=(cfg,at,submit),daemon=True).start()
                except Exception:
                    with LOCK:
                        STATE['running']=False
                    raise
                return self.send({'ok':True})
            return self.send({'error':'不存在'},404)
        except Exception as exc:
            return self.send({'error':str(exc)},400)

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8765)
    args=parser.parse_args()
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    print(f'打开 http://127.0.0.1:{args.port} ，Ctrl+C 退出。',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        CANCEL.set()
        server.server_close()
