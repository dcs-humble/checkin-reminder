"""at.py - mention classmates by GROUP CARD SUFFIX, committing with Enter (no clicking).

THE PROCEDURE (the user's, 10-20; refs m00471/m00472, restated today in m00049)
------------------------------------------------------------------------------
1. Open QQ and make sure the open conversation is 道桥2024.02班 (switch if it is not).
2. Type '@' + the last four digits of the student number.
3. The picker shows exactly one person - DO NOT CLICK IT (clicking a few pixels off lands in
   the chat history; that is how a check-in link got opened by accident).
4. Press Enter.  That takes the member and leaves a chip in the message box.
5. Review the first person, then do the rest, then review everything again at the end.

Per the user's instruction today (m00049) the popup itself is NOT inspected: type, wait a
moment, Enter, then verify the chip.  No pixel geometry, no OCR of the popup.

2026-10-21 23:2x 用户再次定调（原话）："现在不加验证了，就按照我们打字+回车就行，
担心的话，最后打完了在验证"。所以默认 `DCS_AT_PICKER=blind`：打字 → 等 SETTLE → 回车 →
逐个核对 chip，**最后**在 build() 收尾时按草稿统一核对一遍（rep["missing"]）。曾经试过的
"先 OCR 确认弹窗再回车"（verify 模式）在这台机器上不成立：@ 弹窗是独立顶层窗口，PrintWindow
抓不到；改用抓屏后弹窗那行小字仍被 OCR 读成 "202411％"，四位学号认不出来，反而把人漏掉。

WHY THE FOUR DIGITS
-------------------
Group cards are '学号后四位 + 姓名 + 寝室号' ('0615刁闯胜T086031'), so the four digits are unique
inside the group and filter the picker down to one row.  Friends render the remark the user gave
them ('0DisequilibriumG-F-N') instead of the card, which is why filtering by NAME never works -
the digits always do.

THE ONE DANGER, AND THE GUARD THAT STAYS
----------------------------------------
Enter means "take the highlighted member" when the picker is open, but "SEND THE DRAFT" when it
is not.  So the picker is never trusted: after every Enter the draft is read back through the
clipboard (clip.py - OCR cannot read the message box at all) and the chip must be there
verbatim, e.g. '@0615刁闯胜T086031 '.  Three outcomes, and they are not the same:

  chip found          -> the mention landed; carry on.
  draft non-empty     -> a chip for someone else, or no mention at all; one Backspace removes a
  but no chip            chip (a chip is one character in the box) and the person is recorded
                         as failed.
  draft EMPTY         -> the draft is gone, so ENTER SENT IT.  That is fatal: the run stops
                         immediately instead of typing 27 more mentions into a sent chat.

CLI
---
    python at.py chat                      # where are we? which conversation is open?
    python at.py one 0615 --name 刁闯胜      # compose one mention, verify the chip, clear
    python at.py build --text "..." --ids 0615:刁闯胜,0607:马驰
                                           # compose the whole thing, print the draft
    python at.py build ... --send          # only after the draft is reviewed (+ --yes to skip
                                           # the confirmation prompt)
"""
import argparse
import asyncio
import io
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
if sys.stdout:                      # pythonw 下 sys.stdout 是 None，直接 reconfigure 会崩
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import clip          # noqa: E402
import ocr as ocrmod  # noqa: E402
import win           # noqa: E402

