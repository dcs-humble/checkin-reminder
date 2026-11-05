"""config.py - 多班级配置，整个程序的唯一真相（single source of truth）。

一份 config.json 描述"哪些班、每个班用什么花名册、什么时间提醒几次、提醒文案是什么"。
界面只改这个文件；计划任务、无人值守运行都从它派生，不各自存一份。

为什么把班级做成数据而不是代码
------------------------------
原来 main.py 里 ROSTER / COPY / at.chat_ok("道桥2024") 三处是写死的。
要通用到别的班，只要把这三样变成"每个班一组参数"，
main.py 那套已经验证过的发送流程一行都不用动（见 runner.py）。
"""
import json
import os
import re
import shutil
import time

APP = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(APP)                 # …\打卡自动提醒
CONFIG = os.path.join(APP, "config.json")
LOGS = os.path.join(ROOT, "logs")
RUNTIME = os.path.join(ROOT, "runtime", "python")

# 文案末的 👇 是原版就有的（lib/main.py 的 COPY），别弄丢：它指着下面被 @ 的一串人。
# 不是 emoji 强迫症——21:20 实发那次群里的原文就带 👇。
DEFAULT_COPY_FIRST = "今天还没打卡的同学请尽快完成哦，截止 24:00👇 "
DEFAULT_COPY_LAST = "最后 1 小时！以下同学还没打，请抓紧👇 "

# config.json 被隔离重建时置为非空，调用方（GUI / launch.pyw / selfcheck）
# 据此告警。以前是完全静默的：夜里配置坏了→默认配置写回磁盘→
# "班级全丢了"却没人知道原件还在 config.json.bad-* 里。
LAST_LOAD_WARN = ""


def _default_class():
    """首次启动时按现有部署填好，用户不用从零配。"""
    roster = ""
    return {
        "id": "my-class",
        "name": "我的班级",
        # 群标题栏里必须出现其中任意一个词，否则拒发（沿用 main.panel 的校验思路）
        "title_keys": ["请填写群名称关键词"],
        "roster": roster,
        "form_url": "",
        "enabled": True,
        "schedules": [
            {"time": "22:00", "copy": DEFAULT_COPY_FIRST, "enabled": True},
            {"time": "23:00", "copy": DEFAULT_COPY_LAST, "enabled": True},
        ],
    }


def default_config():
    return {
        "version": 1,
        "active": "my-class",
        "classes": [_default_class()],
    }


# ------------------------------------------------------------------ 读写
def _quarantine():
    bad = CONFIG + ".bad-%s" % time.strftime("%Y%m%d-%H%M%S")
    try:
        os.replace(CONFIG, bad)
    except OSError:
        pass
    return bad


