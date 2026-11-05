"""win.py - find, focus and drive the running QQ client with real input events.

WHY THIS EXISTS
  QQ NT exposes no accessibility tree (proven earlier: UIA children=0 on every QQ window,
  MSAA returns nameless stubs that recurse forever), so the only way to operate QQ is the way
  the requirements doc mandates: simulate a real user - focus the window, click, type, press
  keys.  This module holds those primitives plus the window finder.

DESIGN RULES (learned the hard way)
  * tasklist is unreliable here; enumerate windows and join pids to Get-Process instead.
  * Never guess a window: refuse to act unless a QQ window that is genuinely on screen
    (visible, un-cloaked, big enough) can be found.  Fail safe - the doc's edge case 4.
  * Chinese text must go through KEYEVENTF_UNICODE, not virtual keys.

Usage:
    python win.py list                 # every QQ window with geometry/visibility
    python win.py focus                # bring the QQ main window to the front
    python win.py ocr                  # OCR the QQ window (needs look.py)
    python win.py type "文本"           # type text into the focused window
    python win.py click X Y            # click a screen coordinate
    python win.py key 0x0D             # press a virtual key (e.g. Enter)
"""
import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes

if sys.stdout:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class WINDOWPLACEMENT(ctypes.Structure):
    """Used by normal_rect() to read the rect a minimised QQ window returns to."""
    _fields_ = [("length", wintypes.UINT), ("flags", wintypes.UINT),
                ("showCmd", wintypes.UINT), ("ptMinPosition", POINT),
                ("ptMaxPosition", POINT), ("rcNormalPosition", RECT)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


INPUT_KEYBOARD, INPUT_MOUSE = 1, 0
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0002, 0x0004
MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE = 0x0001, 0x8000
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
WHEEL_DELTA = 120

user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
user32.GetWindowPlacement.restype = wintypes.BOOL
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.PrintWindow.restype = wintypes.BOOL
user32.GetWindowDC.argtypes = [wintypes.HWND]
user32.GetWindowDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                            ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), wintypes.UINT]
gdi32.GetDIBits.restype = ctypes.c_int

SW_RESTORE, SW_SHOWNORMAL, SW_MINIMIZE, SW_SHOW = 9, 1, 6, 5
SW_HIDE = 0
SW_SHOWMINIMIZED = 2
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
HWND_TOP = 0
SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW = 0x0002, 0x0001, 0x0040
DWMWA_CLOAKED = 14

# PrintWindow flag that makes Chromium/Electron render its own GPU-composited surface into our
# DC.  Without it (flag 0) QQ returns a single-colour bitmap; and CopyFromScreen cannot be used
# at all while another window overlaps QQ - both proven by pw-capture.py.
PW_RENDERFULLCONTENT = 0x00000002
BI_RGB, DIB_RGB_COLORS = 0, 0


def list_windows():
    """Every top-level window with class, title, pid, visibility, cloaked flag and rect."""
    found = []
    dwm = ctypes.WinDLL("dwmapi", use_last_error=True)

    def cb(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, 512)
        cls = ctypes.create_unicode_buffer(512)
        user32.GetClassNameW(hwnd, cls, 512)
        r = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        cloaked = ctypes.c_int(0)
        try:
            dwm.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked),
                                      ctypes.sizeof(cloaked))
        except Exception:
            pass
        found.append({
            "hwnd": int(hwnd), "pid": pid.value, "title": title.value, "cls": cls.value,
            "visible": bool(user32.IsWindowVisible(hwnd)),
            "iconic": bool(user32.IsIconic(hwnd)),
            "cloaked": cloaked.value,
            "left": r.left, "top": r.top,
            "w": r.right - r.left, "h": r.bottom - r.top,
        })
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found


def process_name(pid):
    """Image file name for a pid, via ctypes only.

    WHY NOT subprocess/Get-Process: spawning `powershell -NoProfile` from inside the Python
    child of our own pwsh call HANGS (observed: a restore script sat with no output until it
    had to be killed).  Opening the process and querying its image name is instant and has no
    child-process risk, which matters because the shipped script must never wedge.
    """
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(h)


kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def _process_image(pid):
    """Full image path for a pid (process_name() only returns the file name)."""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(h)


def qq_pids():
    """Live QQ process ids, resolved without ever spawning a child process."""
    return {w["pid"] for w in list_windows() if process_name(w["pid"]).lower() == "qq.exe"}


