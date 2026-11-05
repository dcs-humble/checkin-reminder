"""失败告警：把"没发出去"这件事送到人眼前。

为什么需要这个文件（2026-10-20 的事故）
--------------------------------------
22:00 的计划任务崩在 lib\\docs.py:45（'NoneType' object has no attribute
'reconfigure'，pythonw 下 sys.stdout 是 None）。任务退出码非 0，但
Task Scheduler 不会告诉任何人，日志躺在 logs\\run-2026-10-20.jsonl 里，
直到第二天早上才被发现——那一晚真正发出去的 19 人是 21:21 手动发的。

所以：任何一次"该发没发"，都必须同时留下三样东西
  1) logs\\ALERT-YYYY-MM-DD-HHMM.txt（阶段 + 异常 + traceback 尾巴）
  2) 桌面气泡通知
  3) 一个非 0 的返回码（给 Task Scheduler 的 LastTaskResult 看）

这里只做 1 和 2；返回码由调用方决定。
"""
import os
import subprocess
import sys
import time
import traceback

# 最近一次弹窗用了哪一层（toast / msg / messagebox / none）。
# 调用方（launch.pyw）会把它写进 task-*.log：锁屏时气泡 5 分钟后就没了，
# 第二天必须能从日志知道"当时到底弹没弹出来"。
LAST_HOW = ""

# 最近一次落盘失败的原因（成功时为空字符串）。
# 为什么要有它（2026-10-21 审计 P0）：write_alert 以前的契约是"写失败也返回路径"，
# 于是 logs\task-2026-10-21.log 里那句「已写告警：…\ALERT-2026-10-21-141914.txt」
# 指向一个根本不存在的文件，事故现场一点痕迹都没留下。现在写失败返回空串，
# 并把原因放在这里，让调用方能如实写日志。
LAST_ERR = ""

# 置顶 MessageBox 的自动关闭时间（毫秒）。60 秒够人看见，又不至于把计划任务钉住。
_MSG_TIMEOUT_MS = 60 * 1000


def _app_dir():
    return os.path.dirname(os.path.abspath(__file__))


def logs_dir():
    return os.path.join(os.path.dirname(_app_dir()), "logs")


def alert_path(when=None):
    t = when or time.localtime()
    # 精确到秒：同一分钟两条告警（两个班同时失败 / 重试期间多入口）以前会互相覆盖。
    return os.path.join(logs_dir(), "ALERT-%s.txt" % time.strftime("%Y-%m-%d-%H%M%S", t))


def write_alert(stage, message, exc=None, extra=None, when=None):
    r"""落盘一条告警。

    返回**真正写成**的文件路径；写失败返回空字符串 ""，原因放进 LAST_ERR。
    绝不抛异常（告警是在别处已经崩掉之后才被调用的，再抛只会盖掉真正的错误）。

    2026-10-21 改动：以前这里无论如何都 `return path`，于是调用方打印
    「已写告警：…\ALERT-…txt」而文件根本不存在（10-21 14:19 那次事故的告警
    就整个丢失了）。现在返回值即事实，调用方必须判断真假。
    """
    global LAST_ERR
    LAST_ERR = ""
    path = alert_path(when)
    try:
        os.makedirs(logs_dir(), exist_ok=True)
        lines = [
            "时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S", when or time.localtime()),
            "阶段: %s" % (stage or "-"),
            "说明: %s" % (message or "-"),
            "",
        ]
        if extra:
            for k, v in extra.items():
                lines.append("%s: %s" % (k, v))
            lines.append("")
        if exc is not None:
            tail = traceback.format_exception(type(exc), exc, exc.__traceback__)
            lines.append("traceback（最后 20 行）:")
            lines.extend(tail[-20:])
        # 追加写：多条告警共存时用分隔线隔开，不再互相覆盖。
        existed = os.path.isfile(path) and os.path.getsize(path) > 0
        with open(path, "a", encoding="utf-8") as f:
            if existed:
                f.write("\n" + "=" * 36 + "\n")
            f.write("\n".join(lines) + "\n")
        return path
    except Exception as e:                              # noqa: BLE001 - 告警不能二次崩
        LAST_ERR = "%s: %s" % (type(e).__name__, e)
        # 连 ALERT 文件都写不进去时，退而求其次追加到 fallback——
        # 告警链路自身必须处处留痕，绝不能"静默地没告警"。
        try:
            with open(os.path.join(logs_dir(), "alert-fallback.log"), "a",
                      encoding="utf-8") as f:
                f.write("[%s] stage=%s 写 ALERT 失败(%s): %s\n"
                        % (time.strftime("%F %T"), stage or "-", e, message or "-"))
        except Exception as e2:                         # noqa: BLE001
            LAST_ERR += "；fallback 也失败(%s: %s)" % (type(e2).__name__, e2)
        return ""


