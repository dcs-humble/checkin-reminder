r"""launch.pyw - 计划任务的入口（无控制台窗口）。

任务计划直接调 pythonw.exe launch.pyw <班级id> <HHMM>。
这个脚本只做三件事：把结果写进 logs\task-*.log；失败时留下 ALERT 文件并弹一个
气泡（2026-10-21 加的，原来失败了没人知道）；顺手清理过期日志。

为什么不弹对话框：半夜弹个框没人点，会把机器卡在对话框上。所以告警用
logs\ALERT-*.txt + 气泡通知，不阻塞。
"""
import os
import sys
import time
import traceback

APP = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(APP)
LOG = os.path.join(ROOT, "logs")

# ---------------------------------------------------------------- 必须先于任何 import
# pythonw.exe 下面根本没有控制台：sys.stdout / sys.stderr 都是 None。而 lib/ 里七个模块在
# 顶层都有 sys.stdout.reconfigure(encoding="utf-8")，一旦被 import 就
# AttributeError: 'NoneType' object has no attribute 'reconfigure'。
# 2026-10-20 22:00 的第一次计划任务就是这样在 import 阶段炸掉的（Last Result 1）。
# 这里在 import runner 之前把它们换成 devnull，lib/ 一行都不用改。
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")

if APP not in sys.path:
    sys.path.insert(0, APP)


def _log(msg):
    try:
        os.makedirs(LOG, exist_ok=True)
        with open(os.path.join(LOG, "task-%s.log" % time.strftime("%F")),
                  "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%F %T"), msg))
    except Exception:                                        # noqa: BLE001
        pass


def _alert(stage, message, exc=None, extra=None, title="打卡提醒没发出去"):
    """告警一定要"尽力而为"：它是在主流程已经出问题之后才被调用的，
    自己再抛异常只会把真正的错误盖掉，所以整段包起来。

    2026-10-21 改动（审计 P0-1）：以前不管写没写成，这里都打印
    「已写告警：<路径>」。10-21 14:19 那次事故的 ALERT 文件根本没生成，
    日志却声称写了，事后查无可查。现在 path 为空串就如实写"没写成"。
    """
    try:
        import alert
        path = alert.alert(stage, message, exc=exc, extra=extra, title=title)
        how = getattr(alert, "LAST_HOW", "?")
        if path:
            _log("已写告警：%s（弹窗方式：%s）" % (path, how))
        else:
            _log("告警文件没写进去（弹窗方式：%s）：%s"
                 % (how, getattr(alert, "LAST_ERR", "") or "未知原因"))
        return path or None
    except Exception:                                        # noqa: BLE001
        _log("写告警也失败了：\n" + traceback.format_exc())
        return None


def _diagnose():
    """每次跑之前记一行环境自检：唤醒定时器是不是开着。读不出来就当没这回事。"""
    try:
        import diag
        tag, why = diag.report()
        _log("环境自检[%s] %s" % (tag, why))
        return tag, why
    except Exception:                                        # noqa: BLE001
        _log("环境自检跳过：%s" % traceback.format_exc().splitlines()[-1])
        return None, ""


