"""Small, reusable visual details. No configuration or business side effects."""
import tkinter as tk
from tkinter import ttk


def panel(parent, fill, border, radius=16):
    bg = parent.cget('background')
    outer = tk.Frame(parent, bg=bg, bd=0)
    canvas = tk.Canvas(outer, bg=bg, bd=0, highlightthickness=0, takefocus=False)
    canvas.place(x=0, y=0, relwidth=1, relheight=1)
    def draw(e):
        if e.widget is not outer: return
        w,h=e.width-1,e.height-3
        r=min(radius,w/2,h/2)
        canvas.delete('surface')
        if fill == bg and border == bg:
            return
        canvas.create_polygon(r,2,w-r,2,w,2,w,r+2,w,h-r+2,w,h+2,w-r,h+2,r,h+2,0,h+2,0,h-r+2,0,r+2,0,2,
                              smooth=True,splinesteps=24,fill='#eeebf2',outline='',tags='surface')
        canvas.create_polygon(r,0,w-r,0,w,0,w,r,w,h-r,w,h,w-r,h,r,h,0,h,0,h-r,0,r,0,0,
                              smooth=True,splinesteps=24,fill=fill,outline=border if border != '#f0e9ed' else '',tags='surface')
        canvas.tk.call('lower',canvas._w)
    outer.bind('<Configure>',draw,add='+')
    return outer


def install(root, style):
    import theme as t
    from PIL import Image, ImageDraw, ImageTk
    host=root or tk._default_root
    if getattr(host,'_soft_theme_installed',False): return
    refs=[]
    def tile(fill,outline=None,back=t.CARD,radius=9):
        image=Image.new('RGB',(144,144),back)
        ImageDraw.Draw(image).rounded_rectangle((1,1,142,142),radius=radius*3,
                                                fill=fill,outline=outline or fill,width=3)
        photo=ImageTk.PhotoImage(image.resize((48,48),Image.Resampling.LANCZOS),master=host)
        refs.append(photo); return photo
    variants={
        'TButton':('#f7f3f8','#f0e8f4','#e8ddef',t.INK,t.CARD),
        'Primary.TButton':(t.PRIMARY,'#765699',t.PRIMARY_HOVER,'white',t.CARD),
        'Ghost.TButton':(t.CARD,t.PRIMARY_SOFT,'#e9dcee',t.PRIMARY,t.CARD),
        'Mini.TButton':('#f7f3f8','#f0e8f4','#e8ddef',t.INK,t.CARD),
        'MiniDanger.TButton':('#fcf0ef','#fae3e1','#f4d4d3',t.BAD,t.CARD),
        'Danger.TButton':('#fcf0ef','#fae3e1','#f4d4d3',t.BAD,t.CARD),
        'Header.TButton':(t.PRIMARY_SOFT,'#e9dcee','#e1d1e8',t.PRIMARY,t.CARD),
        'Side.TButton':(t.CARD,t.PRIMARY_SOFT,'#e9dcee',t.INK,t.CARD),
        'NavTool.TButton':('#ffffff','#f4f0fa','#e9e1f3','#766b85','#ffffff'),
        'Nav.TButton':('#ffffff','#f4f0fa','#e9e1f3','#766b85','#ffffff'),
        'NavActive.TButton':('#eee8f8','#e6dcf3','#dacdec',t.PRIMARY,'#ffffff'),
    }
    for idx,(name,(base,hover,pressed,ink,back)) in enumerate(variants.items()):
        normal=tile(base,back=back)
        active=tile(hover,back=back); down=tile(pressed,back=back)
        disabled=tile('#eee9ee',back=back); focus=tile(base,t.PRIMARY,back)
        element=f'Buri{idx}.surface'
        style.element_create(element,'image',normal,('disabled',disabled),('pressed',down),
                             ('active',active),('focus',focus),border=8,sticky='nsew')
        style.layout(name,[(element,{'sticky':'nsew','children':[
            ('Button.padding',{'sticky':'nsew','children':[('Button.label',{'sticky':'nsew'})]})]})])
        style.configure(name,foreground=ink,background=back,padding=(15,8),font=t.F,
                        anchor='center',borderwidth=0)
        style.map(name,foreground=[('disabled','#a99ca8')])
    style.configure('Primary.TButton',font=t.F_BOLD,padding=(18,9))
    style.configure('MiniDanger.TButton',padding=(8,2),font=t.F_SMALL)
    style.configure('Mini.TButton',padding=(8,2),font=t.F_SMALL)
    style.configure('NavTool.TButton',padding=(4,2),font=t.F_SMALL)
    for name in ('Nav.TButton','NavActive.TButton'):
        style.configure(name,padding=(16,12),anchor='w',font=t.F_BOLD if name.startswith('NavActive') else t.F)
    normal=tile(t.CARD,t.BORDER_STRONG); focused=tile(t.CARD,t.PRIMARY)
    disabled=tile('#f3eff1',t.BORDER)
    style.element_create('Buri.entry','image',normal,('disabled',disabled),('focus',focused),border=8,sticky='nsew')
    style.layout('TEntry',[('Buri.entry',{'sticky':'nsew','children':[
        ('Entry.padding',{'sticky':'nsew','children':[('Entry.textarea',{'sticky':'nsew'})]})]})])
    style.configure('Time.TEntry',font=t.F_TIME)
    style.configure('TEntry',padding=(10,6),selectbackground=t.SELECT,selectforeground=t.INK)
    style.configure('Treeview',rowheight=38)
    style.configure('Treeview.Heading',background=t.CARD_ALT,foreground=t.MUTED,padding=(12,10))
    host._soft_theme_images=refs; host._soft_theme_installed=True


class Tooltip:
    def __init__(self,widget,text):
        self.widget,self.text=widget,text
        self.job=None; self.window=None
        widget.bind('<Enter>',self.schedule,add='+')
        for event in ('<Leave>','<ButtonPress>','<Destroy>'):
            widget.bind(event,self.hide,add='+')
    def schedule(self,_e=None):
        self.hide(); self.job=self.widget.after(600,self.show)
    def show(self):
        self.job=None
        if not self.widget.winfo_exists():return
        top=tk.Toplevel(self.widget); self.window=top
        top.overrideredirect(True); top.attributes('-topmost',True)
        text=self.text() if callable(self.text) else self.text
        tk.Label(top,text=text,bg='#463848',fg='#fffaf5',font=__import__('theme').F_SMALL,
                 padx=12,pady=8,wraplength=270,justify='left').pack()
        top.update_idletasks()
        x=min(self.widget.winfo_rootx(),top.winfo_screenwidth()-top.winfo_reqwidth()-16)
        y=self.widget.winfo_rooty()+self.widget.winfo_height()+7
        if y+top.winfo_reqheight()>top.winfo_screenheight()-40:
            y=self.widget.winfo_rooty()-top.winfo_reqheight()-7
        top.geometry(f'+{max(8,x)}+{max(8,y)}')
    def hide(self,_e=None):
        if self.job is not None:
            self.widget.after_cancel(self.job); self.job=None
        if self.window is not None:
            self.window.destroy(); self.window=None