def load():
    """读配置；文件不存在或坏了就返回默认值并立刻落盘，绝不静默丢配置。"""
    global LAST_LOAD_WARN
    if not os.path.isfile(CONFIG):
        cfg = default_config()
        save(cfg)
        return cfg
    try:
        with open(CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            raise ValueError("config.json 的顶层不是对象")
    except Exception as e:                                   # noqa: BLE001
        bad = _quarantine()
        LAST_LOAD_WARN = ("config.json 损坏，已隔离成 %s 并重置为默认配置"
                          "（%s: %s）" % (os.path.basename(bad), type(e).__name__, e))
        cfg = default_config()
        save(cfg)
        return cfg
    # "合法 JSON 但结构坏"（classes 里有字符串、schedules 是 null ……）以前会在这里
    # KeyError/TypeError，App.__init__ 没有保护，pythonw 下窗口干脆不出现。宁可隔离后重建。
    try:
        cfg.setdefault("classes", [])
        if not isinstance(cfg["classes"], list):
            raise ValueError("classes 不是数组")
        for c in cfg["classes"]:
            if not isinstance(c, dict) or not c.get("id"):
                raise ValueError("classes 里有残缺项")
        cfg.setdefault("active", cfg["classes"][0]["id"] if cfg["classes"] else "")
        cfg.pop("confirm_before_send", None)         # 老配置里留着这个键，顺手清掉
        for c in cfg["classes"]:
            c.setdefault("schedules", [])
            c.setdefault("title_keys", [])
            c.setdefault("form_url", "")
            c.setdefault("form_title_keys", [])    # 表单网页标题的键（≠QQ群名 title_keys）
            c.setdefault("enabled", True)
            if not isinstance(c["schedules"], list):
                raise ValueError("%s 的 schedules 不是数组" % c.get("id"))
            for s in c["schedules"]:
                if not isinstance(s, dict):
                    raise ValueError("%s 的 schedules 里有残缺项" % c.get("id"))
                s.setdefault("enabled", True)
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        bad = _quarantine()
        LAST_LOAD_WARN = ("config.json 结构损坏，已隔离成 %s 并重置为默认配置"
                          "（%s: %s）" % (os.path.basename(bad), type(e).__name__, e))
        cfg = default_config()
        save(cfg)
        return cfg
    # 花名册指着程序外面（桌面/下载）时，load 时就地迁进 data\\——
    # 桌面文件被改名/删除不会让半夜的任务断粮（2026-10-21 审计 P1）。
    if _localize_rosters(cfg):
        try:
            save(cfg)
        except Exception:                                # noqa: BLE001
            pass
    return cfg


def save(cfg):
    os.makedirs(APP, exist_ok=True)
    # tmp 名带 pid：GUI 和计划任务两个进程同时保存时，固定名会交错写出撕裂 JSON。
    tmp = CONFIG + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())            # 磁盘满/配额错在这里就抛，而不是"看着保存成功"
    os.replace(tmp, CONFIG)
    try:                                # 目录项也刷一次，断电后不至于只剩 .tmp
        d = os.open(APP, os.O_RDONLY)
        try:
            os.fsync(d)
        finally:
            os.close(d)
    except OSError:
        pass


# ------------------------------------------------------------------ 查询
def get_class(cfg, cid):
    for c in cfg.get("classes", []):
        if c["id"] == cid:
            return c
    return None


def active_class(cfg):
    return get_class(cfg, cfg.get("active")) or (cfg["classes"][0] if cfg["classes"] else None)


def slot_of(time_str):
    """'22:00' -> '2200'。slot 是 COPY 表的键，也是任务名的一部分。"""
    return re.sub(r"\D", "", time_str or "").rjust(4, "0")[:4]


def enabled_schedules(c):
    return [s for s in c.get("schedules", []) if s.get("enabled", True)]


# ------------------------------------------------------------------ 校验
def new_class_id(cfg, name):
    base = re.sub(r"[^A-Za-z0-9]+", "", name.encode("ascii", "ignore").decode()) or "cls"
    base = base.lower()[:12]
    used = {c["id"] for c in cfg.get("classes", [])}
    if base not in used:
        return base
    i = 2
    while "%s%d" % (base, i) in used:
        i += 1
    return "%s%d" % (base, i)


def duplicate_class(cfg, cid, new_name=None):
    src = get_class(cfg, cid)
    if not src:
        return None
    c = json.loads(json.dumps(src))
    c["name"] = new_name or (src["name"] + "（副本）")
    c["id"] = new_class_id(cfg, c["id"])
    c["roster"] = ""
    c["form_url"] = ""
    cfg["classes"].append(c)
    return c


