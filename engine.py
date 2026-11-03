"""Local Android ticket workflow. Default is rehearsal: stops before submission."""
from pathlib import Path
import argparse
from datetime import datetime
from decimal import Decimal
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
import threading

ROOT = Path(__file__).resolve().parent
PREFIX = 'cn.damai:id/'

class Stop(RuntimeError):
    pass

def bounds(node):
    values = list(map(int, re.findall(r'\d+', node.get('bounds', ''))))
    if len(values) != 4 or values[2] <= values[0] or values[3] <= values[1]:
        raise Stop('控件位置无效')
    return values

def center(node):
    a,b,c,d = bounds(node)
    return (a+c)//2, (b+d)//2

def by_id(root, rid):
    return [n for n in root.iter('node') if n.get('resource-id') == PREFIX+rid]

def one(nodes, label):
    if len(nodes) != 1:
        raise Stop(label+'未找到或不唯一')
    return nodes[0]

def texts(root):
    return [n.get('text', '') for n in root.iter('node') if n.get('text')]

def matching_title(root, cfg):
    normalize=lambda value: ''.join(c for c in value if c.isalnum())
    return any(all(normalize(s) in normalize(t) for s in cfg['title_contains']) for t in texts(root))

def selected_people(root):
    parents = {c:n for n in root.iter() for c in n}
    rows = []
    for n in by_id(root, 'text_name'):
        boxes = by_id(parents[n], 'checkbox')
        if len(boxes) == 1:
            rows.append((n.get('text'), boxes[0]))
    return rows

def plan(root, cfg, stage):
    """Return one guarded action from one UI snapshot, never blind click chains."""
    ts = texts(root)
    if any(any(s in t for s in ['操作过于频繁','完成验证','滑动验证','访问异常']) for t in ts):
        raise Stop('需要本人完成安全验证')
    if not matching_title(root, cfg):
        raise Stop('页面演出标题与配置不一致，请提前打开目标演出详情页')
    if stage == 'detail':
        return 'click', center(one(by_id(root,'trade_project_detail_purchase_status_bar_container_fl'),'购票入口'))
    if stage == 'sku':
        sessions = by_id(root,'project_detail_perform_flowlayout')
        if cfg.get('session_layout') and (not sessions or ET.tostring(sessions[0], encoding='unicode') != cfg['session_layout']):
            raise Stop('场次区域改变，请重新读取当前选购页')
        total = by_id(root,'tv_price')
        qty = by_id(root,'tv_num')
        if total and qty and Decimal(total[0].get('text','0')) == Decimal(str(cfg['price'])) and qty[0].get('text') == '1张':
            return 'click', center(one(by_id(root,'btn_buy_view'),'选票确定'))
        if qty and qty[0].get('text') != '1张':
            raise Stop('当前数量不是1张，停止，避免多买')
        panel = one(by_id(root,'project_detail_perform_price_flowlayout'),'票档区域')
        cards = by_id(panel,'ll_perform_item')
        expected = cfg['calibrated_price_cards']
        if [n.get('bounds') for n in cards] != expected:
            raise Stop('票档布局改变，需要重新核对票档顺序；不会按旧坐标点击')
        # Card choice is captured by the user for this specific event/layout.
        return 'click', center(cards[cfg['price_index']])
    if stage == 'review':
        if cfg['date_text'] not in ts:
            raise Stop('确认页场次时间不匹配')
        if not any(re.fullmatch(r'[￥¥]\s*'+re.escape(str(cfg['price']))+r'(?:\.00)?票档',t) for t in ts):
            raise Stop('确认页票档金额不匹配')
        if not any(re.fullmatch(r'[￥¥]\s*'+re.escape(str(cfg['price']))+r'(?:\.00)?',t) for t in ts):
            raise Stop('确认页合计金额不匹配')
        rows = selected_people(root)
        selected = [name for name,box in rows if box.get('checked') == 'true']
        if any(name != cfg['attendee'] for name in selected):
            raise Stop('存在其他已选观演人，停止核对')
        target = one([box for name,box in rows if name == cfg['attendee']], '目标观演人')
        if target.get('checked') != 'true':
            return 'click', center(target)
        submit = one([n for n in root.iter('node') if n.get('text') == '立即提交'], '提交按钮')
        return 'ready', center(submit)
    raise Stop('无法识别购票页面')

def validate_config(cfg):
    if not isinstance(cfg,dict):
        raise Stop('配置格式错误')
    for key in ['serial','attendee','date_text']:
        if not isinstance(cfg.get(key),str) or not cfg[key].strip():
            raise Stop('缺少 '+key)
    if not isinstance(cfg.get('title_contains'),list) or not cfg['title_contains'] or any(not isinstance(s,str) or not s.strip() for s in cfg['title_contains']):
        raise Stop('必须从当前页面读取完整演出标题')
    if cfg.get('quantity') != 1:
        raise Stop('当前版本支持单张票')
    price=Decimal(str(cfg.get('price','0')))
    if not price.is_finite() or price<=0 or price>100000:
        raise Stop('票价无效')
    if price != price.quantize(Decimal('0.01')):
        raise Stop('票价最多两位小数')
    cfg['price']=format(price.normalize(),'f')
    cards=cfg.get('calibrated_price_cards')
    index=cfg.get('price_index')
    if not isinstance(cards,list) or not cards or not isinstance(index,int) or not 0<=index<len(cards):
        raise Stop('请选择截图中的票档卡片')
    if len(cards)>50 or any(not re.fullmatch(r'\[\d+,\d+\]\[\d+,\d+\]',s) for s in cards):
        raise Stop('票档位置无效')
    if not re.fullmatch(r'\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}',cfg['date_text']):
        raise Stop('场次请填写 YYYY.MM.DD HH:MM')

