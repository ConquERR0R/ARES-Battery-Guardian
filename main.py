"""ARES Battery Guardian entry point.

    python main.py                 start the app (window + tray)
    python main.py --minimized     start in the background, tray icon only, no dashboard
    python main.py --autostart     what Windows runs at sign-in: dashboard opens unless
                                   "Start minimized in system tray" is ticked in Settings
    python main.py --diagnose      print a hardware compatibility report and exit (no GUI, read-only)
    python main.py --diagnose --json   same, as JSON
    python main.py --exit          ask the running ARES (tray) to shut down cleanly
    python main.py --install-startup   start ARES (tray only) at Windows sign-in (run as admin for elevated)
    python main.py --remove-startup    remove ARES from Windows startup
    python main.py --unlock        emergency: release the 90% / 25% protection screen
    python main.py --disable-lock  emergency: turn both protection screens off
    python main.py --wake-check    Smart Wake-Up battery check (run by Task Scheduler)
    python main.py --disable-wake  turn Smart Wake-Up off and remove its wake task immediately
"""
from __future__ import annotations

import argparse
import json
import os
import logging
import sys
from dataclasses import asdict
from logging.handlers import RotatingFileHandler

from app import APP_DISPLAY_VERSION, APP_NAME
from app import paths


def setup_logging(verbose: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = RotatingFileHandler(paths.logs_dir() / "ares.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(fh)
    if sys.stderr is not None:   # pythonw / windowed EXE have no console
        sh = logging.StreamHandler()
        sh.setLevel(logging.WARNING if not verbose else logging.DEBUG)
        root.addHandler(sh)


def run_diagnostics() -> dict:
    """Read-only inspection of the machine's battery and charge-control capabilities."""
    from app.core.battery_monitor import BatteryMonitor
    from app.core.hardware_detector import create_controller, detect
    from app.utils import system_info as si

    prof = detect()
    ctl = create_controller(prof)
    caps = ctl.probe()
    cfg = ctl.read_config() if caps.can_read else None
    plan = ctl.plan(25, 90)
    snap = BatteryMonitor().read()
    return {
        "app": f"{APP_NAME} {APP_DISPLAY_VERSION}",
        "is_admin": si.is_admin(),
        "machine": asdict(prof.machine),
        "is_target_e7470": prof.is_target_e7470,
        "capabilities": {
            "summary": caps.summary, "vendor": caps.vendor, "interface": caps.interface,
            "supported": caps.supported, "can_read": caps.can_read, "can_write": caps.can_write,
            "requires_admin": caps.requires_admin, "password_required": caps.password_required,
            "limits": asdict(caps.limits) if caps.limits else None, "notes": caps.notes,
        },
        "firmware_config": ({**cfg.to_dict(), "describe": cfg.describe()} if cfg else None),
        "plan_for_25_90": ({"start": plan.start, "stop": plan.stop, "exact": plan.exact,
                            "messages": plan.messages} if plan else None),
        "battery": snap.to_dict(),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ARES-Battery-Guardian")
    ap.add_argument("--minimized", action="store_true", help="start hidden in the system tray")
    ap.add_argument("--autostart", action="store_true",
                    help="launched by Windows at sign-in (opens the dashboard unless 'Start minimized' is set)")
    ap.add_argument("--diagnose", action="store_true", help="print hardware compatibility report and exit")
    ap.add_argument("--json", action="store_true", help="with --diagnose: output JSON")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--exit", action="store_true", help="ask the running ARES to exit cleanly, then return")
    ap.add_argument("--install-startup", action="store_true",
                    help="register ARES to start (in the tray) when Windows signs in; run as administrator "
                         "for the elevated task")
    ap.add_argument("--remove-startup", action="store_true", help="remove ARES from Windows startup")
    ap.add_argument("--unlock", action="store_true",
                    help="emergency: release the protection screen until the charger state next changes")
    ap.add_argument("--disable-lock", action="store_true",
                    help="emergency: turn High and Low Battery Protection off and release any protection screen")
    ap.add_argument("--wake-check", action="store_true",
                    help="Smart Wake-Up: check the battery once (run by Task Scheduler after a wake timer)")
    ap.add_argument("--wake-trigger", choices=("low", "high"), help=argparse.SUPPRESS)
    ap.add_argument("--disable-wake", action="store_true",
                    help="turn Smart Wake-Up off and remove its wake task immediately")
    ap.add_argument("--uninstall-cleanup", action="store_true",
                    help="(used by the uninstaller) remove this copy's startup entry and wake task; "
                         "settings, history and all other data are kept")
    ap.add_argument("--after-elevation", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {APP_DISPLAY_VERSION}")
    args = ap.parse_args(argv)

    # These two never touch the data folder (no log file, no database), so the installer and
    # uninstaller can run them without changing anything the user has stored.
    if args.uninstall_cleanup:
        return uninstall_cleanup()
    if args.exit:
        return request_exit()

    setup_logging(args.verbose)
    log = logging.getLogger("ares")
    migrate_storage(log)

    if args.diagnose:
        from app.services.report_service import compatibility_report

        diag = run_diagnostics()
        text = json.dumps(diag, indent=2, default=str) if args.json else compatibility_report(diag)
        out = paths.logs_dir() / ("compatibility_report.json" if args.json else "compatibility_report.txt")
        out.write_text(text, encoding="utf-8")
        if sys.stdout is not None:
            print(text)
            print(f"Saved to {out}")
        return 0

    if args.wake_check:
        return run_wake_check()
    if args.disable_wake:
        return disable_wake()
    if args.install_startup or args.remove_startup:
        return configure_startup(args.install_startup)
    if args.unlock or args.disable_lock:
        return request_unlock(disable=args.disable_lock)
    return run_gui(args, log)


def migrate_storage(log) -> None:
    """Copy data from the pre-v1.3 project folder into the per-user data folder (once)."""
    msg = paths.migrate_legacy_data()
    if msg:
        from app.database.database import Database

        try:
            with Database(paths.database_path()) as db:
                db.log_event("info" if msg.startswith("Copied") else "warning", msg)
        except Exception:
            log.exception("Could not record the data migration")


def run_wake_check() -> int:
    """Task Scheduler entry point: one lightweight battery check (no windows, no tray)."""
    # Only Qt-free modules plus QtCore/QtNetwork (for the single-instance socket): no QtGui,
    # QtWidgets or app.ui code is loaded here (checked by tests/test_smart_wake.py).
    from app.core import smart_wake
    from app.core.power_probe import default_power_probe
    from app.database.database import Database
    from PySide6.QtCore import QCoreApplication

    app = QCoreApplication.instance() or QCoreApplication(sys.argv)   # for the single-instance socket
    with Database(paths.database_path()) as db:
        res = smart_wake.run_check(
            db, probe=default_power_probe,
            send=lambda msg: send_to_running_instance(msg) and (app.processEvents() or True),
            launch=smart_wake.launch_ares,
            lock=smart_wake.CheckLock(paths.data_dir() / "wake_check.lock"))
    if res is not None:
        logging.getLogger("ares").info("Wake check: %s", res.text())
    return 0


def uninstall_cleanup() -> int:
    """Uninstaller hook: remove the Windows startup entry and the Smart Wake-Up task that run THIS
    program. Opens no database and writes no file, so settings and history stay exactly as they are
    (a reinstall re-registers both from the saved settings on its first start)."""
    from app.core import smart_wake, startup_manager

    ok1, lines = startup_manager.remove_own_entries()
    ok2, msg = smart_wake.remove_own_task()
    for line in lines + [msg]:
        _say(line)
    return 0 if ok1 and ok2 else 1


def disable_wake() -> int:
    from app.core import smart_wake
    from app.database.database import Database

    with Database(paths.database_path()) as db:
        db.set_setting("wake_enabled", False)
        ok, msg = smart_wake.ensure(db)
        db.log_event("smart_wake", f"Smart Wake-Up disabled from the command line: {msg}")
    _say(msg)
    return 0 if ok else 1


def instance_server_name() -> str:
    """Local-socket name for this user AND this data folder.

    The lock file lives in the data folder, so the socket name must be tied to the
    same folder: otherwise a test run (separate ARES_DATA_DIR) could talk to, or be
    mistaken for, the real ARES instance running in the tray.
    """
    import getpass
    import hashlib

    folder = str(paths.data_dir().resolve()).lower()
    digest = hashlib.sha1(folder.encode("utf-8")).hexdigest()[:12]
    return f"ARES-Battery-Guardian-{getpass.getuser()}-{digest}"


def _instance_lock():
    from PySide6.QtCore import QLockFile

    lock = QLockFile(str(paths.data_dir() / "ares.lock"))
    # 0 = never treat a lock as stale by age. A lock left by a crashed process is still
    # recovered, because Qt checks whether the owning PID is alive.
    lock.setStaleLockTime(0)
    return lock


def send_to_running_instance(message: bytes, timeout_ms: int = 1000) -> bool:
    """Deliver a command (open, quit, unlock, wake:low, wake:high) to the running ARES. True if sent."""
    from PySide6.QtNetwork import QLocalSocket

    sock = QLocalSocket()
    sock.connectToServer(instance_server_name())
    if not sock.waitForConnected(timeout_ms):
        return False
    if sock.write(message + b"\n") < 0:
        return False
    sock.flush()
    # flush() may already have written everything; waitForBytesWritten() then returns False
    # although the command was delivered, so only wait while bytes are still pending.
    ok = sock.bytesToWrite() == 0 or sock.waitForBytesWritten(timeout_ms)
    ok = ok and sock.bytesToWrite() == 0
    sock.disconnectFromServer()
    if sock.state() != QLocalSocket.UnconnectedState:
        sock.waitForDisconnected(timeout_ms)
    return ok


def _say(msg: str) -> None:
    if sys.stdout is not None:      # pythonw / windowed EXE have no console
        print(msg)


def request_exit(timeout_s: float = 20.0) -> int:
    """`main.py --exit`: ask the running ARES to shut down cleanly, wait until its lock is released."""
    import time

    from PySide6.QtCore import QCoreApplication

    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    if not send_to_running_instance(b"quit"):
        _say("ARES is not running.")
        return 0
    lock = _instance_lock()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if lock.tryLock(250):
            lock.unlock()                       # we only probed it; release immediately
            _say("ARES exited.")
            return 0
    _say("ARES did not exit in time.")
    return 1


def configure_startup(enable: bool) -> int:
    from app.core import startup_manager
    from app.database.database import Database

    with Database(paths.database_path()) as db:
        db.set_setting("autostart", enable)
        ok, msg = startup_manager.apply_preference(db, enable, "install" if enable else "remove")
    _say(msg)
    _say(f"Current method: {startup_manager.method()}")
    return 0 if ok or not sys.platform.startswith("win") else 1


def dashboard_at_launch(args) -> bool:
    """--minimized: never. --autostart (Windows sign-in): unless 'Start minimized' is set. Else: yes."""
    if args.minimized:
        return False
    if args.autostart:
        from app.database.database import Database

        try:
            with Database(paths.database_path()) as db:
                return not bool(db.get_setting("start_minimized"))
        except Exception:                 # unreadable settings: fall back to the default (open)
            return True
    return True


def request_unlock(disable: bool) -> int:
    """Emergency recovery for the protection screens. Works whether or not ARES is running."""
    from PySide6.QtCore import QCoreApplication

    from app.database.database import Database

    app = QCoreApplication.instance() or QCoreApplication(sys.argv)   # QLocalSocket needs an app object
    if disable:
        with Database(paths.database_path()) as db:
            db.set_setting("strict_lock_enabled", False)
            db.set_setting("low_lock_enabled", False)
            db.log_event("strict_lock", "High and Low Battery Protection disabled from the command line "
                                        "(--disable-lock)")
        _say("High and Low Battery Protection screens disabled in settings.")
    delivered = send_to_running_instance(b"unlock")
    app.processEvents()
    if delivered:
        _say("Lock released in the running ARES.")
    else:
        if not disable:
            with Database(paths.database_path()) as db:
                db.set_state("strict_lock_suppressed", True)
                db.set_state("low_lock_suppressed", True)
        _say("ARES is not running; protection screens stay off until the charger state next changes.")
    return 0


def run_gui(args, log) -> int:
    from PySide6.QtCore import QTimer
    from PySide6.QtNetwork import QLocalServer
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    # Closing the dashboard must never end the process: only "Exit ARES" does.
    app.setQuitOnLastWindowClosed(False)

    # ---- single instance: one backend, one monitoring loop ----------------------
    lock = _instance_lock()
    show_dashboard = dashboard_at_launch(args)
    # After "Restart as administrator" the old instance needs a moment to exit.
    if not lock.tryLock(15000 if args.after_elevation else 200):
        # Already running: never a second backend / tray icon / dashboard. Activate the running
        # copy's dashboard instead (unless this launch is meant to stay in the tray).
        if show_dashboard:
            send_to_running_instance(b"open")
        log.info("Another instance is running; exiting this one")
        return 0

    server = QLocalServer()
    bg = None
    try:
        from app.ui.background_app import AresBackgroundApp
        from app.ui.theme import apply_theme

        # A custom ARES_DATA_DIR means a test run or portable copy: never touch Windows startup then.
        bg = AresBackgroundApp(str(paths.database_path()),
                               manage_startup="ARES_DATA_DIR" not in os.environ)
        apply_theme(app, bg.db.get_setting("theme"))   # Fusion + palette: visible check boxes / arrows
        name = instance_server_name()
        QLocalServer.removeServer(name)          # clear a stale socket left by a crash (no-op on Windows)
        if server.listen(name):
            def _on_conn():
                conn = server.nextPendingConnection()
                if conn is None:
                    return
                buf = bytearray()

                def _read():
                    buf.extend(bytes(conn.readAll()))
                    for cmd in bytes(buf).split(b"\n")[:-1]:
                        cmd = cmd.strip()
                        if cmd == b"open":
                            bg.open_dashboard(None)
                        elif cmd in (b"wake:low", b"wake:high"):
                            bg.wake_trigger(cmd.split(b":")[1].decode(), "Smart Wake-Up check")
                        elif cmd == b"unlock":
                            bg.lock.emergency_release("Emergency release from the command line (--unlock)")
                        elif cmd == b"quit":
                            log.info("Exit requested by another process (--exit)")
                            bg.exit_ares()
                    del buf[:bytes(buf).rfind(b"\n") + 1]
                conn.readyRead.connect(_read)
                conn.disconnected.connect(conn.deleteLater)
            server.newConnection.connect(_on_conn)
        else:
            log.warning("Single-instance server could not listen: %s", server.errorString())

        bg.start()                               # monitoring + protection + alarms start right away
        if show_dashboard:
            bg.open_dashboard(None)
        if args.wake_trigger:                    # started by a Smart Wake-Up check
            QTimer.singleShot(1000, lambda: bg.wake_trigger(args.wake_trigger, "ARES started by a wake check"))
        log.info("%s %s started (%s)%s", APP_NAME, APP_DISPLAY_VERSION,
                 "dashboard open" if show_dashboard else "background only",
                 " at Windows sign-in" if args.autostart else "")
        rc = app.exec()
    finally:
        # Release everything that keeps files open, in every exit path, so the data
        # folder can be moved/deleted (Windows refuses to delete files with open handles).
        if bg is not None:
            bg.shutdown()
        server.close()
        lock.unlock()
        log.info("%s stopped; lock released", APP_NAME)
        for h in logging.getLogger().handlers:
            h.flush()
    return rc


if __name__ == "__main__":
    sys.exit(main())