def validate_class(c):
    """返回 [(level, 说明)]；level 为 'error' 时绝不允许发送。"""
    out = []
    if not (c.get("name") or "").strip():
        out.append(("error", "班级名称为空"))
    if not (c.get("title_keys") or []):
        out.append(("error", "没有群标题关键词——无法校验当前打开的是不是这个班"))
    r = c.get("roster") or ""
    if not r:
        out.append(("error", "没有花名册"))
    elif not os.path.isfile(r):
        out.append(("error", "花名册不存在：%s" % r))
    else:
        # 读得出来才算数：文件在但表头坏了/被锁住，半夜一样炸（读不出人=error）。
        n, _suf, probs = roster_summary(r)
        if n == 0:
            out.append(("error", "花名册读不出人：%s"
                        % (probs[0] if probs else r)))
    if not enabled_schedules(c):
        out.append(("warn", "这个班没有启用任何提醒时间"))
    for s in enabled_schedules(c):
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", s.get("time", "")):
            out.append(("error", "时间格式不对：%r（应为 HH:MM）" % s.get("time")))
        if not (s.get("copy") or "").strip():
            out.append(("warn", "%s 的提醒文案是空的" % s.get("time")))
    slots = [slot_of(s.get("time")) for s in enabled_schedules(c)]
    dup = sorted({x for x in slots if slots.count(x) > 1})
    if dup:
        # 同一时刻两条 = 任务名相同（打卡提醒-<id>-<slot>），互相覆盖只剩一条。
        out.append(("error", "两个提醒时间重复（%s），计划任务会互相覆盖"
                    % "、".join("%s:%s" % (x[:2], x[2:]) for x in dup)))
    return out


DATA = os.path.join(ROOT, "data")           # 花名册的本地副本放这里


def stash_roster(cid, src):
    """把选中的 xlsx 复制到 data\\<班级id>.xlsx，返回实际要用的路径。

    为什么复制（2026-10-21）：原来配置里存的是 `C:\\Users\\...\\Desktop\\道桥2024-02班 (1).xlsx`。
    桌面上那个文件被改名、被移动、被 WPS 锁住、或者哪天顺手删了，半夜那次任务就
    卡在"打不开花名册"——而这事没人会提前知道。程序自己存一份，就不会被别人的
    桌面收拾习惯牵着走。

    复制失败（文件被 Excel 独占打开等）就退回原路径，不影响当天能用。
    """
    try:
        os.makedirs(DATA, exist_ok=True)
        ext = os.path.splitext(src)[1].lower() or ".xlsx"
        dst = os.path.join(DATA, "%s%s" % (cid, ext))
        if os.path.abspath(src) == os.path.abspath(dst):
            return src
        shutil.copy2(src, dst)
        return dst
    except Exception:                                        # noqa: BLE001
        return src




def _localize_rosters(cfg):
    """load() 时把指向程序外面的花名册复制进 data\\ 并改指过去。返回是否有改动。

    配置里原来存的是桌面路径：文件被改名/移动/删除，当晚 runner.read_roster
    就抛异常、整夜不发（2026-10-21 审计 P1）。stash 失败保持原路径（不影响当天
    能用），真读不出人会被 validate_class 抓出来，不会无声断粮。
    """
    changed = False
    for c in cfg.get("classes", []):
        src = (c.get("roster") or "").strip()
        if not src:
            continue
        try:
            if os.path.abspath(src).startswith(os.path.abspath(DATA) + os.sep):
                continue
        except Exception:                                    # noqa: BLE001
            continue
        if not os.path.isfile(src):
            continue
        dst = stash_roster(c["id"], src)
        if os.path.abspath(dst) != os.path.abspath(src):
            c["roster"] = dst
            changed = True
    return changed


def roster_summary(path):
    """读花名册做一次体检，返回 (人数, 后四位, 问题列表)。不依赖 lib/，界面可单独调用。"""
    try:
        import sys
        lib = os.path.join(ROOT, "lib")
        if lib not in sys.path:
            sys.path.insert(0, lib)
        import docs
        people = docs.read_roster(path)
    except Exception as e:                                   # noqa: BLE001
        return 0, [], ["读不了这个文件：%s" % e]
    problems = []
    if not people:
        return 0, [], ["没有读到任何人（需要表头有「姓名」，学号列名「学号」）"]
    suffix = {}
    for p in people:
        sid = (p.get("sid") or "").strip()
        if len(sid) < 4:
            problems.append("%s 没有可用学号，无法 @（需要后四位唯一的学号）" % p["name"])
            continue
        suffix.setdefault(sid[-4:], []).append(p["name"])
    dup = {k: v for k, v in suffix.items() if len(v) > 1}
    for k, v in dup.items():
        problems.append("后四位 %s 重复：%s（@ 会歧义）" % (k, "、".join(v)))
    return len(people), sorted(suffix), problems