SENT = "DCS-AT-SENTINEL"
TITLE_Y = 60                      # the conversation title sits above this y
# 这两个等待可以用环境变量调（2026-10-21）。默认值是在这台机器上量出来的：
# 29 人 ≈ 50 秒，其中 0.55 秒/人 是等 @ 弹窗。机器变慢就调大，变快也别调太小——
# 弹窗没来得及出来，那一下按回车就可能是"发送"而不是"选人"。
SETTLE = float(os.environ.get("DCS_AT_SETTLE") or 0.75)
GAP = float(os.environ.get("DCS_AT_GAP") or 0.12)
# @弹窗的确认方式（2026-10-21 23:2x 用户定调）：
#   blind = 打字 → 等一下 → 直接回车 → 用草稿里的 chip 事后验证（默认，用户要的就是这个）
#   verify = 先用 OCR 确认弹窗那一行再回车（老路子；实测这台机器上弹窗小字读不全，
#            "2024110606" 稳定被读成 "202411％"，所以不做默认）
PICKER = (os.environ.get("DCS_AT_PICKER") or "blind").strip().lower()
PICKER_TRIES = int(os.environ.get("DCS_AT_PICKER_TRIES") or 10)
PICKER_POLL = float(os.environ.get("DCS_AT_PICKER_POLL") or 0.22)
# 一个人没确认成功时，把打进去的 "@学号" 擦掉再来一次（最多这么多轮）。
# 2026-10-21 22:44：6 个人里 2 个没 @ 上，而 OCR 是时好时坏的（弹窗行偶尔被并进
# 上一条聊天记录里 -> 长度超限被跳过 / 同时匹配到两行 -> 判"看不准"）。一次抖动不该
# 让这个人从名单里掉出去，所以同一个人重试，而不是立刻放弃。
PICKER_RETRY = int(os.environ.get("DCS_AT_PICKER_RETRY") or 2)
# 连续几个人都看不见弹窗 => OCR 本身有问题（引擎/缩放/窗口尺寸变了）。继续每人白等
# 10×0.22s 会拖过 22:00，所以熔断：后面的人直接记为"没 @ 上"，不打字也不回车。
PICKER_BREAK = int(os.environ.get("DCS_AT_PICKER_BREAK") or 3)
# 兜底：如果整个进程里弹窗一次都没被认出来（OCR 引擎/缩放/QQ 版本变了），说明 verify
# 这条路在这台机器上不成立——第一次确认失败后就地退回 blind（老行为），保证提醒照发。
# 设 DCS_AT_PICKER_FALLBACK=0 可以关掉这个兜底（那时宁可一个都不 @ 也不盲发）。
PICKER_FALLBACK = (os.environ.get("DCS_AT_PICKER_FALLBACK") or "1") != "0"
_picker_misses = 0
_picker_blind = False       # 本次运行余下的人都退回盲等
_ever_confirmed = False     # 本次运行里有没有人真的被 OCR 确认过


def reset_picker():
    """把 OCR 熔断状态清零。每次 run_slot 开头调一次。

    2026-10-21 改动（审计 P1-9）：这三个变量是模块级的，而 GUI 是长驻进程。
    一次演练里 OCR 因为窗口被挡而连续 miss 3 次 → _picker_blind=True，
    之后**整个 GUI 进程余生**都退回盲等，哪怕后来窗口恢复了。
    熔断的本意是"这一次运行里别白等"，不该跨运行累积。
    """
    global _picker_misses, _picker_blind, _ever_confirmed
    _picker_misses = 0
    _picker_blind = False
    _ever_confirmed = False


def input_point(win_d):
    """A point inside the message box, computed from the LIVE window rect.

    The old code used a fixed (480, 490).  A click is the one action that can open a link by
    accident (that already happened once: the pointer landed in the chat history and opened the
    form), so the point is derived from the panel every time instead of assumed: x 45% across,
    y 90% down - well inside the box, well below the last chat line.
    """
    return (win_d["left"] + int(win_d["w"] * 0.45),
            win_d["top"] + int(win_d["h"] * 0.90))


# ------------------------------------------------------------------ seeing
def _img(win_d):
    return win._get_dib(win_d["hwnd"], win_d["w"], win_d["h"])


def _screen_img(win_d, pad=0):
    """抓**屏幕上**这块矩形（不是 PrintWindow）。

    2026-10-21 22:5x 的根因就在这里：`_img()` 走 PrintWindow，只画 QQ 主窗口自己的
    内容 —— 而 @ 弹窗是**另一个顶层窗口**，永远不会出现在那张图里。于是屏幕上明明
    浮着"岳文强2024110606"，OCR 图里却一行都没有，确认永远失败，那两个人就漏了。
    聊天/标题这些用 PrintWindow 更好（不怕被挡住）；只有要看见浮层的地方才用这个。
    """
    try:
        from PIL import ImageGrab
        return ImageGrab.grab(bbox=(win_d["left"] - pad, win_d["top"] - pad,
                                    win_d["left"] + win_d["w"] + pad,
                                    win_d["top"] + win_d["h"] + pad),
                              all_screens=True)
    except Exception:                                     # noqa: BLE001
        return None


def _ocr(img, crop=None):
    """OCR an image (or a crop of it) -> [(x, y, text)] in image coordinates."""
    scale = 1
    if crop:
        img = img.crop(crop)
        if img.height <= 90:
            scale = 2
            img = img.resize((img.width * 2, img.height * 2))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    res = asyncio.run(ocrmod.ocr_png_bytes(buf.getvalue()))
    out = []
    for ln in res.lines:
        x = min(w.bounding_rect.x for w in ln.words)
        y = min(w.bounding_rect.y for w in ln.words)
        t = ocrmod.norm(ln.text)
        if t:
            out.append((int(x / scale) + (crop[0] if crop else 0),
                        int(y / scale) + (crop[1] if crop else 0), t))
    return out


def snap(win_d):
    """OCR the QQ panel (occlusion-proof PrintWindow) -> [(x, y, text)] panel-relative."""
    img = _img(win_d)
    return [] if img is None else _ocr(img)


def title_row(rows):
    return "".join(t for x, y, t in rows if y <= TITLE_Y)


