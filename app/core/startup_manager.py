"""Start ARES automatically when the user signs in to Windows.

Two registration methods; ARES only ever keeps ONE of them (never both, so it is never
started twice and no duplicate entries pile up):

* Normal (no admin needed): the current user's registry Run key
  HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run, value "ARESBatteryGuardian".
  ARES then starts with normal rights: monitoring, protection screens, alarms and
  notifications all work; writing the Dell BIOS charge thresholds does not (needs admin).
* Optional upgrade (when ARES runs as administrator, e.g. via install_startup.bat): a Task
  Scheduler logon task with "Run with highest privileges", so ARES starts elevated without a
  UAC prompt and can also re-apply the BIOS 90% stop at sign-in.

Only ARES' own entry (value / task named after ARES) is ever created or removed; other
startup programs are never read or changed.

The registered command is `<program> --autostart`. Whether the dashboard opens at sign-in is
decided at launch from the "start_minimized" setting, so changing that option never needs
re-registration.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from app import APP_ID, APP_NAME
from app.utils import system_info as si

log = logging.getLogger(__name__)

TASK_NAME = APP_NAME
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_ARG = "--autostart"


def launch_command() -> tuple[str, list[str]]:
    """(executable, args) that Windows runs at sign-in.

    Packaged (PyInstaller): the EXE itself (sys.executable). From source: pythonw.exe (no console
    window) when it exists next to the interpreter, otherwise python.exe, plus main.py.
    """
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve()), [AUTOSTART_ARG]
    exe = sys.executable
    pyw = Path(exe).with_name("pythonw.exe")
    if pyw.exists():
        exe = str(pyw)
    main = Path(__file__).resolve().parents[2] / "main.py"
    return exe, [str(main), AUTOSTART_ARG]


def split_command(cmd: str | None) -> list[str]:
    """Split a Windows command line (as stored in the Run key) into parts; handles quoted paths
    with spaces. Never raises."""
    if not cmd:
        return []
    out, cur, quoted = [], "", False
    for ch in cmd.strip():
        if ch == '"':
            quoted = not quoted
        elif ch.isspace() and not quoted:
            if cur:
                out.append(cur)
                cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


def missing_targets(cmd: str | None) -> list[str]:
    """Files a registered command needs that don't exist (the program, and main.py when run from
    source). An empty list means the command can start."""
    parts = split_command(cmd)
    if not parts:
        return []
    need = [parts[0]] + [p for p in parts[1:] if p.lower().endswith(".py")]
    return [p for p in need if not Path(p).is_file()]


def _command_line() -> str:
    exe, args = launch_command()
    return subprocess.list2cmdline([exe, *args])


# ---- Task Scheduler ---------------------------------------------------------
def task_exists() -> bool:
    if not si.IS_WINDOWS:
        return False
    return si.run_hidden(["schtasks", "/Query", "/TN", TASK_NAME], timeout=15).ok


def create_task() -> tuple[bool, str]:
    if not si.IS_WINDOWS:
        return False, "Not running on Windows."
    if not si.is_admin():
        return False, "Creating an elevated startup task requires administrator rights."
    exe, args = launch_command()
    user = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")
    # Build the task via XML so we can also allow running on battery (schtasks /Create
    # alone defaults to "start only on AC power", which would break monitoring).
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>{APP_NAME} battery monitor</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{_xml(user)}</UserId><Delay>PT20S</Delay></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{_xml(user)}</UserId><LogonType>InteractiveToken</LogonType>
    <RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author"><Exec><Command>{_xml(exe)}</Command>
    <Arguments>{_xml(subprocess.list2cmdline(args))}</Arguments></Exec></Actions>
</Task>"""
    tmp = Path(os.environ.get("TEMP", ".")) / f"{APP_ID}_task.xml"
    tmp.write_text(xml, encoding="utf-16")
    try:
        res = si.run_hidden(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(tmp), "/F"], timeout=30)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return res.ok, (res.stdout or res.stderr).strip()


def delete_task() -> tuple[bool, str]:
    if not task_exists():
        return True, "No startup task."
    res = si.run_hidden(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], timeout=30)
    return res.ok, (res.stdout or res.stderr).strip()


def _xml(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ---- HKCU Run fallback ------------------------------------------------------
def run_key_exists() -> bool:
    if not si.IS_WINDOWS:
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_ID)
            return True
    except OSError:
        return False


def set_run_key(enabled: bool) -> tuple[bool, str]:
    if not si.IS_WINDOWS:
        return False, "Not running on Windows."
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, APP_ID, 0, winreg.REG_SZ, _command_line())
            else:
                try:
                    winreg.DeleteValue(k, APP_ID)
                except FileNotFoundError:
                    pass
        return True, "OK"
    except OSError as exc:
        return False, str(exc)


