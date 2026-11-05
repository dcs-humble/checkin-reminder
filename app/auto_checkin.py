"""Personal daily clock-in. No network activity on import; explicit run only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta
from xml.sax.saxutils import escape

APP = Path(__file__).resolve().parent
ROOT = APP.parent
SETTINGS = APP / 'auto-checkin.json'
STATE = ROOT / 'logs' / 'auto-checkin-state.json'
TASK = '个人每日打卡'
TZ = timezone(timedelta(hours=8))


def now():
    return datetime.now(TZ)


def read(path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp.' + str(os.getpid()))
    with tmp.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def load():
    return read(SETTINGS, dict(enabled=False, time='20:10', url='', title='造桥新青年', verified=''))


def fingerprint(cfg):
    return hashlib.sha256((cfg['url']+'|'+cfg['title']).encode()).hexdigest()


def validate(cfg):
    if not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', cfg.get('time', '')):
        raise ValueError('请输入 HH:MM 格式的打卡时间')
    if not re.fullmatch(r'https://docs\.qq\.com/form/page/clock/[A-Za-z0-9_-]+/?', cfg.get('url', '')):
        raise ValueError('请输入腾讯文档打卡链接（https://docs.qq.com/form/page/clock/…）')
    if not cfg.get('title', '').strip():
        raise ValueError('请填写表单标题关键词，防止打错表单')
    names=cfg.get('additional_names',[])
    if not isinstance(names,list) or any(not isinstance(n,str) or not n.strip() for n in names):
        raise ValueError('额外打卡姓名必须是非空姓名列表')
    if len(names)!=len(set(names)):
        raise ValueError('打卡名单里有重复姓名，请检查')
    if names and cfg.get('enabled'):
        raise ValueError('多人改名提交流程尚未验证，当前不能启用自动执行')
    if cfg.get('enabled') and cfg.get('verified') != fingerprint(cfg):
        raise ValueError('请先关闭自动执行，保存后点“试打卡”，成功验证后再启用')


def save(cfg):
    validate(cfg)
    write(SETTINGS, cfg)


def sync(cfg):
    import schedule as sch
    if not cfg.get('enabled'):
        rc, xml = sch.xml(TASK)
        if rc == 0:
            return sch.set_enabled(TASK, False)
        return True, '自动打卡已关闭'
    validate(cfg)
    sid = sch.current_sid()
    if not sid:
        return False, '无法识别当前 Windows 用户，未创建定时任务'
    values = dict(desc='个人每日打卡 · 使用专用 Edge 中已登录的腾讯文档账号',
                  name=TASK, sid=sid,
                  start=now().strftime('%Y-%m-%dT')+cfg['time']+':00+08:00',
                  exe=str(ROOT/'runtime'/'python'/'pythonw.exe'),
                  args='"'+str(APP/'auto_checkin.py')+'" --scheduled', cwd=str(APP))
    xml = sch.XML.format(**{k:escape(v) for k,v in values.items()})
    # Never blindly retry a potentially submitted clock-in.
    xml = re.sub(r'\s*<RestartOnFailure>.*?</RestartOnFailure>', '', xml, flags=re.S)
    with tempfile.NamedTemporaryFile(suffix='.xml', delete=False) as f:
        path=f.name
        f.write(xml.encode('utf-16'))
    try:
        rc,msg=sch._run(['schtasks','/Create','/TN',TASK,'/XML',path,'/F'])
        return rc == 0, msg
    finally:
        os.unlink(path)


# Exact action labels only. Unknown forms and ambiguous controls fail closed.
JS_SNAPSHOT = r'''(() => {
 const visible=e=>!!(e.getClientRects().length)&&getComputedStyle(e).visibility!=='hidden';
 const controls=[...document.querySelectorAll('button,[role="button"],input[type="button"],input[type="submit"]')].filter(visible);
 const text=e=>(e.innerText||e.value||e.textContent||'').trim();
 const buttons=controls.map(e=>({text:text(e),disabled:!!e.disabled||e.getAttribute('aria-disabled')==='true'}));
 const notices=[...document.querySelectorAll('[role="alert"],[role="status"],.dui-message,.dui-toast')].filter(visible).map(text);
 return {url:location.href,title:document.title,body:(document.body.innerText||'').slice(0,16000),buttons,notices};
})()'''


def classify(st, cfg):
    from urllib.parse import urlparse
    expected=urlparse(cfg['url']); actual=urlparse(st.get('url',''))
    if actual.netloc != expected.netloc or actual.path.rstrip('/') != expected.path.rstrip('/'):
        return 'blocked', '页面已跳转，未操作'
    body=st.get('body','')
    if cfg['title'] not in (st.get('title','')+'\n'+body):
        return 'blocked', '表单标题不匹配，未操作'
    if any(word in body for word in ['登录后才能','请先登录','安全验证','验证码']):
        return 'blocked', '请在专用 Edge 中完成登录或验证后重试'
    if any(word in body for word in ['尚未开始','未到打卡时间','打卡已结束','今日打卡已结束']):
        return 'blocked', '当前不在表单收集时间内'
    dates=re.findall(r'(\d{1,2})月(\d{1,2})日\s*打卡',body)
    today=(str(now().month),str(now().day))
    if not dates or tuple(str(int(n)) for n in dates[0]) != today:
        return 'blocked', '无法确认页面是今天的打卡，未操作；请检查页面日期'
    if any(b['text'] in ('今日已打卡','今天已打卡','已打卡','已完成打卡') for b in st.get('buttons',[])):
        return 'done', '当前账号今天已打卡'
    if any(re.search(r'打卡成功|今日已打卡|今天已打卡', n) for n in st.get('notices',[])):
        return 'done', '页面已确认打卡成功'
    hits=[b for b in st.get('buttons',[]) if b['text'] in ('打卡','立即打卡','一键打卡') and not b['disabled']]
    if len(hits)==1:
        return 'ready', hits[0]['text']
    return 'blocked', '没有找到唯一可用的打卡按钮；可能需要登录、填写信息或适配表单'


def execute(cfg, submit=False):
    """Called only by an explicit app button or an enabled Windows task."""
    sys.path.insert(0,str(ROOT/'lib'))
    import docs
    docs.open_browser()  # Reuse the app's established logged-in profile.
    pg=docs.Page()
    target=None
    try:
        reply=pg.call('Target.createTarget',{'url':'about:blank'})
        target=reply['result']['targetId']
        targets=docs.http_json('http://127.0.0.1:%d/json/list'%docs.PORT)
        tab=next(t for t in targets if t['id']==target)
        pg.close()
        pg.ws=docs.websocket.create_connection(tab['webSocketDebuggerUrl'],timeout=30,suppress_origin=True)
        pg.call('Page.enable')
        pg.call('Page.navigate',{'url':cfg['url']})
        for _ in range(25):
            st=pg.ev(JS_SNAPSHOT)
            status,msg=classify(st,cfg) if isinstance(st,dict) else ('blocked','页面未加载')
            if status in ('done','ready') or (isinstance(st,dict) and '登录后才能' in st.get('body','')):
                break
            time.sleep(1)
        if status=='done':
            return True,msg,'done'
        if status!='ready' or not submit:
            return status=='ready',msg if status!='ready' else '已找到按钮：'+msg,'checked'
        # Persist the intent BEFORE clicking: a timeout cannot cause a second submission.
        set_day(cfg,'uncertain','已发起打卡，等待网页确认；结果不明确时请人工核对')
        label=json.dumps(msg,ensure_ascii=False)
        clicked=pg.ev('''(() => { const es=[...document.querySelectorAll('button,[role="button"],input[type="button"],input[type="submit"]')].filter(e=>e.getClientRects().length&&!e.disabled&&e.getAttribute('aria-disabled')!=='true'&&(e.innerText||e.value||e.textContent||'').trim()==='''+label+''');if(es.length!==1)return false;es[0].click();return true;})()''')
        if clicked is not True:
            return False,'按钮状态已改变，请人工核对；今天不再自动重试','uncertain'
        for _ in range(20):
            time.sleep(1)
            st=pg.ev(JS_SNAPSHOT)
            if isinstance(st,dict) and classify(st,cfg)[0]=='done':
                return True,'打卡成功，网页已确认','done'
        return False,'已点击，但网页未给出可识别的成功提示；请人工核对，今天不再自动重试','uncertain'
    finally:
        # Keep page visible for the user if login, confirmation or manual review is needed.
        pg.close()


def day_key(cfg):
    return now().date().isoformat()+'|'+fingerprint(cfg)


def set_day(cfg,status,message):
    data=read(STATE,{})
    data[day_key(cfg)]=dict(status=status,message=message,at=now().isoformat())
    write(STATE,data)


def run(submit=False,scheduled=False):
    import msvcrt
    STATE.parent.mkdir(parents=True,exist_ok=True)
    with (STATE.parent/'auto-checkin.lock').open('a+b') as lock:
        lock.seek(0); lock.write(b'0'); lock.flush(); lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:
            return False,'另一次个人打卡正在运行'
        try:
            cfg=load();validate(cfg)
            if scheduled:
                if not cfg.get('enabled'):
                    return True,'自动打卡已关闭'
                if now().strftime('%H:%M')<cfg['time']:
                    return False,'早于当天设置时间，未打卡'
            if submit and cfg.get('additional_names'):
                return False,'已保存 '+ '、'.join(cfg['additional_names'])+'；多人改名提交流程尚未验证，未提交打卡'
            previous=read(STATE,{}).get(day_key(cfg),{})
            if previous.get('status') in ('done','uncertain'):
                return previous['status']=='done',previous['message']
            ok,msg,status=execute(cfg,submit=submit)
            if status in ('done','uncertain'):
                set_day(cfg,status,msg)
            if status=='done':
                cfg=load();cfg['verified']=fingerprint(cfg);save(cfg)
            return ok,msg
        finally:
            lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--scheduled',action='store_true')
    parser.add_argument('--run',action='store_true')
    args=parser.parse_args()
    try:
        ok,msg=run(submit=args.run or args.scheduled,scheduled=args.scheduled)
    except Exception as exc:
        ok,msg=False,str(exc)
    STATE.parent.mkdir(parents=True,exist_ok=True)
    with (STATE.parent/'auto-checkin.log').open('a',encoding='utf-8') as f:
        f.write(now().isoformat()+' '+('成功 ' if ok else '失败 ')+msg+'\n')
    return 0 if ok else 1


if __name__=='__main__':
    if sys.stdout is None: sys.stdout=open(os.devnull,'w',encoding='utf-8')
    if sys.stderr is None: sys.stderr=open(os.devnull,'w',encoding='utf-8')
    raise SystemExit(main())