def qq_windows(min_size=300):
    """QQ windows, ordered best-first.  No guessing: returns [] if none qualify.

    ORDER MATTERS, and two separate mistakes here have already cost a working QQ window:
      1. Sorting by area alone picks QQ's hidden offscreen surface over the panel actually on
         screen; focusing that hidden surface COLLAPSES the visible panel to (-32000,-32000).
      2. Among on-screen QQ windows, the 1440x753 title-less one belongs to QQ's embedded
         mini-browser (it was showing a bilibili page), NOT the chat client.  The chat main
         panel is the one titled exactly 'QQ'.

    So: drivable first, then titled-'QQ' first, then largest.  Do not reintroduce a plain
    area sort.
    """
    pids = qq_pids()
    wins = [w for w in list_windows()
            if w["pid"] in pids and w["cls"].startswith("Chrome_WidgetWin")
            and w["w"] >= min_size and w["h"] >= min_size]
    return sorted(wins, key=lambda w: (not (w["visible"] and not w["cloaked"]),
                                       w["title"] != "QQ",
                                       -(w["w"] * w["h"])))


def render_quality(hwnd, w=None, h=None):
    """(distinct_colours, white_fraction) for a window's rendered content, or None on failure.

    This is how we tell QQ's real chat panel from its never-painted helper surface: both are
    titled exactly 'QQ' and both are on screen, and WHICH of them is on top varies from moment to
    moment.  Ordering heuristics therefore picked the blank one and OCR'd nothing.  Painting
    the candidate and measuring its pixels decides it on evidence instead of on window order.
    """
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w = w or (r.right - r.left)
    h = h or (r.bottom - r.top)
    if w <= 0 or h <= 0:
        return None
    img = _get_dib(hwnd, w, h)
    if img is None:
        return None
    cols = img.getcolors(maxcolors=1 << 24)
    distinct = len(cols) if cols else (1 << 24)
    white = sum(c for c, px in cols if px == (255, 255, 255)) if cols else 0
    return distinct, white / float(w * h)


def pick_panel(min_size=300, min_distinct=300):
    """The QQ chat panel, chosen by what it actually renders.  None if none qualifies.

    Returns the winning window dict with `distinct`/`white` annotations, or None so the caller
    can fail safe (the doc's edge case 4) rather than type into a blank surface.
    """
    best = None
    for w in qq_windows(min_size):
        if not (w["visible"] and not w["cloaked"]):
            continue
        q = render_quality(w["hwnd"])
        if q is None or q[1] >= 0.90 or q[0] < min_distinct:
            continue
        w = dict(w, distinct=q[0], white=q[1])
        score = (w["title"] == "QQ", w["distinct"])
        if best is None or score > best[0]:
            best = (score, w)
    return best[1] if best else None


def normal_rect(hwnd):
    """The rect a window returns to when restored, plus its show state - or None.

    Why this matters: when QQ is minimised to the tray its window sits at (-32000,-32000,160x28)
    and renders BLANK, so pick_panel() cannot recognise it by its pixels and the fail-safe
    aborts the whole run.  GetWindowPlacement keeps reporting rcNormalPosition (960x640 at
    (625,272) for the real panel), so RESTORED geometry identifies the chat panel even while it
    is minimised.  Note the never-painted helper (hwnd 789018, 800x600) reports showCmd=1 and is
    therefore never a restore candidate.
    """
    wp = WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(WINDOWPLACEMENT)
    if not user32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
        return None
    r = wp.rcNormalPosition
    return {"left": r.left, "top": r.top, "w": r.right - r.left, "h": r.bottom - r.top,
            "showCmd": wp.showCmd, "minimized": wp.showCmd == SW_SHOWMINIMIZED}