def registered_task_command() -> str | None:
    """Command line stored in the existing startup task, or None if it can't be read."""
    if not si.IS_WINDOWS:
        return None
    res = si.run_hidden(["schtasks", "/Query", "/TN", TASK_NAME, "/XML"], timeout=15)
    if not res.ok:
        return None
    import re
    from html import unescape

    cmd = re.search(r"<Command>(.*?)</Command>", res.stdout, re.S)
    args = re.search(r"<Arguments>(.*?)</Arguments>", res.stdout, re.S)
    if not cmd:
        return None
    return subprocess.list2cmdline([unescape(cmd.group(1).strip())]) + (
        " " + unescape(args.group(1).strip()) if args else "")


def run_key_value() -> str | None:
    if not si.IS_WINDOWS:
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return str(winreg.QueryValueEx(k, APP_ID)[0])
    except OSError:
        return None


def _same_command(a: str | None, b: str | None) -> bool:
    norm = lambda x: [p.lower() for p in split_command(x)]  # noqa: E731
    return bool(a) and norm(a) == norm(b)


def registered_command() -> str | None:
    """The command ARES is registered with (task first, then Run key), or None."""
    if task_exists():
        return registered_task_command()
    return run_key_value()


def ensure_startup(wanted: bool) -> tuple[bool, str]:
    """Make the Windows startup entry match `wanted`, pointing at the CURRENT program.

    Called automatically every time ARES starts, so the entry is created on first run and
    repaired if ARES was moved (e.g. from `python main.py` to the packaged EXE).
    Returns (changed, message). Never raises.
      * Elevated + wanted : elevated logon task (no UAC prompt at logon, BIOS charge control works);
                            any Run-key entry is removed so ARES isn't started twice.
      * Not elevated + wanted : keep an existing task as is (can't verify/replace it without admin);
                            otherwise create/repair the HKCU Run entry (monitoring + lock screen work,
                            BIOS writes need admin).
      * not wanted : remove both.
    """
    if not si.IS_WINDOWS:
        return False, "Not running on Windows; startup unchanged."
    try:
        current = _command_line()
        if not wanted:
            if is_enabled():
                ok, msg = disable()
                return ok, msg
            return False, "Startup is off."
        if si.is_admin():
            if task_exists() and _same_command(registered_task_command(), current):
                if run_key_exists():
                    set_run_key(False)
                    return True, "Removed duplicate Run-key entry; elevated startup task kept."
                return False, "Elevated startup task already up to date."
            ok, msg = create_task()
            if ok:
                set_run_key(False)
                return True, "Elevated startup task created/updated: ARES starts in the tray at sign-in."
            log.warning("Task creation failed (%s); falling back to Run key", msg)
        elif task_exists():
            cmd = registered_task_command()
            if cmd is not None and _same_command(cmd, current):
                if run_key_exists():                    # a leftover Run entry would start ARES twice
                    set_run_key(False)
                    return True, "Removed duplicate Run-key entry; elevated startup task kept."
                return False, "Elevated startup task already up to date."
            if cmd is not None and not missing_targets(cmd):
                return False, ("Elevated startup task present but points at another ARES copy; run ARES "
                               "as administrator once to update it.")
            # The task's program is gone (moved/deleted) or unreadable: it can't start ARES, and we
            # can't fix it without admin. Make sure ARES still starts, via the Run key.
            if _same_command(run_key_value(), current):
                return False, "Run-key startup entry already up to date (old elevated task is broken)."
            ok, msg = set_run_key(True)
            return (True, "The elevated startup task points at a missing program; added a Run-key entry "
                          "so ARES still starts. Run install_startup.bat to repair the task.") if ok else (False, msg)
        if _same_command(run_key_value(), current):
            return False, "Run-key startup entry already up to date."
        ok, msg = set_run_key(True)
        return (True, "Added ARES to Windows startup (current user, no administrator rights needed).") \
            if ok else (False, f"Could not add ARES to Windows startup: {msg}")
    except Exception as exc:  # startup registration must never stop ARES from running
        log.exception("ensure_startup failed")
        return False, f"Could not update Windows startup: {exc}"


# ---- public API -------------------------------------------------------------
def is_enabled() -> bool:
    return task_exists() or run_key_exists()


@dataclass
class StartupStatus:
    enabled: bool                 # an ARES startup entry exists
    method: str                   # "Task Scheduler (elevated)", "Registry Run key (current user)", "Disabled"
    command: str | None           # what is registered
    expected: str | None          # what this copy of ARES would register
    missing: list[str]            # registered files that don't exist
    up_to_date: bool              # registered command == this copy of ARES

    @property
    def works(self) -> bool:
        return self.enabled and not self.missing


