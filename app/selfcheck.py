"""selfcheck.py - 「现在这套东西到底能不能正常发」的一键体检。

为什么要单独一个文件：原来只有一个 `launch.pyw --selftest`，它只回答"import 得了吗"，
而真正会让人半夜收不到提醒的原因，八成不在 import 上，而在这几件事里：

  - 花名册被改名/删掉/换了表头 → 一个都认不出来
  - 计划任务根本没建，或者建了但被禁用
  - 电脑睡眠、唤醒定时器被关 → 到点没醒
  - QQ 没登录 / 群标题关键词填错 → 打开的是别的群
  - 腾讯文档那个 Edge 窗口被关了 → 读不到名单

所以这里做的是一份**能照着想明白的清单**，每条都给出「现在是什么」「该怎么办」，
界面和计划任务共用同一份结果，避免两边说法不一致。

约定：所有检查都不允许抛异常；任何一条挂了都只是变成一条 bad，不能把体检本身搞崩。
"""
import os
import sys
import time

APP = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(APP)

# 2026-10-21 改动（审计 P1-11）：自带的 runtime 是 embedded 发行版，
# python312._pth 让它处于 isolated 模式，sys.path 里**没有当前目录**。
# 所以 `runtime\python\python.exe app\selfcheck.py` 以前会 10 项全报
# "ModuleNotFoundError: No module named 'config'"。这里自己把 app/ 和 lib/
# 塞进去，让 CLI 入口和 GUI 里的调用走同一条路。
for _p in (APP, os.path.join(ROOT, "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

OK, WARN, BAD = "ok", "warn", "bad"
_LEVEL_ORDER = {BAD: 0, WARN: 1, OK: 2}


def _item(key, title, level, detail, fix=""):
    return {"key": key, "title": title, "level": level, "detail": detail, "fix": fix}


def _guard(key, title):
    """把一个可能抛异常的检查包成"失败也只算一条 bad"。"""
    def deco(fn):
        def run(ctx):
            try:
                return fn(ctx)
            except Exception:                                 # noqa: BLE001
                import traceback
                line = traceback.format_exc().strip().splitlines()[-1]
                return _item(key, title, BAD, "检查本身出错了：%s" % line,
                             "把这条连同 logs\\ 里的日志一起发出来。")
        run.__name__ = fn.__name__
        return run
    return deco


# ---------------------------------------------------------------- 各项检查
@_guard("env.python", "运行环境")
def _check_python(ctx):
    v = sys.version_info
    bits = 64 if sys.maxsize > 2 ** 32 else 32
    if v < (3, 9):
        return _item("env.python", "运行环境", BAD,
                     "Python %d.%d 太旧" % (v[0], v[1]), "换成自带 runtime 里的 python。")
    return _item("env.python", "运行环境", OK,
                 "Python %d.%d.%d · %d 位 · %s"
                 % (v[0], v[1], v[2], bits, "pythonw（无控制台）"
                    if sys.executable.lower().endswith("pythonw.exe") else "python"))


@_guard("env.paths", "程序目录")
def _check_paths(ctx):
    import config as cfgmod
    miss = [p for p in (APP, os.path.join(ROOT, "lib"), os.path.join(ROOT, "logs"),
                        os.path.join(ROOT, "runtime", "python"))
            if not os.path.isdir(p)]
    if miss:
        return _item("env.paths", "程序目录", BAD,
                     "缺目录：" + "、".join(miss), "程序可能被移动过，重新解压一份。")
    # 计划任务的 Command 指向 pythonw.exe——它不在，每晚到点必失败；
    # 只查 isdir(runtime) 以前会漏掉这一种"目录在、exe 没了"的残缺。
    pyw = os.path.join(ROOT, "runtime", "python", "pythonw.exe")
    if not os.path.isfile(pyw):
        return _item("env.paths", "程序目录", BAD,
                     "pythonw.exe 不存在——计划任务调的就是它（%s）" % pyw,
                     "程序目录不完整，重新解压一份 runtime。")
    # 日志目录必须真的写得进去，否则失败告警也是空的
    probe = os.path.join(cfgmod.LOGS, ".selfcheck-probe")
    try:
        os.makedirs(cfgmod.LOGS, exist_ok=True)
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
    except Exception as e:                                    # noqa: BLE001
        return _item("env.paths", "程序目录", BAD,
                     "logs 目录写不进去：%s" % e,
                     "多半是杀毒软件锁了目录，或者程序在只读盘上。")
    return _item("env.paths", "程序目录", OK, "目录齐全，logs 可写")


@_guard("cfg.load", "配置文件")
def _check_config(ctx):
    import config as cfgmod
    cfg = ctx.get("cfg") or cfgmod.load()
    ctx["cfg"] = cfg
    w = getattr(cfgmod, "LAST_LOAD_WARN", "")
    if w:
        return _item("cfg.load", "配置文件", BAD, w,
                     "原件在 config.json.bad-* 里，先确认班级没丢，再重新配一遍。")
    classes = cfg.get("classes") or []
    if not classes:
        return _item("cfg.load", "配置文件", BAD, "一个班级都没有",
                     "在界面左边点「新建」。")
    active = cfg.get("active")
    if not any(c.get("id") == active for c in classes):
        return _item("cfg.load", "配置文件", WARN,
                     "记住的当前班级不存在了（config 里 active=%s）" % active,
                     "在左边重新选一个班，保存一次即可。")
    return _item("cfg.load", "配置文件", OK, "%d 个班级，当前是 %s" % (len(classes), active))


@_guard("cls.valid", "班级配置")
def _check_classes(ctx):
    import config as cfgmod
    cfg = ctx["cfg"]
    errs, warns = [], []
    for c in cfg.get("classes") or []:
        if not c.get("enabled", True):
            continue
        for lv, msg in cfgmod.validate_class(c):
            (errs if lv == "error" else warns).append("%s：%s" % (c.get("name"), msg))
    if errs:
        return _item("cls.valid", "班级配置", BAD, errs[0],
                     "缺东西的班级不会建计划任务，补齐后点保存。" if len(errs) == 1
                     else "还有 %d 条问题，逐条补齐。" % len(errs))
    if warns:
        return _item("cls.valid", "班级配置", WARN, warns[0], "能发，但建议处理掉。")
    return _item("cls.valid", "班级配置", OK, "必填项齐全")


@_guard("roster.read", "花名册")
def _check_roster(ctx):
    import config as cfgmod
    cfg = ctx["cfg"]
    lines, worst = [], OK
    for c in cfg.get("classes") or []:
        if not c.get("enabled", True):
            continue
        path = (c.get("roster") or "").strip()
        if not path:
            lines.append("%s：没选花名册" % c.get("name"))
            worst = BAD
            continue
        if not os.path.isfile(path):
            lines.append("%s：文件不存在（%s）" % (c.get("name"),
                                                 os.path.basename(path)))
            worst = BAD
            continue
        n, suf, probs = cfgmod.roster_summary(path)
        if n == 0:
            lines.append("%s：认不出人（%s）" % (c.get("name"),
                                                probs[0] if probs else "表头没对上"))
            worst = BAD
            continue
        if probs:
            if worst == OK:
                worst = WARN
            lines.append("%s：%d 人，%s" % (c.get("name"), n, probs[0]))
        else:
            lines.append("%s：%d 人，学号后四位无重复" % (c.get("name"), n))
    if not lines:
        lines = ["没有启用的班级"]
    return _item("roster.read", "花名册", worst, "；".join(lines),
                 "重新选花名册；表头必须有「姓名」「学号」两列。" if worst != OK else "")


@_guard("task.exists", "计划任务")
def _check_tasks(ctx):
    import config as cfgmod
    import schedule as schmod
    cfg = ctx["cfg"]
    rows, bad, ok = [], [], 0
    for c in cfg.get("classes") or []:
        if not c.get("enabled", True):
            continue
        for s in cfgmod.enabled_schedules(c):
            slot = cfgmod.slot_of(s.get("time"))
            name = schmod.task_name(c, slot)
            q = schmod.query(name)
            if q.get("error"):
                return _item("task.exists", "计划任务", BAD,
                             "查不到任务状态：%s" % q["error"].splitlines()[0][:120],
                             "多半是权限或系统服务的问题；注销重登一次，或用管理员身份运行一次再试。")
            if not q.get("exists"):
                bad.append("%s 没建" % name)
                continue
            status = str(q.get("status", ""))
            if "禁用" in status or "disabled" in status.lower():
                bad.append("%s 被禁用了" % name)
                continue
            res = str(q.get("result") or "").strip()
            if res and res not in ("0", "267011"):
                bad.append("%s 上次结果 %s（失败）" % (name, res))
                continue
            # 任务存在 ≠ 任务内容对：配置改过但任务没重建时它照样"就绪"，
            # 到点却按旧时间跑。逐字比对命令行和触发时间（读不到 XML 不拦）。
            if schmod.matches(name, c["id"], slot, s.get("time")) is False:
                bad.append("%s 内容和配置不一致（时间或路径对不上）" % name)
                continue
            ok += 1
            rows.append("%s → %s" % (name, q.get("next") or "-"))
    if bad:
        return _item("task.exists", "计划任务", BAD, "；".join(bad[:3]),
                     "点「同步到系统」重建；还是不行就用管理员身份运行一次。")
    if not ok:
        return _item("task.exists", "计划任务", WARN, "没有要检查的任务",
                     "先给班级加一个提醒时间。")
    return _item("task.exists", "计划任务", OK, "；".join(rows[:3]))


@_guard("task.legacy", "老的重复任务")
def _check_legacy(ctx):
    import schedule as schmod
    left = [n for n in ("打卡自动提醒-22", "打卡自动提醒-23")
            if schmod.query(n).get("exists")]
    if left:
        return _item("task.legacy", "老的重复任务", BAD,
                     "还在：" + "、".join(left),
                     "它们跑的是老脚本，会和现在的任务重复发。关掉界面再开一次会自动清理；还在的话点「同步到系统」。")
    return _item("task.legacy", "老的重复任务", OK, "没有残留")


@_guard("docs.port", "腾讯文档窗口")
def _check_docs(ctx):
    import docs
    if docs.port_open():
        return _item("docs.port", "腾讯文档窗口", OK, "调试端口 %d 已打开" % docs.PORT)
    return _item("docs.port", "腾讯文档窗口", WARN,
                 "端口 %d 没开（窗口可能被关了）" % docs.PORT,
                 "到点会自动打开，不用管；想现在确认就点「看看今天谁没打卡」。")


@_guard("qq.window", "QQ 窗口")
def _check_qq(ctx):
    import win
    wins = win.qq_windows(min_size=300)
    if not wins:
        return _item("qq.window", "QQ 窗口", WARN, "没找到 QQ 主窗口",
                     "确认 QQ 已登录、没有最小化到托盘；到点前打开一次即可。")
    return _item("qq.window", "QQ 窗口", OK, "找到 %d 个候选窗口" % len(wins))


@_guard("power.wake", "唤醒定时器")
def _check_wake(ctx):
    import diag
    good, why = diag.wake_timers()
    if good is True:
        return _item("power.wake", "唤醒定时器", OK, why)
    if good is None:
        return _item("power.wake", "唤醒定时器", WARN, why,
                     "查不到不代表不能用；确保电脑插着电、不睡眠即可。")
    return _item("power.wake", "唤醒定时器", WARN, why,
                 "电源选项 → 更改计划设置 → 高级 → 睡眠 → 允许使用唤醒定时器 → 启用。")


@_guard("time.today", "今晚的时间点")
def _check_time(ctx):
    import config as cfgmod
    cfg = ctx["cfg"]
    now = time.strftime("%H%M")
    todo = []
    for c in cfg.get("classes") or []:
        if not c.get("enabled", True):
            continue
        for s in cfgmod.enabled_schedules(c):
            slot = cfgmod.slot_of(s.get("time"))
            todo.append((slot, c.get("name")))
    if not todo:
        return _item("time.today", "今晚的时间点", WARN, "没有启用的时间点",
                     "在「什么时候提醒」里加一个。")
    future = [t for t in todo if t[0] > now]
    if future:
        nxt = min(future)
        return _item("time.today", "今晚的时间点", OK,
                     "下一个是 %s:%s（%s）" % (nxt[0][:2], nxt[0][2:], nxt[1]))
    return _item("time.today", "今晚的时间点", WARN,
                 "今天的时间点都过了（%s 个）" % len(todo),
                 "没问题的话今晚就这样；想补发用「立即提醒一次」。")


@_guard("alert.chain", "失败告警链路")
def _check_alert(ctx):
    """弹窗三层里还剩几层？本机 toast 层 100%% 不存在（winrt 无 ui 子包），
    msg.exe 气泡是无人值守时唯一到点能弹出来的东西，必须确认它还在。"""
    import shutil as _sh
    layers = []
    try:
        import winrt.windows.ui.notifications              # noqa: F401
        layers.append("toast")
    except Exception:                                     # noqa: BLE001
        pass
    if _sh.which("msg"):
        layers.append("气泡(msg.exe)")
    layers.append("弹窗框(MessageBox)")
    hard = [x for x in layers if x != "弹窗框(MessageBox)"]
    if not hard:
        # 家庭版（Home/Core）Windows 系统本身就不带 msg.exe——这是永久性的
        # 环境事实，不是故障；降级为 WARN 并把话说准，免得天天红字没人看。
        sysroot = os.environ.get("SystemRoot", r"C:\Windows")
        home = not os.path.isfile(os.path.join(sysroot, "System32", "msg.exe"))
        return _item("alert.chain", "失败告警链路", WARN,
                     "弹窗降级：只有弹窗框可用%s（写盘 ALERT 永远在）"
                     % ("（家庭版系统不带 msg.exe）" if home else "（msg.exe 不可用）"),
                     "弹窗框会在解锁后停在桌面上，60 秒后自动关闭，不会把任务钉住；"
                     "开界面自动补报 24 小时内的 ALERT 文件；写盘兜底 logs\\alert-fallback.log。")
    return _item("alert.chain", "失败告警链路", OK,
                 "可用：%s；失败必写 logs\\ALERT-*.txt（弹窗方式也记在文件里）"
                 % "、".join(layers))


_CHECKS = [_check_python, _check_paths, _check_config, _check_classes, _check_roster,
           _check_tasks, _check_legacy, _check_docs, _check_qq, _check_wake, _check_time,
           _check_alert]


# ---------------------------------------------------------------- 对外接口
def run(cfg=None, only=None):
    """跑一遍体检。only 给一组检查名时只跑这些（界面里刷新单项用）。

    cfg=None 时自己读一份：以前只有 _check_config 会自愈，其余检查在 only= 单项刷新时
    全对着 None.get 崩，界面就显示"检查本身出错了"。"""
    t0 = time.time()
    if cfg is None:
        try:
            import config as cfgmod
            cfg = cfgmod.load()
        except Exception:                                    # noqa: BLE001
            cfg = None
    ctx = {"cfg": cfg}
    items = []
    for fn in _CHECKS:
        if only and fn.__name__ not in only:
            continue
        items.append(fn(ctx))
    ms = int((time.time() - t0) * 1000)
    return items, ms


def summary(items):
    """返回 (level, text)：整体结论取最差的那一条。"""
    if not items:
        return OK, "没检查"
    worst = min(items, key=lambda i: _LEVEL_ORDER[i["level"]])["level"]
    n_bad = sum(1 for i in items if i["level"] == BAD)
    n_warn = sum(1 for i in items if i["level"] == WARN)
    if worst == BAD:
        return BAD, "%d 项有问题" % n_bad
    if worst == WARN:
        return WARN, "%d 项要注意" % n_warn
    return OK, "全部正常"


def format_text(items, ms=None):
    """给日志/控制台用的纯文本版。"""
    icon = {OK: "[OK]  ", WARN: "[注意]", BAD: "[问题]"}
    lines = []
    for it in items:
        lines.append("%s %s：%s" % (icon.get(it["level"], "[?]"), it["title"], it["detail"]))
        if it.get("fix") and it["level"] != OK:
            lines.append("         → %s" % it["fix"])
    if ms is not None:
        lines.append("（体检耗时 %d ms）" % ms)
    return "\n".join(lines)


def main():
    items, ms = run()
    print(format_text(items, ms))
    level, _txt = summary(items)
    # 退出码分级：0=全绿 1=有问题 2=只有"要注意"。WARN 也返回 0 是假绿根源之一。
    return {OK: 0, BAD: 1, WARN: 2}[level]


if __name__ == "__main__":
    sys.exit(main())
