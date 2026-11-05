"""Preview-and-clean dialog for application caches and collectible GUI memory."""
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import ttk
import theme as t
import cache_cleanup as cleanup


def open_dialog(app):
    if app.busy:
        app.hint('当前操作完成后再清理缓存')
        return
    previous=getattr(app,'_cleanup_window',None)
    if previous is not None and previous.winfo_exists():
        previous.lift();return
    app._cleanup_window=CleanupDialog(app)


class CleanupDialog(tk.Toplevel):
    def __init__(self,app):
        super().__init__(app.root)
        self.app=app;self.app_root=Path(__file__).resolve().parent.parent
        self.results=queue.Queue();self.files=[];self.busy=False;self._poll_job=None
        self.title('缓存与内存 · 打卡助手');self.configure(bg=t.BG)
        self.transient(app.root)
        width=min(760,self.winfo_screenwidth()-64);height=min(690,self.winfo_screenheight()-90)
        x=max(16,min(app.root.winfo_rootx()+(app.root.winfo_width()-width)//2,self.winfo_screenwidth()-width-16))
        y=max(16,min(app.root.winfo_rooty()+(app.root.winfo_height()-height)//2,self.winfo_screenheight()-height-56))
        self.geometry(f'{width}x{height}+{x}+{y}');self.minsize(min(680,width),min(640,height))
        self.protocol('WM_DELETE_WINDOW',self.close)
        self.bind('<Escape>',lambda _e:self.close())
        header=tk.Frame(self,bg=t.BG);header.pack(fill='x',padx=28,pady=(24,18))
        tk.Label(header,text='缓存与内存',font=t.face(21),bg=t.BG,fg=t.INK).pack(anchor='w')
        tk.Label(header,text='先查看可清理内容，再释放不再需要的空间。',font=t.F_SMALL,
                 bg=t.BG,fg=t.MUTED).pack(anchor='w',pady=(8,0))
        stats=t.round_panel(self,t.CARD,t.BORDER);stats.pack(fill='x',padx=28,pady=(0,18))
        self.stat_labels=[]
        for i,title in enumerate(('可清理空间','缓存文件','控制台当前内存')):
            stats.columnconfigure(i,weight=1,uniform='stat')
            box=tk.Frame(stats,bg=t.CARD);box.grid(row=0,column=i,sticky='nsew',padx=18,pady=14)
            tk.Label(box,text=title,font=t.F_SMALL,bg=t.CARD,fg=t.MUTED).pack(anchor='w')
            label=tk.Label(box,text='—',font=(t.NUMBERS,17),bg=t.CARD,fg=t.INK)
            label.pack(anchor='w',pady=(8,0));self.stat_labels.append(label)
        outer,content=t.card(self,'本次清理范围','最近诊断文件会保留')
        outer.pack(fill='both',expand=True,padx=28,pady=(0,14))
        self.table=ttk.Treeview(content,columns=('kind','count','size'),show='headings',height=3,selectmode='none')
        for name,title,width in [('kind','缓存类别',370),('count','文件数',70),('size','占用空间',95)]:
            self.table.heading(name,text=title);self.table.column(name,width=width,minwidth=60,anchor='w')
        self.table.pack(fill='x')
        memory=tk.Frame(content,bg=t.CARD);memory.pack(fill='x',pady=(14,0))
        self.memory=tk.BooleanVar(value=True)
        t.Switch(memory,self.memory).pack(side='left')
        tk.Label(memory,text='回收本控制台未使用的内存对象',font=t.F,bg=t.CARD,
                 fg=t.INK).pack(side='left',padx=10)
        tk.Label(content,text='保留配置、花名册、插画、备份与运行日志；仅处理本程序文件。',
                 font=t.F_SMALL,bg=t.CARD,fg=t.MUTED,wraplength=640,justify='left').pack(anchor='w',pady=(12,0))
        self.status=tk.Label(self,text='正在扫描…',font=t.F_SMALL,bg=t.BG,fg=t.MUTED,
                             anchor='w',justify='left',wraplength=690)
        self.status.pack(fill='x',padx=28,pady=(0,10))
        foot=tk.Frame(self,bg=t.BG);foot.pack(fill='x',padx=28,pady=(0,20))
        self.scan_button=ttk.Button(foot,text='重新扫描',command=self.scan)
        self.scan_button.pack(side='left')
        self.clean_button=ttk.Button(foot,text='清理缓存与内存',style='Primary.TButton',command=self.start_clean)
        self.clean_button.pack(side='right')
        self.close_button=ttk.Button(foot,text='关闭',command=self.close)
        self.close_button.pack(side='right',padx=10)
        # Reserve controls first; expanding content must never push actions off-screen.
        foot.pack_configure(side='bottom',before=header)
        self.status.pack_configure(side='bottom',after=foot)
        self.grab_set();self.scan()
        self.after_idle(self.focus_set)
        self._poll_job=self.after(80,self.poll)

    def _start(self,kind,fn):
        if self.busy:return
        self.busy=True
        self.scan_button.state(['disabled']);self.clean_button.state(['disabled'])
        def run():
            try:self.results.put((kind,fn(),None))
            except Exception as exc:self.results.put((kind,None,str(exc)))
        threading.Thread(target=run,daemon=True).start()

    def scan(self):
        if self.busy:return
        self.status.configure(text='正在扫描本程序缓存…',fg=t.MUTED)
        self._start('scan',lambda:cleanup.scan(self.app_root))

    def display_scan(self,result):
        self.files=result['files']
        for item in self.table.get_children():self.table.delete(item)
        for kind,title in cleanup.LABELS.items():
            files=[f for f in self.files if f.kind==kind]
            self.table.insert('','end',values=(title,len(files),cleanup.size_text(sum(f.size for f in files))))
        for label,text in zip(self.stat_labels,(cleanup.size_text(result['bytes']),str(len(self.files)),cleanup.size_text(cleanup.memory_usage()))):
            label.configure(text=text)

    def start_clean(self):
        if self.busy:return
        if self.app.busy:
            self.status.configure(text='当前提醒操作完成后再清理。');return
        if not self.files and not self.memory.get():
            self.status.configure(text='没有可清理的缓存，也未选择内存回收。');return
        self.status.configure(text='正在清理，已更新或被占用的文件会跳过…',fg=t.MUTED)
        files=tuple(self.files)
        self._collect_memory=bool(self.memory.get())
        def run():
            result=cleanup.clean(self.app_root,files)
            result['remaining']=cleanup.scan(self.app_root)
            return result
        self._start('clean',run)

    def poll(self):
        try:
            while True:
                try:kind,result,error=self.results.get_nowait()
                except queue.Empty:break
                self.busy=False
                self.scan_button.state(['!disabled']);self.clean_button.state(['!disabled'])
                if error:
                    self.status.configure(text='处理未完成：'+error,fg=t.BAD)
                    self.app.say('⚠ 缓存清理未完成：'+error,'warn');continue
                if kind=='scan':
                    self.display_scan(result)
                    text='扫描完成。没有需要删除的磁盘缓存。' if not self.files else '扫描完成。仅删除上表范围内、扫描后未发生变化的文件。'
                    if result['warnings']:text+=' '+ '；'.join(result['warnings'])
                    self.status.configure(text=text,fg=t.MUTED)
                else:
                    # Python/Tk resource finalizers must run on the UI thread.
                    memory=cleanup.collect_memory() if self._collect_memory else None
                    self.display_scan(result['remaining'])
                    parts=[f"已清理 {result['count']} 个文件，释放 {cleanup.size_text(result['bytes'])} 磁盘空间"]
                    if memory:parts.append(f"回收 {memory['objects']} 个未使用对象")
                    if result['skipped']:parts.append(f"{len(result['skipped'])} 项已跳过")
                    text='；'.join(parts)+'。'
                    self.status.configure(text=text,fg=t.OK if not result['skipped'] else t.WARN)
                    self.app.hint('缓存与内存清理完成')
                    self.app.say('✔ '+text)
                    for path,why in result['skipped'][:5]:
                        self.app.say('    已保留 '+Path(path).name+'：'+why,'warn')
        finally:
            if self.winfo_exists():self._poll_job=self.after(80,self.poll)

    def close(self):
        if self.busy:
            self.status.configure(text='正在处理，请完成后再关闭。');return
        if self._poll_job is not None:
            self.after_cancel(self._poll_job);self._poll_job=None
        self.grab_release();self.destroy()
