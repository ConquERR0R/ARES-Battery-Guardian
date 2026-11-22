"""V2 Beta packaging support: version label, About page, and the uninstaller cleanup that must
never change user data. Windows pieces are faked (see test_startup.install_fake_windows); the
real installer is tested on Windows by packaging/test_installer.py."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import app
from app.core import smart_wake as sw
from app.core import startup_manager as sm
from app.database.database import Database
from test_smart_wake import FakeScheduler
from test_startup import install_fake_windows

try:
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


class VersionTest(unittest.TestCase):
    def test_beta_label(self):
        self.assertEqual(app.APP_VERSION, "2.0.0")
        self.assertIn("Beta", app.APP_CHANNEL)
        self.assertEqual(app.APP_DISPLAY_VERSION, f"{app.APP_VERSION} {app.APP_CHANNEL}")

    def test_version_info_from_source(self):
        from app.build_info import version_info

        v = version_info()
        self.assertFalse(v["packaged"])
        self.assertEqual(v["build_date"], "running from source")
        self.assertTrue(v["pyside6"] and v["qt"] and v["python"])

    def test_version_info_packaged_reads_build_file(self):
        from app import build_info

        with tempfile.TemporaryDirectory() as d:
            Path(d, build_info.BUILD_INFO_FILE).write_text(
                '{"build_date": "2026-11-21 20:00", "python": "3.14.4", "pyinstaller": "6.22.3"}', encoding="utf-8")
            with mock.patch.object(sys, "frozen", True, create=True), \
                    mock.patch.object(sys, "_MEIPASS", d, create=True):
                v = build_info.version_info()
        self.assertTrue(v["packaged"])
        self.assertEqual(v["build_date"], "2026-11-21 20:00")
        self.assertEqual(v["build_pyinstaller"], "6.22.3")

    def test_cli_version(self):
        r = subprocess.run([sys.executable, str(ROOT / "main.py"), "--version"], capture_output=True,
                           text=True, timeout=60)
        self.assertIn(app.APP_DISPLAY_VERSION, r.stdout)


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class AboutPageTest(unittest.TestCase):
    def test_about_page(self):
        QApplication.instance() or QApplication([])
        from app.ui.about import AboutPage

        pg = AboutPage()
        self.addCleanup(pg.deleteLater)
        text = pg.text()
        self.assertIn(app.APP_VERSION, text)
        self.assertIn("Beta", text)
        self.assertIn("Data folder", text)
        pg.copy()
        self.assertEqual(QApplication.clipboard().text(), text)


class UninstallCleanupTest(unittest.TestCase):
    """Uninstall removes only THIS copy's startup entry and wake task, and changes no settings."""

    def setUp(self):
        install_fake_windows(self)

    def test_removes_own_run_key_only(self):
        sm.set_run_key(True)
        self.assertIn(sm.APP_ID, self.store)
        ok, lines = sm.remove_own_entries()
        self.assertTrue(ok, lines)
        self.assertNotIn(sm.APP_ID, self.store)
        self.assertEqual(self.store, self.others)              # other apps untouched

    def test_keeps_entry_of_another_copy(self):
        self.store[sm.APP_ID] = '"C:\\Python314\\pythonw.exe" "C:\\src\\main.py" --autostart'
        ok, lines = sm.remove_own_entries()
        self.assertTrue(ok)
        self.assertIn(sm.APP_ID, self.store)
        self.assertTrue(any("another ARES copy" in x for x in lines))

    def test_own_elevated_task_removed(self):
        self.task["cmd"] = sm._command_line()
        ok, lines = sm.remove_own_entries()
        self.assertTrue(ok, lines)
        self.assertIsNone(self.task["cmd"])

    def test_wake_task_only_if_ours(self):
        fs = FakeScheduler()
        self.assertEqual(sw.remove_own_task(fs), (True, "No wake task."))
        db = Database(":memory:")
        self.addCleanup(db.close)
        db.set_setting("wake_enabled", True)
        sw.ensure(db, fs)                                        # registered for this (fake) EXE
        ok, msg = sw.remove_own_task(fs)
        self.assertTrue(ok)
        self.assertEqual(fs.tasks, {})
        self.assertTrue(db.get_setting("wake_enabled"))          # setting unchanged
        other = FakeScheduler()
        other.tasks[sw.TASK_NAME] = sw.build_task_xml(r"C:\other\ARES.exe", ["--wake-check"],
                                                     __import__("datetime").datetime(2026, 1, 1), "u")
        ok, msg = sw.remove_own_task(other)
        self.assertIn("another ARES copy", msg)
        self.assertTrue(other.tasks)


class CleanupTouchesNoDataTest(unittest.TestCase):
    """`--uninstall-cleanup` and `--exit` (run by the installer/uninstaller) create no log file and
    open no database in the data folder."""

    def run_main(self, arg):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        env = dict(os.environ, ARES_DATA_DIR=d.name, QT_QPA_PLATFORM="offscreen")
        r = subprocess.run([sys.executable, str(ROOT / "main.py"), arg], env=env, timeout=60,
                           capture_output=True, text=True)
        files = sorted(str(p.relative_to(d.name)) for p in Path(d.name).rglob("*") if p.is_file())
        return r, files

    @unittest.skipIf(sys.platform == "win32", "on Windows this would act on the real Run key and wake "
                     "task of this source copy; packaging/verify_package.py checks it with save/restore")
    def test_uninstall_cleanup(self):
        r, files = self.run_main("--uninstall-cleanup")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(files, [])

    def test_exit_when_not_running(self):
        r, files = self.run_main("--exit")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("not running", r.stdout)
        self.assertEqual(files, [])


if __name__ == "__main__":
    unittest.main()
