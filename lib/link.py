"""link.py - spec step 1: get today's form link out of Q群管家's group message.

Why this is not a one-line regex
--------------------------------
The link is only ever available as PIXELS in QQ's chat window (QQ exposes no accessibility
tree and its message DB is SQLCipher), so it always passes through Windows OCR first.  OCR
mangles Latin badly and predictably.  A REAL capture of the daily message, verbatim from
logs/qq-panel-ocr.txt:

    ( 381, 322)  @金钵成员 （ 汛 文 档 ] 夕 阳 下 倦 鸟 归 製 （ 适 桥
    ( 380, 366)  https://docs.qg.com/form/uge/clock/DRFQ
    ( 381, 388)  nY1 JNbEtiR2xn

for the true link

    https://docs.qq.com/form/page/clock/EXAMPLE_FORM_ID

i.e. `qq`->`qg`, `page`->`uge` (a character was DROPPED), the id split across two lines, and
inside the id `p`->`Q` and `j`->`i`.  So the prefix is not trustworthy and neither is any single
character of the id.

The design that survives that: never trust OCR, PROVE the link over HTTP.
    * Fetch each candidate with plain HTTP and read its <title>.
    * A real clock-in form answers with the form's own title
      (夕阳下倦鸟归巢（造桥新青年）) and ~57859 bytes.
    * Every wrong id answers 200 with the generic title 腾讯文档 (a login/404 page), ~63893
      bytes and a login word in the body.
    * HTTP status is useless as a discriminator: BOTH answer 200.
Because the title is the discriminator, we can try candidates cheaply and stop at the first
one that proves out.  Candidates are ordered raw -> last-known-good -> single character
confusions -> pairs, so the steady-state daily cost is ONE fetch (the form id is stable: it is
a recurring cycle form, so yesterday's proven id is today's id).

A miss falls through to "no link today", which per spec edge case 3 means send NOTHING and log.

CLI
---
    python link.py from-text logs\\qq-panel-ocr.txt -v   # extract + validate from OCR text
    python link.py from-qq -v                            # capture QQ ourselves, then extract
    python link.py validate https://docs.qq.com/form/page/clock/XXXX
    python link.py seed   https://docs.qq.com/form/page/clock/XXXX
"""
import argparse
import concurrent.futures as cf
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
if sys.stdout:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
STATE = os.path.join(HERE, "..", "logs", "link-state.json")

CANON = "https://docs.qq.com/form/page/clock/%s"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
BAD_TITLE = "腾讯文档"
MAX_TRIES = 240
BATCH = 8

# OCR character confusions, most-likely first.  Both directions are applied because we cannot
# tell which side of the confusion a given glyph came from.  Derived from the real capture
# above (p->Q, j->i) plus the usual Latin/CJK-font lookalikes.
CONFUSE = {
    "Q": "pP9Oo", "p": "QP9", "9": "Qqg", "q": "g9", "g": "q9",
    "i": "j1l", "j": "i1", "1": "ilI", "l": "1iI", "I": "1l",
    "0": "Oo", "O": "0o", "o": "0O", "5": "S", "S": "5", "8": "B", "B": "8",
    "2": "Z", "Z": "2", "u": "p", "n": "m", "m": "n", "r": "t", "t": "r",
    "v": "y", "y": "v", "c": "e", "e": "c", "a": "o", "s": "5", "z": "2",
}

# The id sits right after "clock/" (or the mangled "clock" with a dropped slash).  Anchoring on
# the word "clock" alone is what makes this robust to the prefix being mangled: even
# "docs.qg.com/form/uge/clock/DRFQ" still contains an intact "clock".
ID_RE = re.compile(r"clock[^A-Za-z0-9]{0,3}([A-Za-z0-9]{12,24})", re.I)
ALT_RE = re.compile(r"form/[A-Za-z0-9]{2,6}/([A-Za-z0-9]{12,24})", re.I)
COORD_RE = re.compile(r"\(\s*\d+\s*,\s*\d+\s*\)")


# --------------------------------------------------------------------- OCR text -> candidates
def norm(s):
    """Strip ALL whitespace - Windows OCR pads every CJK glyph and splits Latin runs."""
    return re.sub(r"[\s\u3000]+", "", s or "")