def chat_ok(rows, want="道桥2024"):
    """True if the open conversation really is the class group (title bar check)."""
    t = title_row(rows)
    return bool(t) and ("道桥" in t or "02班" in t) and (not want or want[:4] in t or "02班" in t)


# ------------------------------------------------------------------ the draft, exactly
_FOCUS_HWND = 0                   # the QQ panel keys must land in; set by build() / the CLI


def _own_foreground(hwnd):
    """True if `hwnd` (or one of its popups, e.g. an IME/mention window) is in front.

    GetForegroundWindow can return a child/owned window, so the whole ancestor chain is walked:
    requiring an exact match would fail intermittently while QQ is redrawing.
    """
    fg = win.user32.GetForegroundWindow()
    if not fg:
        return False
    for _ in range(8):                       # bounded walk; a cycle would otherwise hang the run
        if fg == hwnd:
            return True
        # GA_ROOT 先走父链；@弹窗/输入法候选是独立顶层窗口，只能靠 GW_OWNER 才走得到 QQ。
        nxt = win.user32.GetAncestor(fg, 2)   # GA_ROOT
        if not nxt or nxt == fg:
            nxt = win.user32.GetWindow(fg, 4)  # GW_OWNER
        if not nxt or nxt == fg:
            break
        fg = nxt
    return False


def bind(win_d):
    """Remember which window the keys are meant for, so draft() can refuse a blind read."""
    global _FOCUS_HWND
    _FOCUS_HWND = (win_d or {}).get("hwnd") or 0
    return _FOCUS_HWND


def draft(win_d=None):
    """The message box's literal text.  '' means empty, None means "could not tell".

    Ctrl+A / Ctrl+C leaves the whole draft SELECTED, so the selection is collapsed with a Right
    arrow afterwards - otherwise the next typed character would silently wipe the draft.

    THE TRAP: if the caret is NOT in the message box, Ctrl+A selects the CHAT HISTORY and Ctrl+C
    returns a multi-line transcript, which looks exactly like "there is a draft".  A real draft is
    one line (chips never wrap), so a result containing a newline is rejected as chat text.

    THE OTHER TRAP (hit for real on 10-20): Ctrl+A / Ctrl+C go to the FOREGROUND window, not to QQ.
    When another window owned the keyboard, the copy grabbed nothing, the clipboard still held the
    sentinel, and this returned '' - "the box is empty" - which is indistinguishable from a draft
    that was already SENT.  So the window is focused first and, if it still is not in front, the
    answer is None ("cannot tell") rather than a confident, dangerous ''.
    """
    hwnd = (win_d or {}).get("hwnd") or _FOCUS_HWND
    if hwnd and not _own_foreground(hwnd):
        return None
    if not clip.put(SENT):
        return None
    win.hotkey(win.VK_CONTROL, win.VK_A)
    time.sleep(0.06)
    win.hotkey(win.VK_CONTROL, win.VK_C)
    time.sleep(0.12)
    got = clip.get()
    win.press_vk(0x27)                    # VK_RIGHT: collapse selection to the end
    time.sleep(0.05)
    if got == SENT:
        # 剪贴板还是哨兵：要么框里真的空，要么这次 Ctrl+C 没生效。再拷一次再下结论，
        # 一次拷贝失败就断言"草稿是空的"曾经把已经写好的人误判成"已经发出去了"。
        time.sleep(0.10)
        win.hotkey(win.VK_CONTROL, win.VK_A)
        time.sleep(0.05)
        win.hotkey(win.VK_CONTROL, win.VK_C)
        time.sleep(0.12)
        got = clip.get()
        win.press_vk(0x27)
        time.sleep(0.05)
        if got == SENT:
            return ""
    if got is None:
        return None
    if "\n" in got or "\r" in got:        # chat transcript, not a draft
        clip.put("")
        return None
    return got


def wipe(win_d):
    """Empty the message box and PROVE it is empty.  Ctrl+A/Delete acts on whatever has focus, so
    without the focus check this silently selects the CHAT HISTORY instead and does nothing."""
    if not win.focus_window(win_d["hwnd"]):
        return False, "cannot focus QQ"
    win.press_vk(win.VK_ESCAPE)
    time.sleep(0.15)
    win.clear_input()
    time.sleep(0.2)
    left = draft(win_d)
    if left is None:                      # no caret in the box yet - put it there and retry once
        x, y = input_point(win_d)
        win.click(x, y)
        time.sleep(0.3)
        win.clear_input()
        time.sleep(0.2)
        left = draft(win_d)
    return (left == ""), ("box cleared" if left == "" else "box still holds %r" % (left or "")[:60])


