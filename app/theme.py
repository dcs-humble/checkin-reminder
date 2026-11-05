"""Shared visual tokens and widgets for the check-in desktop application."""
import tkinter as tk
from tkinter import ttk

# ------------------------------------------------------------------ 颜色
# 中性底色 + 单一强调色。红黄绿只承担状态语义，不参与装饰。
BG = "#f6f5f8"            # 窗口底色（中性灰，不偏蓝）
CARD = "#ffffff"          # 卡片
CARD_ALT = "#f8f7fb"      # 卡片内的次级底（表头、斑马纹）
BORDER = "#e9e6ef"        # 1px 描边（压到几乎看不见，靠留白分层）
BORDER_STRONG = "#ded9e8"  # 需要强调的描边（输入框、日志框）
HEADER = "#ffffff"        # 顶部深色条（近黑）
HEADER_SUB = "#827383"

INK = "#30283f"           # 主文字
MUTED = "#716a80"         # 次要文字
FAINT = "#81788e"         # 更弱的提示

PRIMARY = "#7051a6"
PRIMARY_HOVER = "#573a89"
PRIMARY_SOFT = "#eee8f8"

OK = "#0d7a55"
OK_BG = "#e9f7f0"
WARN = "#9a6a00"
WARN_BG = "#fdf6e7"
BAD = "#c0332f"
BAD_BG = "#fdeeee"

LOG_BG = "#f8f7fb"
SELECT = "#e7def5"

# ------------------------------------------------------------------ 字体
FAMILY = "MiSans"
HEADING = "Noto Sans SC Medium"
NUMBERS = "Segoe UI Semibold"
MONO = "Cascadia Mono"


def face(size=11, bold=False):
    # Use a real medium face instead of synthetic bold for Chinese headings.
    return (HEADING if bold else FAMILY, size, "normal")


def configure_fonts(root):
    from tkinter import font as tkfont
    global FAMILY, HEADING, NUMBERS, MONO
    global F, F_SMALL, F_BOLD, F_H1, F_H2, F_SECTION, F_SUB, F_MONO, F_TIME
    available={name.casefold():name for name in tkfont.families(root)}
    def pick(*names):
        return next((available[n.casefold()] for n in names if n.casefold() in available),"TkDefaultFont")
    FAMILY=pick("MiSans","Microsoft YaHei UI","Microsoft YaHei")
    HEADING=pick("Noto Sans SC Medium","MiSans","Microsoft YaHei UI")
    NUMBERS=pick("Segoe UI Semibold","Segoe UI",FAMILY)
    MONO=pick("Cascadia Mono","Consolas")
    F=face(11); F_SMALL=face(10); F_BOLD=face(11,True)
    F_H1=face(22,True); F_H2=face(12,True); F_SECTION=face(10,True)
    F_SUB=face(10); F_MONO=face(10); F_TIME=(NUMBERS,12)
    # ttk.Entry uses the TkTextFont widget option, not the style's font token.
    for name,spec in (("TkDefaultFont",F),("TkTextFont",F),("TkMenuFont",F),
                      ("TkHeadingFont",F_BOLD),("TkTooltipFont",F_SMALL)):
        try:
            tkfont.nametofont(name,root=root).configure(family=spec[0],size=spec[1],weight="normal")
        except tk.TclError:
            pass


F=face(11); F_SMALL=face(10); F_BOLD=face(11,True)
F_H1=face(22,True); F_H2=face(12,True); F_SECTION=face(10,True)
F_SUB=face(10); F_MONO=face(10); F_TIME=(NUMBERS,12)

# 间距阶梯：全界面只用这几个数，避免每处手调出 3px 的参差
PAD_XS, PAD_S, PAD_M, PAD_L, PAD_XL = 4, 8, 12, 16, 24


