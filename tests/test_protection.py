"""v1.2 tests: Settings page interactivity/persistence, CSV buttons removed, 90% High and 25% Low
Battery Protection (shared window, alarms, priority, recovery).

Runs headless (QT_QPA_PLATFORM=offscreen). Battery readings, the charger probe and the sound
player are injected, so these tests check ARES' logic and Qt behaviour on the development
machine, not real Windows hardware, winsound playback or the Windows 11 compositor.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.battery_monitor import BatterySnapshot
from app.core.strict_lock import HIGH, LOCK, LOW, UNLOCK, ProtectionPolicy
from app.database.database import Database
from fake_hw import use_fake_battery  # noqa: E402

try:
    from PySide6.QtCore import QCoreApplication, QEventLoop, QPoint, Qt, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QPushButton
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


def snap(pct, ac=True, charging=None, available=True):
    return BatterySnapshot(time.time(), available, "ioctl" if available else "none", percent=pct,
                           ac_online=ac, charging=(bool(ac) and pct < 100) if charging is None else charging,
                           discharging=(ac is False))


def spin(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


class FakePlayer:
    def __init__(self):
        self.played, self.stops, self.volumes = [], 0, []

    def play(self, kind, path, volume=100):
        self.played.append((kind, path))
        self.volumes.append(volume)
        return True, f"fake {kind}"

    def stop(self):
        self.stops += 1


# --------------------------------------------------------------------------------------------
# Pure policy
# --------------------------------------------------------------------------------------------
class LowPolicyTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.p = ProtectionPolicy(self.db)

    def test_low_threshold_detection(self):
        self.assertIsNone(self.p.update(26, False).action)
        d = self.p.update(25, False)                          # "falls below or reaches 25%"
        self.assertEqual((d.action, d.mode), (LOCK, LOW))
        self.assertEqual(self.p.active, LOW)

    def test_low_needs_charger_disconnected(self):
        self.assertIsNone(self.p.update(10, True).action)
        self.assertIsNone(self.p.update(10, None).action)     # unknown never locks
        self.assertIsNone(self.p.active)

    def test_low_unlocks_only_on_plug_in(self):
        self.p.update(20, False)
        for pct in (19, 15, 30, 40):                           # percentage changes alone never unlock
            self.assertIsNone(self.p.update(pct, False).action)
        d = self.p.update(20, True)
        self.assertEqual((d.action, d.mode), (UNLOCK, LOW))
        self.assertIn("connected", d.reason)

    def test_low_once_per_battery_session(self):
        actions = [self.p.update(p, False).action for p in (25, 24, 23, 22, 21)]
        self.assertEqual(actions.count(LOCK), 1)
        self.p.release("emergency")
        self.assertIsNone(self.p.update(15, False).action)    # released: stays off this session
        self.p.update(15, True)                                # plug in: new session
        self.assertEqual(self.p.update(15, False).action, LOCK)

    def test_configurable_and_disable(self):
        self.db.set_setting("low_lock_threshold", 30)
        self.assertEqual(self.p.update(30, False).action, LOCK)
        self.db.set_setting("low_lock_enabled", False)
        self.assertEqual(self.p.update(29, False).action, UNLOCK)
        self.assertIsNone(self.p.update(10, False).action)

    def test_priority_low_then_high_never_both(self):
        # Charger plugged in while LOW is active, battery already above the high limit (odd settings):
        self.db.set_setting("strict_lock_threshold", 50)
        self.db.set_setting("low_lock_threshold", 50)
        self.assertEqual(self.p.update(50, False).mode, LOW)
        d1 = self.p.update(50, True)
        self.assertEqual((d1.action, d1.mode), (UNLOCK, LOW))  # LOW released first...
        d2 = self.p.update(50, True)
        self.assertEqual((d2.action, d2.mode), (LOCK, HIGH))   # ...then HIGH may apply
        self.assertEqual(self.p.active, HIGH)
        d3 = self.p.update(50, False)
        self.assertEqual((d3.action, d3.mode), (UNLOCK, HIGH))
        self.assertEqual(self.p.update(50, False).mode, LOW)   # new battery session

    def test_normal_range_no_protection(self):
        for pct, ac in ((26, False), (89, True), (60, False), (60, True)):
            self.assertIsNone(self.p.update(pct, ac).action)

    def test_restart_restores_suppression_not_lock(self):
        self.p.update(20, False)
        self.p.release("emergency")
        p2 = ProtectionPolicy(self.db)
        self.assertIsNone(p2.active)
        self.assertIsNone(p2.update(20, False).action)
        p2.update(20, True)
        self.assertFalse(self.db.get_state("low_lock_suppressed"))


# --------------------------------------------------------------------------------------------
# Manager + shared window (Qt)
# --------------------------------------------------------------------------------------------
@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class ProtectionManagerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        from app.ui import lock_window as lw

        self.lw = lw
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.reading = [(None, True)]
        self.now = [0.0]
        self.player = FakePlayer()
        self.m = lw.ProtectionManager(self.db, power_probe=lambda: self.reading[0], clock=lambda: self.now[0],
                                      player=self.player)
        self.addCleanup(self.m.shutdown)

    def windows(self):
        QCoreApplication.sendPostedEvents(None, 0)
        return [w for w in QApplication.topLevelWidgets()
                if isinstance(w, self.lw.ProtectionWindow) and w.isVisible()]

    # ---- high ---------------------------------------------------------------------------
    def test_high_alarm_and_lock_then_unplug_unlocks_and_stops_alarm(self):
        self.db.set_setting("high_alarm_sound_path", "C:/Windows/Media/Alarm01.wav")
        self.m.on_snapshot(snap(89, True))
        self.assertIsNone(self.m.window)
        self.assertEqual(self.player.played, [])
        self.m.on_snapshot(snap(90, True))
        self.assertEqual(self.m.mode, HIGH)
        self.assertEqual(self.player.played, [(HIGH, "C:/Windows/Media/Alarm01.wav")])
        w = self.m.window
        self.assertTrue(w.isFullScreen())
        self.assertIn("90%", w.battery.text())
        self.assertIn("CONNECTED", w.charger.text())
        stops = self.player.stops
        self.reading[0] = (90, False)
        spin(1300)
        self.assertIsNone(self.m.window)
        self.assertGreater(self.player.stops, stops)            # alarm stopped when cleared
        self.assertFalse(self.m.alarm_timer.isActive())
        msgs = [r["message"] for r in self.db.recent_events()]
        self.assertTrue(any("High Battery Protection ON" in e for e in msgs))
        self.assertTrue(any("High Battery Protection OFF: Charger disconnected" in e for e in msgs))

    # ---- low ----------------------------------------------------------------------------
    def test_low_alarm_lock_texts_and_plug_in_unlocks(self):
        self.reading[0] = (None, False)
        self.m.on_snapshot(snap(26, False))
        self.assertIsNone(self.m.window)
        self.m.on_snapshot(snap(25, False))
        w = self.m.window
        self.assertIsNotNone(w)
        self.assertEqual(w.mode, LOW)
        self.assertEqual(self.player.played, [(LOW, None)])     # LOW default sound (different from HIGH)
        texts = " ".join(lbl.text() for lbl in (w.headline, w.action, w.info, w.status, w.battery, w.charger))
        for needed in ("LOW BATTERY – 25%", "PLEASE CONNECT YOUR CHARGER",
                       "Your battery is running low. Connect your charger to continue using your computer.",
                       "Waiting for charger connection...", "Battery: 25%", "discharging",
                       "Charger: DISCONNECTED"):
            self.assertIn(needed, texts)
        self.assertTrue(w.isFullScreen())
        self.assertTrue(w.windowFlags() & Qt.WindowStaysOnTopHint)
        self.assertTrue(w.windowFlags() & Qt.FramelessWindowHint)
        self.assertEqual(w.windowModality(), Qt.ApplicationModal)
        self.assertFalse(w.close())                              # no ordinary close
        self.reading[0] = (24, False)
        spin(1200)
        self.assertIn("24%", w.battery.text())                   # live update while shown
        self.reading[0] = (24, True)
        spin(1300)
        self.assertIsNone(self.m.window)
        self.assertEqual(self.windows(), [])
        msgs = [r["message"] for r in self.db.recent_events()]
        self.assertTrue(any("Low Battery Protection ON" in e for e in msgs))
        self.assertTrue(any("Low Battery Protection OFF: Charger connected" in e for e in msgs))
        self.assertTrue(any(r["kind"] == "low_battery_lock" for r in self.db.recent_events()))

    def test_no_duplicate_windows_and_no_alarm_flooding(self):
        self.reading[0] = (None, False)
        for _ in range(30):
            self.m.on_snapshot(snap(20, False))
        spin(1200)
        self.assertEqual(self.m.windows_created, 1)
        self.assertEqual(len(self.windows()), 1)
        self.assertEqual(len(self.player.played), 1)
        alarm_events = [r for r in self.db.recent_events() if "alarm" in r["message"]]
        self.assertEqual(len(alarm_events), 1)

    def test_repeat_interval_controlled(self):
        self.db.set_setting("low_alarm_repeat_min", 2)
        self.reading[0] = (None, False)
        self.m.on_snapshot(snap(20, False))
        self.assertTrue(self.m.alarm_timer.isActive())
        self.assertEqual(self.m.alarm_timer.interval(), 2 * 60_000)
        self.m._alarm_repeat()                                   # what the timer does every 2 min
        self.assertEqual(len(self.player.played), 2)
        self.db.set_setting("low_alarm_repeat_min", 0)           # once per session
        self.m.settings_changed()
        self.assertFalse(self.m.alarm_timer.isActive())

    def test_sound_master_switch(self):
        self.db.set_setting("sound_enabled", False)
        self.m.on_snapshot(snap(95, True))
        self.assertIsNotNone(self.m.window)                      # screen still shown
        self.assertEqual(self.player.played, [])

    def test_priority_switch_keeps_single_window(self):
        self.db.set_setting("strict_lock_threshold", 50)
        self.db.set_setting("low_lock_threshold", 50)
        self.reading[0] = (None, False)
        self.m.on_snapshot(snap(50, False))
        self.assertEqual(self.m.mode, LOW)
        self.m.on_snapshot(snap(50, True))                       # plug in: LOW released, HIGH applies
        self.assertEqual(self.m.mode, HIGH)
        self.assertEqual(len(self.windows()), 1)
        self.assertEqual(self.m.windows_created, 2)

    def test_emergency_shortcut_and_failsafe_on_low(self):
        self.reading[0] = (None, False)
        self.m.on_snapshot(snap(20, False))
        self.m.window.emergency_requested.emit()                 # Ctrl+Alt+Shift+U
        self.assertIsNone(self.m.window)
        self.assertTrue(self.db.get_state("low_lock_suppressed"))
        self.m.on_snapshot(snap(19, False))
        self.assertIsNone(self.m.window)                         # stays off until plug-in
        self.m.on_snapshot(snap(19, True))
        self.m.on_snapshot(snap(19, False))
        self.assertIsNotNone(self.m.window)                      # new session
        self.reading[0] = None                                   # detection fails completely
        self.m.on_snapshot(snap(19, None))
        self.m._watch_tick()
        self.assertIsNotNone(self.m.window)
        self.now[0] += self.lw.FAILSAFE_UNKNOWN_S + 1
        self.m._watch_tick()
        self.assertIsNone(self.m.window)                         # safe timeout released it

    def test_real_shortcut_key_sequence(self):
        self.m.on_snapshot(snap(95, True))
        w = self.m.window
        w.activateWindow()
        self.assertTrue(QTest.qWaitForWindowActive(w, 2000))
        QTest.keyClick(w, Qt.Key_U, Qt.ControlModifier | Qt.AltModifier | Qt.ShiftModifier)
        QCoreApplication.processEvents()
        self.assertIsNone(self.m.window)

    def test_idle_poll_detects_without_monitoring_snapshots(self):
        self.m.start()
        self.assertTrue(self.m.watch.isActive())
        self.reading[0] = (22, False)
        self.m._watch_tick()                                     # the 5 s idle poll
        self.assertEqual(self.m.mode, LOW)


# --------------------------------------------------------------------------------------------
# Settings page
# --------------------------------------------------------------------------------------------
@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class SettingsPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "s.db")
        self.db = Database(self.db_path)
        self.addCleanup(self.db.close)
        self.player = FakePlayer()
        # Startup registration is a Windows side effect: replace it with an in-memory registration so
        # these tests never read or write the real registry / Task Scheduler (see test_startup.py
        # for the registration logic itself).
        from app.core import startup_manager as sm
        reg = {"on": False}

        def apply(db, wanted, source=""):
            reg["on"] = bool(wanted)
            db.set_state("startup_last_result", {"ok": True, "message": "test", "wanted": bool(wanted)})
            return True, "test"
        for p in (mock.patch.object(sm, "apply_preference", apply),
                  mock.patch.object(sm, "status", lambda: sm.StartupStatus(
                      reg["on"], "Registry Run key (current user)" if reg["on"] else "Disabled",
                      None, None, [], reg["on"]))):
            p.start()
            self.addCleanup(p.stop)

    def page(self, db=None):
        from app.ui.settings import SettingsPage

        pg = SettingsPage(db or self.db, player=self.player)
        pg.resize(900, 2400)
        pg.show()
        self.addCleanup(pg.deleteLater)
        self.addCleanup(pg.close)
        QCoreApplication.processEvents()
        return pg

    def test_every_checkbox_responds_to_clicks_and_saves_on_save(self):
        pg = self.page()
        keys = []
        pg.setting_changed.connect(keys.append)
        flipped = {}
        for key, cb in pg._checks.items():
            self.assertTrue(cb.isEnabled(), key)
            before = cb.isChecked()
            QTest.mouseClick(cb, Qt.LeftButton, Qt.NoModifier, QPoint(8, cb.height() // 2))
            self.assertNotEqual(cb.isChecked(), before, key)
            flipped[key] = cb.isChecked()
        before = pg.autostart.isChecked()
        QTest.mouseClick(pg.autostart, Qt.LeftButton, Qt.NoModifier, QPoint(8, pg.autostart.height() // 2))
        self.assertEqual(self.db.saved_settings(), {})              # staged only: nothing written yet
        self.assertEqual(keys, [])
        self.assertIn("Unsaved", pg.status_lbl.text())
        QTest.mouseClick(pg.save_btn, Qt.LeftButton)
        for key, value in flipped.items():
            self.assertEqual(bool(self.db.get_setting(key)), value, key)
            self.assertIn(key, keys)
        self.assertEqual(bool(self.db.get_setting("autostart")), not before)
        self.assertIn("All changes saved", pg.status_lbl.text())
        self.assertFalse(pg.is_dirty())

    def test_every_spinbox_accepts_changes_and_saves_on_save(self):
        pg = self.page()
        for key, sp in pg._spins.items():
            if key in ("max_charge", "min_charge"):
                continue
            step = sp.singleStep()
            target = sp.value() - step if sp.value() - step >= sp.minimum() else sp.value() + step
            sp.setFocus()
            QTest.keyClick(sp, Qt.Key_Down if target < sp.value() else Qt.Key_Up)
            self.assertEqual(sp.value(), target, key)
        self.assertTrue(pg.save(), pg.status_lbl.text())
        for key, sp in pg._spins.items():
            self.assertEqual(int(self.db.get_setting(key)), sp.value(), key)

    def test_thresholds_validated_before_saving(self):
        pg = self.page()
        pg.max_spin.setValue(80)
        self.assertTrue(pg.save())
        self.assertEqual(self.db.get_setting("max_charge"), 80)
        pg.min_spin.setValue(80)                                 # resume >= max -> rejected, not saved
        pg.lock_threshold.setValue(70)                           # a valid change in the same save
        self.assertFalse(pg.save())
        self.assertEqual(self.db.get_setting("min_charge"), 25)
        self.assertEqual(self.db.get_setting("strict_lock_threshold"), 90)   # all or nothing
        self.assertIn("Save failed", pg.status_lbl.text())
        self.assertTrue(pg.is_dirty())                           # the edits are still shown
        pg.low_threshold.setValue(50)
        pg.min_spin.setValue(25)
        pg.lock_threshold.setValue(50)                           # low protection must stay below high
        self.assertFalse(pg.save())
        self.assertIn("Low Battery Protection threshold", pg.status_lbl.text())
        pg.lock_threshold.setValue(90)
        pg.wake_low.setValue(50)
        pg.wake_high.setValue(50)
        self.assertFalse(pg.save())
        self.assertIn("Smart Wake-Up low threshold", pg.status_lbl.text())

    def test_browse_selects_wav_and_test_plays_it(self):
        pg = self.page()
        wav = Path(self.tmp.name) / "my alarm.wav"
        wav.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt ")
        with mock.patch("app.ui.settings.QFileDialog.getOpenFileName", return_value=(str(wav), "")) as dlg:
            pg.high_alarm.browse.click()
        self.assertTrue(dlg.called)
        self.assertIn("*.wav", dlg.call_args[0][3])
        self.assertEqual(pg.high_alarm.value(), str(wav))
        pg.high_volume.setValue(40)
        pg.high_alarm.test.click()                               # Test uses the SELECTED (unsaved) sound
        self.assertEqual(self.player.played[-1], (HIGH, str(wav)))
        self.assertEqual(self.player.volumes[-1], 40)
        self.assertTrue(pg.save())
        self.assertEqual(self.db.get_setting("high_alarm_sound_path"), str(wav))
        self.assertEqual(self.db.get_setting("high_alarm_volume"), 40)
        pg.low_alarm.test.click()                                # default low sound
        self.assertEqual(self.player.played[-1], (LOW, None))
        missing = str(Path(self.tmp.name) / "missing.wav")
        pg.low_alarm.set_path(missing)
        self.assertIn("not found", pg.low_alarm.status.text())   # handled gracefully
        self.assertIn("never replaced silently", pg.low_alarm.status.text())
        self.assertTrue(pg.save())
        self.assertEqual(self.db.get_setting("low_alarm_sound_path"), missing)   # kept as chosen
        pg.low_alarm.clear.click()
        self.assertTrue(pg.save())
        self.assertEqual(self.db.get_setting("low_alarm_sound_path"), "")

    def test_cctk_browse_and_profile_dropdown(self):
        pg = self.page()
        redetect = []
        pg.setting_changed.connect(lambda k: redetect.append(k))
        with mock.patch("app.ui.settings.QFileDialog.getOpenFileName", return_value=("C:/x/cctk.exe", "")):
            pg.cctk_browse.click()
        self.assertTrue(pg.save())
        self.assertEqual(self.db.get_setting("cctk_path"), "C:/x/cctk.exe")
        self.assertIn("cctk_path", redetect)
        idx = pg.profile.findText("Balanced")
        pg.profile.setCurrentIndex(idx)                          # remembered on Save, NOT applied
        with mock.patch("app.services.power_profile.apply_profile") as ap:
            self.assertTrue(pg.save())
        ap.assert_not_called()
        self.assertEqual(self.db.get_setting("power_profile"), "Balanced")
        with mock.patch("app.ui.settings.QMessageBox.question", return_value=__import__(
                "PySide6.QtWidgets", fromlist=["QMessageBox"]).QMessageBox.Yes), \
                mock.patch("app.ui.settings.QMessageBox.information"), \
                mock.patch("app.services.power_profile.apply_profile", return_value=(True, "done")) as ap:
            idx = pg.profile.findText("Battery Saver")
            self.assertGreaterEqual(idx, 0)
            pg.profile.setCurrentIndex(idx)
            pg.profile_btn.click()                               # "Apply profile now…"
        ap.assert_called_once_with("Battery Saver")
        self.assertEqual(self.db.get_setting("power_profile"), "Battery Saver")
        self.assertFalse(pg.is_dirty())

    def test_values_persist_across_restart_and_reopen(self):
        pg = self.page()
        pg.lock_threshold.setValue(85)
        pg.low_threshold.setValue(30)
        pg.high_repeat.setValue(5)
        pg.low_enabled.setChecked(False)
        pg.sound_enabled.setChecked(False)
        self.assertTrue(pg.save())
        self.db.close()                                          # "restart ARES"
        db2 = Database(self.db_path)
        self.addCleanup(db2.close)
        pg2 = self.page(db2)
        self.assertEqual(pg2.lock_threshold.value(), 85)
        self.assertEqual(pg2.low_threshold.value(), 30)
        self.assertEqual(pg2.high_repeat.value(), 5)
        self.assertFalse(pg2.low_enabled.isChecked())
        self.assertFalse(pg2.sound_enabled.isChecked())
        from app.database.database import migrate_settings
        migrate_settings(db2)                                    # upgrade path must not overwrite
        self.assertEqual(db2.get_setting("strict_lock_threshold"), 85)

    def test_load_does_not_write(self):
        pg = self.page()
        keys = []
        pg.setting_changed.connect(keys.append)
        pg.load()
        QCoreApplication.processEvents()
        self.assertEqual(keys, [])


# --------------------------------------------------------------------------------------------
# Background app integration
# --------------------------------------------------------------------------------------------
@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class IntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        use_fake_battery(self)            # never the laptop's real battery (see fake_hw.py)

    def _run(self, body):
        from app.ui.background_app import AresBackgroundApp

        with tempfile.TemporaryDirectory() as d:
            db_path = str(Path(d) / "t.db")
            with Database(db_path) as pre:
                pre.set_setting("monitor_interval_s", 30)
                pre.execute("INSERT INTO samples(ts, percent) VALUES(?, ?)", (time.time(), 55))
            reading = [(None, False)]
            player = FakePlayer()
            # Exceptions raised inside Qt slots don't fail a test by themselves (Qt just prints them),
            # so collect them through sys.excepthook and fail explicitly.
            errors = []
            old_hook = sys.excepthook
            sys.excepthook = lambda *exc: errors.append(exc)
            try:
                bg = AresBackgroundApp(db_path, power_probe=lambda: reading[0], alarm_player=player)
                try:
                    bg.start()
                    spin(500)
                    body(bg, reading, player)
                finally:
                    bg.shutdown()
                spin(300)                                        # late queued worker signals arrive here
                QCoreApplication.sendPostedEvents(None, 0)
            finally:
                sys.excepthook = old_hook
            self.assertEqual(errors, [], "exception in a Qt callback: %r" % (errors[:1],))

    def test_csv_buttons_removed_data_kept(self):
        def body(bg, reading, player):
            bg.open_dashboard(1)                                 # History tab
            texts = [b.text() for b in bg.window.findChildren(QPushButton)]
            for gone in ("Export samples CSV", "Export sessions CSV", "Export events CSV"):
                self.assertNotIn(gone, texts)
            self.assertFalse(any("CSV" in t for t in texts))
            rows = bg.db.query("SELECT COUNT(*) AS n FROM samples")[0]["n"]
            self.assertGreaterEqual(rows, 1)                     # history data untouched
            from app.services import report_service
            with tempfile.TemporaryDirectory() as d2:
                self.assertGreaterEqual(report_service.export_samples_csv(bg.db, Path(d2) / "s.csv"), 1)
        self._run(body)

    def test_low_protection_with_dashboard_closed_and_live_settings(self):
        def body(bg, reading, player):
            self.assertIsNone(bg.window)                         # dashboard closed
            bg._on_snapshot(snap(20, False))                     # same path the worker uses
            self.assertEqual(bg.lock.mode, LOW)
            self.assertTrue(bg.thread.isRunning())               # monitoring continues in the background
            self.assertEqual(player.played[0][0], LOW)
            bg.open_dashboard(4)                                 # Settings tab, under the lock
            self.assertFalse(bg.window.isEnabled())
            bg.lock.emergency_release("test")                    # so the page can be used in the test
            self.assertTrue(bg.window.isEnabled())
            sp = bg.window.settings_page
            sp.lock_enabled.setChecked(True)
            bg._on_snapshot(snap(95, True))                      # plug in, high
            self.assertEqual(bg.lock.mode, HIGH)
            sp.lock_enabled.setChecked(False)                    # staged: not applied before Save
            self.assertIsNotNone(bg.lock.window)
            self.assertTrue(sp.save())                           # Save applies it immediately
            self.assertIsNone(bg.lock.window)
            sp.interval.setValue(45)
            self.assertTrue(sp.save())
            spin(1300)                                           # worker push delay
            self.assertEqual(bg.worker.timer.interval(), 45_000)
        self._run(body)


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class ShutdownLifecycleTest(unittest.TestCase):
    """Regression: a battery reading queued by the worker just before Exit was delivered after
    shutdown() had closed the database -> AttributeError in strict_lock.py (seen on Windows)."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        use_fake_battery(self)            # never the laptop's real battery (see fake_hw.py)

    def test_late_worker_signals_after_shutdown_are_ignored(self):
        from app.ui.background_app import AresBackgroundApp

        errors = []
        old_hook = sys.excepthook
        sys.excepthook = lambda *exc: errors.append(exc)
        self.addCleanup(setattr, sys, "excepthook", old_hook)
        with tempfile.TemporaryDirectory() as d:
            bg = AresBackgroundApp(str(Path(d) / "t.db"), power_probe=lambda: (95, True), alarm_player=FakePlayer())
            bg.start()
            spin(300)
            bg.shutdown()
            # The same deliveries a still-queued worker event would make after shutdown:
            bg.worker.snapshot.emit(snap(95, True))
            bg.worker.status.emit(None)
            bg.worker.monitoring_changed.emit(False)
            QTimer.singleShot(0, lambda: bg.worker.snapshot.emit(snap(20, False)))
            spin(200)
            bg.lock._watch_tick()
            bg.lock.settings_changed()
            bg.on_setting_changed("strict_lock_enabled")
            self.assertIsNone(bg.lock.window)
        self.assertEqual(errors, [])

    def test_closed_database_reports_clearly(self):
        import sqlite3
        db = Database(":memory:")
        db.close()
        with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed"):
            db.get_setting("max_charge")
        db.close()                                               # idempotent


class EmergencyCommandLowTest(unittest.TestCase):
    @unittest.skipUnless(HAVE_QT, "PySide6 not installed")
    def test_unlock_and_disable_cover_both_modes(self):
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, ARES_DATA_DIR=d, QT_QPA_PLATFORM="offscreen")
            r = subprocess.run([sys.executable, str(ROOT / "main.py"), "--unlock"], env=env, timeout=30,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            r = subprocess.run([sys.executable, str(ROOT / "main.py"), "--disable-lock"], env=env, timeout=30,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            with Database(Path(d) / "data" / "battery_guardian.db") as db:
                self.assertTrue(db.get_state("low_lock_suppressed"))
                self.assertFalse(db.get_setting("low_lock_enabled"))
                self.assertFalse(db.get_setting("strict_lock_enabled"))


if __name__ == "__main__":
    unittest.main()