def ensure_caret(win_d, tries=3):
    """Make sure typing lands in the message box.  No click unless typing proves not to land.

    Flaky on the first attempt (seen 10-20): right after QQ is brought forward the first synthetic
    keystroke is occasionally swallowed, and one bad attempt used to abort the whole night.  So it
    retries, and it distinguishes "the box is empty" (False) from "could not read it" (None) -
    a None must never be read as proof of anything.
    """
    L, T = input_point(win_d)
    for attempt in range(1, tries + 1):
        if not win.focus_window(win_d["hwnd"]):
            return False, "cannot focus QQ"
        win.press_vk(win.VK_ESCAPE)       # close any stray popup/menu left by a previous run
        time.sleep(0.25)
        win.type_text("@")
        time.sleep(0.40)
        got = draft(win_d)
        win.press_vk(win.VK_BACK)         # remove the probe character either way
        time.sleep(0.15)
        if got:
            return True, "caret was already in the box (attempt %d)" % attempt
        if got is None:
            continue                      # no information - never treat this as "empty"
        # Read succeeded and was empty: the keys went somewhere else.  Click the box once.
        win.click(L, T)
        time.sleep(0.30)
        win.type_text("@")
        time.sleep(0.40)
        again = draft(win_d)
        win.press_vk(win.VK_BACK)
        time.sleep(0.15)
        if again:
            return True, "caret placed by clicking the box (attempt %d)" % attempt
    return False, "typing not reaching the box after %d attempts" % tries


# ------------------------------------------------------------------ one mention
def _chip_of(text, sid, name):
    """The chip for (sid, name) inside `text`, or ''.  A chip reads back '@<sid><name>...'.

    Reached through the clipboard, so it is exact text - no OCR tolerance is needed or wanted.
    A chip whose digits match but whose name does not is NOT accepted: that is a different person.
    """
    if not text:
        return ""
    m = re.search(r"@" + re.escape(sid) + re.escape(name) + r"[^\s]*", text)
    return m.group(0) if m else ""


def _row_keys(rows):
    """OCR 行 -> {(y, 去空格文本)}，用于判断"这一行是打字之后才出现的"（= 弹窗行）。"""
    return set((int(y), _row_key(t)) for _x, y, t in rows if t)


def _picker_band(win_d):
    """输入框正上方那条带子（面板坐标）：@ 弹窗就浮在这里。

    2026-10-21: 这里原来是 `max(h*0.45, iy-6)`，等于从窗口 40% 一直取到输入框，
    把大半个聊天记录都算进来了 -- 上一条群发里每个人的 chip（"0615刁闯胜T086031"）
    都被 OCR 成一行，于是任何一人都能"匹配到两行"而永远确认不了，或者更糟：
    聊天记录里恰好只剩一行时被当成弹窗、把半截草稿回车发出去。
    现在只取输入框上方 300px（弹窗高度），并且 _picker_confirm 用行长兜底。
    """
    iy = input_point(win_d)[1] - win_d["top"]      # 输入框在面板里的 y
    return (int(win_d["w"] * 0.05), max(0, iy - 300),
            int(win_d["w"] * 0.97), max(0, iy - 4))


def _row_key(body):
    return re.sub(r"\s+", "", body or "")


