"""Smart Wake-Up Protection tests (SIMULATED).

Everything here runs with fakes: the Task Scheduler is an in-memory fake, the battery and
charger readings are injected, the PC capabilities are fake GetPwrCapabilities structures and
SetThreadExecutionState is recorded instead of called. These tests prove ARES' decisions,
task definition, safeguards and process wiring. They do NOT prove that a real laptop wakes
from Sleep: that is the manual hardware test in docs/V2_SMART_WAKE_AND_SETTINGS.md.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core import smart_wake as sw
from app.core.power_request import ES_CONTINUOUS, ES_DISPLAY_REQUIRED, ES_SYSTEM_REQUIRED, PowerRequest
from app.database.database import DEFAULT_SETTINGS, Database
from fake_hw import use_fake_battery  # noqa: E402

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


class FakeScheduler:
    """In-memory stand-in for the one Task Scheduler task (same interface as sw.TaskScheduler)."""

    def __init__(self, fail: str | None = None):
        self.tasks: dict[str, str] = {}
        self.next_runs: list[datetime] = []
        self.registers = self.unregisters = 0
        self.fail = fail

    def exists(self):
        return sw.TASK_NAME in self.tasks

    def query_xml(self):
        return self.tasks.get(sw.TASK_NAME)

    def register(self, next_run):
        self.registers += 1
        if self.fail:
            return False, self.fail
        exe, args = sw.check_command()
        self.tasks[sw.TASK_NAME] = sw.build_task_xml(exe, args, next_run, "PC\\user")   # /F: replace
        self.next_runs.append(next_run)
        return True, "SUCCESS"

    def unregister(self):
        self.unregisters += 1
        self.tasks.pop(sw.TASK_NAME, None)
        return True, "removed"


def settings(**kw):
    s = dict(DEFAULT_SETTINGS, wake_enabled=True)
    s.update(kw)
    return s


def caps(**kw):
    base = dict(SystemS3=1, SystemS4=1, HiberFilePresent=1, AoAc=0, WakeAlarmPresent=1, RtcWake=4)
    base.update(kw)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------------------------
# Decisions (pure)
# --------------------------------------------------------------------------------------------
class DecideTest(unittest.TestCase):
    now = 1_000_000.0

    def d(self, pct, ac, recent=(), **kw):
        return sw.decide(pct, ac, settings(**kw), list(recent), self.now)

    def test_off_removes_task(self):
        r = sw.decide(20, False, settings(wake_enabled=False), [], self.now)
        self.assertIsNone(r.action)
        self.assertIsNone(r.next_minutes)

    def test_low_trigger_on_battery_only(self):
        self.assertEqual(self.d(25, False).action, "low")
        self.assertEqual(self.d(10, False).action, "low")
        self.assertIsNone(self.d(26, False).action)
        self.assertIsNone(self.d(20, True).action)              # charger connected: no low trigger
        self.assertIsNone(self.d(20, False, wake_low_enabled=False).action)

    def test_high_trigger_on_charger_only(self):
        self.assertEqual(self.d(89, True).action, "high")
        self.assertEqual(self.d(100, True).action, "high")
        self.assertIsNone(self.d(88, True).action)
        self.assertIsNone(self.d(95, False).action)             # on battery: no high trigger
        self.assertIsNone(self.d(95, True, wake_high_enabled=False).action)

    def test_custom_thresholds(self):
        self.assertEqual(self.d(30, False, wake_low_threshold=30).action, "low")
        self.assertIsNone(self.d(89, True, wake_high_threshold=95).action)

    def test_missing_telemetry_is_safe(self):
        for pct, ac in ((None, True), (50, None), (None, None)):
            r = self.d(pct, ac)
            self.assertIsNone(r.action)
            self.assertEqual(r.next_minutes, 5)
            self.assertIn("unavailable", r.reason)

    def test_interval_is_clamped(self):
        self.assertEqual(sw.base_interval(settings(wake_interval_min=1)), sw.MIN_INTERVAL_MIN)
        self.assertEqual(sw.base_interval(settings(wake_interval_min=500)), sw.MAX_INTERVAL_MIN)
        self.assertEqual(self.d(89, True, wake_interval_min=10).next_minutes, 10)

    def test_battery_drain_safeguards(self):
        self.assertEqual(self.d(90, False).next_minutes, 30)    # far above the low threshold: x6
        self.assertEqual(self.d(45, False).next_minutes, 15)    # x3
        self.assertEqual(self.d(30, False).next_minutes, 5)     # close: base interval
        self.assertEqual(self.d(30, True).next_minutes, 30)     # far below the high threshold on charger
        self.assertEqual(self.d(80, True).next_minutes, 5)
        r = self.d(4, False, wake_low_enabled=False)            # nearly empty: only the hourly backstop
        self.assertEqual(r.next_minutes, sw.BACKSTOP_MIN)
        self.assertIn("paused", r.reason)

    def test_wake_loop_cap(self):
        recent = [self.now - i * 60 for i in range(sw.MAX_CHECKS_PER_HOUR + 1)]
        r = self.d(20, False, recent)
        self.assertIsNone(r.action)
        self.assertEqual(r.next_minutes, sw.MAX_INTERVAL_MIN)
        old = [self.now - 4000 - i for i in range(50)]          # older than an hour: not counted
        self.assertEqual(self.d(20, False, old).action, "low")


# --------------------------------------------------------------------------------------------
# Capability detection
# --------------------------------------------------------------------------------------------
class SupportTest(unittest.TestCase):
    def test_s3_laptop_with_timers_enabled(self):
        s = sw.support_from_caps(caps(), ("enabled", "enabled"))
        self.assertTrue(s.can_wake_from_sleep and s.usable_on_ac and s.usable_on_battery)
        self.assertEqual(s.sleep_state, "S3 Standby")
        self.assertEqual(s.rtc_wake_state, "S3 Sleep")
        self.assertTrue(s.hibernate)
        self.assertTrue(any("Hibernate" in p for p in s.problems()))   # never promised

    def test_timers_disabled_on_battery(self):
        s = sw.support_from_caps(caps(), ("enabled", "disabled"))
        self.assertTrue(s.usable_on_ac)
        self.assertFalse(s.usable_on_battery)
        self.assertTrue(any("on battery is disabled" in p for p in s.problems()))

    def test_modern_standby(self):
        s = sw.support_from_caps(caps(SystemS3=0, AoAc=1), ("enabled", "enabled"))
        self.assertIn("Modern Standby", s.sleep_state)
        self.assertTrue(any("Modern Standby" in p for p in s.problems()))

    def test_unsupported_sleep_state(self):
        s = sw.support_from_caps(caps(SystemS3=0, AoAc=0), ("enabled", "enabled"))
        self.assertFalse(s.can_wake_from_sleep)
        self.assertFalse(s.usable_on_ac)
        self.assertTrue(any("no sleep state" in p for p in s.problems()))

    def test_no_wake_alarm(self):
        s = sw.support_from_caps(caps(WakeAlarmPresent=0), ("enabled", "enabled"))
        self.assertFalse(s.can_wake_from_sleep)

    def test_powercfg_parse_any_language(self):
        en = ("Power Setting GUID: bd3b718a-0680-4d9d-8ab2-e1d2b4ac806d  (Allow wake timers)\n"
              "  Possible Setting Index: 000\n  Current AC Power Setting Index: 0x00000001\n"
              "  Current DC Power Setting Index: 0x00000000\n")
        de = en.replace("Current AC Power Setting Index", "Index der aktuellen Wechselstromeinstellung")
        self.assertEqual(sw.parse_powercfg_rtc(en), ("enabled", "disabled"))
        self.assertEqual(sw.parse_powercfg_rtc(de), ("enabled", "disabled"))
        self.assertEqual(sw.parse_powercfg_rtc("0x00000002 0x00000002"), ("important only", "important only"))
        self.assertEqual(sw.parse_powercfg_rtc(""), (None, None))

    def test_detect_off_windows(self):
        with mock.patch.object(sw.si, "IS_WINDOWS", False):
            s = sw.detect()
        self.assertFalse(s.windows)
        self.assertFalse(s.can_wake_from_sleep)
        self.assertIn("needs Windows", s.summary())


# --------------------------------------------------------------------------------------------
# Task definition and registration
# --------------------------------------------------------------------------------------------
class TaskTest(unittest.TestCase):
    def test_task_xml(self):
        nr = datetime(2026, 11, 21, 22, 5, 0)
        xml = sw.build_task_xml(r"C:\Program Files\ARES & Co\ARES.exe", ["--wake-check"], nr, "PC\\me")
        for must in ("<WakeToRun>true</WakeToRun>", "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>",
                     "<StartWhenAvailable>true</StartWhenAvailable>", "<RunLevel>LeastPrivilege</RunLevel>",
                     "<LogonType>InteractiveToken</LogonType>",
                     "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>",
                     "<StartBoundary>2026-11-21T22:05:00</StartBoundary>",
                     "<StartBoundary>2026-11-21T23:05:00</StartBoundary>",      # hourly backstop
                     "<Interval>PT60M</Interval>", "ARES &amp; Co"):
            self.assertIn(must, xml)
        facts = sw.task_facts(xml)
        self.assertTrue(facts["wake_to_run"])
        self.assertEqual(facts["command"], r"C:\Program Files\ARES & Co\ARES.exe")
        self.assertEqual(facts["arguments"], "--wake-check")
        self.assertEqual(facts["next_run"], "2026-11-21T22:05:00")
        self.assertEqual(sw.task_facts(None), {})

    def test_check_command_has_no_autostart(self):
        exe, args = sw.check_command()
        self.assertEqual(args[-1], sw.WAKE_ARG)
        self.assertNotIn("--autostart", args)

    def test_ensure_register_refresh_remove_without_duplicates(self):
        db = Database(":memory:")
        self.addCleanup(db.close)
        fs = FakeScheduler()
        ok, msg = sw.ensure(db, fs)                         # off by default: nothing registered
        self.assertTrue(ok)
        self.assertEqual(fs.tasks, {})
        db.set_setting("wake_enabled", True)
        now = datetime(2026, 11, 21, 12, 0)
        for _ in range(3):
            self.assertTrue(sw.ensure(db, fs, now)[0])
        self.assertEqual(len(fs.tasks), 1)                  # replaced, never duplicated
        self.assertEqual(fs.next_runs[-1], now + timedelta(minutes=5))
        db.set_setting("wake_enabled", False)
        ok, msg = sw.ensure(db, fs)
        self.assertTrue(ok)
        self.assertEqual(fs.tasks, {})
        self.assertIn("removed", msg)

    def test_ensure_reports_failures_and_never_raises(self):
        db = Database(":memory:")
        self.addCleanup(db.close)
        db.set_setting("wake_enabled", True)
        ok, msg = sw.ensure(db, FakeScheduler(fail="Access is denied."))
        self.assertFalse(ok)
        self.assertIn("Access is denied", msg)
        broken = FakeScheduler()
        broken.register = mock.Mock(side_effect=OSError("schtasks missing"))
        ok, msg = sw.ensure(db, broken)
        self.assertFalse(ok)
        self.assertIn("schtasks missing", msg)

    def test_real_scheduler_is_inert_off_windows(self):
        if sys.platform == "win32":
            self.skipTest("only meaningful off Windows")
        ts = sw.TaskScheduler()
        self.assertFalse(ts.exists())
        self.assertIsNone(ts.query_xml())
        self.assertFalse(ts.register(datetime.now())[0])

    def test_test_configuration(self):
        db = Database(":memory:")
        self.addCleanup(db.close)
        good = sw.support_from_caps(caps(), ("enabled", "enabled"))
        fs = FakeScheduler()
        ok, lines = sw.test_configuration(db, fs, good)      # off and no task: consistent
        self.assertTrue(ok, lines)
        db.set_setting("wake_enabled", True)
        ok, lines = sw.test_configuration(db, fs, good)      # on but no task
        self.assertFalse(ok)
        self.assertTrue(any("not registered" in x for x in lines))
        sw.ensure(db, fs)
        ok, lines = sw.test_configuration(db, fs, good)
        self.assertTrue(ok, lines)
        self.assertTrue(any("registered; next check" in x for x in lines))
        self.assertTrue(any("does not put the PC to sleep" in x for x in lines))   # no hardware claim
        ok, _ = sw.test_configuration(db, fs, sw.support_from_caps(caps(SystemS3=0), ("enabled", "enabled")))
        self.assertFalse(ok)                                  # no sleep state that can be woken
        db.set_setting("wake_enabled", False)
        ok, lines = sw.test_configuration(db, fs, good)      # off but a task is left over
        self.assertFalse(ok)
        ok, lines = sw.test_configuration(db, fs, sw.WakeSupport(windows=False))
        self.assertFalse(ok)


# --------------------------------------------------------------------------------------------
# The check process
# --------------------------------------------------------------------------------------------
class RunCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "w.db")
        self.addCleanup(self.db.close)
        self.db.set_setting("wake_enabled", True)
        self.fs = FakeScheduler()
        self.sent, self.launched = [], []
        self.running = True
        self.t = [1_000_000.0]
        self.reading = (50.0, True)

    def check(self, lock=None):
        return sw.run_check(self.db, probe=lambda: self.reading,
                            send=lambda m: (self.sent.append(m), self.running)[1],
                            launch=lambda a: (self.launched.append(a), True)[1],
                            scheduler=self.fs, lock=lock, clock=lambda: self.t[0])

    def events(self, kind="smart_wake"):
        return [r["message"] for r in self.db.recent_events() if r["kind"] == kind]

    def test_low_after_wake_activates_running_instance(self):
        self.reading = (22.0, False)
        r = self.check()
        self.assertEqual(r.action, "low")
        self.assertEqual(self.sent, [b"wake:low"])
        self.assertEqual(self.launched, [])                  # reused, no second ARES
        self.assertIn("already running", r.result)
        self.assertEqual(len(self.fs.tasks), 1)              # next check scheduled
        self.assertEqual(self.fs.next_runs[-1], datetime.fromtimestamp(self.t[0] + 300))

    def test_high_after_wake_starts_ares_when_not_running(self):
        self.reading = (89.0, True)
        self.running = False
        r = self.check()
        self.assertEqual(r.action, "high")
        self.assertEqual(self.sent, [b"wake:high"])
        self.assertEqual(self.launched, ["high"])
        self.assertIn("ARES was started", r.result)

    def test_no_trigger_just_reschedules(self):
        self.reading = (60.0, True)
        r = self.check()
        self.assertIsNone(r.action)
        self.assertEqual(self.sent, [])
        self.assertEqual(r.next_due, self.t[0] + 15 * 60)    # adaptive (x3)
        self.assertEqual(self.events(), [])                  # quiet checks are not logged

    def test_repeated_trigger_logged_once(self):
        self.reading = (20.0, False)
        for _ in range(3):
            self.check()
            self.t[0] += 300
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(len(self.sent), 3)

    def test_cancellation_removes_task(self):
        self.check()
        self.assertEqual(len(self.fs.tasks), 1)
        self.db.set_setting("wake_enabled", False)
        self.reading = (10.0, False)
        r = self.check()
        self.assertIsNone(r.action)                          # off: never triggers
        self.assertEqual(self.fs.tasks, {})
        self.assertIsNone(r.next_due)
        self.assertEqual(self.sent, [])

    def test_missed_check_is_detected(self):
        self.check()
        self.t[0] += 3 * 3600                                # PC was off / hibernated
        r = self.check()
        self.assertGreaterEqual(r.late_min, 160)
        self.assertIn("late", r.text())
        self.assertIn("late", sw.last_check_text(self.db))

    def test_missing_telemetry(self):
        self.reading = None
        r = self.check()
        self.assertIsNone(r.action)
        self.assertIsNone(r.percent)
        self.assertEqual(self.sent, [])
        self.assertIn("N/A", r.text())

    def test_schedule_failure_is_reported(self):
        self.fs.fail = "Access is denied."
        r = self.check()
        self.assertIn("could not schedule", r.result)
        self.assertTrue(self.events("warning"))

    def test_rate_cap_across_checks(self):
        self.reading = (20.0, False)
        for _ in range(sw.MAX_CHECKS_PER_HOUR + 2):
            r = self.check()
            self.t[0] += 60
        self.assertIsNone(r.action)
        self.assertEqual(r.next_due, self.t[0] - 60 + sw.MAX_INTERVAL_MIN * 60)

    def test_overlapping_checks_are_prevented(self):
        path = Path(self.tmp.name) / "wake.lock"
        held = sw.CheckLock(path)
        self.assertTrue(held.acquire())
        self.assertIsNone(self.check(lock=sw.CheckLock(path)))      # second process exits at once
        self.assertEqual(self.fs.registers, 0)
        held.release()
        self.assertIsNotNone(self.check(lock=sw.CheckLock(path)))
        self.assertFalse(path.exists())                              # released after the check

    def test_stale_lock_is_recovered(self):
        path = Path(self.tmp.name) / "wake.lock"
        path.write_text("12345")
        old = time.time() - sw.LOCK_STALE_S - 10
        os.utime(path, (old, old))
        lk = sw.CheckLock(path)
        self.assertTrue(lk.acquire())
        lk.release()

    def test_last_check_text(self):
        self.assertIn("No wake check", sw.last_check_text(self.db))
        self.reading = (88.0, True)
        self.check()
        self.assertIn("88% on charger", sw.last_check_text(self.db))


# --------------------------------------------------------------------------------------------
# Keep-awake requests
# --------------------------------------------------------------------------------------------
class SeparateDataFolderTest(unittest.TestCase):
    """With ARES_DATA_DIR set (tests, portable copies) the real wake task is never touched: it is
    one per Windows user and always checks the normal data folder. (A Windows test run once left
    a real task behind because `main.py --wake-check` re-scheduled it from a test folder.)"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "w.db")
        self.addCleanup(self.db.close)
        self.db.set_setting("wake_enabled", True)
        env = mock.patch.dict(os.environ, {"ARES_DATA_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        boom = mock.Mock(side_effect=AssertionError("real Task Scheduler used"))
        for name in ("exists", "query_xml", "register", "unregister"):
            p = mock.patch.object(sw.TaskScheduler, name, boom)
            p.start()
            self.addCleanup(p.stop)
        self.boom = boom

    def test_default_scheduler_is_inert(self):
        self.assertIsInstance(sw.default_scheduler(), sw.SeparateDataScheduler)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertNotIsInstance(sw.default_scheduler(), sw.SeparateDataScheduler)

    def test_ensure_leaves_the_task_alone(self):
        ok, msg = sw.ensure(self.db)
        self.assertTrue(ok)
        self.assertIn("not changed", msg)
        self.db.set_setting("wake_enabled", False)
        self.assertTrue(sw.ensure(self.db)[0])
        self.boom.assert_not_called()

    def test_run_check_does_not_reschedule(self):
        res = sw.run_check(self.db, probe=lambda: (50.0, True), send=lambda m: True,
                           launch=lambda a: True)
        self.assertIsNotNone(res)
        self.assertIsNotNone(res.next_due)                   # still computed and recorded
        self.boom.assert_not_called()


class PowerRequestTest(unittest.TestCase):
    def test_flags_and_dedupe(self):
        p = PowerRequest()
        p.set(True, False)
        p.set(True, False)                                   # unchanged: no second call
        p.set(False, True)                                   # display implies system
        p.release()
        self.assertEqual(p.calls, [ES_CONTINUOUS | ES_SYSTEM_REQUIRED,
                                   ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED,
                                   ES_CONTINUOUS])
        p.release()
        self.assertEqual(len(p.calls), 3)


# --------------------------------------------------------------------------------------------
# Protection manager reaction to a wake trigger (Qt)
# --------------------------------------------------------------------------------------------
class FakePlayer:
    def __init__(self):
        self.played = []

    def play(self, kind, path, volume=100):
        self.played.append((kind, path, volume))
        return True, "fake"

    def stop(self):
        pass


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class WakeTriggerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        from app.ui import lock_window as lw

        self.lw = lw
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.reading = [(89.0, True)]
        self.now = [0.0]
        self.power = PowerRequest()
        self.player = FakePlayer()
        self.m = lw.ProtectionManager(self.db, power_probe=lambda: self.reading[0], clock=lambda: self.now[0],
                                      player=self.player, power=self.power)
        self.addCleanup(self.m.shutdown)

    def test_89_on_charger_keeps_awake_until_90_screen(self):
        self.m.wake_trigger("high", "test")
        self.assertIsNone(self.m.window)                     # 90% threshold unchanged: no screen at 89
        self.assertTrue(self.power.system)
        self.assertFalse(self.power.display)
        self.reading[0] = (90.0, True)
        self.now[0] += 600
        self.m._watch_tick()
        self.assertIsNotNone(self.m.window)                  # existing 90% protection screen
        self.assertTrue(self.power.display)
        self.assertEqual(self.player.played[0][0], "high")
        self.reading[0] = (90.0, False)                      # charger removed
        self.m._watch_tick()
        self.assertIsNone(self.m.window)
        self.assertFalse(self.power.system)                  # Windows' own sleep rules apply again

    def test_hold_ends_when_unplugged_or_after_max(self):
        self.m.wake_trigger("high", "test")
        self.assertTrue(self.power.system)
        self.reading[0] = (89.0, False)
        self.m._watch_tick()
        self.assertFalse(self.power.system)
        self.reading[0] = (89.0, True)
        self.m.wake_trigger("high", "test")
        self.assertTrue(self.power.system)
        self.now[0] += self.lw.WAKE_HOLD_MAX_S + 1
        self.m._watch_tick()
        self.assertFalse(self.power.system)

    def test_no_hold_when_protection_disabled_or_charger_unknown(self):
        self.db.set_setting("strict_lock_enabled", False)
        self.m.wake_trigger("high", "test")
        self.assertFalse(self.power.system)
        self.db.set_setting("strict_lock_enabled", True)
        self.reading[0] = (None, None)
        self.m.wake_trigger("high", "test")
        self.assertFalse(self.power.system)
        self.assertIsNone(self.m.window)

    def test_low_after_wake_shows_screen_and_battery_hold_is_capped(self):
        self.reading[0] = (20.0, False)
        self.m.wake_trigger("low", "test")
        self.assertIsNotNone(self.m.window)
        self.assertEqual(self.m.mode, "low")
        self.assertTrue(self.power.display)
        self.now[0] += self.lw.SCREEN_HOLD_ON_BATTERY_S + 1
        self.m._watch_tick()
        self.assertIsNotNone(self.m.window)                  # screen stays, but...
        self.assertFalse(self.power.system)                  # ...Windows may sleep again (battery saver)

    def test_repeated_triggers_do_not_flood_the_log(self):
        for _ in range(5):
            self.m.wake_trigger("high", "test")
        msgs = [r["message"] for r in self.db.recent_events() if r["kind"] == "smart_wake"]
        self.assertEqual(sum("trigger" in m for m in msgs), 1)

    def test_shutdown_releases_power_request(self):
        self.m.wake_trigger("high", "test")
        self.assertTrue(self.power.system)
        self.m.shutdown()
        self.assertFalse(self.power.system)
        self.m.wake_trigger("high", "after shutdown")        # ignored, no crash


# --------------------------------------------------------------------------------------------
# Background app: Settings / tray / startup integration (Qt)
# --------------------------------------------------------------------------------------------
@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class BackgroundWakeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        use_fake_battery(self)            # never the laptop's real battery (see fake_hw.py)

    def make(self):
        from app.ui.background_app import AresBackgroundApp

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.fs = FakeScheduler()
        bg = AresBackgroundApp(str(Path(tmp.name) / "b.db"), power_probe=lambda: (50.0, True),
                               alarm_player=FakePlayer(), wake_scheduler=self.fs, power=PowerRequest())
        self.addCleanup(bg.shutdown)
        return bg

    def test_settings_save_registers_and_tray_disables_immediately(self):
        bg = self.make()
        bg.open_dashboard(4)
        sp = bg.window.settings_page
        sp.wake_enabled.setChecked(True)
        spin(500)
        self.assertEqual(self.fs.tasks, {})                  # not before Save
        self.assertTrue(sp.save())
        spin(600)
        self.assertEqual(len(self.fs.tasks), 1)
        self.assertTrue(bg.tray.a_wake.isChecked())
        sp.wake_interval.setValue(10)
        sp.wake_high.setValue(95)
        self.assertTrue(sp.save())
        spin(600)
        self.assertEqual(len(self.fs.tasks), 1)              # refreshed, one task
        bg.tray.a_wake.trigger()                             # tray: turn off now
        self.assertEqual(self.fs.tasks, {})
        self.assertFalse(bg.db.get_setting("wake_enabled"))
        self.assertFalse(sp.wake_enabled.isChecked())        # page shows it without losing other edits
        self.assertFalse(sp.is_dirty())

    def test_test_wake_button_reports(self):
        bg = self.make()
        bg.db.set_setting("wake_enabled", True)
        ok, lines = bg.test_wake()
        self.assertEqual(len(self.fs.tasks), 1)              # synced first
        self.assertTrue(any("Smart Wake-Up: ON" in x for x in lines))

    def test_wake_trigger_reuses_running_backend(self):
        bg = self.make()
        starts = bg.worker_starts
        bg.wake_trigger("high", "socket")
        bg.wake_trigger("low", "socket")
        self.assertEqual(bg.worker_starts, starts)           # no second monitoring loop
        self.assertLessEqual(len([w for w in QApplication.topLevelWidgets()
                                  if type(w).__name__ == "ProtectionWindow" and w.isVisible()]), 1)

    def test_shutdown_stops_wake_timers(self):
        bg = self.make()
        bg.db.set_setting("wake_enabled", True)
        bg.start()
        self.assertTrue(bg._wake_heal.isActive())
        bg.shutdown()
        self.assertFalse(bg._wake_heal.isActive())
        bg.sync_wake()                                       # late call after shutdown: ignored
        bg.wake_trigger("low", "late")


# --------------------------------------------------------------------------------------------
# Command-line entry points (real processes, isolated data folder)
# --------------------------------------------------------------------------------------------
class CommandLineTest(unittest.TestCase):
    def env(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return d.name, dict(os.environ, ARES_DATA_DIR=d.name, QT_QPA_PLATFORM="offscreen")

    def test_disable_wake(self):
        d, env = self.env()
        with Database(Path(d) / "data" / "battery_guardian.db") as db:
            db.set_setting("wake_enabled", True)
            db.set_setting("wake_interval_min", 15)
        r = subprocess.run([sys.executable, str(ROOT / "main.py"), "--disable-wake"], env=env, timeout=60,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with Database(Path(d) / "data" / "battery_guardian.db") as db:
            self.assertFalse(db.get_setting("wake_enabled"))
            self.assertEqual(db.get_setting("wake_interval_min"), 15)      # nothing else changed

    def test_wake_check_process_records_result_and_exits(self):
        d, env = self.env()
        with Database(Path(d) / "data" / "battery_guardian.db") as db:
            db.set_setting("wake_enabled", True)
        r = subprocess.run([sys.executable, str(ROOT / "main.py"), "--wake-check"], env=env, timeout=60,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with Database(Path(d) / "data" / "battery_guardian.db") as db:
            self.assertIsNotNone(db.get_state("wake_last_check"))
            self.assertEqual(len(db.get_state("wake_checks")), 1)
        self.assertFalse((Path(d) / "data" / "wake_check.lock").exists())

    @unittest.skipUnless(HAVE_QT, "PySide6 not installed")
    def test_running_instance_accepts_wake_command(self):
        d, env = self.env()
        log = Path(d) / "logs" / "ares.log"
        p = subprocess.Popen([sys.executable, str(ROOT / "main.py"), "--minimized"], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def stop():
            subprocess.run([sys.executable, str(ROOT / "main.py"), "--exit"], env=env, timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                p.wait(20)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(10)
        self.addCleanup(stop)
        end = time.monotonic() + 30
        while time.monotonic() < end and "Background monitoring started" not in (
                log.read_text(encoding="utf-8") if log.exists() else ""):
            time.sleep(0.3)
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import main; "
                "from PySide6.QtCore import QCoreApplication; app = QCoreApplication([]); "
                "sys.exit(0 if main.send_to_running_instance(b'wake:low') else 3)")
        r = subprocess.run([sys.executable, "-c", code, str(ROOT)], env=env, timeout=30)
        self.assertEqual(r.returncode, 0)
        db_path = Path(d) / "data" / "battery_guardian.db"
        found = False
        end = time.monotonic() + 15
        while time.monotonic() < end and not found:
            try:
                con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
                try:
                    found = bool(con.execute("SELECT 1 FROM events WHERE message LIKE "
                                             "'Smart Wake-Up trigger (low)%'").fetchall())
                finally:
                    con.close()
            except sqlite3.Error:
                pass
            time.sleep(0.3)
        self.assertTrue(found, "running ARES did not handle the wake command")
        self.assertEqual(log.read_text(encoding="utf-8").count("Background monitoring started"), 1)


# --------------------------------------------------------------------------------------------
# The --wake-check process must stay lightweight: no Qt GUI (QtGui / QtWidgets) and no app.ui code
# --------------------------------------------------------------------------------------------
GUI_BLOCKER = r"""
import importlib.abc, sys
FORBIDDEN = ("PySide6.QtGui", "PySide6.QtWidgets", "app.ui")

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.startswith(FORBIDDEN):
            raise ImportError("GUI module imported by --wake-check: " + name)
        return None

sys.meta_path.insert(0, Blocker())
"""


class WakeCheckIsGuiFreeTest(unittest.TestCase):
    """Runs the real `main.py --wake-check` code path in a fresh interpreter in which importing
    PySide6.QtGui, PySide6.QtWidgets or any app.ui module raises ImportError. QtCore/QtNetwork
    are allowed: they carry the single-instance socket and load no GUI libraries."""

    def run_check(self, extra: str, wake_enabled: bool = True):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        env = dict(os.environ, ARES_DATA_DIR=d.name)
        env.pop("QT_QPA_PLATFORM", None)
        with Database(Path(d.name) / "data" / "battery_guardian.db") as db:
            db.set_setting("wake_enabled", wake_enabled)
        code = GUI_BLOCKER + "\nsys.path.insert(0, sys.argv[1])\n" + extra + (
            "\nimport main\n"
            "rc = main.main(['--wake-check'])\n"
            "bad = sorted(m for m in sys.modules if m.startswith(FORBIDDEN))\n"
            "print('LOADED', sorted(m for m in sys.modules if m.startswith('PySide6.Qt')))\n"
            "print('BAD', bad)\n"
            "sys.exit(rc if not bad else 9)\n")
        r = subprocess.run([sys.executable, "-c", code, str(ROOT)], env=env, timeout=120,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("BAD []", r.stdout)
        self.assertNotIn("GUI module imported", r.stderr)
        loaded = r.stdout.split("LOADED", 1)[1].splitlines()[0]
        self.assertNotIn("QtGui", loaded)
        self.assertNotIn("QtWidgets", loaded)
        with Database(Path(d.name) / "data" / "battery_guardian.db") as db:
            return db.get_state("wake_last_check"), r.stdout

    def test_power_probe_module_imports_no_qt(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import app.core.power_probe as p; "
                "r = p.default_power_probe(); "
                "sys.exit(3 if any(m.startswith('PySide6') for m in sys.modules) else 0)")
        r = subprocess.run([sys.executable, "-c", code, str(ROOT)], timeout=60, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_wake_check_with_real_probe(self):
        res, _ = self.run_check("")                 # real GetSystemPowerStatus (None off Windows)
        self.assertIsNotNone(res)

    def test_wake_check_trigger_path(self):
        # A low-battery trigger with no ARES running exercises the socket + launch path; the
        # launch itself is replaced so no GUI process is started by the test.
        res, _ = self.run_check(
            "import app.core.power_probe as pp, app.core.smart_wake as sw\n"
            "pp.default_power_probe = lambda: (20.0, False)\n"
            "sw.launch_ares = lambda action: print('LAUNCH', action) or True\n")
        self.assertEqual(res["action"], "low")
        self.assertIn("ARES was started", res["result"])

    def test_wake_check_disabled(self):
        res, _ = self.run_check("", wake_enabled=False)
        self.assertIsNone(res["next_due"])


if __name__ == "__main__":
    unittest.main()
