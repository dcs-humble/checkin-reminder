"""gui.py - 打卡自动提醒的控制台。

界面只做三件事：
  1. 编辑 config.json（班级 / 花名册 / 时间 / 文案）——所有改动都存回这一个文件；
  2. 把 config 同步成 Windows 计划任务；
  3. 手动触发"看看今天谁没打"和"立刻发一次"，并把每一步回显出来。

真正的发送逻辑一律走 runner.py，界面里不出现任何 SendInput / OCR 代码——
这样界面上点一次和半夜自动跑一次，走的是同一条已经验证过的路径。
"""
import json
import os
import queue
import sys
import threading
import time
import traceback

APP = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(APP)

# pythonw.exe 下 sys.stdout / sys.stderr 是 None，而 lib/ 顶层有 reconfigure 调用。
# 必须在 import tkinter/config 之前补上，否则界面一启动就 AttributeError。
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
if sys.stderr is None:
    # stderr 不能进黑洞：tkinter 回调异常是 print 到 sys.stderr 的
    #（"Exception in Tkinter callback"），指到 devnull = 启动后按钮出错零痕迹
    #（2026-10-21 审计 P0）。改落 logs\\gui-stderr.log（追加）；连日志都开不了
    #（目录只读/盘满）才退回 devnull——但绝不主动静默。
    try:
        os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
        sys.stderr = open(os.path.join(ROOT, "logs", "gui-stderr.log"), "a",
                          encoding="utf-8", errors="replace")
    except Exception:
        sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")

if APP not in sys.path:
    sys.path.insert(0, APP)

import tkinter as tk                                    # noqa: E402
from tkinter import filedialog, messagebox, ttk        # noqa: E402

import config as cfgmod                                # noqa: E402
import theme                                           # noqa: E402

