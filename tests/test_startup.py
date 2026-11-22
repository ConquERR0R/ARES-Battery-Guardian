"""Windows automatic startup tests (v1.2).

The registry is replaced by an in-memory fake `winreg` and Task Scheduler calls are mocked, so
these tests check ARES' logic on the development machine. They do NOT prove that Windows 11
actually launches ARES at sign-in: see docs/V1.2_STARTUP.md for the manual Windows test.

Numbered comments match the requested list:
 1 enable registers   2 disable removes   3 no duplicates   4 packaged EXE path
 5 dashboard by default   6 start minimized   7 existing process reused
 8 monitoring starts   9 settings persist   10 missing executable handled safely
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core import startup_manager as sm
from app.database.database import Database

try:
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


def fake_winreg(store: dict):
    """Minimal in-memory HKCU Run key with the winreg calls startup_manager uses."""
    w = types.ModuleType("winreg")
    w.HKEY_CURRENT_USER, w.KEY_SET_VALUE, w.REG_SZ = 1, 2, 1

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    w.OpenKey = lambda root, path, *a: Key()

    def query(k, name):
        if name not in store:
            raise FileNotFoundError(name)
        return store[name], 1
    w.QueryValueEx = query
    w.SetValueEx = lambda k, name, _r, _t, value: store.__setitem__(name, value)

    def delete(k, name):
        if name not in store:
            raise FileNotFoundError(name)
        del store[name]
    w.DeleteValue = delete
    return w


def install_fake_windows(tc: unittest.TestCase) -> None:
    """Give test case `tc` an isolated "Windows": a temp packaged EXE in a folder with spaces, an
    in-memory HKCU Run key (tc.store, with two unrelated apps) and a mocked Task Scheduler
    (tc.task). The real registry and Task Scheduler are never read or written, on any OS."""
    tc.tmp = tempfile.TemporaryDirectory()
    tc.addCleanup(tc.tmp.cleanup)
    tc.exe = Path(tc.tmp.name) / "Program Files" / "ARES Battery Guardian" / "ARES-Battery-Guardian.exe"
    tc.exe.parent.mkdir(parents=True)
    tc.exe.write_bytes(b"MZ")
    tc.store = {"OneDrive": '"C:\\OneDrive\\OneDrive.exe" /background', "Teams": "teams.exe"}
    tc.others = dict(tc.store)
    tc.task = {"cmd": None}
    t = tc.task
    for p in (mock.patch.dict(sys.modules, {"winreg": fake_winreg(tc.store)}),
              mock.patch.object(sm.si, "IS_WINDOWS", True),
              mock.patch.object(sm.si, "is_admin", lambda: False),
              mock.patch.object(sys, "frozen", True, create=True),
              mock.patch.object(sys, "executable", str(tc.exe)),
              mock.patch.object(sm, "task_exists", lambda: t["cmd"] is not None),
              mock.patch.object(sm, "registered_task_command", lambda: t["cmd"]),
              mock.patch.object(sm, "delete_task", lambda: (t.__setitem__("cmd", None), (True, "ok"))[1])):
        p.start()
        tc.addCleanup(p.stop)


class RegistryStartupTest(unittest.TestCase):
    """Normal (non-admin) startup registration through the current user's Run key."""

    def setUp(self):
        install_fake_windows(self)
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)

    def ares_values(self):
        return {k: v for k, v in self.store.items() if k not in self.others}

    def test_1_enable_registers_current_user_run_key(self):
        ok, msg = sm.apply_preference(self.db, True)
        self.assertTrue(ok, msg)
        self.assertEqual(list(self.ares_values()), [sm.APP_ID])
        self.assertTrue(sm.status().works)
        self.assertEqual(sm.method(), "Registry Run key (current user)")
        self.assertTrue(self.db.get_state("startup_last_result")["ok"])

    def test_2_disable_removes_only_ares(self):
        sm.apply_preference(self.db, True)
        ok, _ = sm.apply_preference(self.db, False)
        self.assertTrue(ok)
        self.assertEqual(self.ares_values(), {})
        self.assertEqual(self.store, self.others)               # unrelated startup apps untouched
        self.assertFalse(sm.status().enabled)
        self.assertEqual(sm.ensure_startup(False), (False, "Startup is off."))   # nothing left to remove

    def test_3_repeated_enable_no_duplicates(self):
        for _ in range(5):
            sm.apply_preference(self.db, True)
        self.assertEqual(len(self.ares_values()), 1)
        self.assertFalse(sm.ensure_startup(True)[0])             # already up to date: no write
        self.task["cmd"] = sm._command_line()                    # an elevated task exists too...
        sm.ensure_startup(True)
        self.assertEqual(self.ares_values(), {})                 # ...so the Run entry is removed (never both)

    def test_4_packaged_exe_path_with_spaces(self):
        exe, args = sm.launch_command()
        self.assertEqual(Path(exe), self.exe.resolve())
        self.assertEqual(args, ["--autostart"])
        sm.apply_preference(self.db, True)
        cmd = self.store[sm.APP_ID]
        self.assertTrue(cmd.startswith('"'), cmd)                # quoted because of the spaces
        self.assertEqual(sm.split_command(cmd), [str(self.exe.resolve()), "--autostart"])
        self.assertEqual(sm.missing_targets(cmd), [])
        ok, lines = sm.verify(True)
        self.assertTrue(ok, lines)

    def test_10_missing_or_moved_executable(self):
        old = Path(self.tmp.name) / "old place" / "ARES-Battery-Guardian.exe"     # does not exist
        self.store[sm.APP_ID] = f'"{old}" --autostart'
        st = sm.status()
        self.assertTrue(st.enabled)
        self.assertFalse(st.works)
        self.assertEqual(st.missing, [str(old)])
        ok, lines = sm.verify(True)
        self.assertFalse(ok)
        self.assertTrue(any("not found" in ln for ln in lines))
        changed, _ = sm.ensure_startup(True)                     # what ARES does a few s after it starts
        self.assertTrue(changed)
        self.assertEqual(sm.split_command(self.store[sm.APP_ID])[0], str(self.exe.resolve()))
        self.assertTrue(sm.status().works)

    def test_10_broken_elevated_task_without_admin_falls_back(self):
        self.task["cmd"] = f'"{Path(self.tmp.name) / "gone.exe"}" --autostart'
        changed, msg = sm.ensure_startup(True)
        self.assertTrue(changed)
        self.assertIn(sm.APP_ID, self.store)                     # ARES still starts at sign-in
        self.assertIn("missing program", msg)

    def test_failure_is_reported_not_raised(self):
        with mock.patch.object(sm, "set_run_key", return_value=(False, "Access is denied")):
            ok, msg = sm.apply_preference(self.db, True)
        self.assertFalse(ok)
        self.assertIn("Access is denied", msg)
        self.assertFalse(self.db.get_state("startup_last_result")["ok"])

    @unittest.skipUnless(HAVE_QT, "PySide6 not installed")
    def test_settings_checkbox_reflects_actual_registration(self):
        QApplication.instance() or QApplication([])
        from app.ui.settings import SettingsPage

        sm.apply_preference(self.db, True)
        pg = SettingsPage(self.db)
        self.addCleanup(pg.deleteLater)
        self.assertTrue(pg.autostart.isChecked())
        self.assertIn("Enabled", pg.startup_lbl.text())
        self.assertIn("successfully applied", pg.startup_result.text())
        pg.autostart.setChecked(False)                           # user unticks (staged)
        self.assertIn(sm.APP_ID, self.store)                     # nothing changes before Save
        self.assertTrue(pg.save())
        self.assertNotIn(sm.APP_ID, self.store)
        self.assertFalse(self.db.get_setting("autostart"))
        self.assertIn("Disabled", pg.startup_lbl.text())
        self.store[sm.APP_ID] = "manually added"                 # registry changed outside ARES
        pg.load()
        self.assertTrue(pg.autostart.isChecked())                # shows the real state
        pg.autostart.setChecked(False)
        self.assertTrue(pg.save())
        with mock.patch.object(sm, "set_run_key", return_value=(False, "denied")):
            pg.autostart.setChecked(True)
            pg.save()
        self.assertIn("Failed", pg.startup_result.text())
        with mock.patch("app.ui.settings.QMessageBox.warning") as warn, \
                mock.patch("app.ui.settings.QMessageBox.information") as info:
            pg.startup_test_btn.click()
        self.assertTrue(warn.called or info.called)


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class SettingsPersistTest(unittest.TestCase):
    """Saved preference vs. actual registration, and persistence across a restart.

    Uses the fake Windows from install_fake_windows(), so the result is the same on a developer
    PC that has (or hasn't) a real ARES startup entry, and on Linux."""

    def setUp(self):
        QApplication.instance() or QApplication([])
        install_fake_windows(self)
        self.path = str(Path(self.tmp.name) / "p.db")

    def open_page(self, db):
        from app.ui.settings import SettingsPage

        pg = SettingsPage(db)
        self.addCleanup(QCoreApplication.processEvents)
        self.addCleanup(pg.deleteLater)          # runs before the db cleanup registered earlier
        return pg

    def test_9_default_preference_differs_from_registration(self):
        db = Database(self.path)
        self.addCleanup(db.close)                # closed even if an assertion fails (no WinError 32)
        self.assertTrue(db.get_setting("autostart"))            # new install: preference ON
        self.assertFalse(db.get_setting("start_minimized"))     # new install: open the dashboard
        pg = self.open_page(db)
        self.assertFalse(pg.autostart.isChecked())              # ...but nothing registered yet:
        self.assertIn("Disabled", pg.startup_lbl.text())        # the box shows the real state
        sm.apply_preference(db, True, "launch check")           # what ARES does ~3 s after it starts
        pg.load()
        self.assertTrue(pg.autostart.isChecked())
        self.assertIn("successfully applied", pg.startup_result.text())

    def test_9_startup_settings_persist(self):
        db = Database(self.path)
        self.addCleanup(db.close)
        pg = self.open_page(db)
        pg.autostart.setChecked(True)
        self.assertTrue(pg.save())                              # registers in the fake Run key
        self.assertIn(sm.APP_ID, self.store)
        pg.start_minimized.setChecked(True)
        pg.autostart.setChecked(False)
        self.assertTrue(pg.save())                              # removes it again
        self.assertNotIn(sm.APP_ID, self.store)
        self.assertEqual(self.store, self.others)
        db.close()                                              # "restart ARES"
        db2 = Database(self.path)
        self.addCleanup(db2.close)
        self.assertFalse(db2.get_setting("autostart"))
        self.assertTrue(db2.get_setting("start_minimized"))
        pg2 = self.open_page(db2)
        self.assertTrue(pg2.start_minimized.isChecked())
        self.assertFalse(pg2.autostart.isChecked())