def apply(root=None):
    """把 ttk 样式装进当前解释器。重复调用无副作用。"""
    configure_fonts(root)
    s = ttk.Style(root)
    try:
        s.theme_use("clam")
    except tk.TclError:
        pass

    s.configure(".", font=F, background=BG, foreground=INK,
                bordercolor=BORDER, focuscolor=PRIMARY_SOFT)

    # --- 容器
    s.configure("TFrame", background=BG)
    s.configure("Card.TFrame", background=CARD)
    s.configure("Alt.TFrame", background=CARD_ALT)
    s.configure("Header.TFrame", background=HEADER)

    # --- 文字
    s.configure("TLabel", background=BG, foreground=INK, font=F)
    s.configure("Card.TLabel", background=CARD, foreground=MUTED, font=F)
    s.configure("CardMuted.TLabel", background=CARD, foreground=MUTED, font=F_SMALL)
    s.configure("CardTitle.TLabel", background=CARD, foreground=INK, font=F_H2)
    s.configure("H1.TLabel", background=HEADER, foreground=INK, font=F_H1)
    s.configure("H1Sub.TLabel", background=HEADER, foreground=HEADER_SUB, font=F_SUB)
    s.configure("Hint.TLabel", background=BG, foreground=FAINT, font=F_SMALL)
    s.configure("CardHint.TLabel", background=CARD, foreground=FAINT, font=F_SMALL)
    s.configure("Ok.TLabel", background=CARD, foreground=OK, font=F_SMALL)
    s.configure("Bad.TLabel", background=CARD, foreground=BAD, font=F_SMALL)

    # --- 输入：静止时只有一条浅描边，聚焦才出现强调色细环
    s.configure("TEntry", fieldbackground="#ffffff", foreground=INK,
                bordercolor=BORDER_STRONG, lightcolor=BORDER_STRONG,
                darkcolor=BORDER_STRONG, insertcolor=INK, padding=(12, 10))
    s.map("TEntry", bordercolor=[("focus", PRIMARY)],
          lightcolor=[("focus", PRIMARY)], darkcolor=[("focus", PRIMARY)])
    s.configure("TSpinbox", fieldbackground="#ffffff", bordercolor=BORDER_STRONG,
                arrowsize=12, padding=(8, 5))

    s.configure("TCheckbutton", background=CARD, foreground=INK, font=F,
                focuscolor=CARD)
    s.map("TCheckbutton", background=[("active", CARD)],
          indicatorcolor=[("selected", PRIMARY), ("!selected", "#ffffff")])

    # --- 按钮：全部无边框。主按钮实心，其余靠 hover 变色表达可点
    s.configure("Primary.TButton", background=PRIMARY, foreground="#ffffff",
                font=F_BOLD, borderwidth=0, focusthickness=1, relief="flat",
                padding=(20, 10))
    s.map("Primary.TButton",
          background=[("pressed", PRIMARY_HOVER), ("active", PRIMARY_HOVER),
                      ("disabled", "#c2cef0")],
          foreground=[("disabled", "#f2f5ff")])

    s.configure("TButton", background="#f0f2f8", foreground=INK, font=F,
                borderwidth=0, focusthickness=1, relief="flat", padding=(14, 8))
    s.map("TButton",
          background=[("pressed", "#e0e5f2"), ("active", "#e8ecf5"),
                      ("disabled", "#f6f7f9")],
          foreground=[("disabled", FAINT)])

    s.configure("Side.TButton", background=CARD, foreground=INK, font=F,
                borderwidth=0, focusthickness=1, relief="flat", padding=(12, 10),
                anchor="w")
    s.map("Side.TButton",
          background=[("pressed", PRIMARY_SOFT), ("active", "#f1f3fa")])

    s.configure("Ghost.TButton", background=CARD, foreground=PRIMARY, font=F,
                borderwidth=0, focusthickness=1, relief="flat", padding=(10, 6))
    s.map("Ghost.TButton", background=[("active", PRIMARY_SOFT)],
          foreground=[("disabled", FAINT)])

    s.configure("Danger.TButton", background=BAD_BG, foreground=BAD, font=F,
                borderwidth=0, focusthickness=1, relief="flat", padding=(14, 8))
    s.map("Danger.TButton", background=[("active", "#fadcdc")])

    # 侧栏里那排小按钮（新建 / 复制 / 删除）
    s.configure("Mini.TButton", background="#f0f2f8", foreground=INK,
                font=F_SMALL, borderwidth=0, focusthickness=1, relief="flat",
                padding=(10, 8))
    s.map("Mini.TButton",
          background=[("pressed", "#e0e5f2"), ("active", "#e8ecf5"),
                      ("disabled", "#f6f7f9")])
    s.configure("MiniDanger.TButton", background=BAD_BG, foreground=BAD,
                font=F_SMALL, borderwidth=0, focusthickness=1, relief="flat",
                padding=(10, 8))
    s.map("MiniDanger.TButton", background=[("active", "#fadcdc")])

    s.configure("Header.TButton", background=PRIMARY_SOFT, foreground=PRIMARY,
                font=F_SMALL, borderwidth=0, focusthickness=1, relief="flat",
                padding=(14, 8))
    s.map("Header.TButton", background=[("active", SELECT)])

    # --- 计划任务表：去掉网格线，只留斑马纹和一条表头细线
    s.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=INK,
                rowheight=38, borderwidth=0, font=F)
    s.configure("Treeview.Heading", background=CARD_ALT, foreground=MUTED,
                font=F_SECTION, relief="flat", borderwidth=0, padding=(10, 8))
    s.map("Treeview.Heading", background=[("active", CARD_ALT)])
    s.map("Treeview", background=[("selected", SELECT)], foreground=[("selected", INK)])

    # --- 滚动条：细、无箭头，不抢视线
    s.configure("Vertical.TScrollbar", background="#d8dce4", troughcolor=CARD,
                borderwidth=0, arrowsize=10, gripcount=0, width=8)
    s.map("Vertical.TScrollbar", background=[("active", "#c3c9d4")])

    # --- 分隔线
    s.configure("TSeparator", background=BORDER)
    s.configure("Busy.Horizontal.TProgressbar", background=PRIMARY, troughcolor=PRIMARY_SOFT, borderwidth=0, thickness=4)
    from theme_details import install
    install(root, s)
    return s


# ------------------------------------------------------------------ 小部件
def round_panel(parent, fill=CARD, border=BORDER, radius=16):
    from theme_details import panel
    return panel(parent, fill, border, radius)


