"""Build from this copied source tree with Python 3.14 on Windows."""
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parent
subprocess.run([
    sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile', '--windowed',
    '--name', 'DeepSeekMonitor_PortalA', '--distpath', str(root/'dist'),
    '--workpath', str(root/'build'), '--specpath', str(root),
    '--add-data', str(root/'assets')+';assets', str(root/'run_portal.py'),
], check=True, cwd=root)
