"""Unit tests for the logic that can be verified without the real laptop.

The ScriptedDell class below is a TEST DOUBLE used only here to check the
controller's decision logic; the application itself never uses it.

Run:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import charging_controller as cc
from app.core.battery_monitor import BatterySnapshot
from app.database.battery_history import BatteryHistory
from app.database.database import Database
from app.hardware import dell_controller as dc
from app.hardware.base_controller import (BaseChargeController, ChargeConfig, ControllerCapabilities,
                                          WriteResult)
from app.hardware.generic_controller import GenericController
from app.services import report_service
from app.services.notification_service import NotificationService
from app.utils import calculations as calc


class ScriptedDell(BaseChargeController):
    """Behaves like a Dell BIOS: enforces Dell limits and stores the value."""

    vendor = "Dell"

    def __init__(self, mode="Express", start=None, stop=None, accept=True, need_pwd=False, readback_lies=False):
        self.cfg = ChargeConfig(mode, start, stop)
        self.accept, self.need_pwd, self.readback_lies = accept, need_pwd, readback_lies
        self.writes: list[tuple] = []

    def probe(self):
        return ControllerCapabilities("Dell", "test", True, True, True, True, limits=dc.DELL_DEFAULT_LIMITS)

    def read_config(self):
        return ChargeConfig(self.cfg.mode, self.cfg.start, self.cfg.stop)

    def write_thresholds(self, start, stop, password=None):
        self.writes.append((start, stop, password))
        if self.need_pwd and not password:
            return WriteResult(False, False, "password required", self.read_config(), needs_password=True)
        if self.accept and not self.readback_lies:
            self.cfg = ChargeConfig("Custom", start, stop)
        after = self.read_config()
        ok = after.matches(start, stop)
        return WriteResult(ok, ok, "ok" if ok else "not applied", after)

    def restore_mode(self, config, password=None):
        self.cfg = ChargeConfig(config.mode, config.start, config.stop)
        return WriteResult(True, True, "restored", self.read_config())


def snap(pct, ac=True, charging=False, discharging=False, t=None, **kw):
    return BatterySnapshot(t or time.time(), True, "ioctl", percent=pct, ac_online=ac, charging=charging,
                           discharging=discharging, **kw)


class CalculationsTest(unittest.TestCase):
    def test_health_and_wear(self):
        self.assertEqual(calc.health_percent(41000, 55000), 74.5)
        self.assertEqual(calc.wear_percent(41000, 55000), 25.5)
        self.assertIsNone(calc.health_percent(None, 55000))
        self.assertIsNone(calc.health_percent(41000, 0))

    def test_anomalies(self):
        self.assertTrue(calc.capacity_anomalies(70000, 55000))
        self.assertTrue(calc.capacity_anomalies(10000, 55000))
        self.assertFalse(calc.capacity_anomalies(50000, 55000, 30000))
        self.assertTrue(calc.capacity_anomalies(50000, 55000, 60000))

    def test_runtime(self):
        self.assertEqual(calc.runtime_minutes(30000, -10000), 180.0)
        self.assertIsNone(calc.runtime_minutes(30000, 5000))
        self.assertIsNone(calc.runtime_minutes(30000, -100))

    def test_drain_rate(self):
        pts = [(0, 80), (1800, 75), (3600, 70)]
        d = calc.drain_rate(pts, [-8000, -9000, -10000])
        self.assertAlmostEqual(d.percent_per_hour, 10.0)
        self.assertEqual(d.avg_power_w, 9.0)

    def test_plan_dell_25_90(self):
        p = calc.plan_thresholds(25, 90, dc.DELL_DEFAULT_LIMITS)
        self.assertEqual((p.start, p.stop), (50, 90))
        self.assertFalse(p.exact)
        self.assertTrue(any("50%" in m for m in p.messages))

    def test_plan_gap(self):
        p = calc.plan_thresholds(88, 90, dc.DELL_DEFAULT_LIMITS)
        self.assertEqual((p.start, p.stop), (85, 90))
        p = calc.plan_thresholds(60, 55, dc.DELL_DEFAULT_LIMITS)
        self.assertGreaterEqual(p.stop - p.start, 5)
        p = calc.plan_thresholds(50, 80, dc.DELL_DEFAULT_LIMITS)
        self.assertTrue(p.exact)

    def test_validate(self):
        self.assertTrue(calc.validate_user_thresholds(90, 25))
        self.assertTrue(calc.validate_user_thresholds(2, 90))
        self.assertFalse(calc.validate_user_thresholds(25, 90))


class CctkParsingTest(unittest.TestCase):
    def test_parse(self):
        c = dc.parse_cctk_charge_cfg("PrimaryBattChargeCfg=Custom:50-90\n")
        self.assertEqual((c.mode, c.start, c.stop), ("Custom", 50, 90))
        c = dc.parse_cctk_charge_cfg("primarybattchargecfg=express")
        self.assertEqual(c.mode, "Express")
        self.assertIsNone(c.start)
        self.assertIsNone(dc.parse_cctk_charge_cfg("some error"))

    def test_errors(self):
        msg, pwd, adm, uns = dc.classify_cctk_error(
            "The option 'PrimaryBattChargeCfg' is not available or cannot be configured through this tool.")
        self.assertTrue(uns)
        _, pwd, _, _ = dc.classify_cctk_error("Setup password is required to change this option. Use --ValSetupPwd")
        self.assertTrue(pwd)
        _, _, adm, _ = dc.classify_cctk_error("Please run as Administrator")
        self.assertTrue(adm)

    def test_dell_refuses_out_of_range_without_calling_hw(self):
        ctl = dc.DellController("Latitude E7470")
        ctl._caps = ControllerCapabilities("Dell", "x", True, True, True, limits=dc.DELL_DEFAULT_LIMITS)
        ctl._cctk = mock.Mock()
        with mock.patch.object(dc.si, "is_admin", return_value=True):
            r = ctl.write_thresholds(25, 90)
        self.assertFalse(r.success)
        ctl._cctk.write.assert_not_called()

    def test_dell_cctk_write_verified_by_readback(self):
        ctl = dc.DellController("Latitude E7470")
        ctl._caps = ControllerCapabilities("Dell", "x", True, True, True, limits=dc.DELL_DEFAULT_LIMITS)
        state = {"v": "PrimaryBattChargeCfg=Express"}
        cctk = mock.Mock()
        cctk.read.side_effect = lambda: (dc.parse_cctk_charge_cfg(state["v"]), state["v"])

        def write(value, pwd):
            state["v"] = f"PrimaryBattChargeCfg={value}"
            return True, "PrimaryBattChargeCfg=" + value
        cctk.write.side_effect = write
        ctl._cctk = cctk
        with mock.patch.object(dc.si, "is_admin", return_value=True):
            r = ctl.write_thresholds(50, 90)
        self.assertTrue(r.success and r.verified)
        cctk.write.assert_called_once_with("Custom:50-90", None)

    def test_dell_cctk_success_but_readback_differs_is_failure(self):
        ctl = dc.DellController("Latitude E7470")
        ctl._caps = ControllerCapabilities("Dell", "x", True, True, True, limits=dc.DELL_DEFAULT_LIMITS)
        cctk = mock.Mock()
        cctk.read.return_value = (ChargeConfig("Express"), "PrimaryBattChargeCfg=Express")
        cctk.write.return_value = (True, "done")
        ctl._cctk = cctk
        with mock.patch.object(dc.si, "is_admin", return_value=True):
            r = ctl.write_thresholds(50, 90)
        self.assertFalse(r.success)
        self.assertFalse(r.verified)


class ControllerLogicTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.events: list = []
        self.now = [1_000_000.0]

    def make(self, hw):
        return cc.ChargingController(self.db, hw, on_event=lambda k, m: self.events.append((k, m)),
                                     clock=lambda: self.now[0])

    def test_unsupported_never_claims_pause(self):
        c = self.make(GenericController("Acme"))
        st = c.evaluate(snap(95, ac=True, charging=False))
        self.assertEqual(st.state, cc.MONITORING_ONLY)
        self.assertNotIn("Paused", st.headline)
        r = c.apply_thresholds(manual=True)
        self.assertFalse(r.success)

    def test_original_config_saved_once(self):
        self.make(ScriptedDell("Express"))
        self.assertEqual(self.db.get_state("original_config")["mode"], "Express")
        self.make(ScriptedDell("Custom", 50, 90))
        self.assertEqual(self.db.get_state("original_config")["mode"], "Express")

    def test_no_write_when_auto_apply_turned_off(self):
        self.db.set_setting("auto_apply_thresholds", False)
        hw = ScriptedDell("Express")
        c = self.make(hw)
        c.evaluate(snap(70, charging=True))
        c.evaluate(snap(92, charging=True))
        self.assertEqual(hw.writes, [])

    def test_auto_apply_is_default_and_runs_once_at_startup(self):
        hw = ScriptedDell("Express")
        c = self.make(hw)
        st = c.evaluate(snap(70, charging=True))           # first tick after start: firmware set once
        self.assertEqual(hw.writes, [(50, 90, None)])
        self.assertTrue(c.control_active())
        for _ in range(10):
            c.evaluate(snap(75, charging=True))            # between thresholds, already applied
        self.assertEqual(len(hw.writes), 1)
        self.assertEqual(st.state, cc.CHARGING)
        self.assertIn("stop at 90%", st.detail)

    def test_auto_stop_unsupported_hardware_sends_nothing(self):
        c = self.make(GenericController("Acme"))
        st = c.evaluate(snap(95, charging=True))
        self.assertEqual(st.state, cc.MONITORING_ONLY)
        self.assertFalse(c.control_active())

    def test_hysteresis_cycle_with_auto_apply(self):
        self.db.set_setting("auto_apply_thresholds", True)
        hw = ScriptedDell("Express")
        c = self.make(hw)
        c._startup_check_done = True                       # isolate the edge behaviour
        st = c.evaluate(snap(70, charging=True))           # between thresholds -> no command
        self.assertEqual(hw.writes, [])
        self.assertEqual(st.state, cc.CHARGING)
        st = c.evaluate(snap(90, charging=True))           # edge -> one write, still charging
        self.assertEqual(hw.writes, [(50, 90, None)])
        self.assertEqual(st.state, cc.PAUSE_PENDING)
        st = c.evaluate(snap(90, charging=False))          # telemetry confirms
        self.assertEqual(st.state, cc.PAUSED)
        self.assertEqual(st.headline, "Charging Paused")
        self.assertIn("charging_paused", [k for k, _ in self.events])
        for _ in range(20):                                # no repeated commands
            c.evaluate(snap(89, charging=False))
        self.assertEqual(len(hw.writes), 1)
        st = c.evaluate(snap(49, charging=True))
        self.assertEqual(st.state, cc.RESUMED)
        self.assertIn("charging_resumed", [k for k, _ in self.events])
        self.assertEqual(len(hw.writes), 1)

    def test_charging_at_stop_is_not_reported_paused(self):
        hw = ScriptedDell("Custom", 50, 90)
        c = self.make(hw)
        st = c.evaluate(snap(91, charging=True))
        self.assertEqual(st.state, cc.PAUSE_PENDING)
        self.now[0] += 11 * 60
        c.evaluate(snap(91, charging=True))
        self.assertTrue(any(k == "warning" for k, _ in self.events))

    def test_rejected_write_backs_off(self):
        self.db.set_setting("auto_apply_thresholds", True)
        hw = ScriptedDell("Express", accept=False)
        c = self.make(hw)
        c.evaluate(snap(95))
        c.evaluate(snap(95))
        self.assertEqual(len(hw.writes), 1)          # in backoff
        self.now[0] += 6 * 60
        c._fw_read_ts = 0
        c.evaluate(snap(95))
        self.assertEqual(len(hw.writes), 2)

    def test_readback_mismatch_is_not_success(self):
        hw = ScriptedDell("Express", readback_lies=True)
        c = self.make(hw)
        r = c.apply_thresholds(manual=True)
        self.assertFalse(r.verified)
        st = c.evaluate(snap(92, charging=False))
        self.assertNotEqual(st.state, cc.PAUSED)

    def test_password_blocks_retries_until_given(self):
        self.db.set_setting("auto_apply_thresholds", True)
        hw = ScriptedDell("Express", need_pwd=True)
        c = self.make(hw)
        c.evaluate(snap(95))
        self.now[0] += 10 * 3600
        c._fw_read_ts = 0
        c.evaluate(snap(95))
        self.assertEqual(len(hw.writes), 1)
        self.assertEqual(c.blocked_reason, "password")
        c.set_password("secret")
        c.evaluate(snap(95))
        self.assertEqual(hw.writes[-1], (50, 90, "secret"))
        self.assertTrue(c.firmware_matches_plan())
        self.assertNotIn("secret", str(self.db.query("SELECT * FROM controller_state")))
        self.assertNotIn("secret", str([dict(r) for r in self.db.query("SELECT * FROM settings")]))

    def test_restart_recovers_and_detects_external_change(self):
        hw = ScriptedDell("Express")
        c = self.make(hw)
        c.apply_thresholds(manual=True)
        hw.cfg = ChargeConfig("Express")       # e.g. BIOS reset to defaults while app was closed
        self.events.clear()
        self.make(hw)
        self.assertTrue(any("differs" in m for _, m in self.events))

    def test_restore(self):
        hw = ScriptedDell("Express")
        c = self.make(hw)
        c.apply_thresholds(manual=True)
        r = c.restore_original()
        self.assertTrue(r.verified)
        self.assertEqual(hw.cfg.mode, "Express")

    def test_on_battery(self):
        c = self.make(ScriptedDell("Custom", 50, 90))
        st = c.evaluate(snap(80, ac=False, discharging=True))
        self.assertEqual(st.state, cc.ON_BATTERY)


class HistoryAndReportsTest(unittest.TestCase):
    def test_sessions_samples_export(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = tmp.name
        db = Database(Path(d) / "t.db")
        self.addCleanup(db.close)               # closed even if an assertion fails (Windows file lock)
        h = BatteryHistory(db)
        t = 1_700_000_000.0
        for i in range(10):
            h.record(snap(80 - i, ac=False, discharging=True, t=t + i * 60, rate_mw=-9000,
                          remaining_mwh=40000 - i * 150, full_charge_mwh=45000, design_mwh=55000))
        h.record(snap(70, ac=True, charging=True, t=t + 700))
        self.assertEqual(len(h.sessions()), 2)
        self.assertEqual(len(db.query("SELECT * FROM health")), 1)
        n = report_service.export_samples_csv(db, Path(d) / "s.csv")
        self.assertEqual(n, 11)
        self.assertEqual(report_service.export_sessions_csv(db, Path(d) / "x.csv"), 2)
        db.close()
        # crash recovery: reopen, open session closed
        db = Database(Path(d) / "t.db")
        self.addCleanup(db.close)
        BatteryHistory(db)
        self.assertEqual(len(db.query("SELECT * FROM sessions WHERE end_ts IS NULL")), 0)
        db.close()

    def _max_notifier(self, db, protection=False):
        if not protection:
            db.set_setting("strict_lock_enabled", False)    # the alert's own sound plays only without the lock
        toasts, sounds = [], []
        n = NotificationService(db, sink=lambda *a: toasts.append(a),
                                sound=lambda path: (sounds.append(path), (True, "test"))[1])
        return n, toasts, sounds

    def test_max_charge_sound_once_per_cycle(self):
        db = Database(":memory:")
        self.addCleanup(db.close)
        n, toasts, sounds = self._max_notifier(db)
        self.assertFalse(n.check_max_reached(snap(85, charging=True), True))
        self.assertTrue(n.check_max_reached(snap(90, charging=True), True))
        for p in (90, 91, 90, 89, 88, 90):                 # sitting near 90%: no repeats
            self.assertFalse(n.check_max_reached(snap(p), True))
        self.assertEqual(len(sounds), 1)
        self.assertEqual(len(toasts), 1)
        self.assertIn("Firmware charge limit is active", toasts[0][1])
        n.check_max_reached(snap(60, ac=False, discharging=True), True)   # used down -> re-armed
        self.assertTrue(n.check_max_reached(snap(90, charging=True), True))
        self.assertEqual(len(sounds), 2)

    def test_max_charge_alert_survives_restart_and_needs_ac(self):
        db = Database(":memory:")
        self.addCleanup(db.close)
        n, toasts, sounds = self._max_notifier(db)
        self.assertFalse(n.check_max_reached(snap(92, ac=False, discharging=True), True))  # on battery
        self.assertTrue(n.check_max_reached(snap(92), False))
        self.assertIn("NOT active", toasts[-1][1])
        n2, toasts2, sounds2 = self._max_notifier(db)       # app restarted while still at 92%
        self.assertFalse(n2.check_max_reached(snap(92), False))
        self.assertEqual(sounds2, [])

    def test_max_charge_sound_setting_and_custom_file(self):
        db = Database(":memory:")
        self.addCleanup(db.close)
        db.set_setting("strict_lock_enabled", False)        # protection off -> this alert plays the sound
        db.set_setting("high_alarm_sound_path", "C:/Windows/Media/Alarm01.wav")
        n, toasts, sounds = self._max_notifier(db)
        n.check_max_reached(snap(90), True)
        self.assertEqual(sounds, ["C:/Windows/Media/Alarm01.wav"])
        db.set_setting("sound_enabled", False)
        db.set_state("max_alert_armed", True)
        n.check_max_reached(snap(90), True)
        self.assertEqual(len(sounds), 1)                    # sound off, toast still shown
        self.assertEqual(len(toasts), 2)

    def test_max_charge_no_double_sound_when_protection_on(self):
        db = Database(":memory:")
        self.addCleanup(db.close)
        n, toasts, sounds = self._max_notifier(db, protection=True)   # strict lock enabled (default)
        n.check_max_reached(snap(90), True)
        self.assertEqual(sounds, [])                        # the protection alarm plays instead
        self.assertEqual(len(toasts), 1)

    def test_legacy_sound_settings_migrate(self):
        from app.database.database import migrate_settings
        db = Database(":memory:")
        self.addCleanup(db.close)
        db.set_setting("alert_sound_path", "C:/x.wav")
        db.set_setting("sound_at_max", False)
        migrate_settings(db)
        self.assertEqual(db.get_setting("high_alarm_sound_path"), "C:/x.wav")
        self.assertFalse(db.get_setting("sound_enabled"))
        db.set_setting("high_alarm_sound_path", "C:/y.wav")
        migrate_settings(db)                                # idempotent: user's later choice kept
        self.assertEqual(db.get_setting("high_alarm_sound_path"), "C:/y.wav")

    def test_sound_service_off_windows(self):
        from app.services import sound_service
        if sys.platform != "win32":
            self.assertEqual(sound_service.play_alert()[0], False)

    def test_notifications_dedupe(self):
        db = Database(":memory:")
        self.addCleanup(db.close)
        got = []
        n = NotificationService(db, sink=lambda *a: got.append(a))
        for _ in range(5):
            n.check_snapshot(snap(10, ac=False, discharging=True))
        self.assertEqual(len(got), 1)
        n.check_snapshot(snap(40, ac=False, discharging=True, temperature_c=55.0))
        self.assertEqual(got[-1][0], "Battery temperature high")
        db.set_setting("notifications_enabled", False)
        n.controller_event("charging_paused", "x")
        self.assertEqual(len(got), 2)


class TemperatureTest(unittest.TestCase):
    def test_implausible_temperature_discarded(self):
        from app.core.battery_monitor import BatteryMonitor
        s = snap(50, temperature_c=250.0)
        BatteryMonitor._check_temperature(s)
        self.assertIsNone(s.temperature_c)
        s = snap(50)
        BatteryMonitor._check_temperature(s)
        self.assertIsNone(s.temperature_c)
        self.assertIn("No battery temperature", s.temperature_note)


class StartupManagerTest(unittest.TestCase):
    """Windows calls are mocked: these check the decision logic, not real Task Scheduler/registry."""

    def setUp(self):
        from app.core import startup_manager as sm
        self.sm = sm
        self.state = {"task": None, "run": None, "admin": False}
        cmd = '"C:\\ARES\\ARES-Battery-Guardian.exe" --autostart'
        self.cmd = cmd
        st = self.state
        patches = [
            mock.patch.object(sm.si, "IS_WINDOWS", True),
            mock.patch.object(sm.si, "is_admin", lambda: st["admin"]),
            mock.patch.object(sm, "_command_line", lambda: cmd),
            mock.patch.object(sm, "task_exists", lambda: st["task"] is not None),
            mock.patch.object(sm, "registered_task_command", lambda: st["task"]),
            mock.patch.object(sm, "run_key_exists", lambda: st["run"] is not None),
            mock.patch.object(sm, "run_key_value", lambda: st["run"]),
            mock.patch.object(sm, "create_task", lambda: (st.__setitem__("task", cmd), (True, "ok"))[1]),
            mock.patch.object(sm, "delete_task", lambda: (st.__setitem__("task", None), (True, "ok"))[1]),
            mock.patch.object(sm, "set_run_key",
                              lambda on: (st.__setitem__("run", cmd if on else None), (True, "OK"))[1]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_default_setting_is_on(self):
        from app.database.database import DEFAULT_SETTINGS
        self.assertTrue(DEFAULT_SETTINGS["autostart"])

    def test_admin_creates_elevated_task_and_removes_run_key(self):
        self.state.update(admin=True, run=self.cmd)
        changed, _ = self.sm.ensure_startup(True)
        self.assertTrue(changed)
        self.assertEqual(self.state["task"], self.cmd)
        self.assertIsNone(self.state["run"])                 # never both -> no double start
        self.assertEqual(self.sm.ensure_startup(True)[0], False)   # idempotent

    def test_admin_repairs_task_pointing_at_old_location(self):
        self.state.update(admin=True, task='"C:\\old\\pythonw.exe" main.py --minimized')
        self.assertTrue(self.sm.ensure_startup(True)[0])
        self.assertEqual(self.state["task"], self.cmd)

    def test_non_admin_uses_run_key_and_keeps_existing_task(self):
        changed, msg = self.sm.ensure_startup(True)
        self.assertTrue(changed)
        self.assertEqual(self.state["run"], self.cmd)
        self.assertIn("no administrator rights needed", msg)
        self.state.update(run=None, task=self.cmd)
        self.assertFalse(self.sm.ensure_startup(True)[0])     # task present: leave it alone
        self.assertIsNone(self.state["run"])

    def test_turning_off_removes_everything(self):
        self.state.update(task=self.cmd, run=self.cmd, admin=True)
        self.sm.ensure_startup(False)
        self.assertIsNone(self.state["task"])
        self.assertIsNone(self.state["run"])

    def test_failure_never_raises(self):
        with mock.patch.object(self.sm, "_command_line", side_effect=OSError("boom")):
            changed, msg = self.sm.ensure_startup(True)
        self.assertFalse(changed)
        self.assertIn("boom", msg)

    def test_startup_launch_uses_autostart_flag(self):
        from app.core import startup_manager as real
        exe, args = real.launch_command()
        self.assertEqual(args[-1], "--autostart")


if __name__ == "__main__":
    unittest.main()