def restore_candidates(min_size=300):
    """Every title-'QQ' window whose RESTORED size could be the chat panel, largest first.

    Both ways QQ NT (Electron) takes its panel off screen have to be covered:
      * minimised  -> IsIconic=True, rect (-32000,-32000,160x28), normal rect 960x640
      * hidden     -> IsWindowVisible=False, rect still 960x640, and (important) GetWindowPlacement
                      keeps reporting showCmd=SW_SHOWNORMAL, so 'showCmd' cannot tell them apart.
    Because showCmd lies, the caller does not pick ONE candidate: it tries them in order and
    verifies each by pixels, which is the only evidence that survives both shapes.  Tiny tray
    widgets (64x64) are excluded by the restored-size filter.

    2026-10-21 22:5x：第三种形状。QQ 收进托盘后，那个 960x640 的窗口**仍然 IsWindowVisible=True**
    （不是 minimized，rect 也在屏幕上），只是根本不绘制——截图里是它背后的桌面/浏览器。
    旧代码把它当"已经在屏幕上"直接排除，于是只剩那个从没画过的 800x600 辅助窗口可恢复，
    恢复出来是一块白板，find_panel 还是失败（GUI 就报"找不到 QQ 面板"）。
    所以现在：只按"恢复后的尺寸"筛，不管它当前 visible 与否（真正的面板早就被 pick_panel
    返回了，能走到这里的一定是画不出东西的）；再把从没绘制过的 800x600 辅助窗口排除掉。
    """
    out = []
    for w in qq_windows(1):
        if w["title"] != "QQ":
            continue
        nr = normal_rect(w["hwnd"])
        if not nr:
            continue
        if nr["w"] < min_size or nr["h"] < min_size:
            continue
        if (nr["w"], nr["h"]) == (800, 600):
            # 从没绘制过的辅助窗口（QQEX 同样是 800x600）。恢复它 = 白闪一下 + 白等 2 秒。
            continue
        out.append(dict(w, normal=nr, hidden=not w["visible"]))
    out.sort(key=lambda w: -(w["normal"]["w"] * w["normal"]["h"]))
    return out


def panel_diag(min_size=300):
    """One-line-per-window state dump, for the log when find_panel() gives up.

    2026-10-21 22:00 那次失败只留下一句「找不到 QQ 面板」，事后既不知道 QQ 到底开没开，
    也不知道是哪个窗口不达标。下次失败必须能直接从日志读出这些。
    """
    lines = []
    pids = qq_pids()
    lines.append("QQ 进程: %d 个 %s" % (len(pids), sorted(pids)))
    for w in qq_windows(1):
        nr = normal_rect(w["hwnd"]) or {}
        lines.append("  hwnd=%s title=%r vis=%s iconic=%s cloaked=%s rect=%sx%s normal=%sx%s@(%s,%s) showCmd=%s"
                     % (w["hwnd"], w["title"], w["visible"], w["iconic"], w["cloaked"],
                        w["w"], w["h"], nr.get("w"), nr.get("h"), nr.get("left"),
                        nr.get("top"), nr.get("showCmd")))
    return lines


QQ_EXE_CANDIDATES = (
    r"C:\Program Files\Tencent\QQNT\QQ.exe",
    r"C:\Program Files (x86)\Tencent\QQNT\QQ.exe",
    r"C:\Program Files\Tencent\QQ\Bin\QQ.exe",
    r"C:\Program Files (x86)\Tencent\QQ\Bin\QQ.exe",
)


def qq_exe():
    """QQ 可执行文件路径；找不到返回空串。

    先看正在跑的 QQ 进程（哪怕它只是在托盘里），再退回安装路径表。QQ NT 装在哪里
    每台机器都不一样，所以两路都试一遍，而不是写死一个路径。
    """
    for pid in qq_pids():
        path = _process_image(pid)
        if path and os.path.basename(path).lower() == "qq.exe":
            return path
    for cand in QQ_EXE_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    return ""


