"""Settings page for personal clock-in; main app owns the event loop."""
import queue
import re
import threading
import tkinter as tk
from tkinter import ttk, messagebox
import theme as t
import auto_checkin as backend


def build(a,parent):
    from studio_ui import label,heading
    heading(parent,'我的每日打卡','使用专用 Edge 中已登录的腾讯文档账号，每天替自己点一次打卡。')
    cfg=backend.load()
    current=a.cfg.get('classes',[])
    if not cfg.get('url') and current:
        cfg['url']=current[0].get('form_url','')
        cfg['title']=' / '.join(current[0].get('form_title_keys',[])) or '造桥新青年'
    enabled=tk.BooleanVar(value=bool(cfg.get('enabled')))
    url=tk.StringVar(value=cfg.get('url',''))
    title=tk.StringVar(value=cfg.get('title',''))
    clock=tk.StringVar(value=cfg.get('time','20:10'))
    names=tk.StringVar(value='、'.join(cfg.get('additional_names',[])))
    outer,body=t.card(parent,'每天自动打卡','首次使用请先保存设置，再试打卡验证')
    outer.pack(fill='x',padx=28,pady=(0,20))
    toggle=tk.Frame(body.head,bg=t.CARD);toggle.pack(side='right')
    label(toggle,'启用',10,t.MUTED).pack(side='left',padx=8)
    t.Switch(toggle,enabled).pack(side='left')
    for caption,var in [('打卡时间（北京时间，每天）',clock),('腾讯文档打卡链接',url),('表单标题关键词',title),('额外打卡姓名（用逗号或顿号分隔）',names)]:
        label(body,caption,11,bold=True).pack(anchor='w',pady=(12,8))
        ttk.Entry(body,textvariable=var).pack(fill='x')
    label(body,'默认时间为 20:10，可自行修改。电脑需开机、联网并登录 Windows；腾讯文档登录态需有效。',10,t.MUTED,
          wraplength=680,justify='left',anchor='w').pack(fill='x',pady=(16,8))
    label(body,'额外姓名已保存。需先验证改名后是否会新增独立记录；验证完成前，多人自动执行保持关闭。',10,t.MUTED,
          wraplength=680,justify='left',anchor='w').pack(fill='x',pady=(0,12))
    status=tk.StringVar(value=('已关闭 · '+str(len(cfg.get('additional_names',[])))+' 人名单已保存，改名提交流程待验证') if cfg.get('additional_names') else ('已关闭 · 待验证表单' if not cfg.get('enabled') else '已启用 · 每天 '+cfg['time']))
    label(body,'',10,t.PRIMARY,textvariable=status,wraplength=680,justify='left',anchor='w').pack(fill='x',pady=(0,12))
    row=tk.Frame(body,bg=t.CARD);row.pack(fill='x')
    results=queue.Queue();buttons=[];busy=[False]

    def values():
        saved=backend.load()
        return dict(enabled=enabled.get(),time=clock.get().strip(),url=url.get().strip(),
                    title=title.get().strip(),verified=saved.get('verified',''),
                    additional_names=[n.strip() for n in re.split(r'[,，、;；\n]+',names.get()) if n.strip()])

    def start(fn):
        if busy[0] or a.busy:
            status.set('已有操作正在运行，请稍候');return
        busy[0]=True
        for b in buttons:b.state(['disabled'])
        status.set('正在处理…')
        def work():
            try: results.put(fn())
            except Exception as exc:results.put((False,str(exc)))
        threading.Thread(target=work,daemon=False).start()
        def poll():
            if not parent.winfo_exists():return
            try:ok,msg=results.get_nowait()
            except queue.Empty:
                a.root.after(150,poll);return
            busy[0]=False
            for b in buttons:b.state(['!disabled'])
            enabled.set(bool(backend.load().get('enabled')))
            status.set(('✓ ' if ok else '⚠ ')+msg)
            a.say('个人打卡：'+msg,'ok' if ok else 'warn')
        a.root.after(150,poll)

    def save():
        snap=values()
        try:backend.validate(snap)
        except ValueError as exc:status.set(str(exc));return
        def work():
            backend.save(snap)
            ok,msg=backend.sync(snap)
            if not ok:
                snap['enabled']=False;backend.save(snap)
                return False,'计划任务未同步，自动执行已关闭：'+msg
            if snap.get('additional_names'):
                return True,'已保存 '+ '、'.join(snap['additional_names'])+'；多人提交流程待验证，自动执行已关闭'
            return True,('已启用，每天 '+snap['time']+' 自动打卡') if snap['enabled'] else '已保存，自动执行已关闭；可先检查页面或试打卡'
        start(work)

    a._save_auto_checkin=save

    def test(submit):
        snap=values()
        stored=backend.load()
        if any(snap[k]!=stored.get(k) for k in ('url','title','time','additional_names')):
            status.set('请先关闭自动执行并保存当前设置，再进行检查或试打卡');return
        start(lambda:backend.run(submit=submit))

    for caption,command,style in [('保存并同步',save,'Primary.TButton'),
                                   ('检查页面',lambda:test(False),'TButton'),
                                   ('试打卡',lambda:test(True),'TButton')]:
        button=ttk.Button(row,text=caption,command=command,style=style,cursor='hand2')
        button.pack(side='left',padx=(0,10));buttons.append(button)
    outer,records=t.card(parent,'最近执行记录','每天去重；不明确的提交结果当天不再重试')
    outer.pack(fill='x',padx=28,pady=(0,28))
    record=tk.StringVar()
    label(records,'',10,t.MUTED,textvariable=record,wraplength=680,justify='left',anchor='w').pack(fill='x',pady=6)
    def refresh():
        data=backend.read(backend.STATE,{})
        rows=sorted(data.values(),key=lambda x:x.get('at',''),reverse=True)[:7]
        record.set('\n'.join(r['at'][:16].replace('T',' ')+'  '+r['message'] for r in rows) or '尚无执行记录。第一次试打卡成功后，才能启用每天自动执行。')
    ttk.Button(records.head,text='刷新',style='Ghost.TButton',command=refresh).pack(side='right')
    refresh()