def strip_coords(s):
    """Our OCR dumps prefix every line with '(x, y)'.  Those digits sit BETWEEN the two halves
    of a wrapped link once lines are glued, so they must go before any id parsing."""
    return COORD_RE.sub("", s or "")


def id_runs(text):
    """Pull the raw (still mangled) form id runs out of OCR text.

    Handles the line-wrap the real capture shows: the id is split across two lines
    (".../clock/DRFQ" + "nY1 JNbEtiR2xn").  We try each line, each line glued to its
    following line, and finally the whole text joined - the leading coordinate prefixes are
    removed first so they cannot inject digits into the middle of an id."""
    lines = [norm(strip_coords(ln)) for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln]
    out = []

    def take(s, why):
        for rx in (ID_RE, ALT_RE):
            m = rx.search(s)
            if m:
                out.append((m.group(1), why))
                return True
        return False

    for i, ln in enumerate(lines):
        if take(ln, "line%d" % i):
            continue
        if i + 1 < len(lines):                    # the id may have wrapped onto the next line
            if take(ln + lines[i + 1], "line%d+%d" % (i, i + 1)):
                continue
    if not out:
        take("".join(lines), "joined")
    seen, uniq = set(), []
    for run, why in out:
        if run not in seen:
            seen.add(run)
            uniq.append((run, why))
    return uniq


def confusions(cid, max_subs=2):
    """Bounded BFS over single-character OCR confusions, nearest (fewest substitutions) first."""
    out = [cid]
    frontier = [cid]
    seen = {cid}
    for _ in range(max_subs):
        nxt = []
        for c in frontier:
            for pos, ch in enumerate(c):
                for repl in CONFUSE.get(ch, ""):
                    v = c[:pos] + repl + c[pos + 1:]
                    if v not in seen:
                        seen.add(v)
                        out.append(v)
                        nxt.append(v)
        frontier = nxt
    return out


# --------------------------------------------------------------------------- HTTP validation
def title_ok(title, expect=None):
    """Is this the RIGHT form, not merely "a page that is not the login screen"?

    `expect` is the class's title_keys (e.g. ['道桥', '02班']).  The old check only rejected the
    login page by exact title, so any other real Tencent form - or a generic title with a
    different suffix - passed and we read a stranger's roster.  When keys are configured, at
    least one must appear in the title; no keys configured keeps the old (weak) behaviour.
    """
    t = norm(title or "")
    if not t or t == BAD_TITLE:
        return False
    keys = [norm(k) for k in (expect or []) if norm(k)]
    if not keys:
        return True
    return any(k in t for k in keys)


