# -*- coding: utf-8 -*-
"""tkinter 桌面悬浮窗。

设计约束：
  * 无边框、置顶、半透明，贴着桌面看，不占任务栏
  * 全屏靠拖，位置记在本机配置里
  * 抓数据在后台线程，结果经 queue 回主线程刷新——tkinter 不是线程安全的
  * 收起模式只留余额一行，平时不碍事
"""

import os
import queue
import threading
import tkinter as tk
import webbrowser
from datetime import datetime

from . import platform_auth
from .api import DeepSeekClient, ApiError, AuthExpired, NetworkError, Snapshot
from .config import Config, config_path
from .formatting import human_tokens, human_count, money, percent

# ---- 配色（深色）----
BG = "#15171c"
CARD = "#1d2027"
BORDER = "#2c313c"
FG = "#e7e9ee"
FG_DIM = "#8a93a1"
FG_FAINT = "#5f6773"
ACCENT = "#5b9dff"
GOOD = "#4ade80"
WARN = "#fbbf24"
BAD = "#f87171"

PAD = 12
WIDTH = 320


class MonitorApp:
    """悬浮窗主程序。"""

    def __init__(self):
        self.config = Config.load()
        # 首次运行就把默认配置落盘：分发给别人时，对方能直接找到这个文件查看/编辑，
        # 也顺带验证配置目录可写（没权限就早点暴露，别拖到用户点保存那一刻）。
        if not os.path.exists(config_path()):
            self.config.save()

        self.snapshot = None
        self.token = None
        self.token_source = "尚未获取"
        self.last_error = None
        self.queue = queue.Queue()
        self._busy = False
        self._drag = (0, 0)
        self._pos = [0, 0]        # 自记窗口位置（overrideredirect 下 winfo_x 不可靠）
        self._settings_shown = False

        # 高 DPI 支持，避免字发虚
        try:
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

        self.root = tk.Tk()
        self.root.title("DeepSeek 用量监视器")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", bool(self.config.topmost))
        try:
            self.root.attributes("-alpha", float(self.config.alpha))
        except tk.TclError:
            pass
        self.root.configure(bg=BORDER)

        self.scale = self._detect_scale()
        self._build_fonts()
        self._build_ui()
        self._place_window()
        self._bind_events()

        self.root.after(100, self._pump)
        self.root.after(200, self.refresh)
        self._schedule_next()

    # ------------------------------------------------------------------
    # 尺寸 / 字体
    # ------------------------------------------------------------------

    def _detect_scale(self):
        try:
            return max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        except Exception:
            return 1.0

    def px(self, n):
        return int(round(n * self.scale))

    def _build_fonts(self):
        s = self.scale
        ui = "Microsoft YaHei UI"
        mono = "Consolas"
        self.f_title = (ui, int(9 * s), "bold")
        self.f_label = (ui, int(8 * s))
        self.f_value = (mono, int(10 * s), "bold")
        self.f_big = (mono, int(21 * s), "bold")
        self.f_small = (ui, int(7 * s))
        self.f_icon = (ui, int(9 * s))

    # ------------------------------------------------------------------
    # 界面构建
    # ------------------------------------------------------------------

    def _build_ui(self):
        p = self.px
        outer = tk.Frame(self.root, bg=BORDER)
        outer.pack(fill="both", expand=True, padx=0, pady=0)
        self.inner = tk.Frame(outer, bg=BG)
        self.inner.pack(fill="both", expand=True, padx=1, pady=1)

        # ---- 标题栏 ----
        header = tk.Frame(self.inner, bg=BG)
        header.pack(fill="x", padx=p(PAD), pady=(p(9), p(4)))

        self.lbl_title = tk.Label(header, text="DeepSeek", font=self.f_title,
                                  fg=FG, bg=BG)
        self.lbl_title.pack(side="left")

        self.lbl_status = tk.Label(header, text="●", font=self.f_icon,
                                   fg=FG_FAINT, bg=BG)
        self.lbl_status.pack(side="left", padx=(p(6), 0))

        self._icon(header, "✕", self.quit, side="right")
        self._icon(header, "↻", self.refresh, side="right")
        self._icon(header, "－", self.toggle_collapse, side="right")

        self.header = header

        # ---- 可折叠区域 ----
        self.body = tk.Frame(self.inner, bg=BG)

        # 余额
        bal = tk.Frame(self.body, bg=BG)
        bal.pack(fill="x", padx=p(PAD), pady=(p(2), p(6)))
        self.lbl_balance = tk.Label(bal, text="—", font=self.f_big,
                                    fg=GOOD, bg=BG, anchor="w")
        self.lbl_balance.pack(side="left")
        self.lbl_total_cost = tk.Label(bal, text="", font=self.f_small,
                                       fg=FG_FAINT, bg=BG, anchor="e")
        self.lbl_total_cost.pack(side="right", pady=(p(9), 0))

        self._separator()

        # 今日
        self.sec_today = self._section("今日")
        self.v_today_tokens = self.sec_today["value_left"]
        self.v_today_req = self.sec_today["value_right"]
        self.sec_today["set_left_label"]("token")
        self.sec_today["set_right_label"]("请求")
        self.sec_today["add_row"]()
        self.v_today_rate = self.sec_today["row2_value_left"]
        self.v_today_cost = self.sec_today["row2_value_right"]
        self.sec_today["set_row2_left_label"]("命中率")
        self.sec_today["set_row2_right_label"]("费用")

        self._separator()

        # 本月
        self.sec_month = self._section("本月")
        self.v_month_tokens = self.sec_month["value_left"]
        self.v_month_req = self.sec_month["value_right"]
        self.sec_month["set_left_label"]("token")
        self.sec_month["set_right_label"]("请求")
        self.sec_month["add_row"]()
        self.v_month_rate = self.sec_month["row2_value_left"]
        self.v_month_cost = self.sec_month["row2_value_right"]
        self.sec_month["set_row2_left_label"]("命中率")
        self.sec_month["set_row2_right_label"]("费用")

        # 趋势图
        # 注意：Canvas 不指定 width 时默认宽度是 378px，会把整个窗口撑宽，
        # 所以显式给一个极小宽度，靠 pack(fill="x") 撑满可用空间。
        self.chart = tk.Canvas(self.body, width=1, height=p(46), bg=BG,
                               highlightthickness=0, bd=0)
        self.chart.pack(fill="x", padx=p(PAD), pady=(p(8), p(2)))
        self.chart.bind("<Motion>", self._on_chart_hover)
        self.chart.bind("<Leave>", lambda e: self._set_footer(self._footer_text))
        self._chart_tip = None

        # 页脚
        self.lbl_footer = tk.Label(self.inner, text="启动中…", font=self.f_small,
                                   fg=FG_FAINT, bg=BG, anchor="w")
        self.lbl_footer.pack(fill="x", padx=p(PAD), pady=(p(2), p(8)))
        self._footer_text = "启动中…"

        if not self.config.collapsed:
            self.body.pack(fill="x")

        self._build_menu()

    def _icon(self, parent, text, cmd, side):
        lbl = tk.Label(parent, text=text, font=self.f_icon, fg=FG_DIM, bg=BG,
                       cursor="hand2", padx=self.px(3))
        lbl.pack(side=side, padx=(self.px(2), 0))
        lbl.bind("<Button-1>", lambda e: cmd())
        lbl.bind("<Enter>", lambda e: lbl.configure(fg=FG))
        lbl.bind("<Leave>", lambda e: lbl.configure(fg=FG_DIM))
        return lbl

    def _separator(self):
        line = tk.Frame(self.body, bg=BORDER, height=1)
        line.pack(fill="x", padx=self.px(PAD), pady=self.px(4))

    def _section(self, title):
        """造一个『标题 + 两行两列指标』的区块，返回句柄字典。"""
        p = self.px
        box = tk.Frame(self.body, bg=BG)
        box.pack(fill="x", padx=p(PAD), pady=(p(4), p(2)))

        head = tk.Frame(box, bg=BG)
        head.pack(fill="x")
        tk.Label(head, text=title, font=self.f_label, fg=FG_DIM,
                 bg=BG, anchor="w").pack(side="left")
        lbl_stamp = tk.Label(head, text="", font=self.f_small, fg=FG_FAINT,
                             bg=BG, anchor="e")
        lbl_stamp.pack(side="right")

        grid = tk.Frame(box, bg=BG)
        grid.pack(fill="x", pady=(p(3), 0))
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(3, weight=1)

        l1 = tk.Label(grid, text="", font=self.f_small, fg=FG_FAINT, bg=BG, anchor="w")
        v1 = tk.Label(grid, text="—", font=self.f_value, fg=FG, bg=BG, anchor="w")
        l2 = tk.Label(grid, text="", font=self.f_small, fg=FG_FAINT, bg=BG, anchor="e")
        v2 = tk.Label(grid, text="—", font=self.f_value, fg=FG, bg=BG, anchor="e")
        l1.grid(row=0, column=0, sticky="w")
        v1.grid(row=0, column=1, sticky="w", padx=(p(6), p(10)))
        l2.grid(row=0, column=2, sticky="e")
        v2.grid(row=0, column=3, sticky="e", padx=(p(6), 0))

        state = {
            "stamp": lbl_stamp,
            "value_left": v1, "value_right": v2,
            "set_left_label": lambda t: l1.configure(text=t),
            "set_right_label": lambda t: l2.configure(text=t),
        }

        def add_row():
            l3 = tk.Label(grid, text="", font=self.f_small, fg=FG_FAINT, bg=BG, anchor="w")
            v3 = tk.Label(grid, text="—", font=self.f_value, fg=FG, bg=BG, anchor="w")
            l4 = tk.Label(grid, text="", font=self.f_small, fg=FG_FAINT, bg=BG, anchor="e")
            v4 = tk.Label(grid, text="—", font=self.f_value, fg=FG, bg=BG, anchor="e")
            l3.grid(row=1, column=0, sticky="w", pady=(p(3), 0))
            v3.grid(row=1, column=1, sticky="w", padx=(p(6), p(10)), pady=(p(3), 0))
            l4.grid(row=1, column=2, sticky="e", pady=(p(3), 0))
            v4.grid(row=1, column=3, sticky="e", padx=(p(6), 0), pady=(p(3), 0))
            state["row2_value_left"] = v3
            state["row2_value_right"] = v4
            state["set_row2_left_label"] = lambda t: l3.configure(text=t)
            state["set_row2_right_label"] = lambda t: l4.configure(text=t)

        state["add_row"] = add_row
        return state

    def _build_menu(self):
        p = self.px
        m = tk.Menu(self.root, tearoff=0, bg=CARD, fg=FG,
                    activebackground=ACCENT, activeforeground="#ffffff",
                    bd=0, font=self.f_label)

        interval = tk.Menu(m, tearoff=0, bg=CARD, fg=FG,
                           activebackground=ACCENT, activeforeground="#ffffff",
                           bd=0, font=self.f_label)
        self._interval_var = tk.IntVar(value=int(self.config.refresh_seconds))
        for sec, name in ((30, "30 秒"), (60, "1 分钟"), (300, "5 分钟"),
                          (900, "15 分钟"), (1800, "30 分钟")):
            interval.add_radiobutton(label=name, value=sec,
                                     variable=self._interval_var,
                                     command=self._on_interval)
        m.add_cascade(label="刷新间隔", menu=interval)

        # tkinter 的 Variable 必须留引用，否则会被 GC 回收导致菜单失效
        self._topmost_var = tk.BooleanVar(value=bool(self.config.topmost))
        m.add_checkbutton(label="窗口置顶", onvalue=True, offvalue=False,
                          variable=self._topmost_var,
                          command=self._toggle_topmost)
        m.add_separator()
        m.add_command(label="立即刷新", command=self.refresh)
        m.add_command(label="设置…", command=self.open_settings)
        m.add_command(label="重新获取登录态", command=self.reconnect)
        m.add_command(label="打开用量页面", command=lambda: webbrowser.open(
            "https://platform.deepseek.com/usage"))
        m.add_separator()
        m.add_command(label="退出", command=self.quit)
        self.menu = m

    # ------------------------------------------------------------------
    # 窗口行为
    # ------------------------------------------------------------------

    def _place_window(self):
        self.root.update_idletasks()
        w = max(self.px(WIDTH), self.root.winfo_reqwidth())
        h = self.root.winfo_reqheight()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = self.config.window_x
        y = self.config.window_y
        if x is None or y is None:
            x = sw - w - self.px(24)
            y = sh - h - self.px(80)
        # 防止跑到屏幕外
        x = max(0, min(int(x), sw - w))
        y = max(0, min(int(y), sh - h))
        self._pos = [x, y]
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _bind_events(self):
        for w in (self.header, self.lbl_title, self.lbl_status):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<ButtonRelease-1>", self._drag_end)
        self.root.bind("<Button-3>", self._popup_menu)
        self.root.bind("<Escape>", lambda e: self.quit())

    def _drag_start(self, e):
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag_move(self, e):
        x = e.x_root - self._drag[0]
        y = e.y_root - self._drag[1]
        self._pos = [x, y]
        self.root.geometry(f"+{x}+{y}")

    def _drag_end(self, _e):
        self.config.window_x, self.config.window_y = self._pos
        self.config.save()

    def _popup_menu(self, e):
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def toggle_collapse(self):
        if self.body.winfo_ismapped():
            self.body.pack_forget()
            self.config.collapsed = True
        else:
            self.body.pack(fill="x")
            self.config.collapsed = False
        self.config.save()
        self.root.after(10, self._refit)
        if self.snapshot:                # 展开后宽度变了，趋势图要重画
            self.root.after(40, lambda: self._draw_chart(self.snapshot))

    def _refit(self):
        """按内容重算窗口尺寸，同时保持当前位置不跑掉。

        overrideredirect 的窗口在 mainloop 起来之前 winfo_x/y 一直返回 0，
        所以位置以 self._pos 为准自己记着。
        """
        self.root.update_idletasks()
        w = max(self.px(WIDTH), self.root.winfo_reqwidth())
        h = self.root.winfo_reqheight()
        x, y = self._pos
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        return w, h

    def _toggle_topmost(self):
        self.config.topmost = bool(self._topmost_var.get())
        self.root.attributes("-topmost", bool(self.config.topmost))
        self.config.save()

    def _on_interval(self):
        self.config.refresh_seconds = int(self._interval_var.get())
        self.config.save()
        self._schedule_next()

    def _schedule_next(self):
        if getattr(self, "_timer", None):
            try:
                self.root.after_cancel(self._timer)
            except Exception:
                pass
        self._timer = self.root.after(int(self.config.refresh_seconds) * 1000,
                                      self._tick)

    def _tick(self):
        self.refresh()
        self._schedule_next()

    # ------------------------------------------------------------------
    # 数据
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 设置
    # ------------------------------------------------------------------

    def open_settings(self):
        """凭据设置对话框。

        分发给别人用时，这是唯一的配置入口——没有它，遇到"读不到登录态"
        的用户就只能去手改 config.json，等于没有出路。
        """
        win = getattr(self, "_settings_win", None)
        if win is not None and win.winfo_exists():
            win.lift()
            win.focus_force()
            return

        p = self.px
        win = tk.Toplevel(self.root)
        self._settings_win = win
        win.title("DeepSeek 监视器 · 设置")
        win.configure(bg=BG)
        win.attributes("-topmost", True)
        win.resizable(False, False)

        pad = {"padx": p(16)}

        tk.Label(win, text="平台登录态（看用量必需）", font=self.f_label,
                 fg=FG, bg=BG, anchor="w").pack(fill="x", pady=(p(14), p(2)), **pad)

        row1 = tk.Frame(win, bg=BG)
        row1.pack(fill="x", **pad)
        e_token = tk.Entry(row1, font=self.f_value, bg=CARD, fg=FG,
                           insertbackground=FG, relief="flat", width=26)
        e_token.pack(side="left", ipady=p(4), padx=(0, p(8)))
        if self.config.manual_token:
            e_token.insert(0, self.config.manual_token)

        tk.Label(win, text="留空则每次自动从浏览器读取", font=self.f_small,
                 fg=FG_FAINT, bg=BG, anchor="w").pack(fill="x",
                                                      pady=(p(3), p(10)), **pad)

        tk.Label(win, text="API Key（可选，只能看余额）", font=self.f_label,
                 fg=FG, bg=BG, anchor="w").pack(fill="x", pady=(0, p(2)), **pad)
        e_key = tk.Entry(win, font=self.f_value, bg=CARD, fg=FG,
                         insertbackground=FG, relief="flat", width=34)
        e_key.pack(fill="x", ipady=p(4), **pad)
        if self.config.api_key:
            e_key.insert(0, self.config.api_key)
        tk.Label(win, text="没有平台登录态时，凭它至少能看到余额",
                 font=self.f_small, fg=FG_FAINT, bg=BG,
                 anchor="w").pack(fill="x", pady=(p(3), p(10)), **pad)

        hint = tk.Label(win, text="凭据只保存在本机 config.json，不会外发。",
                        font=self.f_small, fg=FG_FAINT, bg=BG, anchor="w",
                        wraplength=p(310), justify="left")
        hint.pack(fill="x", **pad)

        def set_hint(text, color):
            hint.configure(text=text, fg=color)

        def auto_fill():
            tok, src = platform_auth.extract_user_token()
            if tok:
                e_token.delete(0, "end")
                e_token.insert(0, tok)
                set_hint(f"已从「{src}」读取到登录态，记得点保存。", WARN)
            else:
                set_hint(f"自动提取失败：{src}", BAD)

        pick = tk.Label(row1, text="自动提取", font=self.f_small, fg=ACCENT,
                        bg=BG, cursor="hand2")
        pick.pack(side="left")
        pick.bind("<Button-1>", lambda e: auto_fill())

        def save():
            self.config.manual_token = e_token.get().strip() or None
            self.config.api_key = e_key.get().strip() or None
            self.config.save()
            self.token = None
            self._set_status("busy")
            self._set_footer("已保存，正在刷新…")
            win.destroy()
            self.refresh(force_token=True)

        btns = tk.Frame(win, bg=BG)
        btns.pack(fill="x", pady=(p(12), p(14)), **pad)
        for text, cmd, color in (("保存", save, GOOD),
                                 ("取消", win.destroy, FG_DIM)):
            b = tk.Label(btns, text=text, font=self.f_label, fg=color, bg=CARD,
                         cursor="hand2", padx=p(18), pady=p(5))
            b.pack(side="right", padx=(p(6), 0))
            b.bind("<Button-1>", lambda e, c=cmd: c())

        win.update_idletasks()
        x = self._pos[0] + (self.px(WIDTH) - win.winfo_reqwidth()) // 2
        y = self._pos[1] + p(40)
        win.geometry(f"+{max(0, x)}+{max(0, y)}")

    def reconnect(self):
        """重新获取登录态。

        语义是"重新去浏览器里读"，所以此时浏览器优先于手动配置；
        但**不清除**手动配置——手动配过说明用户可能压根没有浏览器登录态，
        清掉会让他无路可走（之前就是这个 bug）。
        """
        self.token = None
        self._set_footer("正在重新获取登录态…")
        self.refresh(force_token=True)

    def refresh(self, force_token=False):
        if self._busy:
            return
        self._busy = True
        self._set_status("busy")
        threading.Thread(target=self._worker,
                         kwargs={"force_token": force_token},
                         daemon=True).start()

    def _worker(self, force_token=False):
        try:
            if force_token or not self.token:
                token, source = None, None
                # 常规启动：手动配置优先（用户显式配过就尊重它）
                if not force_token:
                    token, source = self.config.manual_token, "手动配置"
                # 重新获取：浏览器优先
                if not token:
                    token, source = platform_auth.extract_user_token()
                # 浏览器提取失败，回落到手动配置
                if not token and self.config.manual_token:
                    token = self.config.manual_token
                    source = "手动配置（浏览器提取失败时回落到此）"
                if not token:
                    self.queue.put(("auth_fail", source))
                    return
                self.token = token
                self.token_source = source

            client = DeepSeekClient(user_token=self.token,
                                    api_key=self.config.api_key)
            snap = client.snapshot()
            self.queue.put(("data", snap))
        except AuthExpired as e:
            # 登录态可能只是过期了，清掉让下次重新提
            self.token = None
            self.queue.put(("auth_fail", str(e)))
        except NetworkError as e:
            self.queue.put(("error", f"网络：{e}"))
        except ApiError as e:
            self.queue.put(("error", str(e)))
        except Exception as e:                       # noqa: BLE001
            self.queue.put(("error", f"{type(e).__name__}: {e}"))

    def _pump(self):
        """主线程消费后台结果。"""
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "data":
                    self._apply(payload)
                elif kind == "error":
                    self.last_error = payload
                    self._set_status("error")
                    self._set_footer(f"⚠ {payload}")
                elif kind == "auth_fail":
                    self.last_error = payload
                    self._set_status("error")
                    self._set_footer("⚠ 登录态不可用，右键『设置』填凭据")
                    self.lbl_balance.configure(text="—")
                    # 分发给别人时，对方不知道要右键。首次读不到凭据就直接把设置弹出来，
                    # 否则他只会看到一个空的窗口然后来问你"怎么不显示"。
                    if (not self._settings_shown
                            and not self.config.manual_token
                            and not self.config.api_key):
                        self._settings_shown = True
                        self.root.after(400, self.open_settings)
        except queue.Empty:
            pass
        self._busy = False
        self.root.after(200, self._pump)

    def _apply(self, snap):
        self.snapshot = snap
        self.last_error = None
        self._set_status("ok")

        wallet = self._pick_wallet(snap)
        if wallet:
            low = wallet.balance < float(self.config.low_balance_threshold)
            self.lbl_balance.configure(
                text=money(wallet.balance, wallet.symbol),
                fg=BAD if low else GOOD)
            self.lbl_total_cost.configure(
                text="仅余额模式" if snap.balance_only
                else f"累计 {money(wallet.total_cost, wallet.symbol)}")
        else:
            self.lbl_balance.configure(text="—", fg=FG_DIM)

        if snap.balance_only:
            # 只拿到了官方余额，用量 / 费用 / 趋势都没有数据
            for v in (self.v_today_tokens, self.v_today_req,
                      self.v_today_rate, self.v_today_cost,
                      self.v_month_tokens, self.v_month_req,
                      self.v_month_rate, self.v_month_cost):
                v.configure(text="—")
            self.sec_today["stamp"].configure(text="")
            self.chart.delete("all")
            self._bars = []
        else:
            stamp = snap.fetched_at.strftime("%H:%M")

            t = snap.today
            self.v_today_tokens.configure(text=human_tokens(t.total_tokens))
            self.v_today_req.configure(text=human_count(t.requests))
            self.v_today_rate.configure(text=percent(t.cache_hit_rate))
            self.v_today_cost.configure(
                text=money(self._cost_for(snap.cost_today, wallet),
                           self._sym(wallet)))
            self.sec_today["stamp"].configure(text=stamp)

            m = snap.month
            self.v_month_tokens.configure(text=human_tokens(m.total_tokens))
            self.v_month_req.configure(text=human_count(m.requests))
            self.v_month_rate.configure(text=percent(m.cache_hit_rate))
            self.v_month_cost.configure(
                text=money(self._cost_for(snap.cost_month, wallet),
                           self._sym(wallet)))

        # 先定几何、再画图：否则 Canvas 还没拿到真实宽度，柱子会挤成一片
        self._refit()
        if not snap.balance_only:
            self._draw_chart(snap)

        if snap.balance_only:
            note = "仅余额模式 · 右键『设置』填 API Key 或登录态可解锁用量"
        elif snap.warnings:
            note = "；".join(snap.warnings)
        else:
            note = self.token_source
        self._footer_text = f"{snap.date_string()} · {snap.elapsed_ms}ms · {note}"
        self._set_footer(self._footer_text)

    def _sym(self, wallet):
        return wallet.symbol if wallet else "¥"

    def _cost_for(self, table, wallet):
        """从 {币种: Decimal} 里挑主钱包币种对应的值。"""
        if not table:
            return None
        cur = wallet.currency if wallet else None
        if cur and cur in table:
            return table[cur]
        for c in ("CNY", "USD"):
            if c in table and table[c]:
                return table[c]
        return next(iter(table.values()))

    def _pick_wallet(self, snap):
        """按配置的币种选钱包；未配置则自动挑。"""
        want = self.config.currency
        if want:
            w = snap.wallet(want)
            if w:
                return w
        return snap.primary_wallet

    def _set_status(self, state):
        color = {"ok": GOOD, "busy": WARN, "error": BAD}.get(state, FG_FAINT)
        self.lbl_status.configure(fg=color)

    def _set_footer(self, text):
        self._footer_text = text
        self.lbl_footer.configure(text=text)

    # ------------------------------------------------------------------
    # 趋势图
    # ------------------------------------------------------------------

    def _draw_chart(self, snap):
        c = self.chart
        c.delete("all")
        self._bars = []
        days = snap.daily
        if not days:
            return
        p = self.px
        w = c.winfo_width()
        if w <= 1:                       # 几何还没算出来时的兜底
            w = p(WIDTH - 2 * PAD)
        h = int(c["height"])
        n = len(days)
        gap = max(1, p(1))
        bw = max(2, (w - gap * (n - 1)) / n)
        peak = max((d.usage.total_tokens for d in days), default=0) or 1
        today = datetime.now().strftime("%Y-%m-%d")

        for i, d in enumerate(days):
            x = i * (bw + gap)
            ratio = d.usage.total_tokens / peak
            bh = max(1.0, ratio * (h - p(10)))
            y = h - bh - p(8)
            if d.usage.total_tokens == 0:
                color = "#2a2f3a"
                bh = 1.5
                y = h - p(8) - bh
            elif d.date == today:
                color = ACCENT
            else:
                color = "#3b6ea8"
            item = c.create_rectangle(x, y, x + bw, h - p(8),
                                      fill=color, outline="")
            self._bars.append((x, x + bw, d, item))

        c.create_text(0, h - p(6), text="近 30 天", anchor="sw",
                      fill=FG_FAINT, font=self.f_small)

    def _on_chart_hover(self, e):
        if not getattr(self, "_bars", None):
            return
        for x0, x1, day, _item in self._bars:
            if x0 <= e.x <= x1:
                cost = "  ".join(f"{v:.3f}" for v in day.cost.values() if v)
                txt = f"{day.date}  token {human_tokens(day.usage.total_tokens)}" \
                      f"  请求 {human_count(day.usage.requests)}"
                if cost:
                    txt += f"  ¥{cost}"
                if day.usage.cache_hit_rate is not None:
                    txt += f"  命中 {percent(day.usage.cache_hit_rate)}"
                self._set_footer(txt)
                return

    # ------------------------------------------------------------------

    def run(self):
        self.root.mainloop()

    def quit(self):
        self.config.window_x, self.config.window_y = self._pos
        self.config.save()
        self.root.destroy()
