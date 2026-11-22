"""Window/background lifecycle tests (need PySide6; run headless with QT_QPA_PLATFORM=offscreen).

Checks the required behaviour:
* X destroys the dashboard window (not hidden), the process and monitoring keep running
* "Open Dashboard" builds a new window attached to the SAME backend (one monitoring loop)
* --minimized start shows no window
* a second launch does not start a second backend; it asks the first one to open the dashboard
* only "Exit ARES" stops the monitoring thread
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


def spin(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class BackgroundLifecycleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def test_close_destroys_window_but_monitoring_continues(self):
        from app.database.database import Database
        from app.ui.background_app import AresBackgroundApp
        from app.ui.main_window import MainWindow

        with tempfile.TemporaryDirectory() as d:
            db_path = str(Path(d) / "t.db")
            with Database(db_path) as pre:
                pre.set_setting("monitor_interval_s", 5)

            bg = AresBackgroundApp(db_path)
            snaps = []
            bg.snapshot.connect(lambda s: snaps.append(s))
            bg.start()
            bg.start()                                   # second call must not start another loop
            spin(1500)
            self.assertTrue(bg.thread.isRunning())
            self.assertEqual(bg.worker_starts, 1)

            bg.open_dashboard(0)
            w1 = bg.window
            self.assertIsInstance(w1, MainWindow)
            self.assertTrue(w1.isVisible())
            destroyed = []
            w1.destroyed.connect(lambda *_: destroyed.append(True))

            # Click X
            self.assertTrue(w1.close())
            spin(300)                                    # let deleteLater run
            QCoreApplication.sendPostedEvents(None, 0)
            spin(100)
            self.assertTrue(destroyed, "window object was hidden, not destroyed")
            self.assertIsNone(bg.window)
            self.assertEqual([w for w in QApplication.topLevelWidgets()
                              if isinstance(w, MainWindow)], [])
            self.assertTrue(bg.thread.isRunning(), "monitoring stopped when dashboard closed")

            n = len(snaps)
            spin(6000)                                   # > one 5 s monitoring interval
            self.assertGreater(len(snaps), n, "no monitoring ticks after the window was closed")

            # Reopen from tray -> brand-new window, same backend, still one loop
            bg.tray.open_dashboard.emit()
            w2 = bg.window
            self.assertIsNotNone(w2)
            self.assertTrue(w2.isVisible())
            self.assertEqual(bg.worker_starts, 1)
            bg.tray.open_dashboard.emit()                # opening again must reuse it
            self.assertIs(bg.window, w2)

            # Only Exit ARES stops everything
            quit_called = []
            orig_quit = QApplication.quit
            QApplication.quit = staticmethod(lambda: quit_called.append(True))
            try:
                bg.tray.exit_requested.emit()
            finally:
                QApplication.quit = orig_quit
            spin(300)
            self.assertFalse(bg.thread.isRunning())
            self.assertTrue(quit_called)


def kill_process_tree(proc: subprocess.Popen) -> None:
    """Last-resort cleanup. On Windows a venv's python.exe is a launcher that runs the real
    interpreter as a CHILD process, so Popen.terminate() would only kill the launcher and
    leave ARES running (still holding ares.lock). taskkill /T kills the whole tree."""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        proc.kill()
    proc.wait(15)


def wait_for(pred, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.3)
    return pred()


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class SingleInstanceProcessTest(unittest.TestCase):
    """Starts real ARES processes (offscreen) in an isolated data folder and reads their log."""

    def run_ares(self, env, *args, timeout=30):
        return subprocess.run([sys.executable, str(ROOT / "main.py"), *args], env=env, timeout=timeout,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_autostart_hidden_and_second_launch_reuses_backend(self):
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, ARES_DATA_DIR=d, QT_QPA_PLATFORM="offscreen")
            log = Path(d) / "logs" / "ares.log"
            lock = Path(d) / "data" / "ares.lock"

            def text() -> str:
                return log.read_text(encoding="utf-8") if log.exists() else ""

            first = subprocess.Popen([sys.executable, str(ROOT / "main.py"), "--minimized"], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                self.assertTrue(wait_for(lambda: "background only" in text(), 30), "first instance did not start")
                self.assertNotIn("Dashboard window created", text())
                self.assertEqual(text().count("Background monitoring started"), 1)
                self.assertTrue(lock.exists())

                # User launches ARES again (e.g. Start menu): no new backend, dashboard opens in the first one.
                self.assertEqual(self.run_ares(env).returncode, 0)
                self.assertTrue(wait_for(lambda: "Dashboard window created" in text(), 10))
                self.assertIn("Another instance is running", text())
                self.assertEqual(text().count("Background monitoring started"), 1)
                self.assertIsNone(first.poll(), "first instance died")

                # A second autostart (--minimized) must not open anything either.
                self.assertEqual(self.run_ares(env, "--minimized").returncode, 0)
                time.sleep(1)
                self.assertEqual(text().count("Dashboard window created"), 1)
                self.assertEqual(text().count("Background monitoring started"), 1)

                # Clean shutdown through the same path as tray "Exit ARES".
                self.assertEqual(self.run_ares(env, "--exit").returncode, 0)
                first.wait(20)
                self.assertEqual(first.returncode, 0)
                self.assertIn("lock released", text())
                self.assertFalse(lock.exists(), "ares.lock was not removed on exit")
                wal = Path(d) / "data" / "battery_guardian.db-wal"
                self.assertTrue(not wal.exists() or wal.stat().st_size == 0, "SQLite WAL not checkpointed")
            finally:
                kill_process_tree(first)   # only does anything if a check above failed

    def test_exit_when_not_running(self):
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, ARES_DATA_DIR=d, QT_QPA_PLATFORM="offscreen")
            self.assertEqual(self.run_ares(env, "--exit").returncode, 0)


if __name__ == "__main__":
    unittest.main()
