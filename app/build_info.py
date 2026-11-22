"""Version and build information for the About page and the log (no Qt GUI import)."""
from __future__ import annotations

import json
import platform
import sys
from pathlib import Path

from app import APP_CHANNEL, APP_DISPLAY_VERSION, APP_NAME, APP_VERSION

BUILD_INFO_FILE = "build_info.json"     # written by packaging/ARES-Battery-Guardian-V2-Beta.spec


def packaged_build() -> dict:
    """Build facts recorded by the PyInstaller spec; {} when running from source."""
    if not getattr(sys, "frozen", False):
        return {}
    base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    try:
        return json.loads((base / BUILD_INFO_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def version_info() -> dict:
    """Everything the About page shows. Never raises."""
    info = {
        "name": APP_NAME,
        "version": APP_VERSION,
        "channel": APP_CHANNEL,
        "display_version": APP_DISPLAY_VERSION,
        "python": platform.python_version(),
        "packaged": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
    }
    try:
        import PySide6
        from PySide6.QtCore import qVersion

        info["pyside6"] = PySide6.__version__
        info["qt"] = qVersion()
    except Exception:                    # pragma: no cover - PySide6 is always installed
        info["pyside6"] = info["qt"] = "unknown"
    b = packaged_build()
    info["build_date"] = b.get("build_date", "running from source")
    info["build_python"] = b.get("python", "")
    info["build_pyinstaller"] = b.get("pyinstaller", "")
    return info
