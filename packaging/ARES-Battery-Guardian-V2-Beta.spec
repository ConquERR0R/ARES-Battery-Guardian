# PyInstaller spec for the V2 Beta installer build.
#
# Build (packaging\build_installer.bat does this):
#   pyinstaller --noconfirm --clean --distpath dist_v2beta --workpath build_v2beta ^
#               packaging\ARES-Battery-Guardian-V2-Beta.spec
# Output: dist_v2beta\ARES-Battery-Guardian\ARES-Battery-Guardian.exe (one-folder build), which
# packaging\ARES-Battery-Guardian-V2-Beta.iss turns into ARES-Battery-Guardian-V2-Beta-Setup.exe.
#
# Same Analysis/excludes as the original ARES-Battery-Guardian.spec in the project root (which is
# left unchanged), plus: program icon, Windows version resource and build_info.json for the
# About page. User data is never bundled: it lives in %LOCALAPPDATA%\ARES Battery Guardian.
import datetime
import json
import os
import platform
import sys

import PyInstaller

ROOT = os.path.dirname(SPECPATH)               # SPECPATH = this file's folder (packaging\)
ICON = os.path.join(ROOT, "assets", "ares.ico")
VERSION_FILE = os.path.join(SPECPATH, "version_info.txt")

sys.path.insert(0, ROOT)
from app import APP_DISPLAY_VERSION  # noqa: E402

# Facts shown on the About page (read by app/build_info.py from the bundle root).
_work = globals().get("workpath") or os.path.join(ROOT, "build_v2beta")
os.makedirs(_work, exist_ok=True)
BUILD_INFO = os.path.join(_work, "build_info.json")
with open(BUILD_INFO, "w", encoding="utf-8") as f:
    json.dump({
        "version": APP_DISPLAY_VERSION,
        "build_date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "python": platform.python_version(),
        "pyinstaller": PyInstaller.__version__,
    }, f, indent=2)

block_cipher = None

# QtNetwork is kept: QLocalServer/QLocalSocket provide the single-instance channel.
EXCLUDES = [
    "tkinter", "unittest", "pydoc", "test", "lib2to3",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets", "PySide6.QtCharts", "PySide6.Qt3DCore", "PySide6.QtMultimedia",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtPdf", "PySide6.QtSvg", "PySide6.QtOpenGL",
    "PySide6.QtDBus", "PySide6.QtXml", "PySide6.QtConcurrent", "PySide6.QtPrintSupport",
]

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[(BUILD_INFO, "."), (ICON, "assets")],
    hiddenimports=[],
    excludes=EXCLUDES,
    noarchive=False,
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ARES-Battery-Guardian",
    icon=ICON,
    version=VERSION_FILE,
    console=False,          # windowed app, no console
    uac_admin=False,        # elevation is requested only when needed (or via the elevated startup task)
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="ARES-Battery-Guardian")