def http_title(url, timeout=8, expect=None):
    """Fetch a candidate and report what it really is.  See module docstring: the <title> is
    the only reliable discriminator, HTTP status is not."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        return {"status": e.code, "bytes": 0, "title": "", "ok": False,
                "why": "HTTP %s" % e.code}
    except Exception as e:                                       # noqa: BLE001
        return {"status": 0, "bytes": 0, "title": "", "ok": False,
                "why": "%s: %s" % (type(e).__name__, str(e)[:90])}
    txt = body.decode("utf-8", "replace")
    m = re.search(r"<title[^>]*>(.*?)</title>", txt, re.S | re.I)
    title = norm(re.sub(r"<[^>]+>", "", m.group(1))) if m else ""
    ok = title_ok(title, expect)
    return {"status": status, "bytes": len(body), "title": title, "ok": ok,
            "why": "" if ok else _why_bad_title(title, expect)}


def _why_bad_title(title, expect=None):
    """把"为什么这个标题不算本班表单"说清楚。

    2026-10-21 改动（审计 P1-6）：以前不管哪种失败都打印
    "wrong/login page (<标题>)"，于是 10-21 14:19 那条日志
    `wrong/login page (夕阳下倦鸟归巢（造桥新青年）)` 里的标题明明**就是**本班表单，
    排查的人却以为链接指错了地方。三种情况必须分开说。
    """
    t = norm(title or "")
    if t and title_ok(t, expect):
        return ""                      # 标题本来就是对的，没有"为什么失败"可说
    if not t:
        return "页面没有 <title>，可能不是表单页（或者被重定向到了登录/错误页）"
    if t == BAD_TITLE:
        return "打开的是腾讯文档的登录页，链接可能需要登录才能看"
    keys = [k for k in (expect or []) if norm(k)]
    return ("标题是「%s」，但没有命中配置里的表单标题关键词 %s"
            "（config.json 里这个班的 form_title_keys）" % (title, keys))


def validate(url, timeout=8, expect=None):
    r = http_title(url, timeout=timeout, expect=expect)
    r["url"] = url
    return r


# ------------------------------------------------------------------------------- state cache
def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                            # noqa: BLE001
        return {}


def save_state(**kw):
    st = load_state()
    st.update(kw)
    d = os.path.dirname(os.path.abspath(STATE))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    return st


CACHE_HOURS = float(os.environ.get("DCS_LINK_CACHE_H") or 36)


def last_good(max_hours=None):
    """The cached form url, but only while it is recent.

    The cache used to live forever: once OCR found nothing, yesterday's link was fetched and
    accepted as if it were today's.  A form id is stable, so a recent cache is a good
    short-circuit; an old one is a guess and must not be used.
    """
    st = load_state()
    u = st.get("url") or ""
    if not u:
        return None
    ts = st.get("checked_at") or ""
    try:
        age = (time.time() - time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))) / 3600.0
    except (ValueError, OverflowError):
        age = 1e9
    limit = CACHE_HOURS if max_hours is None else max_hours
    return u if -1e9 < age <= limit else None


def last_good_id():
    st = load_state()
    u = st.get("url") or ""
    m = ID_RE.search(u) or ALT_RE.search(u)
    return m.group(1) if m else None


def seed(url, expect=None):
    """Record a proven link so the daily run costs a single fetch.  Also validates it."""
    r = validate(url, expect=expect)
    if r["ok"]:
        save_state(url=url, id=ID_RE.search(url).group(1) if ID_RE.search(url) else None,
                   title=r["title"], checked_at=time.strftime("%F %T"), seeded=True)
    return r


# ------------------------------------------------------------------------------- extraction
def hamming(a, b):
    """Positional difference count, or 99 when the lengths differ."""
    if a is None or b is None or len(a) != len(b):
        return 99
    return sum(1 for x, y in zip(a, b) if x != y)


def candidates_for(text, include_confusions=True):
    """Ordered, de-duplicated candidate ids with the reason each was generated.

    ORDERING RATIONALE (correctness first, then cost):
      * Only ids derived from TODAY's OCR text are trusted - a stale id is dangerous because
        an earlier cycle's form still exists and would validate happily, so we would read the
        wrong form.  Hence the last-known-good id is a LAST-RESORT candidate, not the first.
      * The form id is stable across cycles, so today's OCR run is normally a 1-2 character
        misread of the known-good id.  Sorting the confusion closure by Hamming distance to
        the known-good id therefore puts the true id in the FIRST batch, which keeps the
        daily run down to one or two HTTP fetches without ever trusting the stale id itself.
    """
    runs = id_runs(text)
    lg = last_good_id() if last_good() else None
    scored, seen = [], set()

    def push(cid, why, key):
        if cid and cid not in seen:
            seen.add(cid)
            scored.append((key, cid, why))

    for run, why in runs:
        # OCR may over-capture (glue the next line's text onto the id): try each truncation.
        # Truncations of a too-long run are near the true id, so they stay in the first batch.
        for L in range(len(run), 11, -1):
            cand = run[:L]
            push(cand, "%s len%d" % (why, L), (hamming(cand, lg), len(run) - L, 0))
            if include_confusions:
                for v in confusions(cand, max_subs=2):
                    push(v, "confusion(%s len%d)" % (why, L),
                         (hamming(v, lg), len(run) - L, hamming(v, cand)))
    if lg:
        push(lg, "last-good(stale-fallback)", (50, 50, 50))
    scored.sort(key=lambda t: t[0])
    return [(c, w) for _, c, w in scored], [r for r, _ in runs]


def extract(text, tries=MAX_TRIES, batch=BATCH, verbose=False, expect=None):
    """OCR text -> {"ok", "url", "id", "title", "tried", "why", "candidates"}.

    `expect` is the class's title_keys; every candidate must match it (see title_ok)."""
    order, runs = candidates_for(text)
    tried = []
    i = 0
    while i < len(order) and i < tries:
        chunk = order[i:i + batch]
        res = {}
        with cf.ThreadPoolExecutor(max_workers=min(batch, len(chunk))) as ex:
            futs = {ex.submit(validate, CANON % cid, 8, expect): (cid, why)
                    for cid, why in chunk}
            for f in cf.as_completed(futs):
                cid, why = futs[f]
                try:
                    res[cid] = f.result()
                except Exception as e:                           # noqa: BLE001
                    res[cid] = {"ok": False, "title": "", "status": 0,
                                "why": "%s: %s" % (type(e).__name__, str(e)[:80])}
        for cid, why in chunk:                                   # keep the intended order
            r = res.get(cid) or {"ok": False, "title": "", "status": 0, "why": "no result"}
            tried.append({"id": cid, "from": why, "status": r.get("status"),
                          "title": r.get("title", ""), "ok": bool(r.get("ok")),
                          "why": r.get("why", "")})
            if verbose:
                print("  try %-22s %-16s %s" % (cid, why, r.get("title") or r.get("why")),
                      file=sys.stderr)
            if r.get("ok"):
                url = CANON % cid
                save_state(url=url, id=cid, title=r["title"],
                           checked_at=time.strftime("%F %T"))
                return {"ok": True, "url": url, "id": cid, "title": r["title"],
                        "from": why, "tried": tried, "candidates": len(order),
                        "why": "title ok"}
        i += batch
    return {"ok": False, "url": None, "id": None, "title": None, "tried": tried,
            "candidates": len(order),
            "why": ("no candidate validated (tried %d of %d; raw runs: %s)"
                    % (len(tried), len(order), ",".join(runs) or "none"))}