def selftest():
    """pythonw 下的自检：证明所有 lib 模块都能 import，而且不碰 QQ、不发任何消息。

    计划任务是没人值守的，崩溃只会留在 task-*.log 里，所以要有一个能主动跑的体检入口：
        pythonw.exe launch.pyw --selftest
    """
    lines = []
    try:
        import config as cfgmod
        import runner                                            # 这一步 import 成功本身就是重点
        import docs, link, win, at
        import diag, alert                                       # noqa: F401 - 顺便验一下是否可 import
        pyw = os.path.join(ROOT, "runtime", "python", "pythonw.exe")
        if not os.path.isfile(pyw):
            raise RuntimeError("计划任务指向的 %s 不存在" % pyw)
        cfg = cfgmod.load()
        lines.append("import OK: runner/docs/link/win/at/diag/alert; pythonw=OK")
        if getattr(cfgmod, "LAST_LOAD_WARN", ""):
            raise RuntimeError("配置曾被隔离重置：" + cfgmod.LAST_LOAD_WARN)
        lines.append("classes=%s" % [c.get("id") for c in cfg.get("classes", [])])
        lines.append("docs.PORT=%s open=%s" % (docs.PORT, docs.port_open(docs.PORT)))
        lines.append("win panels=%d" % len(win.list_windows() or []))
        lines.append("AT sentinel=%r" % at.SENT)
        tag, why = diag.report()
        lines.append("唤醒定时器[%s] %s" % (tag, why))
        lines.append("旧日志清理：%d 个" % diag.rotate_logs())
        for c in cfg.get("classes", []):
            lines.append("class %s roster=%s" % (c.get("id"), cfgmod.validate_class(c)))
        lines.append("SELFTEST OK")
        rc = 0
    except Exception:                                            # noqa: BLE001
        lines.append("异常:\n" + traceback.format_exc())
        lines.append("SELFTEST FAIL")
        rc = 1
    txt = "\n".join(lines)
    try:
        os.makedirs(LOG, exist_ok=True)
        with open(os.path.join(LOG, "selftest.log"), "w", encoding="utf-8") as f:
            f.write("[%s]\n%s\n" % (time.strftime("%F %T"), txt))
    except Exception:                                            # noqa: BLE001
        pass
    return rc


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--selftest":
        return selftest()
    # 演练：把整条真实链路跑一遍，就是不按回车。23:00 之前想确认"今晚会不会正常"时用。
    #   pythonw.exe launch.pyw <班级id> <HHMM> --dry
    dry = "--dry" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--dry"]
    if len(args) < 2:
        _log("参数不足：需要 <班级id> <HHMM>")
        _alert("参数错误", "计划任务的参数不对：%r" % (sys.argv,))
        return 2
    # 配置在本进程 load 时被隔离重建过：夜里没人看界面，必须留一条 ALERT。
    try:
        import config as _cfg
        if getattr(_cfg, "LAST_LOAD_WARN", ""):
            _alert("配置异常", _cfg.LAST_LOAD_WARN,
                   title="打卡提醒：配置文件被重置")
    except Exception:                                        # noqa: BLE001
        pass
    cid, slot = args[0], args[1]
    _log("开始 %s %s%s" % (cid, slot, "（演练，不发送）" if dry else ""))

    try:
        import diag
        diag.rotate_logs()
    except Exception:                                            # noqa: BLE001
        pass
    wake_tag, wake_why = _diagnose()

    try:
        import runner

        res = runner.run_slot(cid, slot, again=dry, send=not dry,      # 演练要绕过防重发闸
                              on_step=lambda s, m: _log("  %s: %s" % (s, m)))
        _log("结束 ok=%s why=%s n=%s%s"
             % (res.get("ok"), res.get("why"), res.get("n"),
                " dropped=%s" % (res.get("dropped") or []) if res.get("dropped") else ""))
    except Exception as e:                                       # noqa: BLE001
        tb = traceback.format_exc()
        _log("异常:\n" + tb)

        extra = {"班级": cid, "时间点": slot}
        if wake_why:
            extra["唤醒定时器"] = wake_why
        _alert("运行异常", "计划任务跑的时候抛异常了，这条提醒没发出去。", exc=e, extra=extra)
        return 1

    if not res.get("ok"):

        extra = {"班级": cid, "时间点": slot, "原因": res.get("why")}
        if wake_tag == "注意" and wake_why:
            extra["唤醒定时器"] = wake_why
        path = _alert("没有发出", "这条提醒没有发出去：%s" % (res.get("why") or "未知原因"),
                      extra=extra)
        return 1


    # 发出去了，但有人没 @ 上：也留一条，第二天可以照着补
    missed = res.get("missed") or []
    if missed and not dry:
        _alert("部分没@上",
               "已经发出去了，但下面这些人没 @ 上，需要手动补一下：%s" % "、".join(missed),
               extra={"班级": cid, "时间点": slot},
               title="打卡提醒：%d 人没 @ 上" % len(missed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