def _picker_confirm(win_d, sid, name, baseline=None):
    """OCR 输入框上方，找"恰好一行"候选人。返回 (state, why)。

    state: True=看见这一行了，可以回车；False=没有；None=看不准（OCR 失败/匹配到多行）。
    判据是"短行 + 含姓名 + 含至少 3 位学号数字"：群名片是"0615刁闯胜T086031"，
    聊天记录里偶尔也会出现同样的字（甚至同样短），所以宁可漏 @ 也不要把聊天记录
    当成弹窗——漏 @ 会被报警列出来，误判则是把半截草稿回车发进群。

    2026-10-21 22:5x：`baseline` 是打字**之前**那一屏的 OCR 结果（(y, 去空格文本) 集合）。
    只认不在 baseline 里的行 = 这一下打字新"变出来"的那一行，也就是弹窗行。两个好处：
      * 聊天记录里那条旧消息（"@0618滕亚东 T086035"）不再算候选人，于是"匹配到两行"
        这种假失败大幅减少；
      * 反过来也堵了一个隐患：旧逻辑下聊天记录里恰好只剩一行匹配时会被当成弹窗，
        回车就把半截草稿发进群。
    """
    # 必须抓屏：@ 弹窗是独立顶层窗口，PrintWindow 的主窗口图里没有它（见 _screen_img）。
    img = _screen_img(win_d)
    if img is None:
        img = _img(win_d)                 # 抓屏不可用时退回老路子（只能看见聊天记录）
    if img is None:
        return None, "no panel image"
    try:
        rows = _ocr(img, _picker_band(win_d))
    except Exception as e:                                # noqa: BLE001
        return None, "OCR failed: %s" % e
    nm = ocrmod.norm(name or "")
    iy = input_point(win_d)[1] - win_d["top"]     # 输入框在面板里的 y
    hits = []
    for _x, y, t in rows:
        body = _row_key(t)
        if not body or len(body) > 40:
            continue
        # 输入框里的那一行永远带 "@"（自己打的 @学号、已经落下的 chip）。弹窗行从不带 @
        # ——"岳文强2024110606"、"0615刁闯胜T086031" 都没有。2026-10-21 23:0x 就是因为
        # 输入框换行后第二行正好是一串 chip（"…@0634"），被当成弹窗确认，回车把半截草稿
        # 发进了群。这条过滤是那次事故的直接补救。
        if "@" in body:
            continue
        # 同理：弹窗浮在输入框**上方**，输入框那条带子里的行一律不算。
        if y >= iy - 26:
            continue
        if baseline is not None and (y, body) in baseline:
            continue                        # 打字之前就在屏上的：那是聊天记录，不是弹窗
        # 主判据是学号：@ 弹窗是拿学号过滤出来的，这一行必然带这四位数字。
        # （姓名不能当主判据 —— OCR 把"刁闯胜"读成"刁闯莊"很常见，按名字硬卡
        #  会导致整晚一个人都确认不了；而聊天记录里恰好出现连续四位同学号的概率极低。）
        #
        # 2026-10-21 22:5x：原来是"这四位数字都在这一行里"（set 判定）。可群名片是
        # "0615刁闯胜T086031"，寝室号 T086031 本身就含 0/6/1/3 —— 于是查 0615 时
        # 别人的寝室号也能把那四位凑齐，一次匹配到 3 行，判"看不准"，人又漏了。
        # 现在要求 **四位连续出现**（弹窗行一定是"0615刁闯胜…"开头），或者 OCR 只错
        # 一位但姓名完整出现。
        if sid:
            strict = sid in body
            loose = bool(nm) and len(nm) >= 2 and nm in body and \
                sum(1 for ch in sid if ch in body) >= 3
            if not (strict or loose):
                continue
        hits.append((int(y), t))
    if not hits:
        return False, ""
    hits.sort()
    # 2026-10-21 22:5x：原来是"恰好一行"。实测 OCR 经常把弹窗那一行拆成两行
    # （姓名一行、学号一行），于是明明是同一个人的弹窗却被判成"2 candidate rows"，
    # 这个人就漏了。现在改成：所有匹配行都挤在同一小段 y 里（<= 48px）就算一行；
    # 真的匹配到上下两处（两个不同的候选人 / 聊天记录）才算看不准。
    span = hits[-1][0] - hits[0][0]
    if span <= 48:
        return True, "picker row: " + hits[0][1][:40]
    return None, "%d candidate rows, span %dpx" % (len(hits), span)