BG = theme.BG


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = cfgmod.load()
        self.q = queue.Queue()
        self.busy = False
        self.rows = []          # 当前班级的时间行控件
        self._checking = False
        root.title("打卡自动提醒")
        # 初始尺寸只是占位，真正的尺寸由 _fit_window() 按内容需要和屏幕高度决定。
        root.geometry("1160x900")
        root.minsize(min(1040, root.winfo_screenwidth() - 48),
                     min(640, root.winfo_screenheight() - 100))
        root.configure(bg=BG)
        self._style()
        self._build()
        self._install_legacy_cleanup()
        self._sig_memo = {c["id"]: self._sched_sig(c)
                          for c in self.cfg.get("classes", [])}
        self._roster_gen = 0        # 花名册/任务列表的"最新一次请求"编号，旧结果直接丢
        self._tasks_gen = 0
        self._refresh_classes()
        self.say("界面已启动 · 配置文件 %s" % cfgmod.CONFIG)
        self.say("每一步都会写进 logs 目录，出问题先看这里。")
        self.root.after(120, self._pump)
        # 开界面先体检一次（安静模式，只更新顶栏胶囊）：把"看起来能跑"和"真的能跑"分开
        self.root.after(650, lambda: self._check(quiet=True))
        # 夜里弹的 MessageBox 没人看见：开界面 0.8 秒后回看 24 小时内的 ALERT 补报
        self.root.after(800, self._show_recent_alerts)
        # 高 DPI 下字体被放大，固定的 1120x860 会把最下面的运行记录挤没；
        # 等控件都排完，按"实际需要的尺寸"把窗口撑开，再夹到屏幕范围内。
        self.root.after(120, self._fit_window)

    def _fit_window(self):
        """按"实际需要的尺寸"撑开窗口，再夹到屏幕可用范围内。

        1080p 屏（去掉任务栏）实际可用约 1040px 高，而这套界面完整需要
        ~980px；所以下限设 820 而不是 900——宁可让运行记录少两行，
        也不要让窗口超出屏幕导致底部的「保存」按钮点不到。
        """
        try:
            self.root.update_idletasks()
            need_h = self.root.winfo_reqheight()
            need_w = self.root.winfo_reqwidth()
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            w = min(1280, sw - 80)
            h = min(920, sh - 96)
            x = max(20, (sw - w) // 2)
            y = max(20, (sh - h) // 2 - 10)
            self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        except Exception:                                     # noqa: BLE001
            pass

    # ------------------------------------------------------------ 外观
    def _style(self):
        theme.apply(self.root)

    def _build(self):
        import studio_ui
        studio_ui.build(self)

    # ------------------------------------------------------------ 自检
    def _show_recent_alerts(self):
        """开界面时回看 24 小时内的告警文件：夜里弹的 MessageBox 没人看见，
        第二天开界面必须能第一时间看到昨晚出过什么事（审计：告警不可见）。"""
        try:
            import glob as _g
            cutoff = time.time() - 86400
            hits = sorted(p for p in _g.glob(os.path.join(ROOT, "logs", "ALERT-*.txt"))
                          if os.path.isfile(p) and os.path.getmtime(p) >= cutoff)
            if not hits:
                return
            self.say("⚠ 24 小时内有 %d 条告警（logs\\ALERT-*.txt）：" % len(hits),
                     "warn")
            for p in hits[-3:]:
                try:
                    with open(p, encoding="utf-8", errors="replace") as f:
                        head = next((ln.rstrip() for ln in f if ln.strip()), "")
                    self.say("    %s  %s" % (os.path.basename(p), head[:90]), "warn")
                except OSError:
                    pass
            self.say("    点「日志文件夹」看完整内容。", "warn")
        except Exception:                                    # noqa: BLE001
            pass

    def _open_logs(self):
        p = os.path.join(ROOT, "logs")
        try:
            os.makedirs(p, exist_ok=True)
            os.startfile(p)                                  # noqa: S606
        except Exception as e:                               # noqa: BLE001
            messagebox.showerror("打不开", "%s\n%s" % (p, e))

    def _clean_cache(self):
        import maintenance_ui
        maintenance_ui.open_dialog(self)

    def _clear_log(self):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")
        if hasattr(self,"_log_empty"):
            self._log_empty.place(relx=.5,rely=.5,anchor="center")

    def _check(self, quiet=False):
        """跑一遍 selfcheck，结果写进顶栏胶囊；不静默时再弹一个清单窗口。"""
        if getattr(self, "_checking", False):
            return
        self._checking = True
        self.set_pill("正在自检…", None)

        def body():
            try:
                import selfcheck
                cfg = cfgmod.load()          # 读磁盘上的，顺带验证 config.json 没坏
                items, ms = selfcheck.run(cfg)
                return {"ok": True, "items": items, "ms": ms}
            except Exception:                                # noqa: BLE001
                return {"ok": False, "why": traceback.format_exc(limit=4)}
        self._busy_spawn(lambda: self.q.put(("check", (body(), quiet))))

    def _show_check(self, res, quiet=False):
        self._checking = False
        if not res.get("ok"):
            self.set_pill("自检失败", "bad")
            if not quiet:
                messagebox.showerror("自检跑不起来", res.get("why") or "")
            return
        import selfcheck
        items, ms = res["items"], res.get("ms", 0)
        level, text = selfcheck.summary(items)
        self.set_pill(text, level)
        self._update_header()
        if not quiet:
            _CheckDialog(self.root, items, ms)

    def _update_header(self):
        import studio_ui
        studio_ui.update(self)
        parts = []
        for c in self.cfg.get("classes") or []:
            if not c.get("enabled", True):
                continue
            slots = [s.get("time") for s in cfgmod.enabled_schedules(c)]
            parts.append("%s · %s" % (c.get("name", ""),
                                      " / ".join(slots) if slots else "没有时间点"))
        if not parts:
            parts = ["还没有启用的班级"]
        self.l_sub.configure(text="%s 自动提醒未打卡的人"
                                % "；".join(parts[:2]))

    # ------------------------------------------------------------ 日志
    def say(self, msg, tag=None):
        """往运行记录里追加一行。tag 不传时按内容的开头符号自动上色。"""
        if hasattr(self,"_log_empty"):
            self._log_empty.place_forget()
        if tag is None:
            head = (msg or "").lstrip()[:2]
            if head.startswith("✔"):
                tag = "ok"
            elif head.startswith("✘"):
                tag = "bad"
            elif head.startswith("⚠"):
                tag = "warn"
            elif head.startswith("——"):
                tag = "cmd"
        self.txt.configure(state="normal")
        self.txt.insert("end", time.strftime("%H:%M:%S") + "  ", "ts")
        self.txt.insert("end", (msg or "") + "\n", tag or ())
        if int(self.txt.index("end-1c").split(".")[0]) > 2000:
            self.txt.delete("1.0", "201.0")
        self.txt.see("end")
        self.txt.configure(state="disabled")
        if hasattr(self, "_nav"):
            self._nav["logs"].configure(text="   运行记录  •")

    def hint(self, msg, bad=False):
        self.l_msg.configure(text=msg,
                             fg=(theme.BAD if bad else theme.FAINT))

    def _pump(self):
        """工作线程只往队列里塞消息，UI 一律在主线程改——tkinter 不允许跨线程碰控件。"""
        try:
            while True:
                try:
                    kind, payload = self.q.get_nowait()
                except queue.Empty:
                    break
                try:
                    if kind == "log":
                        self.say(payload)
                    elif kind == "done":
                        self._finish(payload)
                    elif kind == "tasks":
                        self._show_tasks(*payload)
                    elif kind == "roster":
                        self._show_roster(*payload)
                    elif kind == "refresh_tasks":
                        # 工作线程不能碰控件，连 root.after 也不能调；只能请 UI 线程自己来
                        self._refresh_tasks()
                    elif kind == "roster_check":
                        self._check_roster()
                    elif kind == "check":
                        res, quiet = payload
                        self._show_check(res, quiet=quiet)
                except Exception:
                    # 一条消息处理炸了不能带走整个泵：泵一断 busy 永远 True、
                    # 按钮永久禁用、_checking 卡死（2026-10-21 审计 P0——原 try
                    # 包住整个 while，而 after 重排在 try 之外，一炸就不再重排）。
                    self._log_error("运行记录消息 %r 处理失败" % (kind,))
        finally:
            self.root.after(120, self._pump)

    def _log_error(self, where):
        """界面异常落盘 + 回显一行红字。绝不抛。"""
        tb = traceback.format_exc()
        try:
            os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
            with open(os.path.join(ROOT, "logs", "gui-error.log"), "a",
                      encoding="utf-8") as f:
                f.write("[%s] %s\n%s\n" % (time.strftime("%F %T"), where, tb))
        except Exception:                                   # noqa: BLE001
            pass
        try:
            self.say("✘ %s（详见 logs\\gui-error.log）" % where, "bad")
        except Exception:                                   # noqa: BLE001
            pass

    def _work(self, fn, title):
        if self.busy:
            messagebox.showinfo("稍等", "上一个操作还在跑。")
            return
        self.busy = True
        if hasattr(self,"_progress"):
            self._progress.pack(side="left",padx=8,before=self._save_shortcut)
            self._progress.start(12)
        for b in self.run_btns:
            b.state(["disabled"])
        import studio_ui
        studio_ui.show(self, 'logs')
        self.hint(title + "…")
        self.say("—— %s ——" % title)

        def body():
            try:
                res = fn()
            except Exception:                                # noqa: BLE001
                res = {"ok": False, "why": traceback.format_exc(limit=4)}
            self.q.put(("done", res))
        threading.Thread(target=body, daemon=True).start()

    def _finish(self, res):
        self.busy = False
        if hasattr(self,"_progress"):
            self._progress.stop()
            self._progress.pack_forget()
        for b in self.run_btns:
            b.state(["!disabled"])
        ok = res.get("ok")
        why = res.get("why") or ""
        if ok:
            self.hint("完成", bad=False)
            self.say("✔ " + (why or "完成"))
        else:
            first = why.splitlines()[0][:80] if why else "（没给原因，看运行记录）"
            self.hint("失败：" + first, bad=True)
            self.say("✘ " + (why or "失败（没有原因说明）"))
            self._alert_failure(res, why)
        if "people" in res:
            for p in res["people"]:
                self.say("    %s %s" % (p["sid"], p["name"]))
            if res.get("people"):
                self.say("   共 %d 人" % len(res["people"]))
            elif res.get("ok"):
                self.say("   今天全员已打卡")
        if res.get("unmatched"):
            self.say("   ⚠ 名单里对不上的人：" + "、".join(res["unmatched"]))
        if res.get("missed"):
            # 消息发出去了但这些人没被 @ 到：必须显眼，第二天要靠它补发
            self.say("   ⚠ 没 @ 上（可能是 @ 弹窗没出来）：" + "、".join(res["missed"]))

    def _alert_failure(self, res, why):
        """界面里手动操作失败也落一条 ALERT（2026-10-21 审计 P1-13）。

        以前界面失败只有一行红字：关掉窗口就没了，第二天完全查不到"我明明点过"。
        这里复用 alert.write_alert，但**不再弹窗**——用户就在屏幕前看着，
        弹窗只会挡住他自己点。落盘失败也只是在日志里补一句，绝不打断界面。
        """
        try:
            import alert as alertmod
            path = alertmod.write_alert(
                "界面手动操作", why or "失败（没有原因说明）",
                extra={"结果": json.dumps(res, ensure_ascii=False, default=str)[:800]})
            if path:
                self.say("   ↳ 已写告警：%s" % os.path.basename(path), "warn")
            else:
                self.say("   ↳ 告警文件没写进去：%s" % (alertmod.LAST_ERR or "未知原因"),
                         "warn")
        except Exception:                                    # noqa: BLE001
            self._log_error("_alert_failure")

    # ------------------------------------------------------------ 班级
    def _refresh_classes(self, keep=None):
        self.lb.delete(0, "end")
        cur = keep or self.cfg.get("active")
        idx = 0
        for i, c in enumerate(self.cfg.get("classes", [])):
            self.lb.insert("end", c["name"] + ("" if c.get("enabled", True) else "（停用）"))
            if c["id"] == cur:
                idx = i
        if self.cfg.get("classes"):
            self.lb.selection_set(idx)
            self.lb.see(idx)
            self.cfg["active"] = self.cfg["classes"][idx]["id"]
            self._load_class(self.cfg["classes"][idx])

    def _pick(self):
        s = self.lb.curselection()
        return self.cfg["classes"][s[0]] if s else None

    def _on_pick(self, _e=None):
        c = self._pick()
        if c:
            # 切班前列先把上一个班的界面编辑收回 cfg，否则切走再切回来就静默丢了
            if self.cfg.get("active") and self.cfg.get("active") != c["id"]:
                self._collect()
            self.cfg["active"] = c["id"]
            self._load_class(c)

    def _load_class(self, c):
        self._update_header()
        self.v_name.set(c.get("name", ""))
        self.v_keys.set("，".join(c.get("title_keys") or []))
        self.v_roster.set(c.get("roster", ""))
        self.v_form.set(c.get("form_url", ""))
        self.v_form_keys.set("，".join(c.get("form_title_keys") or []))
        self.v_enable.set(bool(c.get("enabled", True)))
        for w in self.times_box.winfo_children():
            w.destroy()
        self.rows = []
        for s in c.get("schedules", []):
            self._add_row(s)
        self._check_roster()
        self._refresh_tasks()

    def _add_row(self, s=None):
        s = s or {"time": "22:00", "copy": cfgmod.DEFAULT_COPY_FIRST, "enabled": True}
        row = tk.Frame(self.times_box, bg=theme.CARD)
        row.pack(fill="x", pady=theme.PAD_XS)
        vt = tk.StringVar(value=s.get("time", ""))
        vc = tk.StringVar(value=s.get("copy", ""))
        ve = tk.BooleanVar(value=bool(s.get("enabled", True)))
        e1 = ttk.Entry(row, textvariable=vt, width=7, justify="center", style="Time.TEntry", font=theme.F_TIME)
        e1.pack(side="left")
        ttk.Entry(row, textvariable=vc).pack(side="left", fill="x", expand=True,
                                            padx=theme.PAD_S)
        ttk.Button(row, text="删除", style="MiniDanger.TButton", width=4,
                   command=lambda r=row: self._del_row(r)).pack(side="right")
        theme.Switch(row, ve).pack(side="right", padx=(theme.PAD_S, theme.PAD_S))
        tk.Label(row, text="启用", bg=theme.CARD, fg=theme.FAINT,
                 font=theme.F_SMALL).pack(side="right")
        self.rows.append({"frame": row, "time": vt, "copy": vc, "en": ve})

    def _del_row(self, frame):
        self.rows = [r for r in self.rows if r["frame"] is not frame]
        frame.destroy()

    def _collect(self):
        """把界面上的值写回当前班级对象。返回 None 表示没有选中班级。"""
        c = cfgmod.get_class(self.cfg, self.cfg.get("active"))
        if not c:
            return None
        c["name"] = self.v_name.get().strip() or c.get("name") or "未命名班级"
        c["title_keys"] = [x.strip() for x in
                           self.v_keys.get().replace("，", ",").split(",") if x.strip()]
        c["roster"] = self.v_roster.get().strip()
        c["form_url"] = self.v_form.get().strip()
        c["form_title_keys"] = [x.strip() for x in
                                self.v_form_keys.get().replace("，", ",").split(",")
                                if x.strip()]
        c["enabled"] = bool(self.v_enable.get())
        sched = []
        for r in self.rows:
            t = r["time"].get().strip().replace("：", ":")
            if not t:
                continue
            if len(t) == 4 and t.isdigit():          # 2200 -> 22:00
                t = "%s:%s" % (t[:2], t[2:])
            sched.append({"time": t, "copy": r["copy"].get().strip(),
                          "enabled": bool(r["en"].get())})
        c["schedules"] = sched
        return c

    # ------------------------------------------------------------ 保存 / 同步
    def _sched_sig(self, c):
        """一个班"时间表指纹"：时间点/文案/启用 任一变化都要重建计划任务。"""
        try:
            return (bool(c.get("enabled", True)), tuple(sorted(
                (cfgmod.slot_of(s.get("time")), bool(s.get("enabled", True)),
                 s.get("copy") or "")
                for s in (c.get("schedules") or []))))
        except Exception:                                    # noqa: BLE001
            return None

    def _sync_if_changed(self, c):
        """保存后顺手把计划任务也对齐（2026-10-21 改）。

        原来「保存」只写 config.json，计划任务还是旧的：把 22:00 改成 22:30，
        当晚照样 22:00 发——因为 Windows 里的任务定义没变。用户以为"保存了就生效"，
        这个假设必须成立，否则改了时间等于没改。
        2026-10-21 补：不只看刚保存的这一个班——改 A 班时间后切到 B 班再点保存，
        A 的改动一直躺在内存里没人同步，到点还是按旧时间跑（审计 P1）。所以扫
        全部班，指纹和 memo 不一致的都补一遍（c 参数保留只为调用点兼容）。
        """
        if getattr(self, "_suppress_quiet_sync", False):
            return                      # 用户正点「同步到系统」，别和它抢 schtasks
        todo = []
        for cc in self.cfg.get("classes", []):
            sig = self._sched_sig(cc)
            if sig is None or self._sig_memo.get(cc["id"]) == sig:
                continue
            todo.append((cc, sig))
        if not todo:
            return
        # 注意：这里不能先写 _sig_memo。后台同步失败后要能自动重试，
        # 而重试的唯一触发点就是"指纹和 memo 不一致"；提前记账=失败也永不再试。
        self.q.put(("log", "时间表变了，正在更新计划任务（%d 个班）…" % len(todo)))
        for cc, sig in todo:
            self._sync_class_quiet(cc, sig)

    def _sync_class_quiet(self, c, sig, _round=0):
        """后台对齐这一个班的计划任务，不占 busy、不弹框。

        单飞（2026-10-21 审计）：同一个班同时只允许一个同步线程在跑 schtasks——
        连续快速保存出的两个 /Create 打同一个任务名会互相打架，报出假红字。
        在飞时把最新指纹记下，飞完拿它再飞一轮（最多 3 轮，防死循环）。
        """
        cid = c["id"]
        infl = getattr(self, "_sync_inflight", None)
        if infl is None:
            infl = self._sync_inflight = {}
        if cid in infl:
            infl[cid] = sig                    # 在飞：记下最新指纹，飞完补跑
            return
        infl[cid] = sig
        snap = json.loads(json.dumps(self.cfg))          # 线程里只读快照，避免和界面抢 dict

        def body():
            ok = False
            try:
                import schedule as schmod
                rows = schmod.sync(snap, only={cid})
                for nm, good, msg in rows:
                    self.q.put(("log", ("  %s %s" % ("✔" if good else "✘", msg)).strip()))
                bad = [m for _nm, good, m in rows if not good]
                ok = bool(rows) and not bad
                if bad:
                    self.q.put(("log", "计划任务没对齐：%s（点「同步到系统」可重试）"
                                % bad[0].splitlines()[0]))
            except Exception:                            # noqa: BLE001
                self.q.put(("log", "更新计划任务出错：\n" + traceback.format_exc(limit=3)))
            # 成功才记账；失败就清掉，下次保存会再试一次
            if ok:
                self._sig_memo[cid] = sig
            else:
                self._sig_memo.pop(cid, None)
            self.q.put(("refresh_tasks", None))
            again = infl.pop(cid, None)
            if again is not None and again != sig and _round < 3:
                self._sync_class_quiet(c, again, _round + 1)
        threading.Thread(target=body, daemon=True).start()

    def _install_legacy_cleanup(self):
        """启动时删掉交接文档里手工建的两个老任务（打卡自动提醒-22 / -23）。

        app\\schedule.py 的 purge_legacy() 早就写好了，但从来没有任何地方调用它。
        那两个老任务跑的是 lib\\main.py：只要机器上还留着，同一晚会发两条提醒
        （而且老任务 22 点那个还是禁用状态，状态看着"没问题"，最容易漏）。
        """
        def body():
            try:
                import schedule as schmod
                gone = schmod.purge_legacy()
                for name, ok, msg in gone:
                    self.q.put(("log", ("  %s %s" % ("✔" if ok else "✘", msg)).strip()))
                if gone:
                    self.q.put(("log", "已清理 %d 个老的重复任务" % len(gone)))
            except Exception:                            # noqa: BLE001
                # 失败不能零痕迹：老任务留在系统里 = 同一晚两条提醒（审计 P2）
                self.q.put(("log", "清理老任务时出错（不影响今晚发送，但值得看一眼）：\n"
                            + traceback.format_exc(limit=3)))
        threading.Thread(target=body, daemon=True).start()

    def _persist(self):
        """真的写进磁盘才算"保存"。原来四个调用点都不接异常：磁盘满/权限错时
        界面显示"已保存"，其实 config.json 根本没动。"""
        try:
            cfgmod.save(self.cfg)
            return True
        except Exception as e:                            # noqa: BLE001
            msg = "%s: %s" % (type(e).__name__, e)
            self.hint("保存失败：" + msg, bad=True)
            self.say("✘ 配置没能写进磁盘：" + msg)
            messagebox.showerror("保存失败",
                                 "配置没写进磁盘，改动只存在内存里：\n\n" + msg)
            return False

    def _save(self, quiet=False):
        c = self._collect()
        if not c:
            return None
        bad = [m for lv, m in cfgmod.validate_class(c) if lv == "error"]
        if not self._persist():
            return None
        self._sync_if_changed(c)
        if bad:
            self.hint("已保存，但还不能用：" + bad[0], bad=True)
            if not quiet:
                messagebox.showwarning("配置不完整",
                                       "已保存。以下几点必须先解决才能发送：\n\n· "
                                       + "\n· ".join(bad))
        elif not quiet:
            self.hint("已保存", bad=False)
        return c

    def _new_class(self):
        name = _ask(self.root, "新建班级", "班级名称（和 QQ 群名一致比较好认）：")
        if not name:
            return
        self._collect()
        cid = cfgmod.new_class_id(self.cfg, name)
        self.cfg["classes"].append({
            "id": cid, "name": name, "title_keys": [name], "roster": "",
            "form_url": "", "enabled": True,
            "schedules": [{"time": "22:00", "copy": cfgmod.DEFAULT_COPY_FIRST, "enabled": True}],
        })
        prev_active = self.cfg.get("active")
        self.cfg["active"] = cid
        if not self._persist():
            # 没写进磁盘：内存里也退回去，别出现"界面上有、磁盘上没有"的幽灵班
            self.cfg["active"] = prev_active
            self.cfg["classes"] = [x for x in self.cfg["classes"] if x["id"] != cid]
            return
        newc = self.cfg["classes"][-1]
        self._sig_memo[cid] = self._sched_sig(newc)
        self._sync_class_quiet(newc, self._sched_sig(newc))   # 新建完就该把任务建好，不用等用户点同步
        self._refresh_classes(keep=cid)
        self.hint("新建好了，先把花名册选上", bad=False)

    def _dup_class(self):
        c = self._pick()
        if not c:
            return
        self._collect()
        n = cfgmod.duplicate_class(self.cfg, c["id"])
        self.cfg["active"] = n["id"]             # 必须在写盘前改：放后面 active 根本没存住
        if not self._persist():
            self.cfg["active"] = c["id"]         # 没写成，内存状态也退回去
            self.cfg["classes"] = [x for x in self.cfg["classes"] if x["id"] != n["id"]]
            return
        self._refresh_classes(keep=n["id"])
        self.hint("已复制；记得换花名册和群标题关键词", bad=False)

    def _del_class(self):
        c = self._pick()
        if not c:
            return
        if len(self.cfg.get("classes", [])) <= 1:
            messagebox.showinfo("不能删", "至少要留一个班级。")
            return
        if not messagebox.askyesno("删除班级",
                                   "删除「%s」？\n它的提醒时间和计划任务会一起删掉。\n"
                                   "（这个操作不会撤回已经发出去的群消息）" % c["name"]):
            return
        import schedule as schmod
        self._sig_memo.pop(c["id"], None)
        self.cfg["classes"] = [x for x in self.cfg["classes"] if x["id"] != c["id"]]
        self.cfg["active"] = self.cfg["classes"][0]["id"]
        if not self._persist():
            # 磁盘没写成：任务一条都不能删——否则配置里还有这个班、任务却没了，
            # 每晚静默不跑（2026-10-21 审计 P2）。内存也退回原样。
            self.cfg["classes"].append(c)
            self.cfg["active"] = c["id"]
            return
        # 顺序反过来（先落盘再删任务）：两个方向都失败时，最坏是"配置没了、任务还在"
        #（多发一个已删班的提醒，有日志可查），绝不会"配置还在、任务没了"（静默漏发）。
        ok_all = True
        for name, ok, msg in schmod.remove_class(c["id"]):
            ok_all = ok_all and ok
            self.say(("  %s %s" % ("✔" if ok else "✘", msg)).strip())
        if not ok_all:
            self.hint("班级已删，但有的计划任务没删干净（再点一次删除，或点「同步到系统」）",
                      bad=True)
        else:
            self.hint("已删除「%s」" % c["name"], bad=False)
        self._refresh_classes(keep=self.cfg["active"])

    # ------------------------------------------------------------ 花名册
    def _pick_roster(self):
        p = filedialog.askopenfilename(
            title="选花名册（xlsx）",
            initialdir=os.path.dirname(self.v_roster.get()) or ROOT,
            filetypes=[("Excel", "*.xlsx *.xlsm"), ("所有文件", "*.*")])
        if p:
            # 存一份到 data\，桌面那个文件以后被改名/删掉都不影响（P1-7）
            c = self._pick()
            dst = cfgmod.stash_roster(c["id"], p) if c else p
            if dst != p:
                self.say("花名册已复制到程序目录：%s" % dst)
            self.v_roster.set(dst)
            self._check_roster()

    def _check_roster(self):
        """花名册预检：xlsx 解析放后台线程（1000+ 人读一次要好几秒，读的时候界面不能卡）。"""
        self._roster_gen += 1
        gen = self._roster_gen
        path = self.v_roster.get().strip()
        if not path or not os.path.isfile(path):
            self.l_roster.configure(text="还没选花名册" if not path else "文件不存在",
                                    style="Bad.TLabel")
            return
        if getattr(self, "_roster_busy", False):
            self._roster_dirty = True    # 在飞：飞完补读一次，别叠一堆线程
            return
        self._roster_busy = True
        self._roster_dirty = False
        self.l_roster.configure(text="正在读花名册…", style="Hint.TLabel")

        def body():
            try:
                try:
                    n, suf, probs = cfgmod.roster_summary(path)
                except Exception as e:                   # noqa: BLE001
                    n, suf, probs = 0, [], [str(e)]
                self.q.put(("roster", (gen, path, n, suf, probs)))
            finally:
                self._roster_busy = False
                if self._roster_dirty:
                    self.q.put(("roster_check", None))
        self._busy_spawn(body)

    def _show_roster(self, gen, path, n, suf, probs):
        if gen != self._roster_gen or path != self.v_roster.get().strip():
            return                                   # 慢回来的旧结果，别覆盖新的
        if n == 0:
            self.l_roster.configure(text=probs[0] if probs else "读不到人",
                                    style="Bad.TLabel")
            return
        msg = "✓ %d 人，可 @ 的学号后四位 %d 个" % (n, len(suf))
        if probs:
            self.l_roster.configure(text=msg + "；" + probs[0], style="Bad.TLabel")
        else:
            self.l_roster.configure(text=msg, style="Ok.TLabel")

    # ------------------------------------------------------------ 计划任务
    def _refresh_tasks(self):
        # 单飞+代次：连着触发多次时只认最后一轮结果，避免表格闪烁/旧结果覆盖新结果
        self._tasks_gen += 1
        gen = self._tasks_gen
        c = self._pick()
        if not c:
            for i in self.tv.get_children():
                self.tv.delete(i)
            return
        if getattr(self, "_tasks_busy", False):
            self._tasks_dirty = True    # 在飞：飞完补查一轮，快速切班不再叠线程
            return
        self._tasks_busy = True
        self._tasks_dirty = False
        self.l_task.configure(text="正在查…", style="Hint.TLabel")

        def body():
            try:
                import schedule as schmod
                rows, err = [], ""
                try:
                    for s in c.get("schedules", []):
                        slot = cfgmod.slot_of(s.get("time"))
                        name = schmod.task_name(c, slot)
                        q = schmod.query(name)
                        if q.get("error") and not err:
                            err = q["error"]    # 查询本身失败，不能当成"未创建"
                        rows.append((name, q))
                except Exception as e:                       # noqa: BLE001
                    err = str(e)
                self.q.put(("tasks", (gen, rows, err)))
            finally:
                self._tasks_busy = False
                if self._tasks_dirty:
                    self.q.put(("refresh_tasks", None))
        self._busy_spawn(body)

    def _show_tasks(self, gen, rows, err):
        if gen != self._tasks_gen:
            return                                   # 过期的结果
        for i in self.tv.get_children():
            self.tv.delete(i)
        if err:
            self.l_task.configure(text="查不到任务状态：%s" % err.splitlines()[0], style="Bad.TLabel")
            return
        missing = failed = 0
        for i, (name, q) in enumerate(rows):
            tags = ("odd",) if i % 2 else ()
            if q.get("exists"):
                txt, bad = _result_text(q.get("result"))
                self.tv.insert("", "end", values=(
                    name, q.get("status", "?"), q.get("next", "-"), txt),
                    tags=(("bad",) if bad else tags))
                # 上次没跑成功要显眼——这是「今晚到底会不会发」最直接的证据
                if bad:
                    failed += 1
            else:
                missing += 1
                self.tv.insert("", "end", values=(name, "未创建", "-", "-"),
                               tags=("bad",))
        if missing:
            self.l_task.configure(text="点「同步到系统」就能建好", style="Bad.TLabel")
        elif failed:
            self.l_task.configure(
                text="有 %d 个任务上次没跑成功，看下面的日志" % failed, style="Bad.TLabel")
        else:
            self.l_task.configure(text="已就绪", style="Ok.TLabel")

    def _busy_spawn(self, fn):
        """轻量后台任务：不占 busy 标记，结果通过队列回来。"""
        def run():
            try:
                fn()
            except Exception:                                # noqa: BLE001
                self.q.put(("log", traceback.format_exc(limit=3)))
        threading.Thread(target=run, daemon=True).start()

    def _sync(self):
        # 这里自己会同步，别再让 _save 顺手起一个后台同步（两个 schtasks 会打架）
        self._suppress_quiet_sync = True
        try:
            c = self._save(quiet=True)
        finally:
            self._suppress_quiet_sync = False
        if not c:
            return
        snap = json.loads(json.dumps(self.cfg))          # 后台只读快照，别用活的 self.cfg

        def body():
            import schedule as schmod
            # 「同步到系统」= 全班对齐（2026-10-21 审计 P1）：只同步当前选中的班时，
            # 改过又没选中的班会一直按旧时间跑。逐班调用 only=，语义与整表同步一致
            #（sync 对停用的班=删掉它的任务；配置不完整的班=报红不建）。
            rows_all = []
            bad_n = 0
            for cc in snap.get("classes") or []:
                try:
                    rows = schmod.sync(snap, only={cc["id"]})
                except Exception:                            # noqa: BLE001
                    self.q.put(("log", "同步 %s 出错：\n%s"
                                % (cc.get("name"), traceback.format_exc(limit=3))))
                    bad_n += 1
                    continue
                rows_all.extend(rows)
                good = all(r[1] for r in rows)              # 空 rows=无需改动，算好
                if good:
                    # 成功才记账：失败的保持指纹不一致，下次保存/同步会再试
                    self._sig_memo[cc["id"]] = self._sched_sig(cc)
                else:
                    bad_n += 1
                for nm, g, msg in rows:
                    self.q.put(("log", ("  %s %s" % ("✔" if g else "✘", msg)).strip()))
            ok = not bad_n
            if ok:
                why = (("计划任务已对齐（%d 项，全部班）" % len(rows_all))
                       if rows_all else "计划任务已对齐（无需改动）")
            else:
                why = "计划任务没对齐（%d 个班有失败项），点「同步到系统」可重试" % bad_n
            return {"ok": ok, "why": why}
        self._work(body, "同步计划任务")
        self.root.after(1500, self._refresh_tasks)

    # ------------------------------------------------------------ 运行
    def _dry(self):
        c = self._save(quiet=True)
        if not c:
            return

        def body():
            import runner
            return runner.dry_run(c["id"])
        self._work(body, "读取今天的打卡名单")

    def _export_weekly(self):
        """把上周（周一~周日）每天未打卡的人复制到剪贴板。只读表单，不碰 QQ。"""
        c = self._save(quiet=True)
        if not c:
            return

        def body():
            import weekly
            return weekly.export(c["id"],
                                 on_step=lambda s, m: self.q.put(("log", "  " + m)))
        self._work(body, "导出上周未打卡（逐天回看历史，约 1-2 分钟）")

    def _rehearse(self):
        """整条链路跑一遍，最后不按回车（P2-13）。

        和「看看今天谁没打卡」的区别：那个只读了腾讯文档；这个会真的打开 QQ、
        校验群标题、把文案和 @ 全部组进草稿、再复核一遍，然后清空草稿。
        想确认"今晚 22:00 那一趟会不会正常"，21:50 点这个最直接。
        """
        c = self._save(quiet=True)
        if not c:
            return
        bad = [m for lv, m in cfgmod.validate_class(c) if lv == "error"]
        if bad:
            messagebox.showerror("还不能演练", "\n\n".join(bad))
            return
        slots = [cfgmod.slot_of(s.get("time")) for s in cfgmod.enabled_schedules(c)]
        if not slots:
            messagebox.showinfo("没有时间点", "这个班没有启用的提醒时间。")
            return
        now = time.strftime("%H%M")
        past = [s for s in slots if s <= now]
        slot = past[-1] if past else slots[0]
        if not messagebox.askyesno(
                "演练（不发送）",
                "会用 %s 这条的文案走一遍完整流程：打开 QQ、校验群、读名单、组草稿、复核。\n\n"
                "最后**不会按回车**，草稿会被清空。\n\n继续？" % slot):
            return

        def body():
            import runner
            return runner.run_slot(c["id"], slot, again=True, send=False,
                                   on_step=lambda s, m: self.q.put(("log", "  " + m)))
        self._work(body, "演练（不发送）")

    def _run_now(self):
        c = self._save(quiet=True)
        if not c:
            return
        bad = [m for lv, m in cfgmod.validate_class(c) if lv == "error"]
        if bad:
            messagebox.showerror("还不能发", "\n\n".join(bad))
            return
        slots = [cfgmod.slot_of(s.get("time")) for s in cfgmod.enabled_schedules(c)]
        if not slots:
            messagebox.showinfo("没有时间点", "这个班没有启用的提醒时间。")
            return
        # 选最近的一个时间点（它的文案就是现在要发的那句）
        now = time.strftime("%H%M")
        past = [s for s in slots if s <= now]
        slot = past[-1] if past else slots[0]
        sched = next((s for s in c["schedules"]
                      if cfgmod.slot_of(s.get("time")) == slot), None)
        # 手动点是人在按按钮，跟到点自动跑不是一回事：到点要防重复，手动就是想让再发一次。
        # 所以这里 again=True 绕过防重发闸（晚上的验证就是这么发出去的）。
        # 这里必须 import：模块顶部没有 runner，缺了这行点「立即提醒」直接
        # NameError 被 tkinter 吞掉，按钮看起来"点了没反应"（2026-10-21 实测确认）。
        import runner
        prev = runner.sent_today(c["id"], slot)
        # 还没到那个时间点、现在就先发了：到点时任务会认为"这个点已经发过"而自动跳过，
        # 这不是 bug，但用户必须提前知道——不然他以为 22:00 还会再发一条。
        future = slot > now
        warn = ""
        if prev:
            warn += ("今天这个时间点已经发过一次了（%s），再点会**再发一条**。\n\n" % prev)
        if future:
            warn += ("现在还没到 %s，先发的话，今天 %s 那个自动提醒会**自动跳过**"
                     "（它认为这个点已经发过了）。\n\n"
                     % (sched.get("time") if sched else slot, slot))
        if not messagebox.askyesno(
                "确认发送",
                "接下来会：打开 QQ → 校验群标题 → 读今天的名单 → 在群里 @ 未打卡的人。\n\n"
                "用的是 %s 这条的文案：\n%s\n\n%s"
                "发出的消息不能撤回，确定现在发吗？"
                % (sched.get("time") if sched else slot, sched.get("copy", ""), warn)):
            return

        def body():
            import runner
            return runner.run_slot(c["id"], slot, again=True,
                                   on_step=lambda s, m: self.q.put(("log", "  " + m)))
        self._work(body, "立即提醒（%s）" % slot)

    def _wipe(self):
        if self.busy:
            return
        if not messagebox.askyesno("清空草稿",
                                   "会把群里输入框的草稿清掉、把 @ 弹窗关掉。\n"
                                   "已经发出去的消息不受影响。继续？"):
            return
        def body():
            import runner
            ok, why = runner.wipe_draft()
            return {"ok": ok, "why": why}
        self._work(body, "清空草稿")


class _CheckDialog(tk.Toplevel):
    """自检结果清单。每条一行，带颜色和"该怎么办"。"""

    ICON = {"ok": "✓", "warn": "!", "bad": "✕"}
    COLOR = {"ok": theme.OK, "warn": theme.WARN, "bad": theme.BAD}
    BGCOL = {"ok": theme.OK_BG, "warn": theme.WARN_BG, "bad": theme.BAD_BG}

    def __init__(self, parent, items, ms=0):
        tk.Toplevel.__init__(self, parent)
        self.title("一键自检")
        self.configure(bg=theme.BG)
        self.geometry("780x580")
        self.minsize(660, 440)
        self.transient(parent)

        head = tk.Frame(self, bg=theme.HEADER, height=68)
        head.pack(fill="x")
        head.pack_propagate(False)
        title = tk.Frame(head, bg=theme.HEADER)
        title.pack(side="left", padx=theme.PAD_XL, pady=theme.PAD_M)
        tk.Label(title, text="一键自检", bg=theme.HEADER, fg=theme.INK,
                 font=(theme.FAMILY, 14, "bold")).pack(anchor="w")
        n_bad = sum(1 for i in items if i["level"] == "bad")
        n_warn = sum(1 for i in items if i["level"] == "warn")
        sub = ("全部正常" if not n_bad and not n_warn
               else "、".join(x for x in (
                   "%d 项有问题" % n_bad if n_bad else "",
                   "%d 项要注意" % n_warn if n_warn else "") if x))
        tk.Label(title, text="%s · 耗时 %d ms · %s" % (sub, ms, time.strftime("%H:%M:%S")),
                 bg=theme.HEADER, fg=theme.HEADER_SUB,
                 font=theme.F_SUB).pack(anchor="w", pady=(theme.PAD_XS, 0))

        body = tk.Frame(self, bg=theme.BG)
        body.pack(fill="both", expand=True, padx=theme.PAD_L, pady=theme.PAD_M)
        canvas = tk.Canvas(body, bg=theme.BG, highlightthickness=0, bd=0)
        sb = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=theme.BG)
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        for it in items:
            lv = it["level"]
            row = tk.Frame(inner, bg=theme.CARD, highlightthickness=1,
                           highlightbackground=theme.BORDER)
            row.pack(fill="x", pady=(0, theme.PAD_S))
            # 左边一条 3px 的状态色竖条，比整块彩色徽章安静，也更像"状态"而非"按钮"
            tk.Frame(row, bg=self.COLOR.get(lv, theme.MUTED), width=3).pack(
                side="left", fill="y")
            badge = tk.Label(row, text=self.ICON.get(lv, "?"),
                             bg=theme.CARD,
                             fg=self.COLOR.get(lv, theme.INK), width=2,
                             font=(theme.FAMILY, 11, "bold"))
            badge.pack(side="left", fill="y", padx=(theme.PAD_M, 0))
            txt = tk.Frame(row, bg=theme.CARD)
            txt.pack(side="left", fill="both", expand=True,
                     padx=(theme.PAD_XS, theme.PAD_M), pady=theme.PAD_S)
            tk.Label(txt, text=it["title"], bg=theme.CARD, fg=theme.INK,
                     font=(theme.FAMILY, 10, "bold"), anchor="w").pack(fill="x")
            tk.Label(txt, text=it["detail"], bg=theme.CARD, fg=theme.MUTED,
                     font=theme.F_SMALL, anchor="w", justify="left",
                     wraplength=620).pack(fill="x", pady=(theme.PAD_XS, 0))
            if it.get("fix") and lv != "ok":
                tk.Label(txt, text="→ " + it["fix"], bg=theme.CARD,
                         fg=self.COLOR.get(lv),
                         font=theme.F_SMALL, anchor="w", justify="left",
                         wraplength=620).pack(fill="x", pady=(theme.PAD_XS, 0))

        foot = tk.Frame(self, bg=theme.BG)
        foot.pack(fill="x", padx=theme.PAD_L, pady=(0, theme.PAD_M))
        tk.Label(foot, text="红色的不处理，今晚很可能发不出去。", bg=theme.BG,
                 fg=theme.FAINT, font=theme.F_SMALL).pack(side="left")
        ttk.Button(foot, text="关闭", command=self.destroy).pack(side="right")
        ttk.Button(foot, text="复制清单",
                   style="Ghost.TButton",
                   command=lambda: self._copy(items)).pack(
            side="right", padx=(0, theme.PAD_S))
        self.bind("<Escape>", lambda _e: self.destroy())

    def _copy(self, items):
        import selfcheck
        txt = selfcheck.format_text(items)
        try:
            self.clipboard_clear()
            self.clipboard_append(txt)
            self.bell()
        except Exception:                                     # noqa: BLE001
            pass


def _result_text(code):
    """把计划任务的 Last Result 翻成人话。返回 (显示文本, 是否算失败)。

    schtasks 的 267011 = 0x41303 = 任务还没跑过；0 = 成功；其余非零 = 上次跑挂了。
    中文系统这里只会给一个数字，光看数字根本不知道出了什么事，所以必须翻译。
    """
    s = (code or "").strip()
    if not s:
        return "-", False
    if s == "267011":                     # 0x41303 SCHED_S_TASK_HAS_NOT_RUN
        return "还没跑过", False
    if s == "0":
        return "成功", False
    return "失败（%s）" % s, True


def _ask(parent, title, prompt, initial=""):
    """一个不用另建 Toplevel 太多代码的最小输入框。"""
    d = tk.Toplevel(parent)
    d.title(title)
    d.transient(parent)
    d.grab_set()
    d.resizable(False, False)
    d.configure(bg=theme.BG)
    ttk.Label(d, text=prompt, style="TLabel").pack(
        padx=theme.PAD_L, pady=(theme.PAD_L, theme.PAD_S), anchor="w")
    v = tk.StringVar(value=initial)
    e = ttk.Entry(d, textvariable=v, width=38)
    e.pack(padx=theme.PAD_L, fill="x")
    e.focus_set()
    out = {"v": None}

    def ok():
        out["v"] = v.get().strip()
        d.destroy()
    bar = ttk.Frame(d)
    bar.pack(padx=theme.PAD_L, pady=theme.PAD_M, fill="x")
    ttk.Button(bar, text="取消", command=d.destroy).pack(side="right")
    ttk.Button(bar, text="确定", style="Primary.TButton",
               command=ok).pack(side="right", padx=(0, theme.PAD_S))
    d.bind("<Return>", lambda _e: ok())
    d.bind("<Escape>", lambda _e: d.destroy())
    d.wait_window()
    return out["v"]


def _force_front(root):
    """把主窗口顶到最前面，并尽量抢到前台焦点。

    双击桌面快捷方式时用户以为"点了没反应"，其实窗口开在浏览器后面：
    Tk 只负责 CreateWindow，不会 SetForegroundWindow；而 Windows 的前台锁
    又不许后台进程随便抢焦点。这里先把窗口 Raise 到 Z 序最上，再用
    AttachThreadInput 借用前台线程的权限调 SetForegroundWindow；
    都失败就把窗口临时置顶一下（250ms），至少让人看见它出来了。
    """
    try:
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        root.update_idletasks()
        hwnd = u32.GetAncestor(int(root.winfo_id()), 2)      # GA_ROOT
        if not hwnd:
            hwnd = int(root.winfo_id())
        u32.ShowWindow(hwnd, 9)                              # SW_RESTORE
        u32.BringWindowToTop(hwnd)

        fg = u32.GetForegroundWindow()
        tid = u32.GetWindowThreadProcessId(fg, None) if fg else 0
        cur = k32.GetCurrentThreadId()
        attached = False
        if tid and tid != cur:
            attached = bool(u32.AttachThreadInput(cur, tid, True))
        u32.SetForegroundWindow(hwnd)
        u32.SetFocus(hwnd)
        if attached:
            u32.AttachThreadInput(cur, tid, False)

        if u32.GetForegroundWindow() != hwnd:
            # 前台锁没放行：临时 topmost 抢一下视线，再恢复普通窗口
            root.attributes("-topmost", True)
            root.after(250, lambda: root.attributes("-topmost", False))
            try:
                u32.FlashWindow(hwnd, True)
            except Exception:                                # noqa: BLE001
                pass
    except Exception:                                        # noqa: BLE001
        pass                                                 # 置前失败不影响使用


def main():
    # 单实例锁：两个 GUI 同开时会互相整文件覆盖 config.json（各自内存里的整份
    # 班级表 last-writer-wins），还会同时伸手去摸 QQ（2026-10-21 审计 P0）。
    try:
        import ctypes
        _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _k32.CreateMutexW(None, False, "Local\\dcs-gui-single")
        if ctypes.get_last_error() == 183:            # ERROR_ALREADY_EXISTS
            try:
                _r = tk.Tk()
                _r.withdraw()
                messagebox.showinfo("已经在运行",
                                    "打卡自动提醒已经打开一个窗口了，不重复启动。")
                _r.destroy()
            except Exception:                            # noqa: BLE001
                pass
            return
    except Exception:                                    # noqa: BLE001
        pass                      # 拿不到锁不拦着启动，只是失去单实例保护
    root = tk.Tk()
    try:
        root.iconbitmap(default="")
    except Exception:                                        # noqa: BLE001
        pass
    # tkinter 回调（按钮 command）抛异常时只往 stderr print 然后吞掉——界面
    # "点了没反应"却一点线索都没有。覆写成：写 gui-error.log + 只弹一次错框。
    def _cb_exc(exc, val, tb):
        text = "".join(traceback.format_exception(exc, val, tb))
        try:
            import config as _cfg
            os.makedirs(_cfg.LOGS, exist_ok=True)
            with open(os.path.join(_cfg.LOGS, "gui-error.log"), "a",
                      encoding="utf-8") as f:
                f.write("[%s] (回调)\n%s\n" % (time.strftime("%F %T"), text))
        except Exception:                                # noqa: BLE001
            pass
        if not getattr(_cb_exc, "fired", False):
            _cb_exc.fired = True
            try:
                messagebox.showerror(
                    "界面出错",
                    "点这个按钮时程序出错了（已记到 logs\\gui-error.log）：\n\n"
                    + ("%s: %s" % (exc.__name__, val))[:400])
            except Exception:                            # noqa: BLE001
                pass
    root.report_callback_exception = _cb_exc
    try:
        App(root)
    except Exception:                                        # noqa: BLE001
        # 以前 App.__init__ 里任何异常（配置结构坏、config.json 写不进去）都会让
        # pythonw 下的窗口**完全不出现**，也没有任何痕迹。至少留下日志 + 一个弹窗。
        tb = traceback.format_exc()
        try:
            import config as _cfg
            os.makedirs(_cfg.LOGS, exist_ok=True)
            with open(os.path.join(_cfg.LOGS, "gui-error.log"), "a", encoding="utf-8") as f:
                f.write("[%s]\n%s\n" % (time.strftime("%F %T"), tb))
        except Exception:                                    # noqa: BLE001
            pass
        try:
            messagebox.showerror("启动失败",
                                 "程序启动时出错，界面没能打开：\n\n"
                                 + tb.strip().splitlines()[-1]
                                 + "\n\n详细信息见 logs\\gui-error.log")
        except Exception:                                    # noqa: BLE001
            pass
        try:
            root.destroy()
        except Exception:                                    # noqa: BLE001
            pass
        return
    # 窗口开出来以后顶到最前：浏览器/视频全屏时 Tk 窗口会老老实实藏在后面，
    # 用户双击快捷方式就会以为"没启动"。_fit_window() 在 120ms 后才定尺寸，
    # 所以这里晚一点再抢前台，避免刚摆好位置又被前台切换打断。
    root.after(260, lambda: _force_front(root))
    root.mainloop()


if __name__ == "__main__":
    main()
