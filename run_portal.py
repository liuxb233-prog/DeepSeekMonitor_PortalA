"""Standalone entry point for the Portal A edition."""
import sys
from pathlib import Path


def main():
    import ctypes
    from ctypes import wintypes
    mutex = None
    if '--demo' not in sys.argv:
        kernel = ctypes.windll.kernel32
        kernel.CreateMutexW.argtypes = (ctypes.c_void_p,wintypes.BOOL,wintypes.LPCWSTR)
        kernel.CreateMutexW.restype = wintypes.HANDLE
        mutex = kernel.CreateMutexW(None,False,'Local\\DeepSeekMonitorPortalA_Vinci')
        if kernel.GetLastError()==183:
            from tkinter import messagebox
            messagebox.showinfo('DeepSeek Monitor','A 风格版已经在运行，请查看桌面上的悬浮窗。')
            return
    from dsmon.portal import PortalApp
    app = PortalApp(demo='--demo' in sys.argv or '--smoke-test' in sys.argv)
    if '--smoke-test' in sys.argv:
        # Offline packaged-runtime validation. No account/config access.
        def finish():
            output = Path(sys.argv[sys.argv.index('--smoke-test')+1])
            output.write_text('OK: window, artwork, rendering, collapse\n',encoding='utf-8')
            app.toggle_collapse()
            app.root.update_idletasks()
            app.quit()
        app.root.after(700,finish)
    app.run()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import traceback
        log = Path.home()/'.deepseek-monitor'/'portal-error.log'
        log.parent.mkdir(exist_ok=True)
        log.write_text(traceback.format_exc(),encoding='utf-8')
        raise
