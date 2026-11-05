# -*- coding: utf-8 -*-
"""自检：让"明天早上才发现昨晚没发"变成"今晚就知道"。

目前只有两件事，都是踩过的坑：

1. 唤醒定时器（wake timer）
   任务设了 WakeToRun，但 Windows 的"允许使用唤醒定时器"是**电源方案级别**的开关，
   电池供电下默认还是"禁用"。合盖/睡眠 + 用电池 = 22:00 那个时间点根本不会醒。
   这里只读不写，绝不替用户改电源方案。

2. 日志轮转
   logs\\run-*.jsonl 每天一个，任务日志每天一个，ALERT 每次一个。
   一年下来几百个文件，翻起来比没有还难受。留 90 天，其余删掉。

命令行自检：
    python diag.py
"""
import datetime
import glob
import os
import re
import subprocess
import time

APP = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(APP)
LOGS = os.path.join(ROOT, "logs")

POWERCFG = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "powercfg.exe")
SUB_SLEEP = "238c9fa8-0aad-41ed-83f4-97be242c8f20"
RTCWAKE = "bd3b718a-0680-4d9d-8ab2-e1d2b4ac806d"

# powercfg 的输出是 GBK；先按 GBK 解，不行再退回 utf-8，别为一行中文抛异常
def _decode(raw):
    for enc in ("gbk", "utf-8", "mbcs"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def wake_timers():
    """(ok, 说明)。ok: True 正常 / False 明显有问题 / None 读不出来（不吓唬人）。"""
    if not os.path.exists(POWERCFG):
        return None, "找不到 powercfg.exe，跳过唤醒检查"
    try:
        r = subprocess.run([POWERCFG, "/query", "SCHEME_CURRENT", SUB_SLEEP, RTCWAKE],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=5, creationflags=0x08000000)   # CREATE_NO_WINDOW
                           # 5s 而不是 15s：这段跑在发送之前，卡住就直接拖慢 22:00 那条
    except Exception as e:                                       # noqa: BLE001
        return None, "powercfg 调不起来：%s" % e
    text = _decode(r.stdout or b"")
    if r.returncode != 0 or "RTCWAKE" not in text:
        return None, "powercfg 读不出唤醒设置（返回码 %s）" % r.returncode

    def _index(label):
        m = re.search(label + r"[^:：]*[:：]\s*0x([0-9a-fA-F]+)", text)
        return int(m.group(1), 16) if m else None

    ac = _index(r"当前交流电源设置索引")
    dc = _index(r"当前直流电源设置索引")
    if ac is None:
        return None, "powercfg 输出格式认不出来，跳过唤醒检查"

    parts = []
    if ac == 1:
        parts.append("插电：唤醒定时器已启用")
    elif ac == 2:
        parts.append("插电：仅限重要唤醒，22:00 可能叫不醒")
    else:
        parts.append("插电：唤醒定时器**禁用**，睡眠时 22:00 不会自己醒")
    if dc == 1:
        parts.append("用电池：已启用")
    elif dc == 2:
        parts.append("用电池：仅限重要唤醒")
    else:
        parts.append("用电池：**禁用**（笔记本合盖/用电池时不会醒）")

    # 只看插电不够（2026-10-21 审计）：插电启用但电池禁用，只在"一直插着电"
    # 时才安全——拔掉电源那一刻到点就叫不醒。两边都启用才算稳。
    ok = (ac == 1) and (dc is None or dc in (1, 2))
    return ok, "；".join(parts)


def rotate_logs(days=90):
    """删掉 logs 里超过 days 天的运行日志 / 任务日志 / 告警。返回删了几个文件。

    按**文件名里的日期**判断，不用 mtime：被 touch/复制过的旧日志 mtime 是新的，
    按 mtime 它们就永生不删，logs 会一直涨。"""
    if not os.path.isdir(LOGS):
        return 0
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)

    def _when(path):
        m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(path))
        if m:
            try:
                return datetime.datetime.strptime(m.group(1), "%Y-%m-%d")
            except ValueError:
                pass
        return datetime.datetime.fromtimestamp(os.path.getmtime(path))

    killed = 0
    # 2026-10-21 改动（审计 P1-10）：原来只清 run-*.jsonl / task-*.log /
    # ALERT-*.txt / selftest.log 四类，漏掉了 gui-error.log 和 gui-stderr.log
    # ——它们是 tkinter 回调异常和任何 stderr 输出的落点，只会无限涨。
    # 现在改成"扫 logs 下所有 *.log / *.jsonl / *.txt"，用白名单排除少数几条。
    keep = {"alert-fallback.log"}   # 只在 ALERT 写不进去时才写，本身是诊断线索，且极小
    candidates = []
    for pat in ("*.log", "*.jsonl", "*.txt"):
        candidates.extend(glob.glob(os.path.join(LOGS, pat)))
    for path in candidates:
        if os.path.basename(path) in keep:
            continue
        try:
            if _when(path) < cutoff:
                os.remove(path)
                killed += 1
        except OSError:
            pass
    # logs\probe 里是每次 OCR 的 png/txt（一次运行几十个），它们没有日期文件名，
    # 按 mtime 清 3 天前的即可——留着也没人看，堆着只会把 logs 撑大。
    probe = os.path.join(LOGS, "probe")
    if os.path.isdir(probe):
        pcut = time.time() - 3 * 86400
        for path in glob.glob(os.path.join(probe, "*")):
            try:
                if os.path.getmtime(path) < pcut:
                    os.remove(path)
                    killed += 1
            except OSError:
                pass
    # logs 根目录下的截图（m*.png / qq-panel-*.png）没有日期文件名，
    # rotate_logs 的日期正则匹配不到它们 → 以前永不清理，只占空间。按 mtime 清 30 天。
    pc30 = time.time() - 30 * 86400
    for path in glob.glob(os.path.join(LOGS, "*.png")):
        try:
            if os.path.getmtime(path) < pc30:
                os.remove(path)
                killed += 1
        except OSError:
            pass
    return killed


def report():
    """给界面/日志用的一行结论。"""
    ok, why = wake_timers()
    tag = {True: "OK", False: "注意", None: "跳过"}[ok]
    return tag, why


if __name__ == "__main__":
    print(report())
    print("清理旧日志：%d 个" % rotate_logs())
