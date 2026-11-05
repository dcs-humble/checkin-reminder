"""schedule.py - 把 config 里的时间点翻译成 Windows 计划任务。

为什么不用 schtasks 命令行拼参数：交接文档里踩过——中文任务名/中文路径经
powershell.exe(5.1) 的 ANSI 读取会乱码。这里改用 XML + schtasks /XML，
文件以 UTF-16 写，schtasks 对 XML 一律按 Unicode 解析，绕开编码问题。

两个必须遵守的约束（2026-10-20 实测）：
  * <RunLevel> 只能是 LeastPrivilege / 留空。写 HighestAvailable 会被系统拒绝
    （ERROR: Access is denied），因为以最高权限运行需要显式提权注册。
    这套程序本来就不需要管理员权限——它只是往已登录的 QQ 窗口里打字。
  * Principal 里带上 UserId(SID)+InteractiveToken，任务才会在"用户已登录"的
    交互会话里跑。SendInput / SetForegroundWindow 只在交互会话里有意义。

任务名规则： 打卡提醒-<班级id>-<HHMM>
"""
import datetime
import os
import re
import subprocess
import tempfile

APP = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(APP)
PYW = os.path.join(ROOT, "runtime", "python", "pythonw.exe")
TASK_PREFIX = "打卡提醒-"

XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{desc}</Description>
    <URI>\\{name}</URI>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <UserId>{sid}</UserId>
      <LogonType>InteractiveToken</LogonType>
    </Principal>
  </Principals>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT20M</ExecutionTimeLimit>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <RestartOnFailure><Count>2</Count><Interval>PT3M</Interval></RestartOnFailure>
    <StartWhenAvailable>true</StartWhenAvailable>
    <WakeToRun>true</WakeToRun>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>
  </Settings>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start}</StartBoundary>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>{exe}</Command>
      <Arguments>{args}</Arguments>
      <WorkingDirectory>{cwd}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def task_name(cls, slot):
    return "%s%s-%s" % (TASK_PREFIX, cls["id"], slot)


def _decode(out):
    for enc in ("utf-8", "gbk"):
        try:
            return out.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return out.decode("gbk", "replace").strip()


def _run(argv, timeout=60):
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        # 计划任务服务卡住时别让界面一直转圈，当成"跑失败"处理
        return 1, "命令超时（%s 秒）：%s" % (timeout, " ".join(argv[:2]))
    except OSError as e:
        return 1, "命令起不来：%s" % e
    return p.returncode, _decode((p.stdout or b"") + (p.stderr or b""))


def current_sid():
    rc, out = _run(["whoami", "/user"])
    if rc == 0:
        for tok in out.replace("\t", " ").split():
            if tok.startswith("S-1-5-21-") or tok.startswith("S-1-12-"):
                return tok
    return ""