def start_qq():
    """QQ 没在运行时把它拉起来（自动登录通常几秒就进主界面）。返回是否真的启动了进程。"""
    if qq_pids():
        return True
    exe = qq_exe()
    if not exe:
        return False
    try:
        subprocess.Popen([exe], cwd=os.path.dirname(exe), close_fds=True,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        return True
    except Exception:                                    # noqa: BLE001
        return False


def wait_panel(timeout=60, min_size=300):
    """等 QQ 面板出现（刚启动时要等它自动登录），超时返回 None。"""
    deadline = time.time() + timeout
    while True:
        w = find_panel(min_size)
        if w is not None:
            return w
        if time.time() >= deadline:
            return None
        time.sleep(2.0)


def find_panel(min_size=300, min_distinct=300, try_restore=True):
    """The drivable QQ chat panel, restoring a minimised/hidden one if necessary.  None => fail safe.

    Order of preference: an already-visible painted panel (cheapest, never disturbs the user's
    desktop) first; only if that fails do we restore a candidate and re-measure.  A restore never
    types anything, and it is verified by pixels afterwards, so an unhelpful restore still ends in
    a clean fail-safe rather than blind input.

    Restoring is retried with increasing patience: QQ NT paints its first frame late, so measuring
    pixels 1.0s after ShowWindow sometimes lands on a black surface and the old code gave up
    (2026-10-21 22:00, the whole class got no reminder).  When the pixels stay blank we raise the
    window once (ALT + SetForegroundWindow, no typing) to force a repaint, then measure again.
    A candidate that still does not verify is put back off-screen before the next one is tried.
    """
    win = pick_panel(min_size, min_distinct)
    if win is not None or not try_restore:
        return win
    for cand in restore_candidates(min_size):
        print("  panel hwnd=%s is %s (normal %sx%s); restoring"
              % (cand["hwnd"], "hidden" if cand.get("hidden") else "minimised",
                 cand["normal"]["w"], cand["normal"]["h"]))
        user32.ShowWindow(cand["hwnd"], SW_SHOWNORMAL if cand.get("hidden") else SW_RESTORE)
        poked = False
        for wait in (0.35, 0.65, 1.10):
            time.sleep(wait)
            win = pick_panel(min_size, min_distinct)
            if win is not None:
                return win
            if not poked:
                poked = True                     # 只逼一次重绘，别把用户的焦点反复抢走
                try:
                    focus_window(cand["hwnd"], tries=1)
                except Exception:                # noqa: BLE001
                    pass
        if cand.get("hidden"):
            # 没认出来：把"本来是藏着的"这个状态还回去，别在桌面上留一个白框
            user32.ShowWindow(cand["hwnd"], SW_HIDE)
    return None




def focus_window(hwnd, tries=4):
    """Restore + bring to front, verified by GetForegroundWindow.

    Three separate Windows protections have to be beaten, each learned the hard way:
      * SW_RESTORE, never bare SW_SHOW: SW_SHOW on QQ's never-rendered helper surface (hwnd
        789018) produces a 99%-white blank window and leaves the real panel minimised - a
        mistake that already collapsed this user's QQ window to (-32000,-32000,160x28) once.
      * the foreground lock: SetForegroundWindow is refused unless our process counts as
        recently active, so a synthetic ALT press is emitted first.
      * AttachThreadInput to the CURRENT foreground thread so the switch is attributed to a
        thread that is allowed to make it.

    Returns True only if the target truly became foreground; the caller must treat False as
    "do not type anything" (doc edge case 4: fail safe rather than act blindly).
    """
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.ShowWindow(hwnd, SW_SHOWNORMAL)

    for _ in range(tries):
        if user32.GetForegroundWindow() == hwnd:
            return True

        # ALT down/up marks this process as "recently active", which is the documented unlock for
        # SetForegroundWindow when the caller is not already the foreground process.
        press_vk(VK_MENU)
        time.sleep(0.05)

        fg = user32.GetForegroundWindow()
        target_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        our_thread = kernel32.GetCurrentThreadId()
        attached = False
        if target_thread and target_thread != our_thread:
            attached = bool(user32.AttachThreadInput(target_thread, our_thread, True))
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(target_thread, our_thread, False)
        time.sleep(0.45)

    ok = user32.GetForegroundWindow() == hwnd
    # Drop topmost only after we really own the foreground, so a failed attempt never leaves the
    # user's z-order disturbed.
    if ok:
        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        time.sleep(0.2)
    return ok


def _get_dib(hwnd, w, h, flag=PW_RENDERFULLCONTENT):
    """Render `hwnd` into an offscreen bitmap; return a PIL Image or None.

    This is the occlusion-proof capture path: CopyFromScreen copies the composited desktop, so
    while the Harness (or any) window overlaps QQ we could only ever OCR the window on top.
    PrintWindow(PW_RENDERFULLCONTENT) asks QQ to draw itself and ignores z-order completely.
    """
    from PIL import Image
    hdc = user32.GetWindowDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mem, bmp)
    try:
        if not user32.PrintWindow(hwnd, mem, flag):
            return None
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h      # negative => top-down, so y matches screen order
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = BI_RGB
        buf = ctypes.create_string_buffer(w * h * 4)
        if gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), DIB_RGB_COLORS) == 0:
            return None
        return Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(hwnd, hdc)


def capture_window(hwnd, path):
    """Save an occlusion-proof PNG of `hwnd`; returns (path, note)."""
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        return None, f"zero-size rect ({w}x{h})"
    img = _get_dib(hwnd, w, h)
    if img is None:
        return None, "PrintWindow failed"
    img.save(path)
    return path, f"{w}x{h}"


def screen_size():
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def _send(inp):
    n = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    if n != 1:
        raise ctypes.WinError(ctypes.get_last_error())


