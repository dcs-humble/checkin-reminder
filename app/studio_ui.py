"""Product shell for the check-in console; business operations stay in gui.App."""
import tkinter as tk
from tkinter import ttk
import theme as t
from pathlib import Path
from theme_details import Tooltip

NAV = '#ffffff'
NAV_TEXT = '#927a8e'
NAV_ACTIVE = '#eee8f8'


def label(parent, text, size=11, color=None, bg=None, bold=False, **kw):
    return tk.Label(parent, text=text, bg=bg or t.CARD, fg=color or t.INK,
                    font=kw.pop('font',t.face(size,bold)), **kw)


def build(a):
    root = a.root
    root.title('打卡助手 · 肥嘟嘟左卫门')
    import illustrated_ui
    illustrated_ui.load(a)

    side = tk.Frame(root, bg=NAV, width=210)
    side.pack(side='left', fill='y'); side.pack_propagate(False)
    brand = tk.Frame(side, bg=NAV)
    brand.pack(fill='x', padx=20, pady=(28, 30))
    avatar=label(brand, '', bg=NAV)
    if 'brand' in a._mascot_images: avatar.configure(image=a._mascot_images['brand'])
    else: avatar.configure(text='🐽',font=(t.FAMILY,24))
    avatar.pack(side='left')
    names = tk.Frame(brand, bg=NAV); names.pack(side='left', padx=10)
    label(names, '打卡助手', 14, t.INK, NAV, True).pack(anchor='w')
    label(names, 'DAILY CHECK-IN', 9, NAV_TEXT, NAV, font=(t.NUMBERS,9)).pack(anchor='w', pady=(2, 0))

    a._nav = {}; a._pages = {}; a._canvases = {}; a._page_slots = {}
    nav = tk.Frame(side, bg=NAV); nav.pack(fill='x', padx=14)
    a._nav_icons=[]
    for key, glyph, text in [('home','grid','工作台'), ('settings','settings','班级设置'), ('logs','journal','运行记录'), ('checkin','clock','我的打卡')]:
        icon=illustrated_ui.line_icon(nav,glyph,NAV,t.PRIMARY,18)
        a._nav_icons.append(icon)
        b = ttk.Button(nav, text='   '+text, image=icon, compound='left', style='Nav.TButton',
                       cursor='hand2', command=lambda k=key: show(a, k))
        b.pack(fill='x', pady=3); a._nav[key] = b
    tk.Frame(side, bg=t.BORDER, height=1).pack(fill='x', padx=24, pady=24)
    label(side, '班级空间', 10, '#9c8296', NAV, anchor='w').pack(fill='x', padx=26)
    a.lb = tk.Listbox(side, height=3, exportselection=False, activestyle='none',
                      bg=NAV, fg='#6a5067', selectbackground=NAV_ACTIVE,
                      selectforeground=t.PRIMARY, font=t.F, bd=0, highlightthickness=0, selectborderwidth=0)
    a.lb.pack(fill='x', padx=24, pady=(12, 8)); a.lb.bind('<<ListboxSelect>>', a._on_pick)
    tools = tk.Frame(side, bg=NAV); tools.pack(fill='x', padx=24)
    for text, cmd in [('新建', a._new_class), ('复制', a._dup_class), ('删除', a._del_class)]:
        ttk.Button(tools,text=text,command=cmd,style='NavTool.TButton',cursor='hand2',width=3).pack(side='left',padx=1)
    bottom = tk.Frame(side, bg=NAV); bottom.pack(side='bottom', fill='x', padx=24, pady=24)
    if 'mini' in a._mascot_images:
        label(bottom,'',bg=NAV,image=a._mascot_images['mini']).pack(side='left',padx=(0,8))
    words=tk.Frame(bottom,bg=NAV); words.pack(side='left')
    label(words,'左卫门值班中',10,'#71576e',NAV,True).pack(anchor='w')
    label(words,'小事也值得认真。',10,'#9c8296',NAV).pack(anchor='w',pady=(5,0))


    workspace = tk.Frame(root, bg=t.BG); workspace.pack(side='left', fill='both', expand=True)
    top = tk.Frame(workspace, bg=t.CARD, height=60)
    top.pack(fill='x'); top.pack_propagate(False)
    a._breadcrumb = label(top, '工作空间  /  工作台', 10, t.MUTED)
    a._breadcrumb.pack(side='left', padx=28)
    illustrated_ui.date_label(top).pack(side='left')
    ttk.Button(top, text='查看日志文件', style='Ghost.TButton', command=a._open_logs).pack(side='right', padx=(8,24))
    ttk.Button(top, text='环境自检', style='Ghost.TButton', command=a._check).pack(side='right')
    a.l_pill, a.set_pill = t.pill(top, '正在自检…'); a.l_pill.pack(side='right', padx=12)
    tk.Frame(workspace, bg=t.BORDER, height=1).pack(fill='x')

    foot = tk.Frame(workspace, bg=t.CARD)
    foot.pack(side='bottom', fill='x')
    tk.Frame(foot, bg=t.BORDER, height=1).pack(fill='x')
    bar = tk.Frame(foot, bg=t.CARD); bar.pack(fill='x', padx=28, pady=12)
    a.l_msg = label(bar, '就绪', 10, t.MUTED, anchor='w',width=1)
    a.l_msg.pack(side='left', fill='x', expand=True)
    Tooltip(a.l_msg,lambda:a.l_msg.cget('text'))
    a._save_shortcut=label(bar, 'Ctrl + S', 10, t.FAINT)
    a._save_shortcut.pack(side='left', padx=16)
    a._progress=ttk.Progressbar(bar,length=64,mode='indeterminate',style='Busy.Horizontal.TProgressbar')
    def save_current():
        if getattr(a,'_page','home')=='checkin': a._save_auto_checkin()
        else: a._save()
    a._save_button=ttk.Button(bar,text='保存设置',style='Primary.TButton',command=save_current)
    a._save_button.pack(side='right')
    root.bind('<Control-s>',lambda _e:save_current())
    host = tk.Frame(workspace, bg=t.BG); host.pack(fill='both', expand=True)
    for key in ('home', 'settings', 'logs', 'checkin'):
        page = tk.Frame(host, bg=t.BG); a._pages[key] = page
        cv = tk.Canvas(page, bg=t.BG, bd=0, highlightthickness=0)
        sb = ttk.Scrollbar(page, orient='vertical', command=cv.yview)
        sb.pack(side='right', fill='y'); cv.pack(side='left', fill='both', expand=True)
        cv.configure(yscrollcommand=sb.set)
        inner = tk.Frame(cv, bg=t.BG); slot = cv.create_window(0,0,window=inner,anchor='nw')
        cv.bind('<Configure>', lambda e, c=cv, s=slot: c.itemconfigure(s,width=e.width))
        inner.bind('<Configure>', lambda e,c=cv: c.configure(scrollregion=c.bbox('all')))
        a._canvases[key] = cv
        a._page_slots[key] = slot
        a._pages[key].inner = inner
    def wheel(e):
        w = e.widget
        if isinstance(w, (tk.Text, tk.Listbox, ttk.Treeview)): return
        cv = a._canvases[a._page]
        bounds = cv.bbox('all')
        if bounds and bounds[3] > cv.winfo_height(): cv.yview_scroll(-int(e.delta/120),'units')
    root.bind('<MouseWheel>', wheel, add='+')

    home = a._pages['home'].inner
    hero=t.round_panel(home,t.PRIMARY_SOFT,t.PRIMARY_SOFT,22)
    hero.pack(fill='x',padx=28,pady=(22,18))
    portrait=label(hero,'',bg=t.PRIMARY_SOFT,cursor='hand2')
    if 'hero' in a._mascot_images: portrait.configure(image=a._mascot_images['hero'])
    else: portrait.configure(text='🐽',font=(t.FAMILY,40))
    portrait.pack(side='right',padx=(12,26),pady=10)
    intro=tk.Frame(hero,bg=t.PRIMARY_SOFT); intro.pack(side='left',fill='both',expand=True,padx=26,pady=24)
    label(intro,'DAILY CHECK-IN  /  提醒工作台',9,t.PRIMARY,t.PRIMARY_SOFT,True).pack(anchor='w')
    label(intro,'把提醒交给我，',24,t.INK,t.PRIMARY_SOFT,True).pack(anchor='w',pady=(10,0))
    label(intro,'把从容留给自己。',24,t.INK,t.PRIMARY_SOFT,True).pack(anchor='w',pady=(0,10))
    a.l_sub=label(intro,'',10,t.MUTED,t.PRIMARY_SOFT,anchor='w',wraplength=500,justify='left')
    a.l_sub.pack(fill='x')
    hero.bind('<Configure>',lambda e:a.l_sub.configure(wraplength=max(180,e.width-340)),add='+')
    Tooltip(portrait,'左卫门陪你，把每天的小事照顾好。')
    portrait.bind('<Button-1>',lambda _e:a.hint('肥嘟嘟左卫门：今天也要从容完成任务。'))
    metrics=tk.Frame(home,bg=t.BG); metrics.pack(fill='x',padx=28,pady=(0,18))
    a._metric_values=[]
    for i,(title,desc) in enumerate([('当前班级','提醒对象'),('提醒安排','已启用的时间'),('打卡来源','数据来源')]):
        metrics.columnconfigure(i,weight=1,uniform='metric')
        card=t.round_panel(metrics,t.CARD,t.BORDER,14)
        card.grid(row=0,column=i,sticky='nsew',padx=(0,12) if i<2 else 0)
        cap=tk.Frame(card,bg=t.CARD);cap.pack(fill='x',padx=18,pady=(16,8))
        label(cap,title,10,t.MUTED).pack(side='left')
        photo=illustrated_ui.line_icon(cap,['people','clock','link'][i],t.CARD,t.PRIMARY,20)
        a._nav_icons.append(photo);label(cap,'',image=photo).pack(side='right')
        value=label(card,'—',15,bold=True,anchor='w');value.pack(fill='x',padx=18,pady=(0,16))
        value.bind('<Configure>',lambda e,lb=value:lb.configure(wraplength=max(80,e.width)),add='+')
        a._metric_values.append(value)

    outer,actions=t.card(home,'开始一次提醒','按顺序核对，让每次提醒更安心')
    outer.pack(fill='x',padx=28,pady=(0,18))
    row=tk.Frame(actions,bg=t.CARD);row.pack(fill='x',pady=(4,12))
    steps=[('01','核对名单','查看今天还未完成打卡的同学','查看未打卡名单',a._dry,'TButton'),
           ('02','发送前演练','检查提醒内容与群聊是否正确','发送前演练',a._rehearse,'TButton'),
           ('03','发送提醒','确认无误后，发送到当前班级群','立即发送提醒  →',a._run_now,'Primary.TButton')]
    buttons=[]
    for i,(num,title,desc,action,command,style) in enumerate(steps):
        row.columnconfigure(i,weight=1,uniform='action')
        step=tk.Frame(row,bg=t.CARD);step.grid(row=0,column=i,sticky='nsew',padx=(0,18) if i<2 else 0)
        label(step,num+'  '+title,11,t.PRIMARY,bold=True).pack(anchor='w')
        note=label(step,desc,9,t.MUTED,anchor='w',justify='left',wraplength=220)
        note.pack(fill='x',pady=(5,12))
        note.bind('<Configure>',lambda e,lb=note:lb.configure(wraplength=max(80,e.width)),add='+')
        button=ttk.Button(step,text=action,command=command,style=style)
        button.pack(fill='x');buttons.append(button)
    a.b_dry,a.b_rehearse,a.b_run=buttons
    tk.Frame(actions,bg=t.BORDER,height=1).pack(fill='x',pady=(0,8))
    a.b_weekly=ttk.Button(actions,text='复制上周未打卡名单',style='Ghost.TButton',command=a._export_weekly)
    a.b_weekly.pack(side='left')
    a.b_stop=ttk.Button(actions,text='清空 QQ 草稿',style='Ghost.TButton',command=a._wipe)
    a.b_stop.pack(side='left',padx=8)
    a.b_clean=ttk.Button(actions,text='清理缓存',style='Ghost.TButton',command=a._clean_cache)
    a.b_clean.pack(side='right')
    Tooltip(a.b_clean,'查看并清理本程序缓存，回收控制台未使用的内存对象。')
    a.run_btns=(a.b_dry,a.b_rehearse,a.b_weekly,a.b_run,a.b_stop)
    outer,times=t.card(home,'提醒安排','每个时间点独立运行')
    outer.pack(fill='x',padx=28,pady=(0,20))
    ttk.Button(times.head,text='+ 添加时间',style='Ghost.TButton',command=a._add_row).pack(side='right')
    label(times,'提醒时间       提醒内容',10,t.FAINT).pack(anchor='w',pady=(0,4))
    a.times_box=tk.Frame(times,bg=t.CARD); a.times_box.pack(fill='x')
    outer,sch=t.card(home,'自动运行','系统计划任务的实际状态')
    outer.pack(fill='x',padx=28,pady=(0,28))
    ttk.Button(sch.head,text='同步到系统',style='Ghost.TButton',command=a._sync).pack(side='right')
    ttk.Button(sch.head,text='刷新',style='Ghost.TButton',command=a._refresh_tasks).pack(side='right')
    a.l_task=ttk.Label(sch,text='',style='CardHint.TLabel'); a.l_task.pack(anchor='w')
    a.tv=ttk.Treeview(sch,columns=('t','s','n','r'),show='headings',height=3,selectmode='none')
    for col,width,title in [('t',240,'任务名称'),('s',90,'状态'),('n',160,'下次运行'),('r',170,'上次结果')]:
        a.tv.heading(col,text=title); a.tv.column(col,width=width,minwidth=65,anchor='w')
    a.tv.tag_configure('bad',foreground=t.BAD); a.tv.tag_configure('odd',background=t.CARD_ALT)
    a.tv.pack(fill='x',pady=(8,0))

    settings=a._pages['settings'].inner
    illustrated_ui.page_heading(a,settings,'settings','班级设置','整理好名单与来源，让每次提醒都有据可依。')
    outer,info=t.card(settings,'基础信息','修改后保存生效'); outer.pack(fill='x',padx=28,pady=(0,28))
    a.v_name=tk.StringVar(); a.v_keys=tk.StringVar(); a.v_roster=tk.StringVar()
    a.v_form=tk.StringVar(); a.v_form_keys=tk.StringVar(); a.v_enable=tk.BooleanVar(value=True)
    en=tk.Frame(info.head,bg=t.CARD); en.pack(side='right')
    a.l_en=label(en,'启用班级',10,t.MUTED); a.l_en.pack(side='left',padx=8)
    t.Switch(en,a.v_enable).pack(side='left')
    for col in (0,1): info.columnconfigure(col,weight=1,uniform='field')
    def field(row,col,title,var,hint='',browse=False):
        box=tk.Frame(info,bg=t.CARD)
        box.grid(row=row,column=col,sticky='new',padx=(0,18) if col==0 else (18,0),pady=(12,12))
        label(box,title,11,bold=True).pack(anchor='w',pady=(0,9))
        line=tk.Frame(box,bg=t.CARD); line.pack(fill='x')
        if browse: ttk.Button(line,text='浏览',command=a._pick_roster).pack(side='right',padx=(8,0))
        entry=ttk.Entry(line,textvariable=var,width=12)
        entry.pack(side='left',fill='x',expand=True)
        if browse or var is a.v_form:
            Tooltip(entry,var.get)
        note=ttk.Label(box,text=hint,style='CardHint.TLabel',anchor='w',justify='left',wraplength=290)
        note.pack(fill='x',pady=(7,0)); return note
    field(0,0,'班级名称',a.v_name,'使用容易辨认的班级名称')
    field(0,1,'群标题关键词',a.v_keys,'用逗号分隔；群标题须包含其中一个关键词')
    a.l_roster=field(1,0,'花名册文件',a.v_roster,'选择包含姓名、学号的 Excel 文件',True)
    field(1,1,'表单标题关键词',a.v_form_keys,'用逗号分隔；留空时不校验表单标题')
    field(2,0,'打卡表单链接',a.v_form,'留空时自动查找当天链接')
    note=t.round_panel(info,t.PRIMARY_SOFT,t.PRIMARY_SOFT,12); note.grid(row=2,column=1,sticky='new',padx=(18,0),pady=16)
    label(note,'关于自动提醒',10,t.PRIMARY,t.PRIMARY_SOFT,True).pack(anchor='w',padx=16,pady=(14,6))
    label(note,'提醒时间和文案在工作台中管理。\n保存后，时间变更会同步至系统任务。',10,t.MUTED,t.PRIMARY_SOFT,justify='left').pack(anchor='w',padx=16,pady=(0,14))

    logs=a._pages['logs'].inner
    illustrated_ui.page_heading(a,logs,'logs','运行记录','每一次操作，都有迹可循。')
    outer,log=t.card(logs,'活动日志','保留最近约 2,000 行'); outer.pack(fill='both',expand=True,padx=28,pady=(0,28))
    ttk.Button(log.head,text='清空显示',style='Ghost.TButton',command=a._clear_log).pack(side='right')
    wrap=tk.Frame(log,bg=t.LOG_BG); wrap.pack(fill='both',expand=True)
    a._log_empty=label(wrap,'暂无运行记录\n新的操作进度会显示在这里',11,t.FAINT,t.LOG_BG,justify='center')
    a.txt=tk.Text(wrap,height=16,wrap='word',font=t.F_MONO,bg=t.LOG_BG,fg=t.INK,
                  bd=0,padx=16,pady=14,state='disabled',spacing1=3,spacing2=3,spacing3=6,
                  selectbackground=t.SELECT)
    sb=ttk.Scrollbar(wrap,orient='vertical',command=a.txt.yview)
    a.txt.configure(yscrollcommand=sb.set); sb.pack(side='right',fill='y'); a.txt.pack(fill='both',expand=True)
    a.txt.tag_configure('ts',font=(t.MONO,9))
    for name,color in [('ts',t.FAINT),('ok',t.OK),('warn',t.WARN),('bad',t.BAD),('cmd',t.PRIMARY)]: a.txt.tag_configure(name,foreground=color)
    for button,text in [(a.b_dry,'只读取未打卡名单，不发送消息。'),
                        (a.b_rehearse,'组装并核对 QQ 草稿，完成后清空，不发送。'),
                        (a.b_run,'向当前班级群发送提醒，请先核对名单。'),
                        (a.b_weekly,'读取上周记录，将未打卡名单复制到剪贴板。'),
                        (a.b_stop,'清空 QQ 输入框草稿并关闭相关弹窗。')]:
        Tooltip(button,text)
    import auto_checkin_ui
    auto_checkin_ui.build(a,a._pages['checkin'].inner)
    def polish_cursor(widget):
        if isinstance(widget,ttk.Button): widget.configure(cursor='hand2')
        for child in widget.winfo_children(): polish_cursor(child)
    polish_cursor(root)
    show(a,'home')


