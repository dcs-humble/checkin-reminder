"""clip.py - exact Windows clipboard read/write for CF_UNICODETEXT.

WHY THIS EXISTS
    OCR of QQ's input box is TIMING-SENSITIVE: PrintWindow on this Chromium window can
    return a frame captured before the keystroke painted, so a single capture can show an
    EMPTY draft right after text was typed (this produced several false "typing failed"
    conclusions during recon).  Reading the draft through the clipboard is exact and
    timing-proof: write a sentinel, Ctrl+A / Ctrl+C, read back.  If the sentinel is still
    there the input box was empty; otherwise we get the literal draft text.

    The one requirement is that QQ has keyboard focus (qqsend.ensure_focus), because Ctrl+C
    goes to the focused window.

CTYPES GOTCHAS (both cost a crash during recon - do not "simplify" them away)
    * GetClipboardData.restype defaults to c_int, which TRUNCATES the HGLOBAL on 64-bit
      Python -> GlobalLock dereferences a bogus pointer -> access violation 0xC0000005
      with no traceback.  Every function below declares argtypes/restype.
    * SetClipboardData.argtypes must be [UINT, HANDLE]; with the default int conversion
      the HGLOBAL raises "OverflowError: int too long to convert".
    * GlobalLock can return NULL - guard it, and GlobalFree the buffer on every failure
      path, or the clipboard is left locked and later calls hang.
"""
import ctypes
import time
from ctypes import wintypes

u32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

u32.OpenClipboard.argtypes = [wintypes.HWND]
u32.OpenClipboard.restype = wintypes.BOOL
u32.CloseClipboard.argtypes = []
u32.CloseClipboard.restype = wintypes.BOOL
u32.EmptyClipboard.argtypes = []
u32.EmptyClipboard.restype = wintypes.BOOL
u32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
u32.IsClipboardFormatAvailable.restype = wintypes.BOOL
u32.GetClipboardData.argtypes = [wintypes.UINT]
u32.GetClipboardData.restype = wintypes.HANDLE
u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
u32.SetClipboardData.restype = wintypes.HANDLE

k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
k32.GlobalAlloc.restype = wintypes.HGLOBAL
k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
k32.GlobalLock.restype = ctypes.c_void_p
k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
k32.GlobalUnlock.restype = wintypes.BOOL
k32.GlobalFree.argtypes = [wintypes.HGLOBAL]
k32.GlobalFree.restype = wintypes.HGLOBAL


def get(tries=5):
    """Return the clipboard's unicode text, or '' if it holds none / cannot be opened."""
    for _ in range(tries):
        if not u32.OpenClipboard(None):
            time.sleep(0.25)
            continue
        try:
            if not u32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return ""
            h = u32.GetClipboardData(CF_UNICODETEXT)
            p = k32.GlobalLock(h) if h else None
            if not p:
                return ""
            try:
                return ctypes.c_wchar_p(p).value or ""
            finally:
                k32.GlobalUnlock(h)
        finally:
            u32.CloseClipboard()
    return ""


def put(text):
    """Replace the clipboard contents with `text`.  True on success."""
    data = (text + "\0").encode("utf-16-le")
    for _ in range(5):
        if not u32.OpenClipboard(None):
            time.sleep(0.25)
            continue
        h = None
        try:
            u32.EmptyClipboard()
            h = k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not h:
                return False
            p = k32.GlobalLock(h)
            if not p:                      # NULL guard - see module docstring
                k32.GlobalFree(h)
                return False
            ctypes.memmove(p, data, len(data))
            k32.GlobalUnlock(h)
            if not u32.SetClipboardData(CF_UNICODETEXT, h):
                k32.GlobalFree(h)
                return False
            h = None                       # ownership passed to the clipboard
            return True
        finally:
            u32.CloseClipboard()
    return False
