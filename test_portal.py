"""Offline regression checks; never reads the user's account or saved config."""
import ctypes
from decimal import Decimal
from pathlib import Path
import time
from unittest.mock import patch

from PIL import ImageGrab
from dsmon.portal import PortalApp, demo_snapshot, WIDTH, HEIGHT
from dsmon.config import Config
from dsmon.api import Snapshot, Wallet, Usage


def pump(app):
    for _ in range(5):
        app.root.update()
        time.sleep(.03)


def capture(app, name):
    pump(app)
    hwnd = ctypes.windll.user32.GetParent(app.root.winfo_id())
    image = ImageGrab.grab(window=hwnd)
    dest = Path(__file__).parent.parent/'qa'
    dest.mkdir(exist_ok=True)
    image.save(dest/name)


def run():
    app = PortalApp(demo=True, scale=1.25)
    try:
        assert app._images.keys() >= {'hero','mini'}, 'Theme artwork missing'
        assert app.display['balance']=='¥128.64'
        assert app.display['rate']==.8
        capture(app,'portal-expanded.png')
        app.toggle_collapse()
        assert app.display['balance']=='¥128.64'
        assert app._height()==64
        capture(app,'portal-collapsed.png')
        app.toggle_collapse()
        app._set_period('month')
        assert app.display['cache_hit']!='128K'
        app._set_chart_mode('tokens')
        assert len(app._chart_days)==30
        app._set_period('today')
        low=demo_snapshot()
        low.wallets[0].balance=Decimal('0.01')
        app._apply(low)
        assert app.display['low_balance']
        only=Snapshot()
        only.wallets=[Wallet('CNY',Decimal('82.15'))]
        only.balance_only=True
        app._apply(only)
        assert app.display['balance']=='¥82.15'
        assert app.display['cache_hit']=='—' and app.display['rate'] is None
        assert not app._chart_days
        capture(app,'portal-balance-only.png')
        assert app._cost_for({'USD':Decimal('4.21')},only.primary_wallet) is None
        empty=demo_snapshot()
        empty.today=Usage()
        app._apply(empty)
        assert app.display['cache_hit']=='0' and app.display['rate'] is None
        app._busy=True
        app._pump()
        assert app._busy, 'Empty queue must not unlock a running refresh'
        app.queue.put(('error','模拟网络中断'))
        app._pump()
        assert not app._busy and app.state=='error'
        assert app.display['balance']=='¥128.64', 'Keep last successful data on network error'
        app.toggle_collapse()
        assert '余额 · 待更新' in [app.canvas.itemcget(i,'text') for i in app.canvas.find_all()
                                  if app.canvas.type(i)=='text'] or app.demo
        app.toggle_collapse()
        app._pos=[99999,99999]
        w,h=app._refit()
        left,top,right,bottom=app._workarea()
        assert app._pos[0]+w<=right and app._pos[1]+h<=bottom
        print('PASS: artwork, expanded/collapsed, monthly, chart, low balance, balance-only, currencies, zero, worker locking, error, screen bounds')
    finally:
        app.quit()

    for scale in [1.0,1.5,2.0]:
        app=PortalApp(demo=True,scale=scale)
        pump(app)
        for item in app.canvas.find_all():
            if app.canvas.type(item)=='text':
                box=app.canvas.bbox(item)
                assert box[0]>=0 and box[2]<=app.px(WIDTH), (scale,app.canvas.itemcget(item,'text'),box)
        app.quit()
    print('PASS: 100%, 150%, 200% display scaling (with fit-to-screen)')

    config=Config({'api_key':'offline-test-key','alpha':1.0})
    app=PortalApp(config=config,start_network=False)
    try:
        with (patch('dsmon.portal.platform_auth.extract_user_token',return_value=(None,'test')),
              patch('dsmon.portal.DeepSeekClient') as client):
            client.return_value.snapshot.return_value=only
            app._worker()
            assert app.queue.get_nowait()[0]=='data'
            assert client.call_args.kwargs['api_key']=='offline-test-key'
        app.open_settings()
        pump(app)
        def entries(w):
            import tkinter as tk
            found=[]
            for child in w.winfo_children():
                if isinstance(child,tk.Entry):found.append(child)
                found.extend(entries(child))
            return found
        assert all(e.cget('show') for e in entries(app._settings_win))
        app._settings_win.destroy()
        print('PASS: API-key-only worker path and masked settings')
    finally:
        app.quit()


if __name__=='__main__':
    run()