def wait_for(pred, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.3)
    return pred()


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class AutostartLaunchTest(unittest.TestCase):
    """Real ARES processes (offscreen, isolated data folder) started the way Windows starts them."""

    def _launch(self, minimized: bool):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        env = dict(os.environ, ARES_DATA_DIR=d.name, QT_QPA_PLATFORM="offscreen")
        with Database(Path(d.name) / "data" / "battery_guardian.db") as db:
            db.set_setting("start_minimized", minimized)
            db.set_setting("monitor_interval_s", 5)
        log = Path(d.name) / "logs" / "ares.log"
        text = lambda: log.read_text(encoding="utf-8") if log.exists() else ""   # noqa: E731
        p = subprocess.Popen([sys.executable, str(ROOT / "main.py"), "--autostart"], env=env,
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
        self.assertTrue(wait_for(lambda: "started (" in text(), 30), "ARES did not start")
        return env, text

    def test_5_7_8_dashboard_by_default_monitoring_and_reuse(self):
        env, text = self._launch(minimized=False)
        self.assertIn("started (dashboard open) at Windows sign-in", text())                # 5
        self.assertEqual(text().count("Dashboard window created"), 1)
        self.assertEqual(text().count("Background monitoring started"), 1)        # 8
        # 7: Windows (or the user) starts ARES again: the running copy is reused.
        r = subprocess.run([sys.executable, str(ROOT / "main.py"), "--autostart"], env=env, timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0)
        self.assertTrue(wait_for(lambda: "Another instance is running" in text(), 10))
        time.sleep(1)
        self.assertEqual(text().count("Background monitoring started"), 1)        # no second backend
        self.assertEqual(text().count("Dashboard window created"), 1)             # existing window raised

    def test_6_start_minimized(self):
        env, text = self._launch(minimized=True)
        self.assertIn("started (background only) at Windows sign-in", text())
        self.assertNotIn("Dashboard window created", text())
        self.assertEqual(text().count("Background monitoring started"), 1)        # 8: monitoring anyway
        r = subprocess.run([sys.executable, str(ROOT / "main.py"), "--autostart"], env=env, timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0)
        time.sleep(1)
        self.assertNotIn("Dashboard window created", text())                      # stays in the tray


if __name__ == "__main__":
    unittest.main()
