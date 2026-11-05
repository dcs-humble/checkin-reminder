"""docs.py - read the day's 打卡 (clock-in) status from Tencent Docs.

How it works
------------
The user has a DEDICATED Microsoft Edge profile that they logged into Tencent Docs
(QQ login) once.  We launch Edge on that profile with the DevTools protocol enabled and
read the collection-result page as structured DOM data - no OCR, no guessing.

Why not copy the user's everyday profile: its cookie jar is a live SQLite WAL, and a copy
taken at the wrong instant yields a dead session (the page then shows 收集表登录后才能填写).
Why not scrape QQ's local message DB: it is SQLCipher-encrypted.

Why not OCR this page: the page is React-rendered and the two name lists look identical as
text.  Reading the DOM gives us selectors and counters to validate them against.

SELECTORS (verified live; see recon/docs-panels2.py)
    .statistics-section-header        -> "10月18日打卡情况当日20:00-23:59正在收集"
    .collection-statistic-item        -> textContent "41打卡人数" (digits + label)
    li.dui-tabs-bar-item              -> the name-list tabs 未填写 / 已填写
    .record-item-box .item-name       -> one student name per row
Only ONE name panel exists in the DOM at a time, so after clicking a tab we scope the row
query to the nearest ancestor of that tab which actually contains rows.

Correctness guard: the 未填写 row count must equal the 未打卡人数 counter (they agree
exactly).  The 已填写 panel is a virtualised infinite-scroll list, so a name appearing in
BOTH panels is reported as ambiguous and the caller must not trust it.

CLI
---
    python docs.py check                 # login state + today's counts
    python docs.py read  --url U --json out.json
    python docs.py open  --url U         # launch Edge on the dedicated profile
    python docs.py close
"""
import argparse
import datetime
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request

if sys.stdout:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))

# Standalone runtime first (shipped layout), then a relative dev fallback.
# 2026-10-21 改动（审计 P1-12）：这里原来硬编码了开发机的
# 程序根目录的候选，谁在就用谁；都不在就让 import websocket 自己报错。
try:
    import websocket  # noqa: F401
except ImportError:
    for _p in (os.path.join(HERE, "..", "runtime", "python", "Lib", "site-packages"),
               os.path.join(HERE, "..", "pylib"),
               os.path.join(HERE, "pylib")):
        if os.path.isdir(_p):
            sys.path.insert(0, _p)
    import websocket  # noqa: E402,F811

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
BASE = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "dcs-clockin")
PROFILE = os.path.join(BASE, "edge-profile")
STATE = os.path.join(BASE, "docs-state.json")
PORT = 9411
FORM_PREFIX = "https://docs.qq.com/form/page/clock/"
TAB_OPEN, TAB_DONE = "未填写", "已填写"
# --remote-allow-origins=* is mandatory: Chrome rejects the CDP websocket with
# "Handshake status 403 Forbidden" without it.
LAUNCH_ARGS = ["--remote-allow-origins=*", "--no-first-run", "--no-default-browser-check",
               "--disable-sync", "--no-service-autorun", "--disable-background-mode"]


def edge_exe():
    for p in EDGE_CANDIDATES:
        if os.path.isfile(p):
            return p
    # 库函数不抛 SystemExit：计划任务里那是 BaseException，调用方 except Exception 接不住，
    # pythonw 下连一行痕迹都不会留下。
    raise RuntimeError("Microsoft Edge not found in the usual locations")


# 一轮读取的总预算（秒）。以前没有任何全局 deadline，最坏 16+16+20+16 ≈ 3.5 分钟才报错，
# 22:00 那条提醒就被拖过去了。读不完 = 发不出去，宁可早点失败。
BUDGET = float(os.environ.get("DCS_DOCS_BUDGET") or 100)
_DEADLINE = None


def _remaining():
    """距离本轮总预算还剩几秒；没设预算时返回 None。"""
    if _DEADLINE is None:
        return None
    return max(0.0, _DEADLINE - time.time())


# --------------------------------------------------------------------------- CDP plumbing
def port_open(p=PORT, host="127.0.0.1"):
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect((host, p))
        return True
    except OSError:
        return False
    finally:
        s.close()