def type_text(text, per_char=0.012):
    """Type unicode text as real key events.  KEYEVENTF_UNICODE is what makes CJK work.

    The scan code field is a 16-bit WORD, so a non-BMP character (the reminder copy ends with
    the emoji 👇 = U+1F447) cannot be sent as one ord().  Window's KEYEVENTF_UNICODE contract is
    UTF-16, so encode first and emit one event per CODE UNIT; a non-BMP char then becomes its
    surrogate pair automatically, which is exactly what a real keyboard produces.
    """
    raw = text.encode("utf-16-le")
    units = [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]
    for cu in units:
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            inp = INPUT(type=INPUT_KEYBOARD)
            inp.ki = KEYBDINPUT(wVk=0, wScan=cu, dwFlags=flags, time=0, dwExtraInfo=0)
            _send(inp)
        time.sleep(per_char)


def press_vk(vk, times=1, gap=0.05):
    for _ in range(times):
        for flags in (0, KEYEVENTF_KEYUP):
            inp = INPUT(type=INPUT_KEYBOARD)
            inp.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)
            _send(inp)
        time.sleep(gap)


VK_CONTROL, VK_SHIFT, VK_MENU = 0x11, 0x10, 0x12
VK_A, VK_C, VK_V, VK_RETURN, VK_ESCAPE = 0x41, 0x43, 0x56, 0x0D, 0x1B
VK_DELETE, VK_BACK = 0x2E, 0x08
VK_END, VK_HOME, VK_SPACE = 0x23, 0x24, 0x20


def hotkey(*vks, gap=0.04):
    """Press a chord: all keys down in order, then released in reverse (e.g. Ctrl+A)."""
    for vk in vks:
        inp = INPUT(type=INPUT_KEYBOARD)
        inp.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=0, time=0, dwExtraInfo=0)
        _send(inp)
        time.sleep(gap)
    for vk in reversed(vks):
        inp = INPUT(type=INPUT_KEYBOARD)
        inp.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=KEYEVENTF_KEYUP, time=0, dwExtraInfo=0)
        _send(inp)
        time.sleep(gap)


def clear_input():
    """Ctrl+A then Delete - empties the message box so a test never leaves a draft behind."""
    hotkey(VK_CONTROL, VK_A)
    press_vk(VK_DELETE)


def click(x, y, settle=0.25):
    """Absolute mouse click at screen coordinates."""
    sw, sh = screen_size()
    nx, ny = int(x * 65535 / (sw - 1)), int(y * 65535 / (sh - 1))
    mv = INPUT(type=INPUT_MOUSE)
    mv.mi = MOUSEINPUT(dx=nx, dy=ny, mouseData=0,
                       dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, time=0, dwExtraInfo=0)
    _send(mv)
    time.sleep(0.05)
    for flag in (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP):
        dn = INPUT(type=INPUT_MOUSE)
        dn.mi = MOUSEINPUT(dx=nx, dy=ny, mouseData=0,
                           dwFlags=flag | MOUSEEVENTF_ABSOLUTE, time=0, dwExtraInfo=0)
        _send(dn)
    time.sleep(settle)


def wheel(notches, x=None, y=None, settle=0.35):
    """Scroll the mouse wheel at (x, y) - positive notches scroll up, negative down.

    WHY THIS EXISTS: the group's member list (32 rows) and the mention picker's full list cannot
    be enumerated from a single screenshot; they have to be scrolled.  Windows delivers a wheel
    event to whatever window is under the CURSOR, so the pointer MUST be moved to (x, y) first -
    that is why this helper moves before it scrolls instead of scrolling at the last position.
    """
    if x is not None and y is not None:
        sw, sh = screen_size()
        nx, ny = int(x * 65535 / (sw - 1)), int(y * 65535 / (sh - 1))
        mv = INPUT(type=INPUT_MOUSE)
        mv.mi = MOUSEINPUT(dx=nx, dy=ny, mouseData=0,
                           dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, time=0, dwExtraInfo=0)
        _send(mv)
        time.sleep(0.15)
    remaining = int(notches)
    while remaining:
        step = max(-10, min(10, remaining))
        remaining -= step
        inp = INPUT(type=INPUT_MOUSE)
        inp.mi = MOUSEINPUT(dx=0, dy=0, mouseData=step * WHEEL_DELTA,
                            dwFlags=MOUSEEVENTF_WHEEL, time=0, dwExtraInfo=0)
        _send(inp)
        time.sleep(settle)