def xml(name):
    """读回任务的 XML（schtasks /Query /XML）。返回 (rc, 文本)。

    编码按内容特征选，绝不按固定顺序猜（2026-10-21 实测踩坑）：
    /XML 管道输出实测是 GBK（中文目录名直接编进任务命令行，解错就乱），
    必须先「严格」试解——宽松解码谁都不报错，只会静默产出乱码文本，
    而 ASCII 的 <Task 还在，比对检查于是静默失效。
    """
    try:
        p = subprocess.run(["schtasks", "/Query", "/TN", name, "/XML"],
                           capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:                                       # noqa: BLE001
        return 1, "读取任务 XML 失败：%s" % e
    raw = p.stdout or b""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        cands = [raw.decode("utf-16")]
    elif b"\x00" in raw[:400]:
        cands = [raw.decode("utf-16-le", "replace"),
                 raw.decode("utf-16-be", "replace")]
    else:
        # strict 优先：GBK 字节按 utf-8 宽松解不报错、只出乱码，而 ASCII 的
        # '<Task' 仍然在——宽松解码会让错误的候选先中选（2026-10-21 实测）。
        cands = []
        for enc in ("utf-8", "gbk"):
            try:
                cands.append(raw.decode(enc))
            except UnicodeDecodeError:
                continue
        if not cands:
            cands = [raw.decode("utf-8", "replace")]
    for text in cands:
        if "<Task" in text:
            return p.returncode, text.strip()
    return p.returncode, (cands[0] if cands else "").strip()


def _same_task(existing, want):
    """两个任务定义是否等价：只比 Command / Arguments / 触发时间的「时:分」。

    StartBoundary 的日期部分每天本来就不同（create 用当天日期），比全串会
    天天判不一致——那正是"每次保存都 /F 重建、把上次运行结果清零"的根源。
    """
    def fields(x):
        def one(tag):
            m = re.search(r"<%s>(.*?)</%s>" % (tag, tag), x or "", re.S)
            return (m.group(1).strip() if m else "").replace("/", "\\").lower()
        times = tuple(sorted(re.findall(
            r"<StartBoundary>\d{4}-\d{2}-\d{2}T(\d{2}:\d{2}):\d{2}", x or "")))
        return one("Command"), one("Arguments"), times
    we, ww = fields(existing), fields(want)
    return bool(ww[0]) and we == ww


def matches(name, cid, slot, time_str):
    """系统里的任务和当前配置是否一致（自检用）。

    返回 True/False；读不到 XML 返回 None（不知道 ≠ 不一致，不算错）。
    """
    ex = xml(name)
    if ex[0] != 0 or "<Command>" not in (ex[1] or ""):
        return None
    hh, mm = (time_str.replace("：", ":").split(":") + ["0"])[:2]
    r = ROOT.replace(os.sep, "\\")
    # 假 XML 的 StartBoundary 必须带日期前缀：_same_task 的时间正则是
    # r"<StartBoundary>\d{4}-\d{2}-\d{2}T(\d{2}:\d{2}):\d{2}"，
    # 之前写成 "xT22:00:00" 导致 want 侧时间永远为空元组、永远判不一致。
    fake = ("<Command>%s</Command><Arguments>%s</Arguments>"
            "<StartBoundary>2000-01-01T%02d:%02d:00</StartBoundary>"
            % (PYW, '"%s" "%s" %s' % (os.path.join(r, "app", "launch.pyw"), cid, slot),
               int(hh), int(mm)))
    return _same_task(ex[1], fake)


def create(cls, slot, time_str, skip_if_same=True):
    """建/覆盖一个任务。返回 (ok, 说明)。

    skip_if_same=True（默认）时先读回系统里的任务 XML 比对：命令行和触发时间
    都没变就不重建。每次保存都 /F 重建会把「上次运行结果」清零——那是判断
    "昨晚到底发没发出去"的关键证据，不该被一次无关的保存抹掉。
    """
    name = task_name(cls, slot)
    hh, mm = (time_str.replace("：", ":").split(":") + ["0"])[:2]
    start = "%sT%02d:%02d:00+08:00" % (datetime.date.today().isoformat(),
                                       int(hh), int(mm))
    r = ROOT.replace(os.sep, "\\")
    x = XML.format(
        desc="打卡自动提醒 · %s · 每天 %s" % (cls.get("name"), time_str),
        name=name, sid=current_sid() or "",
        start=start, exe=PYW,
        args='"%s" "%s" %s' % (os.path.join(r, "app", "launch.pyw"), cls["id"], slot),
        cwd=os.path.join(r, "app"))
    if skip_if_same:
        ex = xml(name)
        if ex[0] == 0 and "<Command>" in (ex[1] or "") and _same_task(ex[1], x):
            return True, "已一致，未重建 %s（保留上次运行结果）" % name
    fd, path = tempfile.mkstemp(suffix=".xml", prefix="dcs-task-")
    os.close(fd)
    try:
        with open(path, "wb") as f:
            f.write(b"\xff\xfe" + x.encode("utf-16-le"))     # BOM + UTF-16LE
        rc, msg = _run(["schtasks", "/Create", "/TN", name, "/XML", path, "/F"])
        if rc != 0:
            return False, msg or ("schtasks 返回 %d" % rc)
        return True, "已创建 %s（每天 %s）" % (name, time_str)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def delete(name):
    rc, msg = _run(["schtasks", "/Delete", "/TN", name, "/F"])
    return rc == 0, msg


_FIELDS = {"任务名": "task", "TaskName": "task",
           # ⚠ 中文系统下 /V 输出用的字段名是「模式」（不是「状态」），「状态」只在
           # 不带 /V 的简表里出现。两个都收，少一个 GUI 的任务表就会少一列。
           "模式": "status", "状态": "status", "Status": "status",
           "计划任务状态": "state", "Scheduled Task State": "state",
           "下次运行时间": "next", "Next Run Time": "next",
           "上次运行时间": "last", "Last Run Time": "last",
           "上次结果": "result", "Last Result": "result",
           "要运行的任务": "action", "Task To Run": "action"}


_QUERY_FAIL = ("WinError", "拒绝访问", "拒绝访问。", "找不到指定的路径",
               "超时", "Access is denied", "命令起不来", "命令执行失败")


def query(name):
    """返回 {exists, status, next, last, result, action, error}。中文/英文输出都能认。

    rc!=0 分两种：任务真的没有（exists=False, error 为空）和 schtasks 本身跑不了
    （error 非空）。两者混在一起会让界面骗人——明明查询失败却显示"未创建"，
    用户就会去重建一堆其实已经存在的任务。
    """
    rc, msg = _run(["schtasks", "/Query", "/TN", name, "/FO", "LIST", "/V"], timeout=30)
    if rc != 0:
        bad = any(t in (msg or "") for t in _QUERY_FAIL)
        return {"exists": False, "raw": msg, "error": (msg or "").strip() if bad else ""}
    info = {"exists": True, "raw": msg}
    for line in msg.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        key = _FIELDS.get(k.strip(), k.strip().lower().replace(" ", "_"))
        if key in ("task", "status", "state", "next", "last", "result", "action"):
            info.setdefault(key, v.strip())
    return info


def set_enabled(name, on):
    rc, msg = _run(["schtasks", "/Change", "/TN", name,
                    "/ENABLE" if on else "/DISABLE"])
    return rc == 0, msg


def run_now(name):
    rc, msg = _run(["schtasks", "/Run", "/TN", name])
    return rc == 0, msg


def ours():
    """列出本程序建的任务（前缀匹配）。"""
    rc, msg = _run(["schtasks", "/Query", "/FO", "CSV", "/NH"], timeout=90)
    if rc != 0:
        return []
    names = []
    for line in msg.splitlines():
        line = line.strip()
        if not line.startswith('"'):
            continue
        n = line.split('","')[0].strip('"').lstrip("\\")
        if n.startswith(TASK_PREFIX):
            names.append(n)
    return sorted(set(names))


def sync(cfg, only=None):
    """按配置对齐任务：该有的建，过期的删。返回 [(任务名, ok, 说明)]。

    only 给出时只处理这些班——用户点保存只重建当前这个班，不动其他班。
    """
    import config as cfgmod
    res = []
    want = {}
    for c in cfg.get("classes", []):
        if only is not None and c["id"] not in only:
            continue
        if not c.get("enabled", True):
            continue
        # 配置不完整的班不建任务：建了也是每晚到点报错、写一条 ALERT，
        # 不如干脆不建，等花名册/群关键词补齐再保存一次。
        errs = [m for lv, m in cfgmod.validate_class(c) if lv == "error"]
        if errs:
            res.append(("%s%s-?" % (TASK_PREFIX, c["id"]), False,
                        "配置不完整，没建任务：%s" % errs[0]))
            continue
        for s in cfgmod.enabled_schedules(c):
            slot = cfgmod.slot_of(s.get("time"))
            want[task_name(c, slot)] = (c, slot, s.get("time"))

    for name, (c, slot, t) in want.items():
        ok, msg = create(c, slot, t)
        res.append((name, ok, msg))

    for name in ours():
        if name in want:
            continue
        if only is not None and not any(
                name.startswith("%s%s-" % (TASK_PREFIX, i)) for i in only):
            continue
        ok, msg = delete(name)
        res.append((name, ok, "已删除旧的 %s" % name))
    return res


def remove_class(cid):
    res = []
    for name in ours():
        if name.startswith("%s%s-" % (TASK_PREFIX, cid)):
            ok, msg = delete(name)
            res.append((name, ok, msg))
    return res


def purge_legacy(keep_enabled=True):
    """清掉交接文档里手工建的那两个任务（打卡自动提醒-22 / -23）。

    它们跑的是老的 lib/main.py —— 和新程序同时存在的话，同一晚会有两条提醒
    （而且老任务 22 点那个还是禁用的）。迁移完必须删掉。
    """
    out = []
    for name in ("打卡自动提醒-22", "打卡自动提醒-23"):
        rc, _ = _run(["schtasks", "/Query", "/TN", name])
        if rc != 0:
            continue
        ok, msg = delete(name)
        out.append((name, ok, msg))
    return out