def status() -> StartupStatus:
    """The ACTUAL Windows registration (not the saved preference). Never raises."""
    try:
        expected = _command_line()
    except Exception:
        expected = None
    if not si.IS_WINDOWS:
        return StartupStatus(False, "Not available (not Windows)", None, expected, [], False)
    try:
        m = method()
        cmd = registered_command() if m != "Disabled" else None
        return StartupStatus(m != "Disabled", m, cmd, expected, missing_targets(cmd),
                             _same_command(cmd, expected))
    except Exception as exc:
        log.exception("Startup status failed")
        return StartupStatus(False, f"Unknown ({exc})", None, expected, [], False)


def verify(wanted: bool) -> tuple[bool, list[str]]:
    """'Test startup configuration': check, without launching anything, that Windows will (or
    won't) start this copy of ARES at sign-in. Returns (ok, report lines)."""
    st = status()
    lines = [f"Saved preference: {'start at sign-in' if wanted else 'do not start'}",
             f"Windows registration: {st.method}"]
    if st.command:
        lines.append(f"Registered command: {st.command}")
    if not si.IS_WINDOWS:
        return False, lines + ["Startup registration can only be tested on Windows."]
    ok = True
    if wanted != st.enabled:
        ok = False
        lines.append("✗ Registration does not match the setting.")
    if st.enabled:
        if st.missing:
            ok = False
            lines.append("✗ Program not found: " + ", ".join(st.missing))
        else:
            lines.append("✓ Registered program exists.")
        if st.up_to_date:
            lines.append("✓ Points at this copy of ARES.")
        else:
            ok = False
            lines.append(f"✗ Points at a different copy. Expected: {st.expected}")
        if AUTOSTART_ARG not in split_command(st.command) and "--minimized" not in split_command(st.command):
            ok = False
            lines.append("✗ Missing the --autostart option.")
        if task_exists() and run_key_exists():
            ok = False
            lines.append("✗ Registered twice (task and Run key).")
    lines.append("Result: " + ("OK" if ok else "problem found (press Repair / toggle the checkbox)"))
    return ok, lines


def method() -> str:
    if task_exists():
        return "Task Scheduler (elevated)"
    if run_key_exists():
        return "Registry Run key (current user)"
    return "Disabled"


def enable() -> tuple[bool, str]:
    if si.is_admin():
        ok, msg = create_task()
        if ok:
            set_run_key(False)   # avoid starting twice
            return True, "Startup task created: ARES starts elevated at logon."
        log.warning("Task creation failed: %s", msg)
    ok, msg = set_run_key(True)
    if ok:
        return True, ("Added to Windows startup (not elevated). For BIOS charge control at startup, "
                      "run ARES as administrator once and enable startup again.")
    return False, msg


def disable() -> tuple[bool, str]:
    ok1, m1 = delete_task() if task_exists() else (True, "")
    ok2, m2 = set_run_key(False)
    if ok1 and ok2:
        return True, "Removed from Windows startup."
    return False, (m1 + " " + m2).strip()


def remove_own_entries() -> tuple[bool, list[str]]:
    """Uninstaller cleanup: remove the startup entries that start THIS copy of ARES.

    Entries that point at another copy (e.g. `python main.py`) are left alone, and the saved
    "start with Windows" preference is NOT changed, so a reinstall behaves exactly as before.
    Returns (ok, messages). Never raises.
    """
    if not si.IS_WINDOWS:
        return True, ["Not running on Windows; nothing to remove."]
    out: list[str] = []
    ok = True
    try:
        current = _command_line()
        if _same_command(run_key_value(), current):
            done, msg = set_run_key(False)
            ok &= done
            out.append("Removed the Run-key startup entry." if done else f"Run-key entry not removed: {msg}")
        elif run_key_exists():
            out.append("Run-key entry points at another ARES copy; left unchanged.")
        if task_exists():
            if _same_command(registered_task_command(), current):
                done, msg = delete_task()
                ok &= done
                out.append("Removed the elevated startup task." if done else
                           f"Elevated startup task not removed (needs administrator): {msg}")
            else:
                out.append("Elevated startup task points at another ARES copy (or is unreadable); left unchanged.")
        return ok, out or ["No startup entry for this copy."]
    except Exception as exc:
        log.exception("remove_own_entries failed")
        return False, out + [f"Startup cleanup failed: {exc}"]


def apply_preference(db, wanted: bool, source: str = "") -> tuple[bool, str]:
    """Make Windows match the user's preference, record the outcome for the Settings page
    ("startup_last_result") and log changes. Returns (ok, message). Never raises."""
    changed, msg = ensure_startup(bool(wanted))
    if si.IS_WINDOWS:
        st = status()
        ok = st.works if wanted else not st.enabled
    else:
        ok = False
    try:
        db.set_state("startup_last_result", {"ok": ok, "message": msg, "wanted": bool(wanted),
                                             "ts": __import__("time").time()})
        if changed or not ok:
            db.log_event("info" if ok else "warning", (f"Startup ({source}): " if source else "Startup: ") + msg)
    except Exception:
        log.exception("Could not record startup result")
    return ok, msg