def mention(win_d, sid, name, settle=SETTLE, baseline=None):
    """Type '@'+sid -> (prove the picker is showing this person) -> Enter -> prove the chip.

    Returns {sid, name, ok, why, chip, fatal, t}.  `fatal` means the draft vanished after Enter,
    i.e. the message was sent; the caller must stop the whole run.

    WHY THE PICKER IS NOW LOOKED AT (2026-10-21): the old code pressed Enter after a blind
    sleep(0.55) - its own comment admitted "we do not look at it".  When the popup was late or
    never opened, that Enter SENT the half-written draft into the group.  Now Enter is pressed
    only after OCR sees the candidate row; otherwise the typed "@sid" is deleted and this one
    person is reported as missed (the caller still sends, and the alert names them).

    2026-10-21 22:5x（用户报"@ 漏人"）：确认失败不再立刻放弃，同一个人最多重试
    PICKER_RETRY 次（默认 2），每次都是"擦掉自己打的 @学号 → 重新打 → 再看弹窗"。
    `baseline` 是打字前那一屏的 OCR 行，传给 _picker_confirm 用来排除聊天记录。
    """
    t0 = time.time()
    rep = {"sid": sid, "name": name, "ok": False, "why": "", "chip": "",
           "fatal": False, "t": 0.0}

    global _picker_misses, _picker_blind, _ever_confirmed
    blind = PICKER == "blind" or _picker_blind
    if not blind and _picker_misses >= PICKER_BREAK:
        # 前几个人都看不见弹窗：不再打字、不再等、更不回车，交给调用方报"没 @ 上"。
        rep["why"] = ("mention picker unconfirmed %d times in a row - skipped without typing"
                      % _picker_misses)
        return rep

    win.type_text("@" + sid)
    rep["t"] = round(time.time() - t0, 2)

    if blind:
        time.sleep(settle)                # 应急开关：回到老行为
        rep["picker"] = "blind"
    else:
        confirmed, why = False, ""
        tries = 0
        for attempt in range(max(1, PICKER_RETRY + 1)):
            if attempt:
                # 上一次没认出来：只删自己刚打进去的 "@学号"（已经确认过的 chip 在它前面，
                # 一个字符一个 chip，这里删的位数正好是 "@"+4 位数字，碰不到它们），
                # 稍等再打一遍。绝不能按 Escape —— 在 QQ 里 Escape 可能清空整个输入框。
                for _ in range(len(sid) + 1):
                    win.press_vk(win.VK_BACK)
                    time.sleep(0.02)
                time.sleep(0.25)
                win.type_text("@" + sid)
                rep["t"] = round(time.time() - t0, 2)
            trues = 0
            for _ in range(max(1, PICKER_TRIES)):
                state, why = _picker_confirm(win_d, sid, name, baseline)
                tries += 1
                if state:
                    # 两帧都看到才算数（不要求连续：OCR 中间夹一帧"看不准"很常见，
                    # 而要求连续会把成功率砍掉一半 —— 22:44 漏掉的那两个人就是这么丢的）
                    trues += 1
                    if trues >= 2:
                        confirmed = True
                        break
                time.sleep(PICKER_POLL)
            if confirmed:
                break
        rep["picker_tries"] = tries
        if not confirmed:
            # 没证实弹窗：把打进去的 "@学号" 擦干净，绝不能让这记回车把半截草稿发出去
            for _ in range(len(sid) + 1):
                win.press_vk(win.VK_BACK)
                time.sleep(0.02)
            _picker_misses += 1
            rep["t"] = round(time.time() - t0, 2)
            if PICKER_FALLBACK and not _ever_confirmed and not _picker_blind:
                # 整个进程里一次都没认出来：这条 verify 路在这台机器上不成立（缩放变了 /
                # QQ 换了渲染方式）。退回盲等，宁可承担老风险，也不能一条都发不出去。
                _picker_blind = True
                return mention(win_d, sid, name, settle, baseline)
            rep["why"] = "mention picker not confirmed (%s) after %d tries; removed @%s, did NOT press Enter" % (
                why or "no row", tries, sid)
            return rep
        _picker_misses = 0
        _ever_confirmed = True
        rep["picker"] = why

    win.press_vk(win.VK_RETURN)
    time.sleep(0.30)

    got = draft(win_d)
    rep["t"] = round(time.time() - t0, 2)

    if got is None:
        # 读草稿走的是 Ctrl+A / Ctrl+C，作用于**前台窗口**；焦点被抢走时读回来就是 None。
        # 在判定"读不出来"之前重新聚焦再读两次：这段时间里**不再打字、不再回车**，
        # 所以重读不可能变成第二次发送——这是安全的，而误判一次会白丢一个人。
        for _ in range(2):
            try:
                win.focus_window(win_d["hwnd"])
            except Exception:                                # noqa: BLE001
                pass
            time.sleep(0.30)
            got = draft(win_d)
            if got is not None:
                break
        if got is None:
            rep["why"] = "could not read the draft back"
            rep["fatal"] = True
            return rep

    chip = _chip_of(got, sid, name)
    if chip:
        rep.update(ok=True, chip=chip, why="chip verified")
        return rep

    if got == "":
        rep["why"] = "draft is EMPTY after Enter - the message was probably SENT"
        rep["fatal"] = True
        return rep

    rep["why"] = "no chip for %s (draft=%r)" % (sid, got[:120])
    if ("@" + sid) in got:
        # 回车没选中任何人，字面量 "@0615" 还留在框里：整段擦掉，
        # 否则一个 Backspace 只删掉最后一个数字，残留的 "@061" 会跟着消息发出去。
        for _ in range(len(sid) + 2):
            win.press_vk(win.VK_BACK)
            time.sleep(0.02)
    else:
        # 一个 chip 在框里就是一个字符，一个 Backspace 正好删掉它
        win.press_vk(win.VK_BACK)
    time.sleep(0.15)
    return rep