def card(parent, title=None, subtitle=None, pad=(22, 18), bg=CARD):
    """一张白卡片：1px 描边 + 标题行，返回 (外框, 内容框)。

    标题行和内容区分成两个兄弟 Frame（不是父子）：内容区里有的卡片用 grid
    排字段，有的用 pack 排列表，一个容器里只能选一种几何管理器。

    标题行版式：`标题 │ 说明`——中间一条竖细线把"这是什么"和"注意什么"分开，
    比让说明文字紧贴着标题跑更容易读。

    垂直开销刻意压到 48px 左右（上 10 + 标题 18 + 间距 8 + 下 12）：
    四张卡片叠在一起时，每张多 14px 就是 56px，1080p 屏上正好把
    「刚才发生了什么」整块挤出去。
    """
    px, py = pad
    outer = round_panel(parent, bg, BORDER)
    head = None
    if title:
        head = tk.Frame(outer, bg=bg)
        head.pack(fill="x", padx=px, pady=(py - 4, 0))
        tk.Label(head, text=title, bg=bg, fg=INK, font=F_H2,
                 anchor="w").pack(side="left")
        if subtitle:
            tk.Label(head, text=subtitle, bg=bg, fg=FAINT, font=F_SMALL,
                     anchor="w").pack(side="left", padx=(PAD_M, 0))
    inner = tk.Frame(outer, bg=bg)
    inner.pack(fill="both", expand=True, padx=px, pady=(PAD_S, py - 2))
    if head is not None:
        inner.head = head  # type: ignore[attr-defined]
    return outer, inner


def pill(parent, text="", level=None):
    """小胶囊标签（状态）。返回 (label, setter)。"""
    colors = {
        "ok": (OK_BG, OK), "warn": (WARN_BG, WARN),
        "bad": (BAD_BG, BAD), None: (PRIMARY_SOFT, PRIMARY),
    }

    from tkinter import font as tkfont
    from PIL import Image, ImageDraw, ImageTk
    font=tkfont.Font(font=face(10,True))
    lb=tk.Label(parent,bg=parent.cget('background'),font=font,bd=0,padx=0,pady=0,compound='center')
    def set_(txt,lv=None):
        b,f=colors.get(lv,colors[None])
        w,h=font.measure(txt)+24,font.metrics('linespace')+12
        tile=Image.new('RGBA',(w*3,h*3),(0,0,0,0))
        ImageDraw.Draw(tile).rounded_rectangle((0,0,w*3-1,h*3-1),radius=h*1.5,fill=b)
        photo=ImageTk.PhotoImage(tile.resize((w,h),Image.Resampling.LANCZOS),master=parent)
        lb._badge_photo=photo
        lb.configure(text=txt,image=photo,fg=f)
    set_(text,level)
    return lb,set_


def hline(parent, bg=BORDER):
    return tk.Frame(parent, bg=bg, height=1)


class Switch(tk.Canvas):
    """自绘开关（替代 ttk.Checkbutton）。

    clam 主题的勾选框在选中时会被画成一个灰底黑叉，跟旁边的"删除 ×"长得一样，
    用户根本分不清哪是开关哪是删除。自己画一个胶囊开关，语义一眼就明白。
    """

    def __init__(self, parent, variable, bg=CARD, w=40, h=22,
                 on=PRIMARY, off="#ded4df"):
        tk.Canvas.__init__(self, parent, width=w, height=h, bg=bg,
                           highlightthickness=1, highlightbackground=bg,
                           highlightcolor=PRIMARY, takefocus=True, bd=0, cursor="hand2")
        self.var = variable
        self.w, self.h = w, h
        self.on, self.off = on, off
        self.bind("<Button-1>", self._toggle)
        self.bind("<space>", self._toggle)
        self.bind("<Return>", self._toggle)
        try:
            self._trace = variable.trace_add("write", lambda *_a: self._draw())
            self.bind("<Destroy>", self._cleanup, add="+")
        except Exception:                                     # noqa: BLE001
            pass
        self._draw()

    def _cleanup(self, event):
        if event.widget is self and hasattr(self, "_trace"):
            self.var.trace_remove("write", self._trace)

    def _toggle(self, _e=None):
        self.var.set(not bool(self.var.get()))

    def _draw(self):
        from PIL import Image, ImageDraw, ImageTk
        on=bool(self.var.get()); color=self.on if on else self.off
        image=Image.new('RGBA',(self.w*3,self.h*3),(0,0,0,0))
        draw=ImageDraw.Draw(image)
        draw.rounded_rectangle((0,0,self.w*3-1,self.h*3-1),radius=self.h*1.5,fill=color)
        r=self.h/2; cx=self.w-r if on else r; kr=r-3
        draw.ellipse(((cx-kr)*3,(r-kr)*3,(cx+kr)*3,(r+kr)*3),fill='white',outline='#f0e9f1',width=2)
        self._switch_photo=ImageTk.PhotoImage(image.resize((self.w,self.h),Image.Resampling.LANCZOS),master=self)
        self.delete('all'); self.create_image(0,0,image=self._switch_photo,anchor='nw')