def heading(parent,title,desc):
    box=tk.Frame(parent,bg=t.BG); box.pack(fill='x',padx=28,pady=(24,24))
    label(box,title,23,bg=t.BG,bold=True).pack(anchor='w')
    label(box,desc,10,t.MUTED,t.BG).pack(anchor='w',pady=(8,0))


def show(a,key):
    for name,page in a._pages.items():
        page.pack_forget()
        a._nav[name].configure(style='NavActive.TButton' if name==key else 'Nav.TButton')
    a._pages[key].pack(fill='both',expand=True); a._page=key
    if key == 'logs': a._nav[key].configure(text='   运行记录')
    a._breadcrumb.configure(text='工作空间  /  '+{'home':'工作台','settings':'班级设置','logs':'运行记录','checkin':'我的打卡'}[key])
    a._save_button.configure(text='保存打卡设置' if key=='checkin' else '保存设置')
    previous=getattr(a,'_page_motion',None)
    if previous is not None:
        a.root.after_cancel(previous)
    for name,cv in a._canvases.items():
        cv.coords(a._page_slots[name],0,0)
    cv=a._canvases[key]; slot=a._page_slots[key]
    def enter(frame=0):
        if not cv.winfo_exists(): return
        offset=round(10*(1-frame/8)**3)
        cv.coords(slot,0,offset)
        if frame<8:
            a._page_motion=a.root.after(16,lambda:enter(frame+1))
        else:
            a._page_motion=None
            cv.configure(scrollregion=cv.bbox('all'))
    enter()



def update(a):
    c=a._pick() or {}
    slots=[s.get('time','') for s in c.get('schedules',[]) if s.get('enabled',True)]
    values=[c.get('name') or '未选择班级', (' / '.join(slots) or '未设置') if c.get('enabled',True) else '已暂停', '固定表单' if c.get('form_url') else '自动查找']
    for index,(lb,value) in enumerate(zip(a._metric_values,values)):
        font=(t.NUMBERS,18) if index==1 and slots and c.get('enabled',True) else t.face(13 if len(value)>16 else 15,True)
        lb.configure(text=value,font=font,wraplength=max(80,lb.winfo_width()))