# ------------------------------------------------------------------ whole message
def build(win_d, text, pairs, settle=SETTLE, resume=False):
    """Compose the copy plus one mention per (sid, name).  Composes only - never sends.

    `resume=True` appends to the draft already in the box instead of starting over.  That is how
    the user's review procedure is honoured (m00471/m00472): mention ONE person, show the draft,
    then append the rest, then review again - without the second half wiping the first.
    """
    rep = {"ok": False, "why": "", "text": text, "mentions": [], "failed": [],
           "draft": "", "aborted": "", "t": 0.0, "missing": [], "present": 0}
    t0 = time.time()
    if resume:
        # Nothing may be cleaned here: the draft IS the work.  If it vanished the chat already
        # ate it, which is fatal and must never be papered over by typing the rest.
        # Focus FIRST: the read goes through Ctrl+A/Ctrl+C, which act on the foreground window, so
        # reading before focusing answers about whatever window happened to be in front.
        if not win.focus_window(win_d["hwnd"]):
            rep["why"] = "refusing to resume: cannot focus QQ"
            rep["aborted"] = rep["why"]
            return rep
        time.sleep(0.15)
        cur = draft(win_d)
        if cur is None:
            rep["why"] = "refusing to resume: cannot read the draft"
            rep["aborted"] = rep["why"]
            return rep
        if cur == "":
            rep["why"] = "refusing to resume: the box is empty - a previous part may have SENT"
            rep["aborted"] = rep["why"]
            return rep
        rep["resumed_from"] = cur
        ok, why = ensure_caret(win_d)
        if not ok:
            rep["why"] = why
            return rep
        rep["caret"] = why
    else:
        # A leftover draft from an earlier run would be sent along with this message, so the box
        # is emptied first and the emptiness is proven.
        wiped, wwhy = wipe(win_d)
        rep["wiped"] = wwhy
        if not wiped:
            rep["why"] = "refusing to compose: " + wwhy
            return rep

        ok, why = ensure_caret(win_d)
        if not ok:
            rep["why"] = why
            return rep
        rep["caret"] = why

        if text:
            win.type_text(text)
            time.sleep(0.35)
            cur = draft(win_d)
            if not cur or len(cur) < len(text) * 0.6:
                rep["why"] = "copy did not land in the box (draft=%r)" % (cur or "")
                win.clear_input()
                return rep

    # @ 弹窗确认的基线：只有 verify 模式才用得上（blind 模式不看弹窗）。
    base_keys = None
    if PICKER != "blind":
        # 必须和 _picker_confirm 用同一种图（抓屏），否则 y/文本对不上，旧行过滤不掉。
        _bimg = _screen_img(win_d)
        base_keys = _row_keys(_ocr(_bimg) if _bimg is not None else snap(win_d))
    rep["baseline_rows"] = len(base_keys or [])

    for i, (sid, name) in enumerate(pairs):
        if i:
            time.sleep(GAP)
        if not win.focus_window(win_d["hwnd"]):
            rep["aborted"] = "lost foreground before %s" % sid
            break
        m = mention(win_d, sid, name, settle, base_keys)
        rep["mentions"].append(m)
        if not m["ok"]:
            rep["failed"].append(m)
        if m["fatal"]:
            rep["aborted"] = m["why"] + " (stopped at %s)" % sid
            break

    # 收尾读失败 ≠ @ 失败（2026-10-21 审计）：一次 0.3 秒的焦点抖动不该让调用方
    # 把已经 @ 好的人 wipe 掉。重读三次（每次先聚焦）再下结论；"" 仍然=真的空。
    fin = None
    for _retry in range(3):
        fin = draft(win_d)
        if fin is not None:
            break
        try:
            win.focus_window(win_d["hwnd"])
        except Exception:                                # noqa: BLE001
            pass
        time.sleep(0.30)
    rep["draft"] = fin or ""
    rep["t"] = round(time.time() - t0, 2)

    # ---- 统一验证（2026-10-21 23:2x，用户的要求：打字时不验证弹窗，打完一起验）
    # 弹窗不看了，代价是"某一下回车可能没选中人"。那就把核对放到最后做一次：
    # 草稿里每个人的 chip 是 "@0617曹语馨T101032 "，按四位学号逐个核。
    if fin:
        fk = _row_key(fin)
        rep["missing"] = [sid for sid, _n in pairs if sid not in fk]
        rep["present"] = len(pairs) - len(rep["missing"])
    else:
        rep["missing"] = [sid for sid, _n in pairs]

    if rep["aborted"]:
        rep["why"] = rep["aborted"]
    elif rep["failed"]:
        rep["why"] = "%d mention(s) failed - do NOT send" % len(rep["failed"])
    elif fin is None:
        # 原来写成 `or ""`，读不回来也能 ok=True；调用方会把"没看见的草稿"当成"写好了的草稿"。
        rep["why"] = "composed, but the final draft could not be read back - not confirming it"
    elif fin == "":
        # 刚写完就空了：要么被回车发了，要么 QQ 把草稿吃了。两种情况都不能继续。
        rep["aborted"] = "draft is EMPTY right after composing - it may already have been sent"
        rep["draft"] = ""
        rep["why"] = rep["aborted"]
    else:
        rep["ok"] = not rep["missing"]
        rep["why"] = ("composed %d/%d, nothing sent yet" % (rep["present"], len(pairs))
                      if rep["ok"] else
                      "composed %d/%d - 最后核对缺这几位，先别发" % (rep["present"], len(pairs)))
    return rep


