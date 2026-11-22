# PyInstaller spec - build with:  pyinstaller --noconfirm ARES-Battery-Guardian.spec
# Produces dist\ARES-Battery-Guardian\ARES-Battery-Guardian.exe (one-folder build: starts faster
# and uses less memory than --onefile, which unpacks itself to %TEMP% on every launch).
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
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[],
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
    console=False,          # windowed app, no console
    uac_admin=False,        # elevation is requested only when needed (or via the elevated startup task)
    version=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="ARES-Battery-Guardian")
