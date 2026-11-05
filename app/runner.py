"""runner.py - 界面/计划任务共用的那一层，把 config 里的一个班喂给已经验证过的 lib/。

设计原则：**不重写发送流程**。
    lib\\_legacy\\main.py 的 who_today / verify_still_missing / at.build / at.send 是唯一真实
    跑通过（2026-10-20 实发 19 人）的那一套；那个脚本已经归档，逻辑搬到了本文件 + lib\\at.py。
    这里只做三件事：
      1. 把「班级」从 main.py 写死的常量变成参数（roster / 标题校验 / 文案）；
      2. 把 main.py 里那些 print + input() 换成可回调的日志与确认；
      3. 把每一步的结果结构化返回，界面才能显示"到哪一步、失败在哪"。

安全边界一条都不能松：
    * 名单计数对不上 -> 不发（docs.who_is_missing 的 unchecked_reliable）
    * 表单不是今天的 -> 不发
    * 群标题对不上 -> 不发
    * 发送前复核发现有人刚打卡 -> 不发
    * 草稿读不回来 -> 不按回车
"""
import ctypes
import os
import re
import sys
import time
from ctypes import wintypes

APP = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(APP)
LIB = os.path.join(ROOT, "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import config as cfgmod       # noqa: E402
import docs                   # noqa: E402
import link                   # noqa: E402
import win                    # noqa: E402
import at                     # noqa: E402

LOGS = os.path.join(ROOT, "logs")

# 迟发保护（2026-10-21 审计 P0-4）
# ---------------------------------
# 计划任务开了 StartWhenAvailable（schedule.py:45），所以"22:00 那会儿电脑是关的"
# 会在开机那一刻补跑一次。补跑本身是好事，但文案是写给那个时间点的：
# 23:00 的「最后 1 小时！」如果凌晨 3 点才发出去，等于告诉全班一个假信息。
# 所以：到点后超过 LATE_SKIP_S 才醒来，就**不发**，改为报警让人自己决定。
# 手动发送 / 演练（again=True）不受这条限制——那是人当场按的，他知道现在几点。
LATE_SKIP_S = 30 * 60

# -------------------------------------------------------------- 跨进程互斥
LOCK = os.path.join(LOGS, "dcs-run.lock")
LOCK_TTL = 25 * 60             # 一轮发送从读名单到回车最多 ~10 分钟
LOCK_GRACE = 5                 # 锁文件刚建、内容还没写完的空窗

# 2026-10-22 修复（用户报"跑不了了"）
# -------------------------------------
# 原来判"上一个进程死了没有"只看锁文件的 mtime 是否超过 LOCK_TTL（25 分钟）。
# 10-21 23:07 那次：界面里点了发送 → 写到一半把窗口关了（守护线程随进程消失，
# finally 里的 _lock_release 根本没机会跑）→ 锁留在地上。此后每一次「演练 /
# 立即提醒 / 看名单 / 导出」都只得到 run_busy「另一个提醒程序正在进行」，
# 而持锁的 pid 34504 早就没了；要等满 25 分钟才会被当成陈旧锁接管。
# 六个 ALERT 文件（23:08:13 / 23:08:21 / 23:08:37 / 23:09:22 / 23:11:34 /
# 23:11:52）就是这么来的，当晚一条都没发出去。
# 现在：**先问 pid 还在不在**，死了立刻接管（TTL 只用来兜"pid 活着但明显卡死"
# 和"pid 读不出来"这两种说不准的情况）。
PROCESS_SYNCHRONIZE = 0x00100000
_ERROR_INVALID_PARAMETER = 87
_ERROR_ACCESS_DENIED = 5
_WAIT_TIMEOUT = 0x102


def _pid_alive(pid):
    """True=还在，False=已经没了，None=说不准（都不许当成"死了"去抢锁）。"""
    if not pid or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k32.OpenProcess(PROCESS_SYNCHRONIZE, False, int(pid))
        if not h:
            err = ctypes.get_last_error()
            if err == _ERROR_INVALID_PARAMETER:
                return False        # 没有这个 pid
            if err == _ERROR_ACCESS_DENIED:
                return True         # 进程在，只是不让我打开
            return None
        try:
            return k32.WaitForSingleObject(h, 0) == _WAIT_TIMEOUT
        finally:
            k32.CloseHandle(h)
    except Exception:                                    # noqa: BLE001
        return None


def _lock_owner():
    """(pid|None, tag, age秒, exists)。pid 读不出来时返回 None——调用方必须当"说不准"。"""
    try:
        age = time.time() - os.path.getmtime(LOCK)
    except OSError:
        return None, "", 0.0, False
    pid, tag = None, ""
    try:
        with open(LOCK, encoding="utf-8", errors="replace") as f:
            tag = (f.readline() or "").strip()
            raw = (f.readline() or "").strip()
        if raw.isdigit():
            pid = int(raw)
    except OSError:
        pass
    return pid, tag, age, True


def _log_safe(event, **kw):
    """记日志失败绝不能让业务逻辑炸掉（接管锁的时候尤其不能）。"""
    try:
        log(event, **kw)
    except Exception:                                    # noqa: BLE001
        pass


def _lock_acquire(tag):
    """跨进程互斥：手动发送/演练/看名单/导出和计划任务不许同时进行。

    O_CREAT|O_EXCL 原子创建，谁先谁得；锁里记 pid。抢夺条件按优先级：
      1. pid 读不出来、且锁已经存在超过 LOCK_GRACE 秒 -> 抢；
      2. pid 就是我自己 -> 抢（本进程上一轮没跑完 finally）；
      3. pid 确认已经死了 -> 立刻抢，**不等 TTL**；
      4. pid 说不准、且超过 LOCK_TTL -> 抢；
      5. 其他（pid 还活着）-> 不抢，返回 False。
    返回 True=拿到锁。
    """
    os.makedirs(LOGS, exist_ok=True)
    payload = "%s\n%d\n%f\n" % (tag, os.getpid(), time.time())
    for _attempt in range(4):
        try:
            fd = os.open(LOCK, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            try:
                os.write(fd, payload.encode("utf-8"))
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            pass
        except OSError as e:
            _log_safe("lock_create_failed", tag=tag, err=str(e))
            return False

        pid, owner_tag, age, exists = _lock_owner()
        if not exists:
            time.sleep(0.05)         # 刚好被上一个进程释放：重试一次拿锁
            continue
        alive = _pid_alive(pid) if pid else None
        stale = False
        why = ""
        if pid is None:
            stale = age > LOCK_GRACE
            why = "锁里没有可读的 pid（已存在 %d 秒）" % age
        elif pid == os.getpid():
            stale = True
            why = "这把锁是本进程上一轮写的（已存在 %d 秒），上一轮没跑完" % age
        elif alive is False:
            stale = True
            why = "持锁进程 %d 已经不存在了（锁已存在 %d 秒）" % (pid, age)
        elif alive is None and age > LOCK_TTL:
            stale = True
            why = "查不出进程 %d 是否还活着，而锁已经放了 %d 秒" % (pid, age)
        elif age > LOCK_TTL:
            why = "进程 %d 还活着，但已经持锁 %d 秒" % (pid, age)
        if not stale:
            _log_safe("lock_busy", tag=tag, owner=owner_tag, pid=pid,
                      age=int(age), why=why)
            return False
        try:
            os.remove(LOCK)
        except FileNotFoundError:
            pass
        except OSError as e:
            _log_safe("lock_takeover_failed", tag=tag, pid=pid, err=str(e))
            return False
        _log_safe("lock_stale_takeover", tag=tag, owner=owner_tag, pid=pid,
                  age=int(age), why=why)
        time.sleep(0.02)
    return False


def _lock_release():
    """只释放自己写进去的锁：接管陈旧锁之后，老进程醒来也拿不掉新进程的锁。"""
    try:
        with open(LOCK, encoding="utf-8", errors="replace") as f:
            f.readline()                # tag
            owner = f.readline().strip()  # pid
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        return
    if owner and owner != str(os.getpid()):
        return
    try:
        os.remove(LOCK)
    except OSError:
        pass



class Abort(Exception):
    """任何一步不满足安全条件就抛这个，调用方据此停止且绝不再往下按回车。"""


def log(event, **kw):
    import json
    os.makedirs(LOGS, exist_ok=True)
    rec = dict(event=event, t=time.strftime("%F %T"), **kw)
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    # O_APPEND + 一次 write：界面手动发送和计划任务会同时往同一个 jsonl 里写，
    # 普通 "a" 模式在多进程下可能交错半行；单行单次追加才不会被撕开。
    fd = os.open(os.path.join(LOGS, "run-%s.jsonl" % time.strftime("%F")),
                 os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o666)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    return rec


def parse_day(header):
    m = re.search(r"(\d{1,2})月(\d{1,2})日", header or "")
    return "%02d-%02d" % (int(m.group(1)), int(m.group(2))) if m else None


# ------------------------------------------------------------------ 防重发
def _norm_slot(s):
    """'22' / '22:00' / '2200' -> '2200'。老日志记的是 2 位，新日志是 4 位。"""
    d = re.sub(r"\D", "", str(s or ""))
    if len(d) == 2:
        d += "00"
    return d.rjust(4, "0")[:4]


def sent_today(cls_id, slot):
    """今天这个班、这个时间点，是不是已经真实发出去过？

    为什么必须有这道闸
    ------------------
    1. 用户可能先在界面里手点过一次「立即提醒」，到点计划任务又跑一遍；
    2. 任务设了 RestartOnFailure（失败重试 2 次），万一上一次是在"已发出、
       但退出码非 0"的状态下结束的，重试就会在同一晚再 @ 一遍全班。
    发出去的消息撤不回来，所以宁可少发一次，等人工确认。

    兼容老格式：lib/main.py 写的 run_commit / run_begin(slot="22")。
    """
    import json
    path = os.path.join(LOGS, "run-%s.jsonl" % time.strftime("%F"))
    if not os.path.isfile(path):
        return None
    want = _norm_slot(slot)
    cur = None
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        ev = rec.get("event")
        if ev in ("run_begin",):
            cur = _norm_slot(rec.get("slot"))
            continue
        if ev in ("send", "run_commit") and rec.get("sent"):
            raw = rec.get("slot")
            if raw is None and ev == "run_commit":
                raw = cur            # 老 lib/main.py 的 run_commit 不带 slot，才允许兜底
            if raw is not None and _norm_slot(raw) == want:
                return rec.get("t")
    return None



# ------------------------------------------------------------------ 读名单
def read_missing(cls, save=True):
    """读"今天谁还没打卡"。返回 dict；ok=False 时 why 说明原因，调用方必须拒发。"""
    out = {"ok": False, "why": "", "people": [], "url": None, "header": None,
           "counters": {}, "unmatched": [], "reliable": False}

    # title_keys 是 QQ 群名的键（面板确认群用）；表单网页标题是另一回事——
    # 本班表单真名叫「夕阳下倦鸟归巢（造桥新青年）」，拿群名键去卡它必然失败
    # （2026-10-21 实测：form_url 迁移进配置后，演练在这一行直接拦死）。
    # 表单标题用 form_title_keys；没配就退回旧的弱校验（只拒绝登录页/空标题）。
    keys = cls.get("title_keys") or []
    form_keys = cls.get("form_title_keys") or []
    url = (cls.get("form_url") or "").strip()
    if url:
        v = link.validate(url, expect=form_keys)
        if not v.get("ok"):
            out["why"] = "配置里的表单链接打不开或不是本班的打卡表单：%s" % v.get("why")
            return out
        out["url"], out["title"], out["link_from"] = url, v.get("title"), "配置"
    else:
        # 缓存只顶 36 小时：昨天的链接不能当成今天的证据
        cached = link.last_good()
        if cached and link.validate(cached, expect=form_keys).get("ok"):
            out["url"], out["link_from"] = cached, "缓存"
        else:
            r = link.from_qq(expect=keys)
            if not r.get("ok"):
                out["why"] = "群聊里没找到今天的打卡链接：%s" % r.get("why")
                return out
            out["url"], out["title"], out["link_from"] = r["url"], r.get("title"), r.get("from")

    # 表单浏览器：没人值守时也要能自己起来。
    # 用 docs.open_browser(None) 而不是只判断端口——main.ensure_docs_browser 也是这么做的：
    # 冷机器上 9411 没开时会把专用 profile 的 Edge 拉起来并等它就绪。
    try:
        if not docs.port_open(docs.PORT):
            docs.open_browser(None)
    except (SystemExit, RuntimeError) as e:                  # 起不来说明原因，别静默
        out["why"] = "起不来表单浏览器（Edge 专用 profile）：%s" % e
        return out

    # 腾讯文档的单页应用会「假活着」：地址栏已经是 #/result，统计 DOM 却一个都没渲染。
    # 实测 22:2x 连续三次都是这个状态，而带导航重读一次就恢复了（31 人未打卡）。
    # 无人值守时这不是"偶发"而是"当晚全部失效"，所以要重试，而且要**强制重新加载**：
    # 只调 read_status(None) 不会导航，永远读不出东西。
    d = None
    for attempt in range(3):
        try:
            d = docs.read_status(out["url"], wait=16 + 8 * attempt)
        except Exception as e:                               # noqa: BLE001
            # CDP 断了 / 页面崩了：以前会被吞成"无法识别的页面"，日志里看不出真相
            log("docs_error", cls=cls["id"], attempt=attempt,
                why="%s: %s" % (type(e).__name__, str(e)[:300]))
            d = docs.blank_status("读取打卡页面出错：%s: %s"
                                   % (type(e).__name__, str(e)[:200]))
        if d["logged_in"]:
            break
        log("docs_stall", cls=cls["id"], attempt=attempt, why=d.get("why"))
        if attempt < 2:
            try:
                docs.open_browser(out["url"])       # 关不掉的话至少强制导航 + 等就绪
            except (SystemExit, RuntimeError):
                pass
            time.sleep(3)
    out["logged_in"] = d["logged_in"]
    out["header"] = d["header"]
    out["counters"] = d["counters"]
    if not d["logged_in"]:
        out["why"] = "腾讯文档没有登录：%s" % d.get("why")
        return out

    out["open_day"] = parse_day(out["header"])
    if out["open_day"] is None:
        # 读不出日期不等于放行：以前 `if out["open_day"] and ...` 在 None 时
        # 整个判断短路，36 小时缓存里的旧表单/残留页面全都能溜过去（fail-open）。
        # 确认不了是今天的表单就拒发，宁可报警等人来看。
        out["why"] = "统计页标题里读不出日期（header=%r），无法确认是今天的表单，拒绝发送" % out["header"]
        return out
    if out["open_day"] != time.strftime("%m-%d"):
        out["why"] = "表单显示的是 %s，不是今天（%s）" % (out["header"], time.strftime("%m-%d"))
        return out

    roster = docs.read_roster(cls["roster"])
    w = docs.who_is_missing(d, roster)
    out["unmatched"] = w["unmatched"]
    out["reliable"] = w["unchecked_reliable"]
    if not w["unchecked_reliable"]:
        out["why"] = ("未填写行数与「未打卡人数」对不上，拒绝点名（计数=%s）" % out["counters"])
        return out

    by_name = {r["name"]: (r.get("sid") or "") for r in roster}
    people = []
    no_sid = []
    for nm in w["missing"]:
        sid = by_name.get(nm) or ""
        if len(sid) < 4:
            # 2026-10-21 改动（审计 P0-3）：以前这里直接 return，一个人学号缺失
            # 就让**整晚一条都发不出去**——和"单人失败不中断"的设计语义正相反。
            # 现在跳过这个人、记进 no_sid，剩下的人照发，缺的人由调用方写进 missed
            # 让人第二天手动补。花名册表头错了（一个人都认不出）仍会走上面的
            # unchecked_reliable 闸门，不会静默漏人。
            no_sid.append(nm)
            continue
        people.append({"name": nm, "sid": sid[-4:]})
    out["people"] = people
    out["no_sid"] = no_sid
    out["roster_size"] = len(roster)
    out["checked_in"] = len(w["checked_in"])
    if no_sid:
        log("read_missing_no_sid", cls=cls["id"], n=len(no_sid), names=no_sid)
    if not people:
        # 全部缺学号 = 花名册的学号列整体有问题，这种要报警而不是发一条空消息。
        out["why"] = ("需要提醒的 %d 个人都没有可用的学号后四位（%s），"
                      "多半是花名册的「学号」列没了或换了表头"
                      % (len(no_sid), "、".join(no_sid[:5])))
        return out
    out["ok"] = True
    if save:
        log("read_missing", cls=cls["id"], ok=True, n=len(people),
            url=out["url"], header=out["header"], no_sid=len(no_sid))
    return out


def verify_still_missing(cls, people):
    """发送前复核：草稿里的人现在是不是还没打卡。不可用就返回 ok=False。"""
    old = [p["name"] for p in (people or [])]
    r = read_missing(cls, save=False)
    if not r.get("ok"):
        return {"ok": False, "why": "无法重新读取名单：%s" % r.get("why"), "dropped": [], "added": []}
    new = [p["name"] for p in r["people"]]
    dropped = [n for n in old if n not in new]
    added = [n for n in new if n not in old]
    if dropped:
        return {"ok": False, "dropped": dropped, "added": added,
                "why": "%d 人刚刚打了卡，草稿里仍 @ 着他们：%s" % (len(dropped), "、".join(dropped))}
    return {"ok": True, "dropped": [], "added": added,
            "why": "名单与草稿一致（%d 人仍需提醒）" % len(new)}


# ------------------------------------------------------------------ QQ 侧
LEFT_W = 150                  # 会话列表的大致宽度；更靠右的点属于聊天区，点下去会选中文字


def _title_ok(title, keys):
    return bool(keys) and any(k and k in (title or "") for k in keys)


def _click_session(cls):
    """在左侧会话列表里点一下这个班，返回是否点成了。

    为什么需要这个：QQ 面板不保证停在班级群。实测遇到过一次面板停在别的群，
    title_row 只读出 '00'，一个关键词都不命中——那时只能整晚放弃。
    点错群不会误发（点完还要复核标题），最坏是白点一次，所以可以放心试。
    """
    w = win.find_panel(300)
    if not w:
        return False
    at.bind(w)
    rows = at.snap(w)
    keys = cls.get("title_keys") or []

    # 只在左侧列表里找，且必须是「整行都是群名」的样子——聊天区里同样会 OCR 出含关键词的
    # 消息，点到那里只是选中文字，白忙一场。
    hits = []
    for r in rows:
        x, y, txt = r[0], r[1], r[2]
        if x > LEFT_W:
            continue
        if _title_ok(txt, keys):
            hits.append((x, y, txt))
    if not hits:
        return False

    for x, y, txt in hits:
        # 行文字的左边缘在 x，点 x 会落在图标上；往右挪一点落到文字上更稳。
        win.click(x + 30, y)
        time.sleep(0.8)
        got = at.title_row(at.snap(w))
        if _title_ok(got, keys):
            log("panel_switch", cls=cls["id"], clicked=txt, title=got)
            return True
    return False


def find_panel(cls):
    """找 QQ 面板并校验标题栏是不是这个班。不匹配就抛 Abort——绝不发到错的群。"""
    w = win.find_panel(300)
    if not w and not win.qq_pids():
        # 计划任务是没人值守的：QQ 没开就等于整班收不到提醒。能自己拉起来就拉起来。
        log("qq_not_running")
        if win.start_qq():
            log("qq_autostart", wait_s=60)
            w = win.wait_panel(60)
            log("qq_autostart_done", ok=bool(w))
    if not w:
        # 失败原因必须能事后复现：QQ 没开 / 开着但窗口认不出来，是两种完全不同的处理。
        diag = win.panel_diag(300) if hasattr(win, "panel_diag") else []
        for line in diag:
            log("panel_diag", line=line)
        if not win.qq_pids():
            raise Abort("找不到 QQ 面板：QQ 也没能自动启动。请打开 QQ 并登录后再点一次")
        raise Abort("找不到 QQ 面板：QQ 在运行，但没有能聊天的窗口"
                    "（可能最小化到托盘、被别的窗口挡住，或还停在登录界面）。"
                    "请手动点开 QQ 主窗口，再点一次")
    at.bind(w)
    keys = cls.get("title_keys") or []
    if not keys:
        raise Abort("「%s」没配群标题关键词，无法确认发到哪个群" % cls.get("name"))

    title = at.title_row(at.snap(w))
    if _title_ok(title, keys):
        return w

    log("panel_mismatch", cls=cls["id"], title=title, keys=keys)
    if _click_session(cls):
        return w
    raise Abort("当前打开的会话标题是 %r，不像「%s」（需要包含 %s），"
                "会话列表里也没点到它"
                % (title, cls.get("name"), " / ".join(keys)))


def _missing_mentions(rep, candidates):
    """build() 的返回里，candidates 中哪些人最终没有 @ 上。

    2026-10-21 23:2x：@ 弹窗不再逐人验证（用户定调：打字+回车，打完统一验证），
    所以以 build() 最后那次"按草稿逐个核对"的 missing 为准；mentions 里的 ok
    只在单人核对失败时才会是 False，两者取并集。
    """
    got = {m.get("sid") for m in (rep.get("mentions") or []) if m.get("ok")}
    miss = set(rep.get("missing") or [])
    return [p for p in candidates if p.get("sid") not in got or p.get("sid") in miss]


def _read_draft(w, tries=3):
    """读草稿；读不回来就重新聚焦再读。返回 ''（空）/ 文本 / None（三次都不确定）。

    为什么要重试（2026-10-21）：at.draft() 走的是 Ctrl+A/Ctrl+C，读的是**前台窗口**。
    偶发一次焦点被别的窗口抢走，就返回 None，而原来这一下会让整晚直接 Abort——
    一次 0.3 秒的抖动换全班一条提醒都没有，代价完全不成比例。
    注意 None 仍然不等于空：三次都读不出来还是要停，绝不猜。
    """
    for i in range(tries):
        if i:
            try:
                win.focus_window(w["hwnd"])
            except Exception:                                # noqa: BLE001
                pass
            time.sleep(0.30)
        got = at.draft(w)
        if got is not None:
            return got
    return None


def _compose(w, cls, copy_text, people, step):
    """把文案和一群人@进输入框。只组草稿，不按回车。

    返回 (草稿全文, 没 @ 上的人)。

    一个人 @ 失败不再拖垮整晚（2026-10-21 改）
    -----------------------------------------
    原来只要有一个人的 chip 没弹出来就 raise Abort，结果是**全班一条都收不到**。
    可实际上那一个人没提醒到，只影响那一个人；整晚不发才真的误事。
    所以现在：记下来、继续 @ 下一个，最后把漏掉的名字交给调用方报出来。
    """
    head, tail = people[0], people[1:]
    missed = []

    rep = at.build(w, copy_text, [(head["sid"], head["name"])])
    if rep.get("aborted"):
        # 草稿消失 = 上一条已经被回车发出去了。这一步无论如何都要停，不能继续打字。
        raise Abort("组草稿时中断：%s" % rep["aborted"])
    if rep.get("ok"):
        step("first", "已 @ %s（%s）" % (head["name"], head["sid"]))
    else:
        # 「收尾读回 None」≠ @ 失败（2026-10-21 审计）：at.build 收尾一次读取
        # 失败时 rep.ok=False、draft=""，而 mentions 可能全是成功的。照失败处理
        # 会让下面 live_now="" → resume=False → 非 resume 分支 wipe 掉首人的
        # chip → 发出去的消息没有第一个人，且 missed=[] 不触发漏@告警。
        # 所以先自己复核草稿（带聚焦重试）再定论：草稿是唯一事实来源。
        live0 = _read_draft(w)
        if live0 is None:
            raise Abort("首人 @ 之后草稿读不回来（焦点可能被抢走），不组后面的人，也不按回车")
        if live0 and at._chip_of(live0, head["sid"], head["name"]):
            step("first", "已 @ %s（%s）（收尾读回失败，复核草稿里 chip 在）"
                 % (head["name"], head["sid"]))
            rep = dict(rep, ok=True, draft=live0)
        else:
            # 草稿是地面真相：chip 不在（含草稿其实是空的），就算 mentions 报了
            # ok 也算没 @ 上——否则下面 len(missed)>=len(people) 的总闸拦不住。
            if head["sid"] not in {m.get("sid") for m in missed}:
                missed.append(head)
            step("first", "@ %s 没成功（%s），继续 @ 后面的人"
                 % (head["name"], ((rep.get("why") or "草稿里没有他的 chip").splitlines()[0][:80])))
            rep = dict(rep, draft=live0)

    if tail:
        # resume=True 要求框里已经有东西，否则会误判成"上一条已经发出去了"。
        # 第一个人失败时草稿里只剩文案；文案为空（配置异常）才退回到整段重敲。
        live_now = rep.get("draft")
        resume = bool(live_now)
        rep2 = at.build(w, "" if resume else copy_text,
                        [(p["sid"], p["name"]) for p in tail], resume=resume)
        if rep2.get("aborted"):
            raise Abort("组草稿时中断：%s" % rep2["aborted"])
        left = _missing_mentions(rep2, tail)
        missed.extend(left)
        step("rest", "其余 %d 人就位（没 @ 上 %d 人）" % (len(tail), len(left)))

    live = _read_draft(w)
    if live is None:
        raise Abort("草稿三次都读不回来（可能焦点被别的窗口抢走），不按回车")
    if live == "":
        raise Abort("草稿是空的——可能已经被回车发出去过，不按回车")
    if people and len(missed) >= len(people):
        # 一个都没 @ 上：不是"某个人运气不好"，而是弹窗/OCR 整体失效了。
        # 这时候发出去的就是一条没有任何人的群消息——等于全班漏发，还占掉了当晚
        # 唯一一次发送机会。宁可不发、报警，让人来看一眼。
        try:
            ok, why = wipe_draft(w)
            log("abort_wipe", cls=cls["id"], ok=ok, why=why)
        except Exception:                                # noqa: BLE001
            pass
        raise Abort("全部 %d 人的 @ 都没有确认成功（@弹窗 OCR 可能失效），不发送" % len(people))
    step("draft", live)
    return live, missed


def compose_and_send(cls, copy_text, people, on_step=None, max_fix=3, send=True,
                     slot=None, allow_repeat=False):
    """一段文案 + 一群人 -> 草稿 -> 复核 -> 回车。on_step(阶段, 信息) 用于界面回显。

    复核时发现有人刚打完卡怎么办（m00348 用户选定：自动剔除后重发）
    ------------------------------------------------------------
    原版是整体拒发——安全，但那个班当晚一个人都收不到提醒，而实际上只需要把
    已经打完卡的那一两个人去掉。所以这里：剔掉刚落下的、清空草稿、重新组一遍。
    重新组之前一定先 wipe：不 wipe 就会把新文案和旧 @ 叠在一起发出去，那才是真事故。
    """
    def step(stage, msg, **kw):
        log("step", cls=cls["id"], stage=stage, msg=msg, **kw)
        if on_step:
            on_step(stage, msg)

    w = find_panel(cls)
    step("panel", "已确认群：%s" % cls.get("name"))

    trying = list(people)
    fixed = []                     # 复核时被剔掉的人，调用方要能看见"少 @ 了谁"
    missed = []                    # @ 没成功的人：仍然照常发送，但必须报出来
    for attempt in range(max_fix + 1):
        live, not_mentioned = _compose(w, cls, copy_text, trying, step)
        missed = not_mentioned

        v = verify_still_missing(cls, trying)
        log("verify", cls=cls["id"], ok=v["ok"], why=v["why"],
            dropped=v.get("dropped"), added=v.get("added"), attempt=attempt,
            missed=[m["name"] for m in missed])

        if v["ok"]:
            step("verify", v["why"])
            if missed:
                # 漏 @ 的人不影响其他人收提醒，但这条日志是事后追责/补发的唯一线索
                log("mention_missed", cls=cls["id"],
                    people=[{"name": m["name"], "sid": m["sid"]} for m in missed])
                step("warn", "有 %d 人没 @ 上：%s（其余人照常发送）"
                     % (len(missed), "、".join(m["name"] for m in missed)))
            if not send:
                # 演练：草稿已经全部组好、复核也通过了，就是**不按回车**。
                # 让「今晚到底会不会正常」可以在 23:00 之前就问出来，而不用等真的发一次。
                log("rehearse", cls=cls["id"], n=len(trying), draft=live)
                return {"sent": False, "why": "演练通过（%d 人），没有发送" % len(trying),
                        "draft": live, "n": len(trying), "people": trying,
                        "dropped": fixed, "rehearsal": True,
                        "missed": [m["name"] for m in missed]}
            if not allow_repeat and slot is not None:
                # 二次防重发：上面的复核要重读页面（秒级窗口），期间计划任务
                # 可能恰好把同一个时间点发过了。at.send 之前最后一刻再查一次，
                # run_slot 开头那道闸和这里才算真正闭环。
                prev2 = sent_today(cls["id"], slot)
                if prev2:
                    log("send_skip_dupe", cls=cls["id"], slot=slot, prev=prev2)
                    try:
                        wipe_draft(w)       # 已经组好的草稿绝不留在框里等人误发
                    except Exception:                            # noqa: BLE001
                        pass
                    return {"sent": False,
                            "why": "发送前发现 %s 已经发过（%s），不重复发送"
                                   % (slot, prev2),
                            "draft": "", "n": len(trying), "people": trying,
                            "dropped": fixed, "dupe": True,
                            "missed": [m["name"] for m in missed]}
            ok, why = at.send(w)
            # slot 必须落盘：sent_today 靠它认"这一晚这个时间点发过没有"；
            # 不带 slot 时只能借用最近一条 run_begin，界面手动发送会和计划任务串台。
            log("send", cls=cls["id"], slot=slot, sent=ok, why=why, n=len(trying),
                draft=live)
            return {"sent": ok, "why": why, "draft": live, "n": len(trying),
                    "people": trying, "dropped": fixed,
                    "missed": [m["name"] for m in missed]}

        if not v.get("dropped") or attempt >= max_fix:
            raise Abort("发送前复核没通过：%s" % v["why"])

        # 有人刚打完卡：剔掉、清空、重来。没清空就重来的话，新旧两段会叠在一起发出去。
        dropped = v["dropped"]
        fixed.extend(dropped)
        trying = [p for p in trying if p["name"] not in dropped]
        step("fix", "%s 刚打完卡，剔除后重新组草稿（剩 %d 人）" % ("、".join(dropped), len(trying)))
        wipe_draft(w)
        time.sleep(0.4)
        if not trying:
            raise Abort("复核后没有仍需提醒的人了（%s 都打了卡）" % "、".join(dropped))

    raise Abort("复核反复不通过，放弃本次发送")


def wipe_draft(w=None):
    """清空输入框，返回 (ok, why)。传 w 就清那个面板，不传就重新找一个。"""
    if w is None:
        w = win.find_panel(300)
        if not w:
            return False, "找不到 QQ 面板"
        at.bind(w)
    return at.wipe(w)


# ------------------------------------------------------------------ 一个班的一次运行
def late_by(slot, now=None):
    """现在比 slot（'2200'）晚了多少秒。负数=还没到点。

    跨零点也成立：slot=23:00、现在 00:30 → 晚 5400 秒（不是 -82800）。
    now 可注入，方便验证；正常调用不传。
    """
    d = re.sub(r"\D", "", str(slot or "")).rjust(4, "0")[:4]
    try:
        hh, mm = int(d[:2]), int(d[2:])
    except ValueError:
        return 0
    now = now or time.localtime()
    target = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, hh, mm, 0, 0, 0, -1))
    diff = time.mktime(tuple(now)) - target
    if diff < -12 * 3600:          # 昨天的时间点，跨了零点
        diff += 24 * 3600
    return diff


