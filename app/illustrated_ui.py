"""Illustration assets and coordinated page treatments, with safe fallbacks."""
from pathlib import Path
from datetime import datetime, timezone, timedelta
import tkinter as tk
from tkinter import ttk
import theme as t

ASSETS=Path(__file__).parent/'assets'


def load(a):
    from PIL import Image,ImageTk
    a._mascot_images={}
    specs=[('icon','buri-app-v2.png',(48,48)),('brand','buri-app-v2.png',(42,42)),
           ('hero','buri-desk-v2.png',(230,164)),('settings','buri-organize-v2.png',(150,110)),
           ('logs','buri-journal-v2.png',(150,110)),('mini','buriburizaemon-v1.png',(45,58))]
    for key,name,size in specs:
        try:
            with Image.open(ASSETS/name) as source:
                image=source.convert('RGBA'); image.thumbnail(size,Image.Resampling.LANCZOS)
                a._mascot_images[key]=ImageTk.PhotoImage(image,master=a.root)
        except (OSError,ValueError,tk.TclError):
            continue
    if 'icon' in a._mascot_images:
        a.root.iconphoto(True,a._mascot_images['icon'])
    try:
        icon=ASSETS/'buri-app-v2.ico'
        if icon.is_file():a.root.iconbitmap(str(icon))
    except tk.TclError:
        pass


def page_heading(a,parent,key,title,description):
    from studio_ui import label
    bg=t.PRIMARY_SOFT
    border=t.PRIMARY_SOFT
    outer=t.round_panel(parent,bg,border,20)
    outer.pack(fill='x',padx=28,pady=(24,20))
    art=label(outer,'',bg=bg)
    if key in a._mascot_images:art.configure(image=a._mascot_images[key])
    art.pack(side='right',padx=(8,22),pady=12)
    text=tk.Frame(outer,bg=bg);text.pack(side='left',fill='both',expand=True,padx=(24,0),pady=22)
    label(text,'把日常的小事，照顾好。',10,t.PRIMARY,bg,True).pack(anchor='w')
    label(text,title,24,t.INK,bg).pack(anchor='w',pady=(9,7))
    note=label(text,description,10,t.MUTED,bg,wraplength=480,justify='left',anchor='w')
    note.pack(fill='x')
    outer.bind('<Configure>',lambda e:note.configure(wraplength=max(240,e.width-248)),add='+')
    return outer


def date_label(parent):
    from studio_ui import label
    now=datetime.now(timezone(timedelta(hours=8)))
    day='一二三四五六日'[now.weekday()]
    return label(parent,now.strftime('%m月%d日')+' · 周'+day,10,t.FAINT)


def line_icon(parent,kind,bg,ink,size=20):
    """Small code-native UI symbols, consistent stroke and cap treatment."""
    from PIL import Image,ImageDraw,ImageTk
    scale=3;im=Image.new('RGBA',(size*scale,size*scale),(0,0,0,0));d=ImageDraw.Draw(im)
    def line(points):d.line([(int(x*scale),int(y*scale)) for x,y in points],fill=ink,width=2*scale,joint='curve')
    if kind=='people':
        d.ellipse((6*scale,2*scale,13*scale,9*scale),outline=ink,width=2*scale)
        d.arc((2*scale,10*scale,17*scale,24*scale),180,360,fill=ink,width=2*scale)
    elif kind=='clock':
        d.ellipse((2*scale,2*scale,18*scale,18*scale),outline=ink,width=2*scale)
        line([(10,5),(10,10),(14,12)])
    elif kind=='link':
        d.rounded_rectangle((2*scale,6*scale,12*scale,14*scale),radius=4*scale,outline=ink,width=2*scale)
        d.rounded_rectangle((8*scale,6*scale,18*scale,14*scale),radius=4*scale,outline=ink,width=2*scale)
    elif kind=='grid':
        for x,y in [(2,2),(12,2),(2,12),(12,12)]:d.rounded_rectangle((x*scale,y*scale,(x+6)*scale,(y+6)*scale),radius=scale,outline=ink,width=2*scale)
    elif kind=='settings':
        for y,x in [(5,7),(10,14),(15,8)]:
            line([(2,y),(18,y)])
            d.ellipse(((x-2)*scale,(y-2)*scale,(x+2)*scale,(y+2)*scale),fill=bg,outline=ink,width=scale)
    else:
        d.rounded_rectangle((4*scale,2*scale,16*scale,18*scale),radius=2*scale,outline=ink,width=2*scale)
        line([(7,7),(13,7)]);line([(7,11),(13,11)]);line([(7,15),(11,15)])
    photo=ImageTk.PhotoImage(im.resize((size,size),Image.Resampling.LANCZOS),master=parent)
    return photo