def http_json(url, method="GET", timeout=6, tries=12, delay=0.7):
    """Edge returns an EMPTY body for about a second after launch, so retry until it parses."""
    last = ""
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
            if body and body.strip():
                return json.loads(body)
            last = "empty body (status %s)" % r.status
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, str(e)[:80])
        time.sleep(delay)
    raise RuntimeError("%s unusable after %d tries: %s" % (url, tries, last))


def read_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def write_state(**kw):
    os.makedirs(BASE, exist_ok=True)
    st = read_state()
    st.update(kw)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    return st


class Page:
    """A tiny CDP client for one page target."""

    def __init__(self, port=PORT, tries=10):
        self.port = port
        t = None
        for _ in range(tries):
            lst = http_json("http://127.0.0.1:%d/json/list" % port, tries=3)
            t = next((x for x in lst if x.get("type") == "page" and x.get("webSocketDebuggerUrl")), None)
            if t:
                break
            time.sleep(1)
        if not t:
            raise RuntimeError("no page target on the dedicated Edge profile")
        self.ws = websocket.create_connection(t["webSocketDebuggerUrl"], timeout=30,
                                            suppress_origin=True)
        self.ws.settimeout(30)
        self.n = 0

    def call(self, method, params=None, wait=30):
        """Send one CDP command and return its reply.

        旧代码把"连接断了"也返回 {}，和"页面上没有这个数据"长得一模一样，无人值守时
        日志里只会写"无法识别的页面"，真相被埋掉。现在传输层错误一律抛 RuntimeError。
        """
        self.n += 1
        myid = self.n
        try:
            self.ws.send(json.dumps({"id": myid, "method": method, "params": params or {}}))
        except Exception as e:                                  # noqa: BLE001
            raise RuntimeError("CDP send failed for %s: %s: %s"
                               % (method, type(e).__name__, str(e)[:120])) from e
        end = time.time() + wait
        while time.time() < end:
            try:
                self.ws.settimeout(max(0.5, end - time.time()))
                m = json.loads(self.ws.recv())
            except Exception as e:                              # noqa: BLE001
                raise RuntimeError("CDP recv failed for %s: %s: %s"
                                   % (method, type(e).__name__, str(e)[:120])) from e
            if m.get("id") == myid:
                return m
        raise RuntimeError("CDP %s timed out after %.0fs" % (method, wait))

    def ev(self, expr, wait=30):
        r = self.call("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True, "awaitPromise": True}, wait)
        res = r.get("result", {}).get("result", {})
        if "value" in res:
            return res["value"]
        ex = r.get("result", {}).get("exceptionDetails")
        if ex:
            return "<EXC %s>" % str(ex.get("text"))[:90]
        return "<no value>"

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- js snippets
JS_COUNTS = r"""
(() => {
  const o = {counters: {}, header: null};
  const h = document.querySelector('.statistics-section-header');
  o.header = h ? (h.textContent || '').trim() : null;
  document.querySelectorAll('.collection-statistic-item').forEach(it => {
    const m = (it.textContent || '').trim().match(/^(\d+)(.*)$/);
    if (m) o.counters[m[2].trim()] = parseInt(m[1], 10);
  });
  return o;
})()
"""


def _js_click_tab(want):
    return ("(() => {"
            "const w = " + json.dumps(want) + ";"
            "const ts = [...document.querySelectorAll('li.dui-tabs-bar-item')];"
            "const t = ts.find(e => (e.textContent || '').replace(/[\\s*]/g, '') === w);"
            "if (!t) return {ok: false, tabs: ts.map(e => (e.textContent || '').trim())};"
            "t.click(); return {ok: true};"
            "})()")


_JS_ROWS = r"""
(() => {
  const w = __WANT__;
  const ts = [...document.querySelectorAll('li.dui-tabs-bar-item')];
  const t = ts.find(e => (e.textContent || '').replace(/[\s*]/g, '') === w);
  let root = document, n = t;
  for (let i = 0; i < 6 && n; i++) {
    if (n.querySelector && n.querySelector('.record-item-box')) { root = n; break; }
    n = n.parentElement;
  }
  const rows = [...root.querySelectorAll('.record-item-box')];
  const names = rows.map(b => {
    const e = b.querySelector('.item-name');
    return e ? (e.textContent || '').trim() : '';
  }).filter(Boolean);
  return {count: names.length, names: names};
})()
"""


def _js_rows(want):
    return _JS_ROWS.replace("__WANT__", json.dumps(want))


def login_state(pg):
    """(logged_in, detail).  Only the collection owner reaches the result view; anonymous
    visitors land on '#/fill' plus a login panel.

    The href ALONE is not evidence: setting the fragment is a same-document navigation, so a
    page can read '#/result' while the SPA is still showing the fill view.  Require the
    fragment or the '打卡情况' heading AND the statistics DOM to actually exist."""
    href = str(pg.ev("location.href") or "")
    txt = str(pg.ev("document.body?document.body.innerText.slice(0,1500):''") or "")
    stats = pg.ev("document.querySelectorAll('.collection-statistic-item').length")
    has_stats = isinstance(stats, int) and stats > 0
    if ("#/result" in href or "打卡情况" in txt) and has_stats:
        return True, "result view (%d statistic items)" % stats
    if "登录腾讯文档" in txt or "登录后才能填写" in txt:
        return False, "login prompt shown"
    if "#/result" in href or "打卡情况" in txt:
        return False, "result route but no statistics DOM (session/SPA stalled)"
    return False, "unrecognised page (href=%s)" % href[:90]


def result_url(url, frag="/result"):
    """Add a cache-busting query AND the result fragment to a clock-in form URL.

    Navigating to a URL that differs ONLY in its fragment is a same-document navigation: the
    browser does not reload, so the React SPA keeps whatever view it was on (typically the
    owner's '#/fill-detail').  Adding a unique query forces a real document load, which is what
    actually renders the statistics view."""
    if not url:
        return url
    base = url.split("#", 1)[0]
    base += ("&" if "?" in base else "?") + "_t=%d&nlc=1" % int(time.time() * 1000)
    return base + "#" + (frag or "/result").lstrip("#")


def wake_stats(pg, wait=20):
    """Force the statistics view to RE-FETCH the current cycle.

    A freshly loaded '#/result' route is not merely stale, it is WRONG: observed live, a direct
    load reported 未打卡人数=2 with zero rendered rows (a leftover snapshot), while the very same
    document after visiting 打卡 and coming back to 统计 reported the true 未打卡人数=100 with all
    100 rows rendered.  The SPA only refetches when the view is re-entered, so this visit is
    mandatory - never trust a counter read straight after navigation.

    Verified sequence: click the 打卡 tab -> pause -> click the 统计 tab -> statistics DOM
    returns with current data (both tabs are li.dui-tabs-bar-item, so the same clicker works)."""
    pg.call("Page.enable")
    a = pg.ev(_js_click_tab("打卡"))
    if not (isinstance(a, dict) and a.get("ok")):
        return False
    rem = _remaining()
    time.sleep(min(3.5, rem if rem is not None else 3.5))
    b = pg.ev(_js_click_tab("统计"))
    if not (isinstance(b, dict) and b.get("ok")):
        return False
    return _await_stats(pg, wait)


def select_day(pg, day):
    """Select an integer day in the displayed month, or an exact date across months.

    Exact dates navigate the calendar header and match the cell's full date title.
    A missing/disabled date or a mismatched result header fails closed.
    """
    target = day if isinstance(day, datetime.date) else None
    opened = pg.ev("(() => {const t=document.querySelector('.calendar-date-title');"
                   "if(!t) return false; t.click(); return true;})()")
    if not opened:
        return False
    time.sleep(0.5)
    if target:
        for _ in range(25):
            shown = pg.ev("(() => {const y=document.querySelector('.dui-datepicker-header-year');"
                          "const m=document.querySelector('.dui-datepicker-header-month');"
                          "return y&&m ? [parseInt(y.textContent),parseInt(m.textContent)] : null;})()")
            if not isinstance(shown, list) or len(shown) != 2 or not all(shown):
                return False
            delta = (target.year - shown[0]) * 12 + target.month - shown[1]
            if delta == 0:
                break
            if _ == 24 or _remaining() == 0:
                return False
            selector = '.dui-datepicker-header-prev' if delta < 0 else '.dui-datepicker-header-next'
            clicked = pg.ev("(() => {const b=document.querySelector(%s);"
                            "if(!b || /disabled/.test(b.className)) return false;"
                            "b.click();return true;})()" % json.dumps(selector))
            if not clicked:
                return False
            time.sleep(0.3)
        wanted = "%d年%d月%d日" % (target.year, target.month, target.day)
        condition = "td.title === %s" % json.dumps(wanted)
    else:
        condition = ("!/last-month-cell|next-month-cell/.test(td.className) && "
                     "td.querySelector('.dui-calendar-date')?.textContent.trim() === %s"
                     % json.dumps(str(day)))
    result = pg.ev("(() => {const hit=[...document.querySelectorAll('td.dui-calendar-cell')]"
                   ".find(td=>%s);if(!hit || /disabled/.test(hit.className)) return false;"
                   "hit.click();return true;})()" % condition)
    if not result:
        return False
    if target:
        for _ in range(20):
            header = pg.ev("document.querySelector('.statistics-section-header')?.textContent || ''")
            match = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", str(header))
            if match and tuple(map(int, match.groups())) == (target.month, target.day):
                return True
            if _remaining() == 0:
                break
            time.sleep(0.3)
        return False
    return True


def navigate(pg, url, wait=16, day=None):
    """Open the form on its result view and make sure the numbers are CURRENT.

    Two independent routes are known to work; the query-busting navigate is tried first and the
    hash+hard-reload is the fallback (both verified against the live form).  Either way the view
    is then woken with wake_stats(), because a plain load may show a stale snapshot."""
    pg.call("Page.enable")
    pg.call("Page.navigate", {"url": result_url(url)})
    ok = _await_stats(pg, wait)
    if not ok and _remaining() != 0:
        pg.ev("location.hash='#/result'")
        pg.call("Page.reload", {"ignoreCache": True})
        ok = _await_stats(pg, wait)
    ok = wake_stats(pg) and ok
    if day is not None:
        # 选日期失败以前被静默吞掉 -> 回测时读到的是"今天"的数据，比报错危险得多
        if not select_day(pg, day):
            pg.day_error = "选日期 %s 失败（日历里找不到这一格）" % day
            ok = False
        else:
            time.sleep(min(3.0, _remaining() if _remaining() is not None else 3.0))
    return ok


def _await_stats(pg, wait):
    deadline = time.time() + wait
    rem = _remaining()
    if rem is not None:
        deadline = min(deadline, time.time() + rem)
    while time.time() < deadline:
        time.sleep(1.0)
        st = pg.ev("document.querySelectorAll('.collection-statistic-item').length")
        if isinstance(st, int) and st > 0:
            time.sleep(1.0)
            return True
    return False


# --------------------------------------------------------------------------- launch
def kill_profile_edge():
    """Last resort before relaunching: an Edge already holding our profile dir makes the new
    process exit with 'ProcessSingleton for your profile directory'.  Kill only processes whose
    command line mentions our profile (never the user's everyday Edge)."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
          "Where-Object { $_.CommandLine -like '*dcs-clockin*' } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $_.ProcessId }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=30)
        ids = [x for x in (r.stdout or "").split() if x.strip().isdigit()]
        print("  killed %d Edge process(es) holding the dedicated profile" % len(ids))
        return ids
    except subprocess.TimeoutExpired:
        print("  ! killing profile Edge timed out")
        return []


def open_browser(url=None, wait=6, window=(1300, 880)):
    """Ensure Edge is up on the dedicated profile (launching it if needed) and navigate."""
    if port_open(PORT):
        print("Edge already listening on %d" % PORT)
    else:
        os.makedirs(PROFILE, exist_ok=True)
        exe = edge_exe()
        args = ([exe, "--remote-debugging-port=%d" % PORT, "--user-data-dir=%s" % PROFILE]
                + LAUNCH_ARGS + ["--window-size=%d,%d" % window, url or "about:blank"])
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP and DEVNULL stdio: capturing a child's
        # stdio keeps it tied to us and can take Edge down with us.
        p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL,
                             creationflags=0x00000008 | 0x00000200)
        print("launched Edge pid=%d profile=%s" % (p.pid, PROFILE))
        up = False
        for i in range(30):
            time.sleep(1)
            rc = p.poll()
            if rc is not None:
                print("  Edge exited rc=%d after %ds - retrying once after clearing the profile lock"
                      % (rc, i + 1))
                kill_profile_edge()
                time.sleep(2)
                p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     stdin=subprocess.DEVNULL,
                                     creationflags=0x00000008 | 0x00000200)
                for j in range(30):
                    time.sleep(1)
                    if p.poll() is not None:
                        raise RuntimeError("Edge exited again rc=%s" % p.poll())
                    if port_open(PORT):
                        print("  CDP up at t+%ds (after retry)" % (j + 1))
                        up = True
                        break
                break
            if port_open(PORT):
                print("CDP up at t+%ds" % (i + 1))
                up = True
                break
        if not up and not port_open(PORT):
            raise RuntimeError("CDP port never opened")
    v = http_json("http://127.0.0.1:%d/json/version" % PORT)
    print("browser:", v.get("Browser"))
    write_state(port=PORT, profile=PROFILE, launched_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    if url:
        pg = Page()
        pg.call("Page.enable")
        navigate(pg, url, wait=max(wait, 16))
        pg.close()
    return True


def close_browser(timeout=8):
    """Ask Edge to close and PROVE it actually did.  旧版发完 Browser.close 就 sleep 1.5
    返回 True，也不关 websocket：关不掉时下一个任务以为浏览器已经没了。"""
    if not port_open(PORT):
        return True
    try:
        v = http_json("http://127.0.0.1:%d/json/version" % PORT)
        b = websocket.create_connection(v["webSocketDebuggerUrl"], timeout=10,
                                       suppress_origin=True)
        try:
            b.send(json.dumps({"id": 1, "method": "Browser.close"}))
            time.sleep(0.5)
        finally:
            try:
                b.close()
            except Exception:                               # noqa: BLE001
                pass
    except Exception as e:                                  # noqa: BLE001
        print("close_browser: %s: %s" % (type(e).__name__, str(e)[:120]))
    end = time.time() + timeout
    while time.time() < end:
        if not port_open(PORT):
            return True
        time.sleep(0.3)
    return not port_open(PORT)


# --------------------------------------------------------------------------- reading
def read_tab(pg, want, expect=None, tries=3, settle=2.5):
    """Click a name-list tab and return its rows, re-trying while the count disagrees with
    `expect` (the list renders asynchronously and the counter is the ground truth)."""
    last = {"count": 0, "names": []}
    for i in range(tries):
        rem = _remaining()
        if rem == 0:
            return {"ok": False, "count": last["count"], "names": last["names"],
                    "why": "读取总预算 %ss 已用完" % int(BUDGET)}
        c = pg.ev(_js_click_tab(want))
        if not (isinstance(c, dict) and c.get("ok")):
            return {"ok": False, "count": 0, "names": [],
                    "why": "tab %r not found" % want,
                    "tabs": (c or {}).get("tabs") if isinstance(c, dict) else None}
        time.sleep(min(settle, rem) if rem is not None else settle)
        r = pg.ev(_js_rows(want))
        if isinstance(r, dict) and r.get("names"):
            last = r
            if expect is None or r["count"] == expect:
                return {"ok": True, "count": r["count"], "names": r["names"], "tries": i + 1}
        time.sleep(1.0)
    return {"ok": True, "count": last["count"], "names": last["names"], "tries": tries,
            "mismatch": expect is not None and last["count"] != expect}


def canon_name(s):
    """Canonical form of a person's name as it appears in the form or the roster.

    Tencent Docs appends a self-marker to the logged-in user's own row: 已填写 rendered
    `刁闯胜（我）` where the roster says `刁闯胜`.  Left as-is that one person never matches
    exactly, so they are silently treated as "not on the form" and never @-mentioned.  Strip the
    marker (plus whitespace) so both sides compare on the real name.  Exact match is still the
    rule afterwards - no fuzzy matching, per the spec."""
    if s is None:
        return ""
    t = re.sub(r"[\s\u3000]+", "", str(s))
    t = re.sub(r"[（(]\s*我\s*[)）]$", "", t)
    return t


def blank_status(why):
    """read_status 的失败形状：字段齐全，why 写真相。"""
    return {"logged_in": False, "why": why, "href": "", "title": "",
            "header": None, "counters": {}, "unchecked": [], "checked": [],
            "unchecked_ok": False, "checked_ok": False, "ambiguous": []}


def read_status(url=None, wait=16, day=None):
    """Open the form and read the day's status.

    Returns: logged_in, why, href, title, header, counters, unchecked, checked,
    unchecked_ok (row count matched the 未打卡人数 counter), checked_ok, ambiguous.

    未填写 is the load-bearing list: it renders completely (verified: 100 rows == 未打卡人数 100)
    and the form is COLLEGE-WIDE, so the class's non-submitters are 未填写 ∩ roster.  已填写 is
    read only as a cross-check and is allowed to fail - a failed 已填写 must never lose the
    daily run, because the class list does not depend on it.
    """
    global _DEADLINE
    _DEADLINE = time.time() + BUDGET          # 整轮总预算，超了就报错，不拖 22:00
    pg = None
    try:
        try:
            pg = Page()
            pg.call("Page.enable")
            pg.call("Runtime.enable")
        except RuntimeError as e:
            return blank_status("CDP 连接失败：%s" % e)
        if url:
            try:
                nav_ok = navigate(pg, url, wait=wait, day=day)
            except RuntimeError as e:
                return blank_status("CDP 读取中断：%s" % e)
            if not nav_ok:
                why = getattr(pg, "day_error", None) or "页面没进入统计视图"
                return blank_status(why)
        ok, why = login_state(pg)
        out = {"logged_in": ok, "why": why, "href": str(pg.ev("location.href")),
               "title": str(pg.ev("document.title") or ""),
               "header": None, "counters": {}, "unchecked": [], "checked": [],
               "unchecked_ok": False, "checked_ok": False, "ambiguous": []}
        if not ok:
            return out
        c = pg.ev(JS_COUNTS)
        if isinstance(c, dict):
            out["header"] = c.get("header")
            out["counters"] = c.get("counters") or {}
        n_open = out["counters"].get("未打卡人数")
        n_done = out["counters"].get("打卡人数")
        ru = read_tab(pg, TAB_OPEN, expect=n_open)
        out["unchecked"] = [canon_name(n) for n in (ru.get("names") or [])]
        out["unchecked_ok"] = bool(ru.get("ok")) and (n_open is None or ru.get("count") == n_open)
        if not ru.get("ok"):
            out["unchecked_why"] = ru.get("why") or "未填写名单没读到"
        # 已填写 is a bonus cross-check only; its tab click is known to get lost, so do not depend
        try:
            rd = read_tab(pg, TAB_DONE, expect=n_done, tries=2)
            out["checked"] = [canon_name(n) for n in (rd.get("names") or [])]
            out["checked_ok"] = bool(rd.get("ok")) and (n_done is None or rd.get("count") == n_done)
        except Exception as e:                                # noqa: BLE001 - never fatal
            out["checked_why"] = "已填写 read failed: %s" % e
        out["ambiguous"] = sorted(set(out["unchecked"]) & set(out["checked"]))
        return out
    finally:
        _DEADLINE = None
        if pg is not None:
            pg.close()


def who_is_missing(status, roster):
    """Split the class roster against the form's 未填写 list.

    The form collects for the whole college, so a class member's absence from 未填写 is what
    matters - not the form's overall numbers.  Returns
        missing   : class members NOT in 未填写  -> the people to @
        unmatched : class names that appear in neither list -> not on the form at all, so they
                    cannot be judged; the spec says log these, never @ them
        not_on_form_reason
    """
    unchecked = set(status.get("unchecked") or [])
    checked = set(status.get("checked") or [])
    names = [r["name"] for r in roster]
    missing = [n for n in names if n in unchecked]
    covered = unchecked | checked
    # If the 已填写 read failed, absence from 未填写 is still strong evidence of having submitted
    # (the 未填写 list is complete; its row count matched its counter), so only claim "unknown"
    # when we have no cross-check at all AND the person is in neither list.
    unmatched = [n for n in names if n not in covered]
    return {"missing": missing, "unmatched": unmatched,
            "checked_in": [n for n in names if n in checked],
            "unchecked_total": len(unchecked), "checked_total": len(checked),
            "checked_reliable": bool(status.get("checked_ok")),
            "unchecked_reliable": bool(status.get("unchecked_ok"))}


# --------------------------------------------------------------------------- roster
def read_roster(xlsx):
    """Roster Excel -> [{name, sid}].  The 姓名 and 学号 columns are located by header name so
    the user can reorder or extend the sheet freely."""
    import openpyxl
    wb = openpyxl.load_workbook(xlsx, data_only=True, read_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not rows:
        return []
    hdr = [str(c).strip() if c is not None else "" for c in rows[0]]
    i_name = hdr.index("姓名") if "姓名" in hdr else (1 if len(hdr) > 1 else 0)
    i_sid = hdr.index("学号") if "学号" in hdr else None
    out = []
    for r in rows[1:]:
        if not r or i_name >= len(r) or r[i_name] is None:
            continue
        nm = str(r[i_name]).strip()
        if not nm:
            continue
        sid = ""
        if i_sid is not None and i_sid < len(r) and r[i_sid] is not None:
            sid = str(r[i_sid]).strip()
        out.append({"name": canon_name(nm), "sid": sid})
    return out


# --------------------------------------------------------------------------- cli
def cmd_check(args):
    st = read_state()
    if not port_open(st.get("port", PORT)):
        print("dedicated Edge is NOT running - run: python docs.py open")
        return 2
    pg = Page(st.get("port", PORT))
    ok, why = login_state(pg)
    print("logged in: %s  (%s)" % (ok, why))
    print("href:", pg.ev("location.href"))
    if ok:
        c = pg.ev(JS_COUNTS)
        if isinstance(c, dict):
            print("header:", c.get("header"))
            print("counters:", c.get("counters"))
    pg.close()
    return 0 if ok else 1


def cmd_read(args):
    url = args.url or read_state().get("form_url")
    if not url:
        print("no form url known; pass --url", file=sys.stderr)
        return 2
    d = read_status(url, wait=args.wait, day=getattr(args, "day", None))
    print(json.dumps({k: v for k, v in d.items() if k not in ("unchecked", "checked")},
                     ensure_ascii=False, indent=1))
    print("unchecked:", " ".join(d["unchecked"]))
    print("checked  :", " ".join(d["checked"]))
    if getattr(args, "roster", None):
        roster = read_roster(args.roster)
        w = who_is_missing(d, roster)
        print("\nROSTER %d people" % len(roster))
        print("to @ (%d): %s" % (len(w["missing"]), " ".join(w["missing"])))
        print("unmatched (%d): %s" % (len(w["unmatched"]), " ".join(w["unmatched"])))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        print("wrote", args.json)
    return 0 if d["logged_in"] else 1


def cmd_who(args):
    url = args.url or read_state().get("form_url")
    if not url:
        print("no form url known; pass --url", file=sys.stderr)
        return 2
    d = read_status(url, wait=16, day=args.day)
    roster = read_roster(args.roster)
    w = who_is_missing(d, roster)
    print("date/header :", d["header"])
    print("counters    :", d["counters"])
    print("form rows   : unchecked %d, checked %d (%s)"
          % (w["unchecked_total"], w["checked_total"],
             "ok" if d["unchecked_ok"] else "MISMATCH vs 未打卡人数"))
    print("roster      : %d" % len(roster))
    print("TO @ (%d)   :" % len(w["missing"]), " ".join(w["missing"]))
    print("unmatched(%d):" % len(w["unmatched"]), " ".join(w["unmatched"]))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"status": d, "who": w}, f, ensure_ascii=False, indent=1)
        print("wrote", args.json)
    return 0 if d["logged_in"] else 1


def cmd_open(args):
    open_browser(args.url)
    return 0


def cmd_close(args):
    close_browser()
    return 0


def main():
    ap = argparse.ArgumentParser(description="read Tencent Docs clock-in status")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("open")
    p.add_argument("--url")
    p.set_defaults(fn=cmd_open)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    p = sub.add_parser("read")
    p.add_argument("--url")
    p.add_argument("--json")
    p.add_argument("--wait", type=int, default=16)
    p.add_argument("--day", help="back-test a day of the current month, e.g. 18")
    p.add_argument("--roster", help="roster .xlsx; prints the class diff too")
    p.set_defaults(fn=cmd_read)
    p = sub.add_parser("who", help="class roster minus the form's 未填写 list")
    p.add_argument("--url")
    p.add_argument("--roster", required=True)
    p.add_argument("--day")
    p.add_argument("--json")
    p.set_defaults(fn=cmd_who)
    sub.add_parser("close").set_defaults(fn=cmd_close)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