def _toast_windows(title, text):
    """Windows 10/11 的 toast 通知。

    runtime\\python 里带了 winrt，但 winrt 的 toast 需要注册 AppUserModelID，
    脚本进程经常注册失败。失败就退回到 msg.exe（Win10+ 默认存在），再失败
    退回到一个置顶的 MessageBox——三层兜底，总有一层能弹出东西。
    """
    # 一层：winrt toast
    try:
        import asyncio
        from winrt.windows.ui.notifications import ToastNotificationManager, ToastNotification
        from winrt.windows.data.xml.dom import XmlDocument

        xml = (
            '<toast><visual><binding template="ToastGeneric">'
            "<text>%s</text><text>%s</text>"
            "</binding></visual></toast>" % (_xml_escape(title), _xml_escape(text))
        )
        doc = XmlDocument()
        doc.load_xml(xml)
        notifier = ToastNotificationManager.create_toast_notifier("打卡自动提醒")
        notifier.show(ToastNotification(doc))
        return "toast"
    except Exception:                                   # noqa: BLE001
        pass

    # 二层：msg.exe（会弹在会话里，不需要额外依赖）
    try:
        subprocess.run(["msg", "*", "/time:300", "%s\n%s" % (title, text)],
                       timeout=8, capture_output=True)
        return "msg"
    except Exception:                                   # noqa: BLE001
        pass

    # 三层：置顶 MessageBox，带 60 秒自动关闭
    # 2026-10-21 改动（审计 P0-2）：这里以前调 MessageBoxW，它没有超时参数。
    # 本机 winrt toast 和 msg.exe 都不可用（家庭版不带 msg.exe），所以每次告警
    # 必然落到这一层，然后一直挂着等人点确定——计划任务的 ExecutionTimeLimit
    # 是 PT20M，等于告警自己把任务钉死到被强杀，后面的清理逻辑都跑不到。
    # MessageBoxTimeoutW 是未文档化但 user32 里稳定导出的（本机实测存在），
    # 第 5 个参数是超时毫秒数，超时自动关；导出失败才退回阻塞版本。
    try:
        import ctypes
        u = ctypes.windll.user32
        text = "%s\n\n%s" % (title, text)
        flags = 0x00000040 | 0x00001000 | 0x00040000
        try:
            u.MessageBoxTimeoutW(None, text, title, flags, 0, _MSG_TIMEOUT_MS)
        except Exception:                               # noqa: BLE001
            u.MessageBoxW(None, text, title, flags)
        return "messagebox"
    except Exception:                                   # noqa: BLE001
        return "none"


def _xml_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def notify(title, text):
    """写告警 + 弹窗，返回 (告警文件路径, 用了哪种弹窗方式)。

    路径为空串表示落盘失败，调用方要如实反映，不能一律当成功。
    """
    global LAST_HOW
    path = write_alert("notify", text, extra={"标题": title})
    where = ("（详情见 %s）" % path) if path else "（告警文件也没写进去：%s）" % LAST_ERR
    how = _toast_windows(title, "%s\n%s" % (text, where))
    LAST_HOW = how
    return path, how


def alert(stage, message, exc=None, extra=None, title="打卡提醒没发出去"):
    """一步到位：落盘 + 弹窗。返回告警文件路径（空串=没写成）。绝不抛异常。"""
    global LAST_HOW
    path = write_alert(stage, message, exc=exc, extra=extra)
    tail = ("详情见 logs\\%s" % os.path.basename(path)) if path \
        else ("告警文件也没写进去：%s" % (LAST_ERR or "未知原因"))
    try:
        LAST_HOW = _toast_windows(
            title, "%s\n%s\n%s" % (stage or "", message or "", tail))
    except Exception:                                   # noqa: BLE001
        LAST_HOW = "none"
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write("弹窗方式: %s\n" % (LAST_HOW or "unknown"))
        except Exception:                               # noqa: BLE001
            pass
    return path


if __name__ == "__main__":
    p = alert("selftest", "这是一条测试告警，可以忽略", extra={"来源": "alert.py __main__"})
    print(p)
    sys.exit(0)