def send(win_d):
    """The final Enter - only ever called after the composed draft was reviewed.

    Escape first: with a mention popup still open, Enter picks a row (or does nothing) instead
    of sending, yet the old code reported "sent" regardless.  The draft is read before and
    after, so "sent" is only claimed when the box is actually empty afterwards.
    """
    if not win.focus_window(win_d["hwnd"]):
        return False, "lost foreground - not sending"
    before = draft(win_d)
    if not before:
        return False, ("cannot read the draft - not sending" if before is None
                       else "draft is empty - nothing to send")
    if re.search(r"@\d{4}", before):
        # 框里还剩字面量 "@0615" = 还有一个没被选中的 @，弹窗多半开着，这时回车是"选行"
        # 而不是"发送"。先 Escape 关弹窗。只在真的看到字面量时才按：Escape 在 QQ 里
        # 有可能顺手清空输入框，无脑按会把要发的消息删掉。
        win.press_vk(win.VK_ESCAPE)
        time.sleep(0.25)
        again = draft(win_d)
        if again is None:
            return False, "cannot read the draft after Escape - not sending"
        if again != before:
            return False, "Escape changed the draft (had a picker open) - not sending, re-review"
        before = again
    win.press_vk(win.VK_RETURN)
    time.sleep(0.8)
    after = draft(win_d)
    if after is None:
        return False, "pressed Enter but cannot read the box afterwards - send NOT proven"
    if after == "":
        return True, "sent (box is empty after Enter)"
    return False, "Enter did not send: box still holds %r" % after[:80]


# ------------------------------------------------------------------ CLI
def _pairs(s):
    out = []
    for part in s.split(","):
        if not part.strip():
            continue
        sid, _, nm = part.partition(":")
        out.append((sid.strip(), nm.strip()))
    return out


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("chat")

    p1 = sub.add_parser("one")
    p1.add_argument("sid")
    p1.add_argument("--name", default="")
    p1.add_argument("--settle", type=float, default=SETTLE)
    p1.add_argument("--keep", action="store_true", help="leave the chip in the box")

    p2 = sub.add_parser("build")
    p2.add_argument("--text", default="")
    p2.add_argument("--ids", required=True, help="sid:name,sid:name,...")
    p2.add_argument("--send", action="store_true")
    p2.add_argument("--yes", action="store_true", help="skip the review prompt")
    p2.add_argument("--resume", action="store_true",
                    help="append to the draft already in the box (do not wipe it)")
    p2.add_argument("--settle", type=float, default=SETTLE)
    p2.add_argument("--json")
    a = ap.parse_args()

    w = win.find_panel(300)
    if not w:
        print(json.dumps({"ok": False, "why": "no drivable QQ panel"}, ensure_ascii=False))
        return 3
    info = {"panel": {"hwnd": w["hwnd"], "rect": [w["left"], w["top"], w["w"], w["h"]]}}
    bind(w)
    rows = snap(w)
    info["title"] = title_row(rows)
    info["is_class_group"] = chat_ok(rows)
    if a.cmd == "chat":
        print(json.dumps(info, ensure_ascii=False, indent=1))
        return 0 if info["is_class_group"] else 4
    if not info["is_class_group"]:
        print(json.dumps(dict(info, ok=False, why="current conversation is not 道桥2024-02班"),
                         ensure_ascii=False, indent=1))
        return 4

    if a.cmd == "one":
        ok, why = ensure_caret(w)
        info["caret"] = why
        m = mention(w, a.sid, a.name, a.settle) if ok else {"ok": False, "why": why}
        info["mention"] = m
        info["draft_after"] = draft(w)
        if not a.keep:
            # A probe must never leave a chip one Enter away from being sent - and the clear is
            # verified, because Ctrl+A without focus selects the chat history and does nothing.
            wiped, wwhy = wipe(w)
            info["cleared"] = wiped
            info["clear_detail"] = wwhy
        info["ok"] = bool(m.get("ok")) and info.get("cleared", True)
        print(json.dumps(info, ensure_ascii=False, indent=1))
        return 0 if info["ok"] else 1

    rep = build(w, a.text, _pairs(a.ids), a.settle, resume=a.resume)
    out = dict(info, **rep)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)

    if not a.send:
        return 0 if rep["ok"] else 1

    # ---- sending happens ONLY here, and only for a clean compose
    if not rep["ok"]:
        print("NOT SENDING: " + rep["why"])
        return 1
    print("\n===== DRAFT TO BE SENT =====")
    print(rep["draft"])
    print("============================\n")
    if not a.yes:
        try:
            ans = input("Send this to 道桥2024.02班? type yes: ").strip()
        except EOFError:
            ans = ""
        if ans.lower() not in ("y", "yes", "是"):
            print("cancelled - draft left in the box")
            return 2
    ok, why = send(w)
    print(json.dumps({"sent": ok, "why": why}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
