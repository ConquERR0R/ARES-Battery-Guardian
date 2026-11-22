"""Install / launch / update / uninstall / reinstall test for ARES-Battery-Guardian-V2-Beta-Setup.exe.

    python packaging\\test_installer.py [--setup installer_output\\ARES-Battery-Guardian-V2-Beta-Setup.exe]

Runs the real Setup.exe silently for the CURRENT USER (no administrator rights) and leaves ARES
installed at the end. Your real data folder (%LOCALAPPDATA%\\ARES Battery Guardian) is hashed
before and after every step and must stay identical (or, if a real ARES is running and recording,
keep every setting and lose no rows). Setup and the uninstaller run with ARES_DATA_DIR set to a
temporary folder, so the "exit the running ARES" steps they perform only reach test instances, and
the installed ARES is only ever launched against temporary data folders. The real HKCU Run value
is restored exactly; a real Smart Wake-Up task is never touched.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import winreg
from pathlib import Path

from wintest_common import (APP_DISPLAY_VERSION, APP_NAME, CSIDL_DESKTOPDIRECTORY, CSIDL_PROGRAMS, EXE_NAME,
                            ROOT, STARTUP_TASK, WAKE_TASK, Report, compare_snapshots, create_dummy_wake_task,
                            delete_task, exit_instance, has_traceback, is_admin, log_text, new_data_dir,
                            processes_named, read_db, read_run_value, real_data_root, remove_dir,
                            restore_run_value, run, seed_test_db, set_run_value, shell_folder, shortcut_target,
                            snapshot, start, task_exists, task_xml, test_env, wait_for_log)

APP_GUID = "{10EAE2D6-A69E-4449-BBA4-73FE0626C524}"
UNINSTALL_KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_GUID}_is1"
DEFAULT_SETUP = ROOT / "installer_output" / "ARES-Battery-Guardian-V2-Beta-Setup.exe"
LOG_DIR = ROOT / "packaging" / "logs"

# Settings a user might have changed, including custom alarm paths: must survive an update.
USER_SETTINGS = {"max_charge": 85, "min_charge": 30, "high_alarm_sound_path": r"C:\Sounds\my high.wav",
                 "low_alarm_sound_path": r"C:\Sounds\my low.wav", "start_minimized": True, "theme": "light"}


def install_dir() -> Path:
    return Path(os.environ["LOCALAPPDATA"]) / "Programs" / APP_NAME


def uninstall_entry() -> dict | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
            out = {}
            for name in ("DisplayName", "DisplayVersion", "InstallLocation", "UninstallString"):
                try:
                    out[name] = winreg.QueryValueEx(k, name)[0]
                except OSError:
                    pass
            return out
    except OSError:
        return None


def shortcuts() -> dict[str, Path]:
    return {"Start Menu": shell_folder(CSIDL_PROGRAMS) / f"{APP_NAME}.lnk",
            "Desktop": shell_folder(CSIDL_DESKTOPDIRECTORY) / f"{APP_NAME}.lnk"}


class Run:
    def __init__(self, setup: Path, r: Report):
        self.setup, self.r = setup, r
        self.real = real_data_root()
        self.base = snapshot(self.real)
        self.run_value = read_run_value()
        self.wake_xml = task_xml(WAKE_TASK)
        self.startup_xml = task_xml(STARTUP_TASK)

    # -- invariants checked after every step --------------------------------------------------
    def real_state_unchanged(self, step: str) -> None:
        ok, detail = compare_snapshots(self.base, snapshot(self.real))
        self.r.check(ok, f"{step}: real data folder preserved", detail)
        self.r.check(read_run_value() == self.run_value, f"{step}: real Run value unchanged", repr(read_run_value()))
        self.r.check(task_xml(WAKE_TASK) == self.wake_xml and task_xml(STARTUP_TASK) == self.startup_xml,
                     f"{step}: real scheduled tasks unchanged")

    def setup_run(self, tag: str, data_dir: Path) -> int:
        log = LOG_DIR / f"06_setup_{tag}.log"
        cmd = [str(self.setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/TASKS=desktopicon",
               f"/LOG={log}"]
        rc = subprocess.run(cmd, env=test_env(data_dir), timeout=600).returncode
        self.r.note(f"Setup ({tag}) exit {rc}; Inno Setup log: {log}")
        return rc

    def check_installed(self, step: str) -> None:
        exe = install_dir() / EXE_NAME
        self.r.check(exe.exists(), f"{step}: program installed", str(exe))
        self.r.check((install_dir() / "unins000.exe").exists(), f"{step}: uninstaller present")
        entry = uninstall_entry() or {}
        self.r.check(entry.get("DisplayVersion") == APP_DISPLAY_VERSION, f"{step}: Apps & features entry",
                     f"{entry}")
        for where, lnk in shortcuts().items():
            target = shortcut_target(lnk) if lnk.exists() else None
            self.r.check(lnk.exists() and target is not None and Path(target) == exe,
                         f"{step}: {where} shortcut", f"{lnk} -> {target}")
        self.r.check(not str(install_dir()).lower().startswith(str(self.real).lower() + "\\"),
                     f"{step}: program folder is separate from the data folder", str(install_dir()))

    def launch_and_exit(self, step: str) -> None:
        exe = install_dir() / EXE_NAME
        d = new_data_dir("launch")
        p = None
        try:
            p = start([str(exe)], test_env(d))
            ok = wait_for_log(d, "started (dashboard open)", 90)
            self.r.check(ok and p.poll() is None, f"{step}: installed ARES launches (test data folder)")
            rc = exit_instance(exe, d)
            p.wait(30)
            self.r.check(rc == 0 and not has_traceback(d), f"{step}: installed ARES exits cleanly", f"exit {rc}")
            rc = run([str(exe), "--wake-check"], env=test_env(d), timeout=120).returncode
            self.r.check(rc == 0, f"{step}: installed ARES --wake-check", f"exit {rc}")
        finally:
            if p is not None and p.poll() is None:
                p.kill()
            remove_dir(d)

    # -- steps --------------------------------------------------------------------------------
    def fresh_install(self, tag: str) -> None:
        d = new_data_dir("setup")
        try:
            rc = self.setup_run(tag, d)
            self.r.check(rc == 0, f"{tag}: Setup succeeded", f"exit {rc}")
            self.check_installed(tag)
            self.r.check(not any(d.rglob("*.db")), f"{tag}: Setup created no database")
        finally:
            remove_dir(d)
        self.launch_and_exit(tag)
        self.real_state_unchanged(tag)

    def update_while_running(self) -> None:
        step = "update"
        exe = install_dir() / EXE_NAME
        d = new_data_dir("update")
        p = None
        try:
            seed_test_db(d, USER_SETTINGS, events=5)
            before = read_db(d / "data" / "battery_guardian.db")
            p = start([str(exe), "--minimized"], test_env(d))
            self.r.check(wait_for_log(d, "started (background only)", 90), f"{step}: test ARES running before update")
            rc = self.setup_run("update", d)
            try:
                p.wait(60)
            except subprocess.TimeoutExpired:
                pass
            self.r.check(rc == 0, f"{step}: Setup succeeded over the running copy", f"exit {rc}")
            asked = "Exit requested by another process (--exit)" in log_text(d)
            self.r.check(p.poll() is not None and asked, f"{step}: running ARES was asked to exit and closed cleanly",
                         f"exited: {p.poll() is not None}; clean --exit seen in its log: {asked}")
            after = read_db(d / "data" / "battery_guardian.db")
            kept = all(after.get("settings", {}).get(k) == before.get("settings", {}).get(k) for k in USER_SETTINGS)
            lost = {t: n for t, n in (before.get("counts") or {}).items() if (after.get("counts") or {}).get(t, 0) < n}
            self.r.check(kept and not lost, f"{step}: settings, custom alarm paths and history kept",
                         f"settings kept: {kept}; rows lost: {lost or 'none'}; events "
                         f"{(before.get('counts') or {}).get('events')} -> {(after.get('counts') or {}).get('events')}")
            self.check_installed(step)
            # the updated program reads the same data folder with everything intact
            p = start([str(exe), "--minimized"], test_env(d))
            self.r.check(wait_for_log(d, "started (background only)", 90, count=2),
                         f"{step}: updated ARES starts on the existing data")
            exit_instance(exe, d)
            p.wait(30)
            again = read_db(d / "data" / "battery_guardian.db")
            self.r.check(all(again["settings"].get(k) == before["settings"].get(k) for k in USER_SETTINGS),
                         f"{step}: settings still intact after the updated version ran")
        finally:
            if p is not None and p.poll() is None:
                exit_instance(exe, d)
                try:
                    p.wait(20)
                except subprocess.TimeoutExpired:
                    p.kill()
            remove_dir(d)
        self.real_state_unchanged(step)

    def uninstall(self) -> None:
        step = "uninstall"
        exe = install_dir() / EXE_NAME
        d = new_data_dir("uninstall")
        test_entries = not task_exists(STARTUP_TASK)
        try:
            seed_test_db(d, USER_SETTINGS, events=3)
            before = read_db(d / "data" / "battery_guardian.db")
            p = start([str(exe), "--minimized"], test_env(d))
            wait_for_log(d, "started (background only)", 90)
            if test_entries:      # this copy's startup entries must be removed by the uninstaller
                set_run_value(subprocess.list2cmdline([str(exe), "--autostart"]))
            dummy_task = test_entries and self.wake_xml is None and create_dummy_wake_task(str(exe))[0]
            log = LOG_DIR / "06_uninstall.log"
            subprocess.run([str(install_dir() / "unins000.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                            f"/LOG={log}"], env=test_env(d), timeout=300)
            deadline = time.monotonic() + 180          # the uninstaller re-launches itself from %TEMP%
            while time.monotonic() < deadline and (uninstall_entry() or (install_dir() / EXE_NAME).exists()):
                time.sleep(1)
            time.sleep(3)
            self.r.note(f"uninstaller log: {log}")
            self.r.check(not (install_dir() / EXE_NAME).exists() and uninstall_entry() is None,
                         f"{step}: program and Apps & features entry removed")
            self.r.check(not any(lnk.exists() for lnk in shortcuts().values()), f"{step}: shortcuts removed")
            try:
                p.wait(30)
            except subprocess.TimeoutExpired:
                pass
            self.r.check(p.poll() is not None, f"{step}: running ARES closed by the uninstaller")
            if test_entries:
                self.r.check(read_run_value() is None, f"{step}: this copy's Run value removed")
            if dummy_task:
                self.r.check(not task_exists(WAKE_TASK), f"{step}: this copy's wake task removed")
            after = read_db(d / "data" / "battery_guardian.db")
            self.r.check(all((after.get("settings") or {}).get(k) == before["settings"].get(k) for k in USER_SETTINGS) and
                         all((after.get("counts") or {}).get(t, 0) >= n for t, n in (before.get("counts") or {}).items()),
                         f"{step}: data of the running copy kept (settings and rows)")
            self.r.check(self.real.exists() == self.base["folder_exists"], f"{step}: data folder still there",
                         str(self.real))
        finally:
            restore_run_value(self.run_value)
            if self.wake_xml is None:
                delete_task(WAKE_TASK)
            remove_dir(d)
        self.real_state_unchanged(step)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", type=Path, default=DEFAULT_SETUP)
    ap.add_argument("--log", type=Path, default=LOG_DIR / "06_test_installer.log")
    args = ap.parse_args()
    if sys.platform != "win32":
        print("test_installer.py runs on Windows only.")
        return 2
    r = Report(f"Installer test: {args.setup}", args.log)
    r.start()
    if not args.setup.exists():
        r.add("FAIL", "Setup.exe present", str(args.setup))
        return r.finish()
    if is_admin():
        r.note("running as administrator: the per-user install is still used, but please re-run non-elevated "
               "for a result that matches a normal user")
    t = Run(args.setup.resolve(), r)
    running = processes_named(EXE_NAME) + processes_named("pythonw.exe")
    r.note(f"real data folder: {t.real} ({len(t.base['files'])} files); real Run value: {t.run_value!r}; "
           f"real wake task: {t.wake_xml is not None}; elevated startup task: {t.startup_xml is not None}")
    r.note(f"ARES-like processes running at start: {len(running)}")
    previously = uninstall_entry()
    r.note(f"already installed before the test: {previously}")
    steps = [("install", lambda: t.fresh_install("install")), ("update", t.update_while_running),
             ("uninstall", t.uninstall), ("reinstall", lambda: t.fresh_install("reinstall"))]
    for name, fn in steps:
        r.note(f"--- {name} ---")
        try:
            fn()
        except Exception as exc:
            r.add("FAIL", name, f"{type(exc).__name__}: {exc}")
    r.note("ARES V2 Beta is left installed. Your real data was never opened by the test.")
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