def ocr_window(win, tag="qq", region=None):
    """OCR a QQ window and return the text, WITHOUT depending on z-order.

    Captures via PrintWindow(PW_RENDERFULLCONTENT) and OCRs that PNG through ocr.py's
    --from-png path.  A plain screen grab is useless here: any window overlapping QQ (the
    Harness window does, constantly) would be what got OCR'd instead.

    Every call gets fresh, unique temp files under logs\\probe\\, and the OCR exit code is
    checked: previously a crashed child left last night's txt in lib\\ and we happily typed
    into QQ using yesterday's screen.
    """
    d = os.path.join(HERE, "..", "logs", "probe")
    os.makedirs(d, exist_ok=True)
    stem = "%s-%d-%d" % (tag, os.getpid(), int(time.time() * 1000) % 10000000)
    png = os.path.join(d, stem + ".png")
    out = os.path.join(d, stem + ".txt")
    path, note = capture_window(win["hwnd"], png)
    if path is None:
        return "# capture failed: %s\n" % note
    try:
        os.remove(out)                       # 先删，子进程没写出东西就绝不会读到旧文件
    except OSError:
        pass
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        r = subprocess.run([PY, os.path.join(HERE, "ocr.py"), "--from-png", path,
                            "--out", out, "--png", png],
                           capture_output=True, encoding="utf-8", errors="replace",
                           env=env, timeout=180)
    except subprocess.TimeoutExpired:
        return "# OCR timed out\n"
    if r.returncode != 0:
        return "# OCR failed rc=%s: %s\n" % (
            r.returncode, (r.stderr or r.stdout or "").strip()[-400:])
    try:
        with open(out, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        return "# OCR produced no output: %s\n" % e


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["list", "focus", "ocr", "type", "click", "key"])
    ap.add_argument("args", nargs="*")
    a = ap.parse_args()

    if a.cmd == "list":
        pids = sorted(qq_pids())
        print(f"live QQ pids: {pids}")
        wins = qq_windows(min_size=1)
        print(f"QQ windows (Chrome_WidgetWin, any size): {len(wins)}")
        for w in wins:
            q = render_quality(w["hwnd"]) if w["visible"] and not w["cloaked"] else None
            qs = f"distinct={q[0]:<7} white={q[1]:.1%}" if q else "not rendered"
            print(f"  hwnd={w['hwnd']:>10} pid={w['pid']:<6} vis={str(w['visible']):<5} "
                  f"iconic={str(w['iconic']):<5} cloaked={w['cloaked']} "
                  f"rect=({w['left']},{w['top']},{w['w']},{w['h']}) "
                  f"title={w['title']!r} {qs}")
        win = pick_panel(300)
        print("PICKED PANEL: " + (f"hwnd={win['hwnd']} distinct={win['distinct']}"
                                  if win else "none (fail safe)"))
        cands = restore_candidates(1)
        if cands:
            print(f"restorable minimised QQ panels: {len(cands)}")
            for c in cands:
                n = c["normal"]
                print(f"  hwnd={c['hwnd']} normal=({n['left']},{n['top']},{n['w']}x{n['h']}) "
                      f"area={n['w'] * n['h']}")
        return

    win = find_panel(300)
    if not win:
        print("NO DRIVABLE QQ PANEL FOUND - refusing to act (fail safe).")
        print("Every QQ window is off screen, cloaked, or renders blank even after a restore.")
        print("Open QQ's main panel on the class group, then retry.")
        sys.exit(3)
    print(f"panel hwnd={win['hwnd']} title={win['title']!r} "
          f"rect=({win['left']},{win['top']},{win['w']},{win['h']}) "
          f"distinct={win['distinct']} white={win['white']:.1%}")

    if a.cmd == "focus":
        ok = focus_window(win["hwnd"])
        print(f"focused hwnd={win['hwnd']} -> {ok}")
        print(f"foreground now = {user32.GetForegroundWindow()}")
    elif a.cmd == "ocr":
        print(ocr_window(win))
    elif a.cmd == "type":
        focus_window(win["hwnd"])
        type_text(" ".join(a.args))
        print("typed")
    elif a.cmd == "click":
        x, y = int(a.args[0]), int(a.args[1])
        click(x, y)
        print(f"clicked ({x},{y})")
    elif a.cmd == "key":
        focus_window(win["hwnd"])
        press_vk(int(a.args[0], 0))
        print("pressed")


if __name__ == "__main__":
    main()
