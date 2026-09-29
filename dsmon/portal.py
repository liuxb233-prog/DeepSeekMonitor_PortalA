"""Portal A theme. The original data modules and original ui.py stay intact."""
from __future__ import annotations

import ctypes
import math
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from datetime import datetime, timedelta
from decimal import Decimal

from PIL import Image, ImageTk
from . import ui as legacy
from . import platform_auth
from .api import DeepSeekClient, Snapshot, Wallet, Usage, DayUsage, AuthExpired, ApiError, NetworkError
from .config import Config
from .formatting import human_tokens, human_count, money, percent

BG = '#101b17'
CARD = '#1a2722'
BORDER = '#35483e'
FG = '#f1f5ee'
DIM = '#a6b7ac'
FAINT = '#738b7d'
LIME = '#b6f65b'
GREEN = '#72e653'
WARN = '#ffd078'
BAD = '#ff8588'
KEY = '#ff00ff'
WIDTH = 400
HEIGHT = 648
ASSETS = Path(__file__).resolve().parent.parent / 'assets'


def token_text(n):
    for divisor,suffix in [(1_000_000_000,'B'),(1_000_000,'M'),(1_000,'K')]:
        if abs(n)>=divisor:
            return f'{n/divisor:.1f}'.rstrip('0').rstrip('.')+suffix
    return str(n)

# Shared settings and native menus adopt the new theme; data code is unchanged.
for _key, _value in dict(BG=BG, CARD=CARD, BORDER=BORDER, FG=FG, FG_DIM=DIM,
                        FG_FAINT=FAINT, ACCENT='#477931', GOOD=LIME,
                        WARN=WARN, BAD=BAD, WIDTH=WIDTH).items():
    setattr(legacy, _key, _value)


def demo_snapshot():
    """Explicit offline preview fixture; never used in a live session."""
    snap = Snapshot()
    snap.wallets = [Wallet('CNY', Decimal('128.64'), total_cost=Decimal('71.36'))]
    snap.today = Usage(requests=142, cache_hit=128400, cache_miss=32100, response=18600)
    snap.month = Usage(requests=3126, cache_hit=2810400, cache_miss=780100, response=628600)
    snap.cost_today = {'CNY': Decimal('2.36')}
    snap.cost_month = {'CNY': Decimal('71.36')}
    for i in range(30):
        value = Decimal(str(round(1.2 + .7 * math.sin(i*.71) + .5 * math.cos(i*1.63) + (2.4 if i == 21 else 0), 3)))
        day = (datetime.now() - timedelta(days=29-i)).strftime('%Y-%m-%d')
        snap.daily.append(DayUsage(day, Usage(requests=60+i, cache_hit=60000+i*1900,
                                            cache_miss=16000+i*300, response=8000+i*220), {'CNY': value}))
    return snap


