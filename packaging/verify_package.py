"""Verify the PACKAGED ARES executable on Windows (run by packaging\\build_installer.bat).

    python packaging\\verify_package.py [--exe PATH] [--log packaging\\logs\\04_verify_package.log]

Default exe: dist_v2beta\\ARES-Battery-Guardian\\ARES-Battery-Guardian.exe. Every run of the EXE uses
a new temporary ARES_DATA_DIR, so your real settings and history are never opened. The real
HKCU Run value is saved and restored exactly; checks that would change a real Smart Wake-Up or
elevated startup task are skipped (reported as SKIP) instead.

Checks: bundle layout (Qt plugins, icon, build info, no user data bundled), Windows version
resource, --diagnose, --wake-check (Smart Wake off and on), --disable-wake, --uninstall-cleanup,
--install-startup / --remove-startup, --autostart, single instance, --exit.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from wintest_common import (APP_DISPLAY_VERSION, EXE_NAME, ROOT, STARTUP_TASK, WAKE_TASK, Report,
                            create_dummy_wake_task, delete_task, exit_instance, file_version_strings,
                            has_traceback, is_admin, log_text, new_data_dir, read_run_value, remove_dir,
                            restore_run_value, run, seed_test_db, set_run_value, setting, start, task_exists, task_xml,
                            test_env, wait_for_log)

DEFAULT_EXE = ROOT / "dist_v2beta" / "ARES-Battery-Guardian" / EXE_NAME


def check_layout(r: Report, exe: Path) -> None:
    internal = exe.parent / "_internal"
    r.check(exe.exists(), "EXE present", str(exe))
    r.check(internal.is_dir(), "_internal folder present")
    info = internal / "build_info.json"
    try:
        data = json.loads(info.read_text(encoding="utf-8"))
        r.check(data.get("version") == APP_DISPLAY_VERSION, "build_info.json version", json.dumps(data))
    except (OSError, ValueError) as exc:
        r.add("FAIL", "build_info.json readable", str(exc))
    r.check((internal / "assets" / "ares.ico").exists(), "program icon bundled (assets\\ares.ico)")
    wanted = {
        "Qt platform plugin (platforms\\qwindows.dll)": "platforms/qwindows.dll",
        "Qt style plugin (styles\\*.dll)": "styles/*.dll",
        "Qt image format plugin for .ico (imageformats\\qico.dll)": "imageformats/qico.dll",
        "Python runtime (python3*.dll)": "python3*.dll",
        "QtNetwork module (single-instance channel)": "PySide6/QtNetwork.pyd",
        "QtWidgets module": "PySide6/QtWidgets.pyd",
        "sqlite3 module (_sqlite3.pyd)": "_sqlite3.pyd",
    }
    for name, pattern in wanted.items():
        hits = sorted(internal.rglob(pattern))
        r.check(bool(hits), name, ", ".join(str(h.relative_to(exe.parent)) for h in hits[:4]) or "not found")
    user_data = [p for p in exe.parent.rglob("*") if p.suffix.lower() in (".db", ".db-wal", ".db-shm")]
    r.check(not user_data, "no database / user data bundled", ", ".join(map(str, user_data)) or "none")
    total = sum(p.stat().st_size for p in exe.parent.rglob("*") if p.is_file())
    r.note(f"bundle size {total / 1e6:.1f} MB, {sum(1 for _ in exe.parent.rglob('*'))} entries")


def check_version_resource(r: Report, exe: Path) -> None:
    v = file_version_strings(exe)
    r.check(v.get("ProductVersion") == APP_DISPLAY_VERSION and v.get("FileVersion") == "2.0.0.1",
            "Windows version resource", json.dumps(v))


def check_diagnose(r: Report, exe: Path) -> None:
    d = new_data_dir("diag")
    try:
        rc = run([str(exe), "--diagnose", "--json"], env=test_env(d), timeout=180).returncode
        out = d / "logs" / "compatibility_report.json"
        ok = rc == 0 and out.exists()
        detail = f"exit {rc}"
        if ok:
            diag = json.loads(out.read_text(encoding="utf-8"))
            detail += f"; report keys: {', '.join(list(diag)[:8])}"
        r.check(ok and not has_traceback(d), "--diagnose --json (packaged hardware probe)", detail)
    finally:
        remove_dir(d)


def check_wake_check(r: Report, exe: Path) -> None:
    before = task_xml(WAKE_TASK)
    d = new_data_dir("wake")
    try:
        rc = run([str(exe), "--wake-check"], env=test_env(d), timeout=120).returncode
        r.check(rc == 0 and not has_traceback(d), "--wake-check with Smart Wake-Up off", f"exit {rc}")
        seed_test_db(d, {"wake_enabled": True, "wake_low_enabled": True, "wake_high_enabled": True})
        t0 = time.monotonic()
        rc = run([str(exe), "--wake-check"], env=test_env(d), timeout=120).returncode
        took = time.monotonic() - t0
        line = next((ln for ln in log_text(d).splitlines() if "Wake check:" in ln), "")
        r.check(rc == 0 and not has_traceback(d), "--wake-check with Smart Wake-Up on",
                f"exit {rc} in {took:.1f}s; log: {line.split('Wake check:', 1)[-1].strip() or '(no result line)'}")
        if "ARES was started" in line:
            started = wait_for_log(d, "started (background only)", 90)
            r.note(f"the wake check started a tray-only ARES for the test folder (seen in log: {started}); "
                   "asking it to exit")
        exit_instance(exe, d)
        r.check(task_xml(WAKE_TASK) == before, "--wake-check on a test folder does not create or move the real wake task",
                f"task present before/after: {before is not None}/{task_exists(WAKE_TASK)}")
    finally:
        remove_dir(d)


def check_disable_wake(r: Report, exe: Path) -> None:
    """--disable-wake on a separate data folder turns the setting off and must leave the real
    wake task (one per Windows user, always checking the normal data folder) exactly as it is."""
    before = task_xml(WAKE_TASK)
    d = new_data_dir("nowake")
    try:
        seed_test_db(d, {"wake_enabled": True, "wake_interval_min": 15})
        rc = run([str(exe), "--disable-wake"], env=test_env(d), timeout=120).returncode
        r.check(rc == 0 and setting(d, "wake_enabled") is False and setting(d, "wake_interval_min") == 15,
                "--disable-wake turns Smart Wake-Up off (other settings kept)",
                f"exit {rc}; wake_enabled={setting(d, 'wake_enabled')}")
        r.check(task_xml(WAKE_TASK) == before and not has_traceback(d),
                "--disable-wake on a test folder leaves the real wake task unchanged",
                f"task present before/after: {before is not None}/{task_exists(WAKE_TASK)}")
    finally:
        remove_dir(d)


def check_uninstall_cleanup(r: Report, exe: Path) -> None:
    saved_run = read_run_value()
    startup_xml = task_xml(STARTUP_TASK)
    if startup_xml and str(exe).lower() in startup_xml.lower():
        r.add("SKIP", "--uninstall-cleanup", "an elevated startup task runs this EXE; not touching it")
        return
    wake_real = task_exists(WAKE_TASK)
    d = new_data_dir("cleanup")
    try:
        # 1) entries for THIS exe are removed
        set_run_value(subprocess.list2cmdline([str(exe), "--autostart"]))
        if not wake_real:
            create_dummy_wake_task(str(exe))
        rc = run([str(exe), "--uninstall-cleanup"], env=test_env(d), timeout=120).returncode
        removed_run = read_run_value() is None
        removed_task = wake_real or not task_exists(WAKE_TASK)
        r.check(rc == 0 and removed_run and removed_task, "--uninstall-cleanup removes this copy's entries",
                f"exit {rc}; Run value removed: {removed_run}; wake task removed: "
                f"{'not tested (real task present)' if wake_real else removed_task}")
        # 2) entries for ANOTHER copy are left alone
        other = r"C:\Other ARES\ARES-Battery-Guardian.exe"
        set_run_value(subprocess.list2cmdline([other, "--autostart"]))
        if not wake_real:
            create_dummy_wake_task(other)
        rc = run([str(exe), "--uninstall-cleanup"], env=test_env(d), timeout=120).returncode
        kept_run = (read_run_value() or ("",))[0].lower().startswith('"c:\\other ares')
        kept_task = wake_real or task_exists(WAKE_TASK)
        r.check(rc == 0 and kept_run and kept_task, "--uninstall-cleanup leaves another copy's entries",
                f"exit {rc}; Run value kept: {kept_run}; wake task kept: {kept_task}")
        created = [p for p in d.rglob("*")]
        r.check(not created, "--uninstall-cleanup writes nothing to the data folder",
                ", ".join(str(p) for p in created) or "data folder untouched")
    finally:
        if not wake_real:
            delete_task(WAKE_TASK)
        restore_run_value(saved_run)
        r.check(read_run_value() == saved_run, "real Run value restored exactly", repr(saved_run))
        remove_dir(d)


def check_startup_args(r: Report, exe: Path) -> None:
    if task_exists(STARTUP_TASK):
        r.add("SKIP", "--install-startup / --remove-startup",
              "an elevated ARES startup task exists; --remove-startup would try to delete it")
        return
    if is_admin():
        r.add("SKIP", "--install-startup / --remove-startup",
              "running as administrator would create an elevated task; run this script non-elevated")
        return
    saved_run = read_run_value()
    d = new_data_dir("startup")
    try:
        rc1 = run([str(exe), "--install-startup"], env=test_env(d), timeout=120).returncode
        val = read_run_value()
        expected = subprocess.list2cmdline([str(exe), "--autostart"])
        r.check(rc1 == 0 and val is not None and val[0].lower() == expected.lower(),
                "--install-startup writes the HKCU Run value", f"exit {rc1}; value {val[0] if val else None!r}")
        rc2 = run([str(exe), "--remove-startup"], env=test_env(d), timeout=120).returncode
        r.check(rc2 == 0 and read_run_value() is None, "--remove-startup removes it", f"exit {rc2}")
    finally:
        restore_run_value(saved_run)
        r.check(read_run_value() == saved_run, "real Run value restored exactly", repr(saved_run))
        remove_dir(d)


def check_autostart_single_instance(r: Report, exe: Path) -> None:
    d = new_data_dir("auto")
    first = None
    try:
        seed_test_db(d, {"start_minimized": True})
        first = start([str(exe), "--autostart"], test_env(d))
        ok = wait_for_log(d, "started (background only) at Windows sign-in", 90)
        mon = wait_for_log(d, "Background monitoring started", 30)
        r.check(ok and mon and first.poll() is None, "--autostart starts in the tray (start minimized on)",
                f"running: {first.poll() is None}")
        t0 = time.monotonic()
        rc = run([str(exe)], env=test_env(d), timeout=60).returncode
        second_done = time.monotonic() - t0
        text = log_text(d)
        r.check(rc == 0 and "Another instance is running; exiting this one" in text
                and text.count("Background monitoring started") == 1 and first.poll() is None,
                "second launch hands over to the running instance (single instance)",
                f"exit {rc} after {second_done:.1f}s; monitoring loops started: "
                f"{text.count('Background monitoring started')}")
        rc = exit_instance(exe, d)
        try:
            first.wait(30)
        except subprocess.TimeoutExpired:
            pass
        r.check(rc == 0 and first.poll() is not None and wait_for_log(d, "stopped; lock released", 10),
                "--exit closes the running instance cleanly", f"exit {rc}; process ended: {first.poll() is not None}")
        r.check(not has_traceback(d), "no errors in the test instance log")
        # normal launch (dashboard) after a clean exit
        first = start([str(exe)], test_env(d))
        ok = wait_for_log(d, "started (dashboard open)", 90)
        r.check(ok, "normal launch opens the dashboard")
        r.check(exit_instance(exe, d) == 0, "--exit after a normal launch")
        first.wait(30)
    except subprocess.TimeoutExpired as exc:
        r.add("FAIL", "autostart / single-instance test timed out", str(exc))
    finally:
        if first is not None and first.poll() is None:
            exit_instance(exe, d)
            try:
                first.wait(20)
            except subprocess.TimeoutExpired:
                first.kill()
                r.add("FAIL", "test instance had to be killed")
        remove_dir(d)
    d = new_data_dir("noexit")
    try:
        rc = run([str(exe), "--exit"], env=test_env(d), timeout=60).returncode
        r.check(rc == 0, "--exit when nothing is running", f"exit {rc}")
    finally:
        remove_dir(d)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", type=Path, default=DEFAULT_EXE)
    ap.add_argument("--log", type=Path, default=ROOT / "packaging" / "logs" / "04_verify_package.log")
    args = ap.parse_args()
    if sys.platform != "win32":
        print("verify_package.py runs on Windows only.")
        return 2
    exe = args.exe.resolve()
    r = Report(f"Packaged ARES verification: {exe}", args.log)
    r.start()
    if not exe.exists():
        r.add("FAIL", "EXE present", str(exe))
        return r.finish()
    r.note(f"administrator: {is_admin()}; real wake task present: {task_exists(WAKE_TASK)}; "
           f"elevated startup task present: {task_exists(STARTUP_TASK)}; real Run value: {read_run_value()!r}")
    for step in (check_layout, check_version_resource, check_diagnose, check_wake_check,
                 check_disable_wake, check_uninstall_cleanup, check_startup_args,
                 check_autostart_single_instance):
        try:
            step(r, exe)
        except Exception as exc:                       # report and continue with the next check
            r.add("FAIL", step.__name__, f"{type(exc).__name__}: {exc}")
    return r.finish()


if __name__ == "__main__":
    sys.exit(main())