def run(args, emit=print, stop_event=None, config=None):
    stop_event=stop_event or threading.Event()
    cfg = config or json.loads(Path(args.config).read_text(encoding='utf-8'))
    validate_config(cfg)
    if cfg['quantity'] != 1 or Decimal(str(cfg['price'])) <= 0:
        raise Stop('本机已校准流程仅支持1张及明确票价')
    import android_compat
    import uiautomator2 as u2
    d = u2.connect(cfg['serial'])
    d.settings['wait_timeout'] = 0
    original = d.jsonrpc.getConfigurator()
    d.jsonrpc.setConfigurator({'waitForIdleTimeout':0,'waitForSelectorTimeout':0})
    result = {'mode':'submit' if args.submit else 'rehearsal','steps':[], 'submitted_click':False}
    start = time.perf_counter()
    def snapshot():
        before = d.app_current()
        if before['package'] != 'cn.damai':
            raise Stop('大麦未在前台，已停止')
        activity = before['activity']
        if 'ContainerActivity' in activity:
            raise Stop('安全验证页：请本人完成验证后重新启动')
        xml = d.dump_hierarchy()
        stage = ('review' if 'DmOrder' in activity else 'sku' if 'NcovSku' in activity else 'detail' if 'ProjectDetail' in activity else 'unknown')
        return stage, ET.fromstring(xml)
    try:
        stage, root = snapshot()
        plan(root,cfg,stage)  # validate before waiting or clicking
        emit('预检通过；模式：'+result['mode'])
        if args.at:
            sale = datetime.fromisoformat(args.at)
            if sale.tzinfo is None:
                raise Stop('开售时间必须包含时区，例如 +08:00')
            remaining = sale.timestamp()-time.time()
            if remaining < 0:
                raise Stop('开售时间已过去，请核对时间')
            deadline = time.monotonic()+remaining
            emit('等待开售：'+sale.isoformat()+'（使用电脑时钟，请提前校准 Windows 时间）')
            while time.monotonic() < deadline:
                if stop_event.wait(min(.1,max(0,deadline-time.monotonic()))):
                    raise Stop('用户停止')
        start = time.perf_counter()
        last_action = None
        action_time = 0.0
        for count in range(300):
            if stop_event.is_set():
                raise Stop('用户停止')
            if time.perf_counter()-start > args.timeout:
                raise Stop('流程超时，已停止；没有无限重试')
            tick = time.perf_counter()
            stage, root = snapshot()
            action, pos = plan(root,cfg,stage)
            elapsed = time.perf_counter()-start
            result['steps'].append({'stage':stage,'action':action,'elapsed_s':round(elapsed,3),'inspect_s':round(time.perf_counter()-tick,3)})
            emit(f'{elapsed:.3f}s {stage} {action}')
            if action == 'ready':
                if not args.submit:
                    result['outcome']='ready_before_submit'
                    break
                # One submission attempt only. Never retry an uncertain result.
                if stop_event.is_set():
                    raise Stop('用户停止')
                d.click(*pos)
                result['submitted_click']=True
                result['outcome']='submission_attempted_check_official_order'
                emit('已点击提交一次，请在大麦核对订单并自行付款。程序不会重试提交。')
                break
            key=(stage,pos)
            if key == last_action:
                if time.perf_counter()-action_time > 3:
                    raise Stop('点击后页面没有推进，已停止；不反复连点')
                time.sleep(.15)
                continue
            if stop_event.is_set():
                raise Stop('用户停止')
            d.click(*pos)
            last_action=key
            action_time=time.perf_counter()
            time.sleep(.12)
        else:
            raise Stop('达到检查次数上限')
    except Exception as exc:
        result['outcome']='stopped'
        result['reason']=str(exc)
        raise
    finally:
        result['duration_s']=round(time.perf_counter()-start,3)
        logdir=ROOT/'local'/'logs'
        logdir.mkdir(exist_ok=True,parents=True)
        (logdir/(datetime.now().strftime('%Y%m%d-%H%M%S')+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        try:
            d.jsonrpc.setConfigurator({k:original[k] for k in ['waitForIdleTimeout','waitForSelectorTimeout'] if k in original})
        except Exception:
            pass
    return result

if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',default=str(ROOT/'ticket-config.local.json'))
    p.add_argument('--at',help='开售时间，例如 2026-11-04T12:00:00+08:00')
    p.add_argument('--submit',action='store_true',help='明确启用真实提交一次；默认停在提交前')
    p.add_argument('--timeout',type=float,default=45)
    args=p.parse_args()
    try:
        print(json.dumps(run(args),ensure_ascii=False),flush=True)
    except (Stop,Exception) as exc:
        print('已停止：'+str(exc),file=sys.stderr,flush=True)
        sys.exit(1)