class PortalApp(legacy.MonitorApp):
    def __init__(self, *, demo=False, config=None, scale=None, start_network=True):
        self.demo = demo
        self.config = config if config is not None else (Config({'alpha': 1.0}) if demo else Config.load())
        self._save_enabled = not demo and config is None
        self.snapshot = demo_snapshot() if demo else None
        self.token = None
        self.token_source = '尚未获取'
        self.last_error = None
        self.queue = queue.Queue()
        self._busy = False
        self._settings_shown = False
        self._settings_win = None
        self._drag = None
        self._pos = [0, 0]
        self._footer_text = '演示数据 · 未连接账号' if demo else '等待同步…'
        self.state = 'demo' if demo else 'idle'
        self.period = 'today'
        self.chart_mode = 'cost'
        self._actions = []
        self._hover = None
        self._chart_days = []
        self._closed = False
        self._images = {}
        self.display = {}
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title('DeepSeek Monitor · Portal A' + (' · 演示' if demo else ''))
        self.root.overrideredirect(True)
        self.root.attributes('-topmost', bool(self.config.topmost))
        self.root.configure(bg=KEY)
        self.root.attributes('-transparentcolor', KEY)
        self.root.attributes('-alpha', max(.65, min(1., float(self.config.alpha))))
        self.scale = scale or max(1., self.root.winfo_fpixels('1i')/96.)
        # Fit the working area even at large Windows text/display scales.
        self.scale = min(self.scale, (self.root.winfo_screenheight()-90)/HEIGHT)
        self._build_fonts()
        self.canvas = tk.Canvas(self.root, bg=KEY, bd=0, highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)
        self._load_art()
        self._build_menu()
        self.menu.insert_command(0, label='展开 / 收起', command=self.toggle_collapse)
        self.menu.configure(activeforeground=BG)
        self._place_window()
        self._bind_events()
        self._render()
        self.root.deiconify()
        if not demo and start_network:
            self.root.after(100, self._pump)
            self.root.after(250, self.refresh)
            self._schedule_next()

    def _save(self):
        if self._save_enabled:
            self.config.save()

    def font(self, size, bold=False, mono=False):
        return ('Consolas' if mono else 'Microsoft YaHei UI', -self.px(size), 'bold' if bold else 'normal')

    def _build_fonts(self):
        self.f_title = self.font(16, True)
        self.f_label = self.font(12)
        self.f_value = self.font(14, True, True)
        self.f_big = self.font(42, True, True)
        self.f_small = self.font(11)
        self.f_icon = self.font(16)

    def _load_art(self):
        path = ASSETS / 'portal.png'
        if path.exists():
            with Image.open(path) as source:
                source = source.convert('RGBA')
                for name, size in [('hero', 136), ('mini', 48)]:
                    thumb = source.copy()
                    thumb.thumbnail((self.px(size), self.px(size)), Image.Resampling.LANCZOS)
                    self._images[name] = ImageTk.PhotoImage(thumb)
        elif (ASSETS / 'concept-a.png').exists():
            # Display sprite regions of the approved concept using Tk's native
            # image API. The original artwork file is retained byte-for-byte.
            with Image.open(ASSETS / 'concept-a.png') as original:
                source = ImageTk.PhotoImage(original)
            for name, region, target in [
                ('hero', (666, 222, 877, 399), 128),
                ('mini', (258, 1162, 335, 1240), 45),
            ]:
                x0,y0,x1,y1 = region
                sprite = tk.PhotoImage()
                sprite.tk.call(sprite, 'copy', source, '-from', x0,y0,x1,y1)
                numerator = max(1, round(self.px(target)*10/(x1-x0)))
                self._images[name] = sprite.zoom(numerator).subsample(10)

    def _height(self):
        return 64 if self.config.collapsed else HEIGHT

    def _workarea(self):
        class Rect(ctypes.Structure):
            _fields_ = [(v, ctypes.c_long) for v in ('left', 'top', 'right', 'bottom')]
        area = Rect()
        if ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(area), 0):
            return area.left, area.top, area.right, area.bottom
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def _place_window(self):
        left, top, right, bottom = self._workarea()
        w, h = self.px(WIDTH), self.px(self._height())
        x = self.config.window_x
        y = self.config.window_y
        self._pos = [right-w-self.px(24) if x is None else int(x),
                     bottom-h-self.px(24) if y is None else int(y)]
        self._refit()

    def _refit(self):
        w, h = self.px(WIDTH), self.px(self._height())
        left, top, right, bottom = self._workarea()
        x = max(left, min(self._pos[0], right-w))
        y = max(top, min(self._pos[1], bottom-h))
        self._pos = [x, y]
        self.canvas.configure(width=w, height=h)
        self.root.geometry(f'{w}x{h}{x:+d}{y:+d}')
        return w, h

    def _round(self, x, y, w, h, *, fill=CARD, outline=BORDER, radius=13, tags=()):
        r = min(radius, w/2, h/2)
        points = [x+r,y, x+w-r,y, x+w,y, x+w,y+r, x+w,y+h-r, x+w,y+h,
                  x+w-r,y+h, x+r,y+h, x,y+h, x,y+h-r, x,y+r, x,y]
        return self.canvas.create_polygon(*[self.px(v) for v in points], smooth=True,
                                          splinesteps=24, fill=fill, outline=outline,
                                          width=self.px(1), tags=tags)

    def _text(self, x, y, text, size=12, color=FG, bold=False, anchor='w', mono=False, tags=()):
        return self.canvas.create_text(self.px(x), self.px(y), text=text,
                                       font=self.font(size, bold, mono), fill=color,
                                       anchor=anchor, tags=tags)

    def _line(self, coords, color=BORDER, width=1, **kw):
        return self.canvas.create_line(*[self.px(v) for v in coords], fill=color,
                                       width=max(1,self.px(width)), **kw)

    def _action(self, rect, action, tip):
        self._actions.append((rect, action, tip))

    def _button(self, x, y, symbol, action, tip, width=29):
        self._round(x,y,width,28,fill='#223129',outline='#3d5144',radius=7)
        self._text(x+width/2,y+14,symbol,17,DIM,anchor='center')
        self._action((x,y,x+width,y+28),action,tip)

    def _render(self):
        c = self.canvas
        c.delete('all')
        self._actions = []
        self._chart_days = []
        self._hover = None
        self._round(1,1,WIDTH-2,self._height()-2,fill=BG,outline='#82c638',radius=20)
        wallet = self._pick_wallet(self.snapshot) if self.snapshot else None
        balance = money(wallet.balance,wallet.symbol) if wallet else '—'
        low = wallet is not None and wallet.balance < Decimal(str(self.config.low_balance_threshold))
        balance_color = BAD if low else LIME
        self.display = {'balance': balance, 'low_balance': low}
        if self.config.collapsed:
            if 'mini' in self._images:
                c.create_image(self.px(10),self.px(8),image=self._images['mini'],anchor='nw')
            else:
                self._portal(31,32,18)
            self._text(62,21,'DeepSeek',12,FG,True)
            self._text(62,41,'by Vinci',10,FAINT)
            self._line([140,15,140,49])
            self._text(153,19,'演示余额' if self.demo else ('余额 · 待更新' if self.state=='error' else '余额'),10,DIM)
            self._text(153,41,balance,23,balance_color,True,mono=True)
            self._button(293,18,'↻',self.refresh,'立即刷新')
            self._button(329,18,'⌃',self.toggle_collapse,'展开完整面板')
            self._button(365,18,'×',self.quit,'退出',width=23)
            return

        # Subtle portal-world ornament; the data area remains quiet.
        for x,y,r in [(25,76,1),(229,83,1.3),(239,44,.8),(190,104,.8),(366,104,1),(160,69,.8)]:
            c.create_oval(self.px(x-r),self.px(y-r),self.px(x+r),self.px(y+r),fill='#6d9e3b',outline='')
        self._text(20,33,'DeepSeek',20,FG,True)
        self._text(128,33,'Monitor',20,LIME,True)
        self._text(21,56,'by Vinci  /  PORTAL EDITION',10,DIM)
        self._text(21,84,'探索更大的智能宇宙',11,FAINT)
        for x,symbol,fn,tip in [(264,'⚙',self.open_settings,'账号设置'),(295,'↻',self.refresh,'立即刷新'),
                                (326,'−',self.toggle_collapse,'收起为余额条'),(357,'×',self.quit,'退出')]:
            self._button(x,15,symbol,fn,tip,width=27)
        self._round(16,118,368,99,fill='#1b2b23')
        if 'hero' in self._images:
            c.create_image(self.px(319),self.px(106),image=self._images['hero'],anchor='center')
            # Native UI foreground covers the source card under the characters,
            # making the portrait peek over this application's balance card.
            self.canvas.create_polygon(*[self.px(v) for v in [
                249,120,258,121,275,127,291,137,310,147,330,157,
                347,164,389,169,389,174,249,174]], fill='#1b2b23',outline='')
        else:
            self._portal(325,93,34)
        self._text(30,139,'余额',12,DIM)
        self._text(30,182,balance,43 if len(balance)<10 else 31,balance_color,True,mono=True)
        if low:
            self._text(369,203,'余额偏低',10,BAD,anchor='e')
        elif self.demo:
            self._text(369,203,'演示数据',9,WARN,anchor='e')
        elif self.snapshot and self.snapshot.balance_only:
            self._text(369,203,'仅余额模式',9,WARN,anchor='e')

        available = self.snapshot is not None and not self.snapshot.balance_only
        monthly = self.period == 'month'
        usage = (self.snapshot.month if monthly else self.snapshot.today) if available else None
        costs = (self.snapshot.cost_month if monthly else self.snapshot.cost_today) if available else {}
        spend = self._cost_for(costs,wallet)
        total = wallet.total_cost if wallet and available else None
        for x,label,value in [(16,'本月花费' if monthly else '今日花费',spend),(205,'累计消费',total)]:
            self._round(x,226,179,64)
            self._text(x+14,243,label,11,DIM)
            self._text(x+14,271,money(value,self._sym(wallet)),23,FG,True,mono=True)

        self._round(16,299,368,200)
        self._text(30,321,'Token 用量',13,FG,True)
        for x,label,period in [(267,'今日','today'),(320,'本月','month')]:
            selected = self.period == period
            self._round(x,310,49,24,fill='#37522c' if selected else CARD,
                        outline='#557c39' if selected else BORDER,radius=7)
            self._text(x+24.5,322,label,11,LIME if selected else DIM,anchor='center')
            self._action((x,310,x+49,334),lambda v=period:self._set_period(v),'查看'+label+'用量与花费')
        for y,label,field in [(353,'缓存命中','cache_hit'),(382,'缓存未命中','cache_miss'),(411,'输出','response')]:
            value = token_text(getattr(usage,field)) if usage else '—'
            self.display[field] = value
            self._text(31,y,label,12,DIM)
            self._text(367,y,value,15,FG,True,anchor='e',mono=True)
            self._line([31,y+14,368,y+14],color='#2b3c32')
        rate = usage.cache_hit_rate if usage else None
        self.display['rate'] = rate
        self._text(31,447,'缓存命中率',11,DIM)
        self._round(122,443,168,7,fill='#33473a',outline='',radius=3)
        if rate is not None and rate > 0:
            self._round(122,443,168*min(1,max(0,rate)),7,fill=LIME,outline='',radius=3)
        self._text(367,447,percent(rate),13,LIME if rate is not None else DIM,True,anchor='e',mono=True)
        total_tokens = token_text(usage.total_tokens) if usage else '—'
        requests = human_count(usage.requests) if usage else '—'
        self._text(31,478,f'合计 {total_tokens} tokens',10,FAINT)
        self._text(367,478,f'{requests} 次请求',10,FAINT,anchor='e')
        if usage and usage.prompt:
            self._action((22,465,250,493),lambda:None,f'合计含 {human_tokens(usage.prompt)} 未分类输入 Token')

        self._round(16,508,368,105)
        self._text(30,528,'近 30 天',12,FG,True)
        for x,label,mode in [(277,'花费','cost'),(328,'Token','tokens')]:
            self._text(x,528,label,11,LIME if self.chart_mode==mode else FAINT,anchor='center')
            self._action((x-24,516,x+24,540),lambda v=mode:self._set_chart_mode(v),'切换趋势：'+label)
        self._draw_chart(self.snapshot if available else None)
        self._draw_status()

    def _portal(self, x, y, r):
        # Native vector fallback if the optional bitmap is missing.
        for i,color in enumerate(['#223d1d','#416e23','#71ac30','#b6f65b','#4b8d2a']):
            rr = r-i*3
            self.canvas.create_oval(self.px(x-rr),self.px(y-rr),self.px(x+rr),self.px(y+rr),outline=color,width=self.px(3))

    def _draw_status(self):
        self.canvas.delete('status')
        if self.config.collapsed:
            return
        color = {'ok':GREEN,'demo':WARN,'busy':WARN,'error':BAD}.get(self.state,FAINT)
        self.canvas.create_oval(self.px(23),self.px(628),self.px(29),self.px(634),fill=color,outline='',tags='status')
        if self.state == 'busy':
            note = '正在同步…'
        elif self.state == 'error':
            note = '同步失败 · 点击查看详情'
        elif self.demo:
            note = '演示数据 · 未连接账号'
        elif self.snapshot:
            label = '部分数据' if self.snapshot.warnings else ('仅余额' if self.snapshot.balance_only else '已同步')
            note = label+' · '+self.snapshot.fetched_at.strftime('%H:%M')
        else:
            note = '等待同步…'
        self._text(37,631,note,10,color if self.state=='error' else DIM,tags='status')
        self._text(377,631,'PORTAL / A',9,FAINT,anchor='e',tags='status')

    def _draw_chart(self, snap):
        self._chart_days = []
        if not snap or not snap.daily:
            message = '平台登录后显示用量趋势' if snap is None else '暂无趋势数据'
            self._text(200,570,message,11,FAINT,anchor='center')
            return
        wallet = self._pick_wallet(snap)
        days = sorted(snap.daily,key=lambda d:d.date)[-30:]
        vals = [self._cost_for(d.cost,wallet) if self.chart_mode=='cost' else d.usage.total_tokens for d in days]
        valid = [float(v) for v in vals if v is not None]
        if not valid:
            self._text(200,570,'暂无该币种的花费数据',11,FAINT,anchor='center')
            return
        peak = max(max(valid),.001)
        for y in [550,572,591]:
            self._line([31,y,368,y],color='#2b3c32',dash=(2,5))
        points = []
        segments = []
        for i,(day,value) in enumerate(zip(days,vals)):
            x = 31 + (337*i/max(1,len(days)-1))
            if value is None:
                if points:segments.append(points)
                points = []
                continue
            y = 591-38*float(value)/peak
            points.extend([x,y])
            self._chart_days.append((x,day,value))
        if points:segments.append(points)
        for coords in segments:
            if len(coords)>2:
                polygon = [coords[0],592]+coords+[coords[-2],592]
                self.canvas.create_polygon(*[self.px(v) for v in polygon],fill='#243923',outline='')
                self._line(coords,color=LIME,width=1.6,joinstyle='round')
            else:
                x,y = coords
                self.canvas.create_oval(self.px(x-2),self.px(y-2),self.px(x+2),self.px(y+2),fill=LIME,outline='')
        self._text(31,602,days[0].date[5:],9,FAINT)
        self._text(368,602,days[-1].date[5:],9,FAINT,anchor='e')

    def _set_period(self, period):
        self.period = period
        self._render()

    def _set_chart_mode(self, mode):
        self.chart_mode = mode
        self._render()

    def _cost_for(self, table, wallet):
        # Never label a fallback USD amount as CNY (or vice versa).
        return table.get(wallet.currency) if table and wallet else None

    def _bind_events(self):
        self.canvas.bind('<ButtonPress-1>',self._press)
        self.canvas.bind('<B1-Motion>',self._drag_move)
        self.canvas.bind('<ButtonRelease-1>',self._release)
        self.canvas.bind('<Motion>',self._motion)
        self.canvas.bind('<Leave>',lambda e:self._clear_tip())
        self.root.bind('<Button-3>',self._popup_menu)
        self.root.bind('<Escape>',lambda e:self.quit())
        self.root.bind('<F5>',lambda e:self.refresh())
        self.root.bind('<Control-comma>',lambda e:self.open_settings())
        self.root.bind('<Control-space>',lambda e:self.toggle_collapse())

    def _hit(self, x, y):
        for rect,fn,tip in reversed(self._actions):
            if rect[0]<=x<=rect[2] and rect[1]<=y<=rect[3]:
                return fn,tip
        return None

    def _press(self, e):
        x,y = e.x/self.scale,e.y/self.scale
        action = self._hit(x,y)
        if action:
            action[0]()
            return
        if not self.config.collapsed and y>=618:
            self._show_details()
        elif y<118 or self.config.collapsed:
            self._drag = (e.x_root-self._pos[0],e.y_root-self._pos[1])

    def _drag_move(self, e):
        if self._drag is not None:
            self._pos = [e.x_root-self._drag[0],e.y_root-self._drag[1]]
            self.root.geometry(f'{self._pos[0]:+d}{self._pos[1]:+d}')

    def _release(self, e):
        if self._drag is not None:
            self._drag = None
            self._refit()
            self.config.window_x,self.config.window_y = self._pos
            self._save()

    def _motion(self, e):
        x,y = e.x/self.scale,e.y/self.scale
        action = self._hit(x,y)
        self.canvas.configure(cursor='hand2' if action or y>=618 else ('fleur' if y<118 else ''))
        if action:
            self._tip(action[1],x,y)
        elif not self.config.collapsed and 543<y<607 and self._chart_days:
            _,day,value = min(self._chart_days,key=lambda row:abs(row[0]-x))
            wallet = self._pick_wallet(self.snapshot)
            amount = money(value,self._sym(wallet),3) if self.chart_mode=='cost' else human_tokens(value)+' tokens'
            self._tip(day.date+'   '+amount,x,y)
        else:
            self._clear_tip()

    def _tip(self, text, x, y):
        if self._hover == text:
            return
        self.canvas.delete('tip')
        self._hover = text
        # Tooltips stay within the widget, including the collapsed strip.
        yy = min(self._height()-16,y+24)
        item = self._text(200,yy,text,10,FG,anchor='center',tags='tip')
        box = self.canvas.bbox(item)
        bg = self.canvas.create_rectangle(box[0]-5,box[1]-4,box[2]+5,box[3]+4,
                                          fill='#30442f',outline='#69874b',tags='tip')
        self.canvas.tag_lower(bg,item)

    def _clear_tip(self):
        self._hover = None
        self.canvas.delete('tip')

    def toggle_collapse(self):
        self.config.collapsed = not self.config.collapsed
        self._save()
        self._refit()
        self._render()

    def _toggle_topmost(self):
        self.config.topmost = bool(self._topmost_var.get())
        self.root.attributes('-topmost',self.config.topmost)
        self._save()

    def _on_interval(self):
        self.config.refresh_seconds = int(self._interval_var.get())
        self._save()
        if not self.demo:self._schedule_next()

    def _set_status(self, state):
        self.state = state
        if self.config.collapsed:self._render()
        else:self._draw_status()

    def _set_footer(self, text):
        self._footer_text = text
        self._draw_status()

    def refresh(self, force_token=False):
        if self.demo:
            self.snapshot = demo_snapshot()
            self._render()
            return
        super().refresh(force_token)

    def _worker(self, force_token=False):
        try:
            if force_token or not self.token:
                token,source = (None,None) if force_token else (self.config.manual_token,'手动配置')
                if not token:
                    token,source = platform_auth.extract_user_token()
                if not token and self.config.manual_token:
                    token,source = self.config.manual_token,'手动配置'
                if not token and not self.config.api_key:
                    self.queue.put(('auth_fail',source))
                    return
                self.token,self.token_source = token,source or 'API Key'
            snap = DeepSeekClient(user_token=self.token,api_key=self.config.api_key).snapshot()
            self.queue.put(('data',snap))
        except AuthExpired:
            self.token = None
            self.queue.put(('auth_fail','登录态已失效，请重新登录或在设置中填写凭据。'))
        except NetworkError:
            self.queue.put(('error','网络连接失败，请检查网络后重试。'))
        except ApiError:
            self.queue.put(('error','接口暂时不可用，请稍后重试或重新登录。'))
        except Exception as exc:
            # Exception bodies can contain request details; show only the type.
            self.queue.put(('error','同步失败：'+type(exc).__name__))

    def _pump(self):
        try:
            while True:
                kind,payload = self.queue.get_nowait()
                self._busy = False  # Only release after a worker actually completes.
                if kind=='data':
                    self._apply(payload)
                else:
                    self.last_error = payload
                    self._footer_text = '登录态不可用，请打开设置。' if kind=='auth_fail' else str(payload)
                    self.state = 'error'
                    self._render()
                    if kind=='auth_fail' and not self._settings_shown and not self.config.manual_token and not self.config.api_key:
                        self._settings_shown = True
                        self.root.after(400,self.open_settings)
        except queue.Empty:
            pass
        if not self._closed:self.root.after(200,self._pump)

    def _apply(self, snap):
        self.snapshot = snap
        self.last_error = None
        self.state = 'ok'
        self._footer_text = ('仅余额模式：填写平台登录态后可显示用量。' if snap.balance_only else '已同步至 '+snap.date_string())
        if snap.warnings:self._footer_text += '\n部分数据未能更新，请稍后重试。'
        self._render()

    def _show_details(self):
        from tkinter import messagebox
        messagebox.showinfo('同步状态',self._footer_text,parent=self.root)

    def open_settings(self):
        if self.demo:
            self._show_demo_settings()
            return
        super().open_settings()
        # Keep existing workflow; mask stored credentials by default.
        def mask(widget):
            for child in widget.winfo_children():
                if isinstance(child,tk.Entry):child.configure(show='•')
                mask(child)
        mask(self._settings_win)
        self._settings_win.update_idletasks()
        l,t,r,b = self._workarea()
        w,h = self._settings_win.winfo_reqwidth(),self._settings_win.winfo_reqheight()
        x,y = self._settings_win.winfo_x(),self._settings_win.winfo_y()
        self._settings_win.geometry(f'{max(l,min(x,r-w)):+d}{max(t,min(y,b-h)):+d}')

    def _show_demo_settings(self):
        from tkinter import messagebox
        messagebox.showinfo('离线演示','当前为演示模式，不读取或保存账号凭据。\n请双击正式版连接账号。',parent=self.root)

    def quit(self):
        self._closed = True
        self.config.window_x,self.config.window_y = self._pos
        self._save()
        for callback in self.root.tk.call('after','info'):
            self.root.after_cancel(callback)
        self.root.destroy()
