r"""weekly.py - 一键导出「上周」每天未打卡的人到剪贴板。

界面快捷操作里的「导出上周未打卡（复制）」跑的就是这个模块；也可以单独跑：
    runtime\python\pythonw.exe app\weekly.py            # 无窗口，结果进剪贴板
    runtime\python\python.exe  app\weekly.py --show     # 顺便打印一份

输出固定 7 行（周一~周日），每行「周X：名字、名字」，确实没人缺卡才写「无」。
人名来自花名册（当前班级 config 里的 roster），每天的缺卡名单 = 花名册 ∉ 当天「未填写」。

数据从哪来：腾讯文档那个每日常用的「打卡」表单——和每晚发提醒的是同一个。
它的统计页有日历，lib/docs.py 的 select_day() 能按完整日期自动切换月份，于是可以
逐天 read_status(url, day=X) 回看历史（docs.py:312，本就是为回测留的）。

三条自己的规矩：
  * 只读表单 + 写剪贴板，全程不碰 QQ（一个键都不敲、一下都不点，优化建议 §0 的
    安全不变量一条都不沾边）；
  * 日历按完整日期切换月份，失败明写「读不到」，绝不用今天的数据冒充历史；
  * 每天读完拿表单标题里的日期和目标日期核对，对不上就标出来，宁可少信一天。
"""
import datetime
import os
import re
import sys

APP = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(APP)
for _p in (APP, os.path.join(ROOT, "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import clip                                    # noqa: E402
import config as cfgmod                        # noqa: E402
import docs                                    # noqa: E402
import link                                    # noqa: E402

WEEK_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def last_week(today=None):
    """上一个完整自然周的 7 个日期：[上周一, ..., 上周日]。"""
    today = today or datetime.date.today()
    this_mon = today - datetime.timedelta(days=today.weekday())
    mon = this_mon - datetime.timedelta(days=7)
    return [mon + datetime.timedelta(days=i) for i in range(7)]


def form_url():
    """表单地址 = 链接识别存下来的那个（与每晚提醒同一个表单）。

    刻意不走 link.from_qq()：那要去 QQ 窗口里翻链接，本功能不碰 QQ。
    link.last_good() 默认只认 36 小时内的缓存，回看历史时过期一点没关系——
    表单 id 是稳定的——所以直接读 state，最后才退回班级配置里的 form_url。
    """
    u = (link.load_state() or {}).get("url") or ""
    if not u:
        try:
            c = cfgmod.active_class(cfgmod.load())
            u = (c or {}).get("form_url") or ""
        except Exception:                                    # noqa: BLE001
            u = ""
    return u


def _header_day(header):
    """从统计页标题（如「10月21日打卡情况」）里抠出 (月, 日)；抠不到返回 None。"""
    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", str(header or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def _export(cid=None, on_step=None, today=None):
    """（内部）逐天读上周 7 天的打卡名单，拼成 7 行文本放进剪贴板。

    返回 {"ok": True, "text", "errors", "cls"} 或 {"ok": False, "why"}。
    on_step(tag, msg) 每步回显（界面把它写进运行记录区）。
    """
    def step(tag, msg):
        if on_step:
            try:
                on_step(tag, msg)
            except Exception:                                # noqa: BLE001
                pass

    cfg = cfgmod.load()
    c = cfgmod.get_class(cfg, cid) if cid else None
    if c is None:
        c = cfgmod.active_class(cfg)
    if not c:
        return {"ok": False, "why": "配置里没有可用的班级"}
    if not c.get("roster"):
        return {"ok": False, "why": "这个班还没选花名册，先在界面上选好"}
    try:
        roster = docs.read_roster(c["roster"])
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False, "why": "花名册读不出来（%s）：%s: %s"
                % (c["roster"], type(e).__name__, e)}
    if not roster:
        return {"ok": False, "why": "花名册是空的：%s" % c["roster"]}
    url = form_url()
    if not url:
        return {"ok": False, "why": "还没有打卡表单链接：先点一次「看看今天谁没打卡」识别链接"}
    step("roster", "花名册 %d 人 · 表单 %s" % (len(roster), url.rsplit("/", 1)[-1]))

    if not docs.port_open(docs.PORT):
        step("browser", "打开读表单用的浏览器…")
        docs.open_browser(None)

    days = last_week(today)
    lines = []
    errors = 0
    for i, d in enumerate(days):
        label = WEEK_CN[i]
        step(label, "读 %s 的打卡名单…" % d.strftime("%m月%d日"))
        st = docs.read_status(url, wait=16, day=d)
        if not st.get("logged_in"):
            lines.append("%s：（读不到：%s）" % (label, st.get("why") or "页面没进统计视图"))
            errors += 1
            continue
        hd = _header_day(st.get("header"))
        if hd != (d.month, d.day):
            lines.append("%s：（日期核对失败：%s）" % (label, st.get("header") or "没有日期标题"))
            errors += 1
            continue
        if not st.get("unchecked_ok"):
            lines.append("%s：（未填写名单计数对不上，没敢用）" % label)
            errors += 1
            continue
        if not st.get("unchecked"):
            # 表单是全院共用的，任何一天都不可能全院 0 人未填写；
            # 读出空名单只可能是统计页没渲染完——宁可报错，不能谎报「无人缺卡」。
            lines.append("%s：（名单是空的，统计页多半没渲染出来）" % label)
            errors += 1
            continue
        w = docs.who_is_missing(st, roster)
        miss = w.get("missing") or []
        lines.append("%s：%s" % (label, "、".join(miss) if miss else "无"))
        if w.get("unmatched"):
            step("warn", "%s：%d 个花名册上的人两栏都没出现（无法判定）：%s"
                 % (label, len(w["unmatched"]), "、".join(w["unmatched"])))
        step(label, "这天未打卡 %d 人" % len(miss))

    text = "\n".join(lines)
    try:
        clip.put(text)
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "text": text,
                "why": "名单读出来了，但剪贴板写入失败：%s: %s" % (type(e).__name__, e)}
    step("done", "已复制到剪贴板（上周 7 天，直接粘贴即可）：\n" + text)
    return {"ok": True, "text": text, "errors": errors, "cls": c.get("name")}


def export(cid=None, on_step=None, today=None):
    """导出入口：和 run_slot / dry_run 共享同一把跨进程锁。

    导出要逐天翻**发送流程复核也在读**的那个统计页，两者并行会互相把
    日历切来切去（复核拿错名单 = 可能 @ 错人）。抢不到锁就明说，绝不并行。
    """
    import runner
    if not runner._lock_acquire("weekly"):
        return {"ok": False,
                "why": "提醒程序正在运行（导出和发送不能同时进行），等它跑完再导出"}
    try:
        return _export(cid=cid, on_step=on_step, today=today)
    finally:
        runner._lock_release()


def main():
    import json
    res = export(on_step=lambda tag, msg: None)
    if "--show" in sys.argv or not res.get("ok"):
        print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
