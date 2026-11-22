"""Shared helpers for packaging/verify_package.py and packaging/test_installer.py (Windows only).

Safety rules these helpers enforce for every test:
  * The packaged ARES is always started with ARES_DATA_DIR pointing at a NEW temporary folder,
    so the real %LOCALAPPDATA%\\ARES Battery Guardian data is never opened by a test.
  * The real HKCU Run-key value and the real Smart Wake-Up task are saved before a test that could
    change them and restored exactly afterwards; tests that would touch a task we can't restore
    exactly are skipped and reported as SKIP.
  * The real data folder is only READ (file hashes; the database is read from a temporary copy).
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import APP_DISPLAY_VERSION, APP_ID, APP_NAME  # noqa: E402

IS_WINDOWS = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WAKE_TASK = "ARES Battery Guardian Wake Check"     # app/core/smart_wake.py TASK_NAME
STARTUP_TASK = APP_NAME                            # app/core/startup_manager.py TASK_NAME
EXE_NAME = "ARES-Battery-Guardian.exe"


# ---- reporting ----------------------------------------------------------------------------
@dataclass
class Report:
    title: str
    log_path: Path | None = None
    rows: list[tuple[str, str, str]] = field(default_factory=list)

    def _emit(self, line: str) -> None:
        print(line, flush=True)
        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def start(self) -> None:
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self.log_path.write_text("", encoding="utf-8")
        self._emit(f"== {self.title} ==  {datetime.now():%Y-%m-%d %H:%M:%S}")
        self._emit(f"Python {sys.version.split()[0]} on {sys.platform}; ARES source version {APP_DISPLAY_VERSION}")

    def note(self, msg: str) -> None:
        self._emit(f"   . {msg}")

    def add(self, status: str, name: str, detail: str = "") -> None:
        self.rows.append((status, name, detail))
        self._emit(f"[{status}] {name}" + (f" -- {detail}" if detail else ""))

    def check(self, cond: bool, name: str, detail: str = "") -> bool:
        self.add("PASS" if cond else "FAIL", name, detail)
        return cond

    def finish(self) -> int:
        counts = {s: sum(1 for r in self.rows if r[0] == s) for s in ("PASS", "FAIL", "SKIP")}
        self._emit(f"== Summary: {len(self.rows)} checks, {counts['PASS']} passed, "
                   f"{counts['FAIL']} failed, {counts['SKIP']} skipped ==")
        return 1 if counts["FAIL"] else 0


# ---- processes ----------------------------------------------------------------------------
def test_env(data_dir: Path) -> dict:
    env = dict(os.environ)
    env["ARES_DATA_DIR"] = str(data_dir)
    return env


def new_data_dir(tag: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"ares-test-{tag}-"))


def run(cmd: list[str], env: dict | None = None, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, env=env, timeout=timeout, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW)


def start(cmd: list[str], env: dict) -> subprocess.Popen:
    return subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def log_text(data_dir: Path) -> str:
    p = data_dir / "logs" / "ares.log"
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def wait_for_log(data_dir: Path, needle: str, timeout: float = 60, count: int = 1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log_text(data_dir).count(needle) >= count:
            return True
        time.sleep(0.5)
    return False


def has_traceback(data_dir: Path) -> bool:
    return "Traceback (most recent call last)" in log_text(data_dir)


def exit_instance(exe: Path, data_dir: Path) -> int:
    """Ask a test instance (that data folder only) to exit; never affects the real ARES."""
    try:
        return run([str(exe), "--exit"], env=test_env(data_dir), timeout=60).returncode
    except subprocess.TimeoutExpired:
        return -1


def remove_dir(p: Path) -> None:
    shutil.rmtree(p, ignore_errors=True)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def processes_named(image: str) -> list[str]:
    res = run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"], timeout=30)
    return [ln for ln in res.stdout.splitlines() if image.lower() in ln.lower()]


# ---- test databases (temporary folders only) ----------------------------------------------
def seed_test_db(data_dir: Path, settings: dict, events: int = 0) -> None:
    """Create/seed the database in a TEST data folder with the same schema code ARES uses."""
    from app.database.database import Database

    (data_dir / "data").mkdir(parents=True, exist_ok=True)
    with Database(data_dir / "data" / "battery_guardian.db") as db:
        if settings:
            db.set_settings(settings)
        for i in range(events):
            db.log_event("info", f"verification event {i}")


def read_db(db_file: Path) -> dict:
    """Settings and row counts, read from a temporary COPY (the original is never opened)."""
    out: dict = {"exists": db_file.exists()}
    if not db_file.exists():
        return out
    tmp = Path(tempfile.mkdtemp(prefix="ares-dbcopy-"))
    try:
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(db_file) + suffix)
            if src.exists():
                shutil.copy2(src, tmp / (db_file.name + suffix))
        con = sqlite3.connect(str(tmp / db_file.name))
        try:
            out["settings"] = {k: v for k, v in con.execute("SELECT key, value FROM settings")}
            tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            out["counts"] = {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
        finally:
            con.close()
    except sqlite3.Error as exc:
        out["error"] = str(exc)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def setting(data_dir: Path, key: str):
    db = read_db(data_dir / "data" / "battery_guardian.db")
    raw = (db.get("settings") or {}).get(key)
    return None if raw is None else json.loads(raw)


# ---- real data folder snapshot (read-only) -------------------------------------------------
def real_data_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / APP_NAME


def snapshot(folder: Path) -> dict:
    """sha256 + size of every file under `folder`/data (logs and cache are not user data)."""
    files = {}
    base = folder / "data"
    if base.exists():
        for p in sorted(base.rglob("*")):
            if p.is_file():
                try:
                    files[str(p.relative_to(folder))] = hashlib.sha256(p.read_bytes()).hexdigest()
                except OSError as exc:          # a file locked by a running ARES
                    files[str(p.relative_to(folder))] = f"unreadable: {exc}"
    db = read_db(base / "battery_guardian.db")
    return {"files": files, "settings": db.get("settings"), "counts": db.get("counts"),
            "folder_exists": folder.exists()}


def compare_snapshots(before: dict, after: dict) -> tuple[bool, str]:
    """(ok, detail). Identical files = ok. If files differ (a real ARES was running and kept
    writing), ok only when every setting is unchanged and no table lost rows."""
    if before["files"] == after["files"]:
        return True, f"{len(before['files'])} data files byte-identical"
    missing = sorted(set(before["files"]) - set(after["files"]))
    if missing:
        return False, f"files missing afterwards: {missing}"
    if before.get("settings") != after.get("settings"):
        changed = sorted(k for k in set(before["settings"] or {}) | set(after["settings"] or {})
                         if (before["settings"] or {}).get(k) != (after["settings"] or {}).get(k))
        return False, f"settings changed: {changed}"
    lost = {t: (n, (after.get("counts") or {}).get(t, 0)) for t, n in (before.get("counts") or {}).items()
            if (after.get("counts") or {}).get(t, 0) < n}
    if lost:
        return False, f"rows lost: {lost}"
    return True, ("database files changed only by new rows (a running ARES is still recording); "
                  "all settings identical, no rows lost")


# ---- registry: HKCU Run value -------------------------------------------------------------
def read_run_value():
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, APP_ID)       # (data, type)
    except OSError:
        return None


def restore_run_value(saved) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if saved is None:
            try:
                winreg.DeleteValue(k, APP_ID)
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(k, APP_ID, 0, saved[1], saved[0])


def set_run_value(command: str) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, APP_ID, 0, winreg.REG_SZ, command)


# ---- Task Scheduler -----------------------------------------------------------------------
def task_exists(name: str) -> bool:
    return run(["schtasks", "/Query", "/TN", name], timeout=30).returncode == 0


def task_xml(name: str) -> str | None:
    res = run(["schtasks", "/Query", "/TN", name, "/XML"], timeout=30)
    return res.stdout if res.returncode == 0 else None


def create_dummy_wake_task(command_exe: str) -> tuple[bool, str]:
    """Register a wake task like ARES does, but due in one year (never runs during the test).
    Only call this when no real wake task exists; always delete_task() afterwards."""
    from app.core.smart_wake import build_task_xml

    user = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")
    xml = build_task_xml(command_exe, ["--wake-check"], datetime.now() + timedelta(days=365), user)
    tmp = Path(tempfile.mkdtemp(prefix="ares-task-")) / "task.xml"
    tmp.write_text(xml, encoding="utf-16")
    try:
        res = run(["schtasks", "/Create", "/TN", WAKE_TASK, "/XML", str(tmp), "/F"], timeout=60)
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)
    return res.returncode == 0, (res.stdout or res.stderr).strip()


def delete_task(name: str) -> None:
    if task_exists(name):
        run(["schtasks", "/Delete", "/TN", name, "/F"], timeout=60)


# ---- Windows file version resource ----------------------------------------------------------
def file_version_strings(path: Path) -> dict:
    ver = ctypes.windll.version
    size = ver.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return {}
    buf = ctypes.create_string_buffer(size)
    if not ver.GetFileVersionInfoW(str(path), 0, size, buf):
        return {}
    out = {}
    for key in ("FileVersion", "ProductVersion", "ProductName", "FileDescription", "OriginalFilename"):
        ptr, n = ctypes.c_void_p(), ctypes.c_uint()
        if ver.VerQueryValueW(buf, f"\\StringFileInfo\\040904B0\\{key}", ctypes.byref(ptr), ctypes.byref(n)) and n.value:
            out[key] = ctypes.wstring_at(ptr.value, n.value).rstrip("\x00")
    return out


def shell_folder(csidl: int) -> Path:
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf)
    return Path(buf.value)


CSIDL_PROGRAMS = 0x02            # %APPDATA%\Microsoft\Windows\Start Menu\Programs
CSIDL_DESKTOPDIRECTORY = 0x10    # the user's Desktop (OneDrive\Desktop when redirected)


def shortcut_target(lnk: Path) -> str | None:
    """Target of a .lnk file (via the Windows Script Host COM object; test-only helper)."""
    ps = f"(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}').TargetPath"
    res = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=60)
    return res.stdout.strip() if res.returncode == 0 else None