def from_qq(verbose=False, expect=None):
    """Capture QQ's group chat ourselves and extract from it.  Any failure here is a
    'cannot read QQ' condition, which per spec must never be silently ignored."""
    import win
    w = win.find_panel(300)
    if not w:
        return {"ok": False, "why": "no drivable QQ panel (fail safe: nothing sent)",
                "url": None, "tried": [], "candidates": 0}
    txt = win.ocr_window(w, tag="qq-link")
    if not txt.strip() or txt.lstrip().startswith("#"):
        # ocr_window 失败时第一行是 "# OCR failed.../capture failed"，不能当文本继续抽
        return {"ok": False, "why": "QQ OCR produced no usable text: %s"
                % (txt.strip().splitlines() or ["<empty>"])[0][:120],
                "url": None, "tried": [], "candidates": 0}
    d = os.path.join(HERE, "..", "logs")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "qq-link-ocr.txt"), "w", encoding="utf-8") as f:
        f.write(txt)
    r = extract(txt, verbose=verbose, expect=expect)
    r["ocr_text"] = txt
    return r


# ------------------------------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="spec step 1: find today's form link")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("from-text")
    p.add_argument("path")
    p.add_argument("--json")
    p.add_argument("-v", "--verbose", action="store_true")
    p = sub.add_parser("from-qq")
    p.add_argument("--json")
    p.add_argument("--text")
    p.add_argument("-v", "--verbose", action="store_true")
    p = sub.add_parser("validate")
    p.add_argument("url")
    p = sub.add_parser("seed")
    p.add_argument("url")
    a = ap.parse_args()

    if a.cmd == "validate":
        r = validate(a.url)
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 0 if r["ok"] else 1

    if a.cmd == "seed":
        r = seed(a.url)
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 0 if r["ok"] else 1

    if a.cmd == "from-text":
        with open(a.path, encoding="utf-8") as f:
            r = extract(f.read(), verbose=a.verbose)
    else:
        r = from_qq(verbose=a.verbose)

    pr = {k: v for k, v in r.items() if k != "ocr_text"}
    print(json.dumps(pr, ensure_ascii=False, indent=1))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=1)
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