def run_slot(cid, slot, on_step=None, again=False, send=True):
    """计划任务的入口：读配置 -> 读名单 -> 组合发送。返回结构化结果，不抛异常。

    again=False 时，同一晚同一时间点已经成功发过就直接跳过（见 sent_today）。
    界面上"我确实想再发一次"的场景，调 run_slot(..., again=True)。
    """
    cfg = cfgmod.load()
    cls = cfgmod.get_class(cfg, cid)
    res = {"ok": False, "cls": cid, "slot": slot, "why": "", "n": 0}
    if not cls:
        res["why"] = "配置里没有这个班：%s" % cid
        return res
    sched = None
    for s in cls.get("schedules", []):
        if cfgmod.slot_of(s.get("time")) == slot:
            sched = s
            break
    if not sched:
        res["why"] = "这个班没有 %s 这个时间点" % slot
        return res
    copy_text = (sched.get("copy") or "").strip()
    if not copy_text:
        res["why"] = "%s 的文案是空的" % sched.get("time")
        return res

    if not again:
        prev = sent_today(cid, slot)
        if prev:
            res.update(ok=True, why="今天 %s 这个时间点已经发过了（%s），不重复发送" % (sched["time"], prev))
            log("run_skip_dupe", cls=cid, slot=slot, prev=prev)
            if on_step:
                on_step("skip", res["why"])
            return res
        # 迟发保护：见 LATE_SKIP_S 的说明。补跑发生在半夜时，宁可不发。
        late = late_by(slot)
        if send and late > LATE_SKIP_S:
            mins = int(late // 60)
            brief = copy_text if len(copy_text) <= 16 else copy_text[:16] + "…"
            res["why"] = ("已经比 %s 晚了 %d 分钟（大概是电脑关机后补跑的），"
                          "这时候再发「%s」会误导人，所以没有发。"
                          "需要的话在界面上手动发一次。" % (sched["time"], mins, brief))
            log("run_too_late", cls=cid, slot=slot, late_s=int(late))
            if on_step:
                on_step("skip", res["why"])
            return res

    if not _lock_acquire("run:%s:%s" % (cid, slot)):
        res["why"] = "另一个提醒程序正在进行（发送/演练/看名单/导出），本次不重复开始"
        log("run_busy", cls=cid, slot=slot)
        if on_step:
            on_step("skip", res["why"])
        return res
    # 每次运行从零开始算 OCR 熔断（审计 P1-9）：GUI 是长驻进程，不复位的话
    # 一次演练里的连续 miss 会让之后所有运行都退回盲等。
    try:
        at.reset_picker()
    except AttributeError:                     # 老版本 at.py，忽略
        pass
    log("run_begin", cls=cid, slot=slot, time=sched.get("time"))
    try:
        r = read_missing(cls)
        if not r.get("ok"):
            res["why"] = r["why"]
            log("run_no_data", cls=cid, why=r["why"])
            return res
        people = r["people"]
        res["n"] = len(people)
        if not people:
            res.update(ok=True, why="今天全员已打卡，不发消息")
            log("run_nobody", cls=cid)
            return res
        if on_step:
            on_step("people", "今天还有 %d 人没打卡" % len(people))
        out = compose_and_send(cls, copy_text, people, on_step=on_step, send=send,
                               slot=slot, allow_repeat=again)
        if out.get("rehearsal"):
            res.update(ok=True, why=out["why"], n=out["n"], rehearsal=True,
                       dropped=out.get("dropped") or [],
                       missed=out.get("missed") or [])
            # 演练的草稿必须清干净。留着的话，真正 23:00 那次会先 wipe 再重来（at.build 的
            # 非 resume 分支会清空），所以不会重复发；但万一下次走的不是这条路，框里就躺着
            # 一段没人审过的 @，太危险。这里验证清空结果并写日志。
            ok, why = wipe_draft()
            log("rehearse_wipe", cls=cls["id"], ok=ok, why=why)
            if not ok:
                res.update(ok=False, why="演练通过但草稿没清干净（%s）——请手动清空输入框" % why)
            return res
        res.update(ok=out["sent"], why=out["why"], n=out["n"],
                   dropped=out.get("dropped") or [],
                   missed=out.get("missed") or [])
        return res
    except Abort as e:
        res["why"] = str(e)
        log("run_abort", cls=cid, why=str(e))
        return res
    except Exception as e:                                   # noqa: BLE001
        res["why"] = "意外错误：%s" % e
        log("run_error", cls=cid, why=repr(e))
        return res
    finally:
        _lock_release()


def dry_run(cid):
    """只看名单，不碰 QQ。界面上的「先看看今天谁没打」按钮。"""
    cfg = cfgmod.load()
    cls = cfgmod.get_class(cfg, cid)
    if not cls:
        return {"ok": False, "why": "没有这个班", "people": []}
    # 看名单也要抢锁：发送流程的「复核」在读同一个统计页，这时把日历切走
    # 一天，复核就会拿错名单——而复核拿错名单的后果是不该发的人被 @。
    if not _lock_acquire("dry:%s" % cid):
        return {"ok": False, "why": "提醒程序正在发送/导出中，等它跑完再看名单",
                "people": []}
    try:
        return read_missing(cls)
    finally:
        _lock_release()
