"""Strict Charging Protection (HIGH mode) tests. Low Battery Protection, priority, alarms and
Settings are covered in test_protection.py.

Policy tests are plain Python. Window/manager tests need PySide6 and run headless
(QT_QPA_PLATFORM=offscreen). Battery readings and the charger probe are injected, so
these tests check ARES' logic and Qt window behaviour, not real Windows hardware.

Numbered comments refer to the requested test list:
 1 lock at 90% plugged in        2 no lock at 90% unplugged     3 unplug -> unlock
 4 charger stays -> stays locked  5 dashboard closed -> monitoring continues
 6 tray reopens dashboard          7 Exit ARES stops monitoring   8 no duplicate lock windows
 9 charger detection failure -> safe release                    10 restart -> no stuck lock
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

from app.core.battery_monitor import BatterySnapshot
from app.core.strict_lock import HIGH, LOCK, UNLOCK, ProtectionPolicy
from app.database.database import Database
from fake_hw import use_fake_battery  # noqa: E402

try:
    from PySide6.QtCore import QCoreApplication, QEventLoop, Qt, QTimer
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


def snap(pct, ac=True, charging=None, available=True):
    return BatterySnapshot(time.time(), available, "ioctl" if available else "none", percent=pct,
                           ac_online=ac, charging=(ac and pct < 100) if charging is None else charging,
                           discharging=(ac is False))


class FakePlayer:
    """Records alarm playback instead of using winsound."""

    def __init__(self):
        self.played, self.stops, self.volumes = [], 0, []

    def play(self, kind, path, volume=100):
        self.played.append((kind, path))
        self.volumes.append(volume)
        return True, f"fake {kind}"

    def stop(self):
        self.stops += 1


def spin(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.p = ProtectionPolicy(self.db)

    def test_1_locks_at_90_with_charger(self):
        self.assertIsNone(self.p.update(89, True).action)
        d = self.p.update(90, True)
        self.assertEqual(d.action, LOCK)
        self.assertEqual(d.mode, HIGH)
        self.assertEqual(self.p.active, HIGH)

    def test_2_no_lock_at_90_without_charger(self):
        self.assertIsNone(self.p.update(90, False).action)
        self.assertIsNone(self.p.update(95, False).action)
        self.assertFalse(self.p.active)

    def test_3_unplug_unlocks(self):
        self.p.update(91, True)
        d = self.p.update(91, False)
        self.assertEqual(d.action, UNLOCK)
        self.assertIn("disconnected", d.reason)

    def test_4_percent_drop_with_charger_keeps_lock(self):
        self.p.update(92, True)
        for pct in (91, 88, 80, 50, 20):
            self.assertIsNone(self.p.update(pct, True).action)
        self.assertTrue(self.p.active)

    def test_8_repeated_polling_single_lock_decision(self):
        actions = [self.p.update(93, True).action for _ in range(50)]
        self.assertEqual(actions.count(LOCK), 1)

    def test_once_per_charging_session_and_rearm_after_unplug(self):
        self.p.update(90, True)
        self.p.release("emergency")
        self.assertIsNone(self.p.update(95, True).action)      # same session: no re-lock
        self.p.update(80, False)                                  # unplugged -> new session
        self.assertEqual(self.p.update(90, True).action, LOCK)

    def test_20_percent_never_locks_high(self):
        self.db.set_setting("low_lock_enabled", False)       # (LOW mode is tested in test_protection.py)
        self.assertIsNone(self.p.update(20, True).action)
        self.assertIsNone(self.p.update(20, False).action)

    def test_9_unknown_charger_never_locks(self):
        self.assertIsNone(self.p.update(95, None).action)
        self.assertFalse(self.p.active)

    def test_10_restart_does_not_restore_lock_and_suppression_persists(self):
        self.p.update(95, True)
        self.p.release("emergency")
        p2 = ProtectionPolicy(self.db)                         # "restart"
        self.assertFalse(p2.active)
        self.assertIsNone(p2.update(95, True).action)          # suppressed until unplug
        p2.update(95, False)
        self.assertFalse(self.db.get_state("strict_lock_suppressed"))
        self.assertEqual(p2.update(95, True).action, LOCK)

    def test_disabled_in_settings(self):
        self.db.set_setting("strict_lock_enabled", False)
        self.assertIsNone(self.p.update(99, True).action)
        self.db.set_setting("strict_lock_enabled", True)
        self.p.update(99, True)
        self.db.set_setting("strict_lock_enabled", False)
        self.assertEqual(self.p.update(99, True).action, UNLOCK)

    def test_configurable_threshold(self):
        self.db.set_setting("strict_lock_threshold", 80)
        self.assertEqual(self.p.update(80, True).action, LOCK)


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class LockWindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        from app.ui import lock_window as lw

        self.lw = lw
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.ac = [True]
        self.now = [0.0]
        self.player = FakePlayer()
        self.m = lw.ProtectionManager(self.db, power_probe=lambda: (None, self.ac[0]), clock=lambda: self.now[0],
                                      player=self.player)
        self.addCleanup(self.m.shutdown)

    def lock_windows(self):
        QCoreApplication.sendPostedEvents(None, 0)
        return [w for w in QApplication.topLevelWidgets()
                if isinstance(w, self.lw.ProtectionWindow) and w.isVisible()]

    def test_1_window_is_fullscreen_topmost_frameless_modal(self):
        self.m.on_snapshot(snap(90, True))
        w = self.m.window
        self.assertIsNotNone(w)
        self.assertTrue(w.isVisible())
        self.assertTrue(w.isFullScreen())
        flags = w.windowFlags()
        self.assertTrue(flags & Qt.WindowStaysOnTopHint)
        self.assertTrue(flags & Qt.FramelessWindowHint)          # no close/minimize buttons
        self.assertEqual(w.windowModality(), Qt.ApplicationModal)
        texts = " ".join(lbl.text() for lbl in (w.brand, w.headline, w.action, w.info, w.status))
        for needed in ("ARES BATTERY GUARDIAN", "BATTERY LIMIT REACHED", "PLEASE REMOVE YOUR CHARGER",
                       "Your battery has reached the configured limit. Disconnect the charger to continue "
                       "using your computer.", "Waiting for charger disconnection..."):
            self.assertIn(needed, texts)
        self.assertIn("90%", w.battery.text())
        self.assertIn("CONNECTED", w.charger.text())

    def test_2_no_window_when_unplugged(self):
        self.m.on_snapshot(snap(95, False))
        self.assertIsNone(self.m.window)
        self.assertEqual(self.lock_windows(), [])

    def test_ordinary_close_is_ignored(self):
        self.m.on_snapshot(snap(90, True))
        w = self.m.window
        self.assertFalse(w.close())                            # Alt+F4 / close request
        self.assertTrue(w.isVisible())
        self.assertIs(self.m.window, w)

    def test_3_unplug_unlocks_within_a_second_and_logs(self):
        self.m.on_snapshot(snap(90, True))
        self.ac[0] = False
        spin(1300)                                             # one fast watch tick (1 s)
        self.assertIsNone(self.m.window)
        self.assertEqual(self.lock_windows(), [])
        events = [r["message"] for r in self.db.recent_events()]
        self.assertTrue(any("ON" in e for e in events))
        self.assertTrue(any("OFF: Charger disconnected" in e for e in events))

    def test_4_stays_locked_while_charger_connected(self):
        self.m.on_snapshot(snap(92, True))
        for pct in (91, 89, 85):
            self.m.on_snapshot(snap(pct, True))
        spin(1300)
        self.assertIsNotNone(self.m.window)
        self.assertTrue(self.m.window.isVisible())

    def test_8_no_duplicate_windows(self):
        for _ in range(30):
            self.m.on_snapshot(snap(95, True))
        spin(1200)
        self.assertEqual(self.m.windows_created, 1)
        self.assertEqual(len(self.lock_windows()), 1)

    def test_9_detection_failure_releases_safely(self):
        self.m.on_snapshot(snap(95, True))
        self.ac[0] = None                                       # probe fails...
        self.m.on_snapshot(snap(95, None))                      # ...and the monitor can't tell either
        self.m._watch_tick()
        self.assertIsNotNone(self.m.window)                     # not instantly
        self.now[0] += self.lw.FAILSAFE_UNKNOWN_S + 1
        self.m._watch_tick()
        self.assertIsNone(self.m.window)
        self.assertTrue(self.db.get_state("strict_lock_suppressed"))
        self.m.on_snapshot(snap(95, True))                      # detection back, still plugged in
        self.assertIsNone(self.m.window)                        # no re-lock this session
        self.m.on_snapshot(snap(95, False))                     # unplug re-arms
        self.m.on_snapshot(snap(95, True))
        self.assertIsNotNone(self.m.window)

    def test_emergency_shortcut_releases(self):
        self.m.on_snapshot(snap(95, True))
        self.m.window.emergency_requested.emit()
        self.assertIsNone(self.m.window)
        self.assertTrue(any("Emergency release" in r["message"] for r in self.db.recent_events()))

    def test_10_shutdown_and_restart_leave_no_lock(self):
        self.m.on_snapshot(snap(95, True))
        self.m.shutdown()                                       # e.g. Exit ARES / crash recovery path
        self.assertEqual(self.lock_windows(), [])
        m2 = self.lw.ProtectionManager(self.db, power_probe=lambda: None, clock=lambda: self.now[0],
                                       player=FakePlayer())
        self.addCleanup(m2.shutdown)
        self.assertIsNone(m2.window)                            # nothing restored from disk
        m2.on_snapshot(snap(95, None))                          # charger unknown at start-up
        self.assertIsNone(m2.window)


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class LockWithBackgroundAppTest(unittest.TestCase):
    """5, 6, 7 with the lock involved, using the real background app (offscreen)."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        use_fake_battery(self)            # never the laptop's real battery (see fake_hw.py)

    def test_lock_works_with_dashboard_closed_and_blocks_dashboard(self):
        from app.ui.background_app import AresBackgroundApp
        from app.ui.lock_window import ProtectionWindow

        with tempfile.TemporaryDirectory() as d:
            db_path = str(Path(d) / "t.db")
            with Database(db_path) as pre:
                pre.set_setting("monitor_interval_s", 5)
            ac = [True]
            bg = AresBackgroundApp(db_path, power_probe=lambda: (None, ac[0]), alarm_player=FakePlayer())
            try:
                bg.start()
                spin(800)
                self.assertIsNone(bg.window)                    # dashboard closed
                bg._on_snapshot(snap(90, True))                 # same path the worker uses
                self.assertTrue(bg.lock.active)
                self.assertTrue(bg.thread.isRunning())          # 5 monitoring continues

                bg.tray.open_dashboard.emit()                   # 6 reopen while locked
                self.assertIsNotNone(bg.window)
                self.assertFalse(bg.window.isEnabled())         # dashboard not usable under the lock
                bg.window.close()                               # closing dashboard keeps lock + monitoring
                spin(300)
                self.assertIsNone(bg.window)
                self.assertTrue(bg.lock.active)

                bg.tray.open_dashboard.emit()
                ac[0] = False                                   # unplug -> unlock
                t0 = time.monotonic()
                while bg.lock.active and time.monotonic() - t0 < 5:
                    spin(50)
                took = time.monotonic() - t0
                # The 1 s reaction itself is checked without the dashboard (LockWindowTest.test_3_...);
                # here it may take a little longer on a busy Windows test run, but it must release.
                self.assertFalse(bg.lock.active, f"still locked after {took:.1f} s; watch timer active="
                                 f"{bg.lock.watch.isActive()} interval={bg.lock.watch.interval()} "
                                 f"ac={bg.lock._ac} snapshot_ac={bg.lock._snapshot_ac}")
                self.assertLess(took, 2.5)
                self.assertTrue(bg.window.isEnabled())          # back to the normal dashboard
                bg._on_snapshot(snap(90, True))                 # plug in again -> new session locks
                self.assertTrue(bg.lock.active)
            finally:
                orig = QApplication.quit
                QApplication.quit = staticmethod(lambda: None)
                try:
                    bg.tray.exit_requested.emit()               # 7 Exit ARES
                finally:
                    QApplication.quit = orig
            spin(300)
            QCoreApplication.sendPostedEvents(None, 0)
            self.assertFalse(bg.thread.isRunning())
            self.assertFalse(bg.lock.active)
            self.assertEqual([w for w in QApplication.topLevelWidgets()
                              if isinstance(w, ProtectionWindow) and w.isVisible()], [])


class EmergencyCommandTest(unittest.TestCase):
    @unittest.skipUnless(HAVE_QT, "PySide6 not installed")
    def test_disable_lock_command_when_not_running(self):
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, ARES_DATA_DIR=d, QT_QPA_PLATFORM="offscreen")
            r = subprocess.run([sys.executable, str(ROOT / "main.py"), "--disable-lock"], env=env, timeout=30,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            with Database(Path(d) / "data" / "battery_guardian.db") as db:
                self.assertFalse(db.get_setting("strict_lock_enabled"))

    @unittest.skipUnless(HAVE_QT, "PySide6 not installed")
    def test_unlock_command_when_not_running_suppresses(self):
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, ARES_DATA_DIR=d, QT_QPA_PLATFORM="offscreen")
            r = subprocess.run([sys.executable, str(ROOT / "main.py"), "--unlock"], env=env, timeout=30,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            with Database(Path(d) / "data" / "battery_guardian.db") as db:
                self.assertTrue(db.get_state("strict_lock_suppressed"))


if __name__ == "__main__":
    unittest.main()
