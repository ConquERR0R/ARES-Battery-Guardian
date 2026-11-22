"""Persistent settings tests (V2, Part B). SIMULATED: restarts, updates and Windows restarts are
simulated with fresh processes / database connections in isolated temporary folders; the real
%LOCALAPPDATA% folder, registry and Task Scheduler are never touched.
"""
from __future__ import annotations

import array
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import paths
from app.database import database as dbm
from app.database.database import DEFAULT_SETTINGS, SCHEMA_VERSION, Database, migrate_settings
from app.services import sound_service
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


def write_wav(path: Path, samples: list[int], width: int = 2) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(width)
        w.setframerate(8000)
        if width == 2:
            a = array.array("h", samples)
            if sys.byteorder != "little":
                a.byteswap()
            w.writeframes(a.tobytes())
        else:
            w.writeframes(bytes(samples))


def read_wav16(path: Path) -> list[int]:
    with wave.open(str(path), "rb") as w:
        a = array.array("h")
        a.frombytes(w.readframes(w.getnframes()))
    if sys.byteorder != "little":
        a.byteswap()
    return list(a)


class FakePlayer:
    def __init__(self):
        self.played = []

    def play(self, kind, path, volume=100):
        self.played.append((kind, path, volume))
        return True, "fake"

    def stop(self):
        pass


# Non-default value for every setting shown on the Settings page.
CHANGED = {
    "max_charge": 85, "min_charge": 40, "auto_apply_thresholds": False,
    "strict_lock_enabled": False, "strict_lock_threshold": 95,
    "high_alarm_enabled": False, "high_alarm_volume": 60, "high_alarm_repeat_min": 7,
    "low_lock_enabled": False, "low_lock_threshold": 15,
    "low_alarm_enabled": False, "low_alarm_volume": 30, "low_alarm_repeat_min": 0,
    "monitoring_enabled": False, "monitor_interval_s": 120, "notifications_enabled": False,
    "sound_enabled": False, "notify_low_pct": 12, "temp_warn_c": 55, "notify_health_drop_pct": 9,
    "autostart": False, "start_minimized": True,
    "power_profile": "Balanced", "apply_profile_on_startup": True,
    "wake_enabled": True, "wake_low_enabled": False, "wake_low_threshold": 20,
    "wake_high_enabled": False, "wake_high_threshold": 92, "wake_interval_min": 15,
    "theme": "light", "audio_override_enabled": True,
}


# --------------------------------------------------------------------------------------------
# Storage location and migration
# --------------------------------------------------------------------------------------------
class StorageLocationTest(unittest.TestCase):
    def test_windows_uses_localappdata_not_program_or_temp_folder(self):
        with mock.patch.object(paths.sys, "platform", "win32"), \
                mock.patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\u\AppData\Local"}, clear=False):
            os.environ.pop("ARES_DATA_DIR", None)
            root = paths.user_data_root()
        self.assertEqual(str(root).replace("\\", "/"), "C:/Users/u/AppData/Local/ARES Battery Guardian")
        self.assertNotIn("Temp", str(root))
        self.assertNotIn("_MEI", str(root))                  # never the PyInstaller extraction folder

    def test_override_and_subfolders(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"ARES_DATA_DIR": d}):
            self.assertEqual(paths.base_dir(), Path(d))
            self.assertEqual(paths.database_path(), Path(d) / "data" / paths.DB_NAME)
            self.assertTrue(paths.cache_dir().is_dir())
            self.assertIsNone(paths.migrate_legacy_data())  # isolated: never imports other data

    def test_legacy_project_folder_data_is_copied_once_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            legacy = Path(d) / "old" / "battery_guardian.db"
            target = Path(d) / "new" / "data" / "battery_guardian.db"
            with Database(legacy) as old:
                old.set_setting("strict_lock_threshold", 93)
                old.log_event("info", "old history")
            msg = paths.migrate_legacy_data(target, legacy)
            self.assertIn("Copied", msg)
            self.assertTrue(legacy.is_file())                # old file left untouched
            with Database(target) as new:
                self.assertEqual(new.get_setting("strict_lock_threshold"), 93)
                self.assertTrue(any(r["message"] == "old history" for r in new.recent_events()))
                new.set_setting("strict_lock_threshold", 97)
            self.assertIsNone(paths.migrate_legacy_data(target, legacy))   # target exists: no copy
            with Database(target) as new:
                self.assertEqual(new.get_setting("strict_lock_threshold"), 97)
            self.assertIsNone(paths.migrate_legacy_data(Path(d) / "x.db", Path(d) / "missing.db"))


class LegacyWalTest(unittest.TestCase):
    def test_legacy_copy_includes_uncheckpointed_wal_data(self):
        """The laptop's old project-folder database has a large -wal file next to it: the
        history in that WAL must be part of the one-time copy."""
        import shutil
        with tempfile.TemporaryDirectory() as d:
            live = Path(d) / "live.db"
            con = sqlite3.connect(live)
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA wal_autocheckpoint=0")
            con.executescript(dbm.SCHEMA)
            con.executemany("INSERT INTO samples(ts, percent) VALUES (?, ?)", [(i, 50) for i in range(300)])
            con.execute("INSERT INTO settings VALUES ('strict_lock_threshold', '87')")
            con.commit()
            legacy = Path(d) / "old" / "battery_guardian.db"
            legacy.parent.mkdir()
            for suffix in ("", "-wal", "-shm"):               # files as left on disk, not checkpointed
                shutil.copy(str(live) + suffix, str(legacy) + suffix)
            con.close()
            self.assertGreater((Path(str(legacy) + "-wal")).stat().st_size, 0)
            target = Path(d) / "new" / "battery_guardian.db"
            self.assertIn("Copied", paths.migrate_legacy_data(target, legacy))
            with Database(target) as db:
                self.assertEqual(db.query("SELECT count(*) FROM samples")[0][0], 300)
                self.assertEqual(db.get_setting("strict_lock_threshold"), 87)


class DatabaseSafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "data" / "battery_guardian.db"

    def make_v1_database(self):
        """A database as written by ARES v1.2 (schema without user_version, legacy keys)."""
        self.path.parent.mkdir(parents=True)
        con = sqlite3.connect(self.path)
        con.executescript(dbm.SCHEMA)
        con.execute("PRAGMA user_version=0")
        con.executemany("INSERT INTO settings VALUES (?,?)", [
            ("strict_lock_threshold", "88"), ("alert_sound_path", '"C:\\\\my.wav"'), ("sound_at_max", "false")])
        con.execute("INSERT INTO samples(ts, percent) VALUES (1, 50)")
        con.execute("INSERT INTO events VALUES (1, 'info', 'v1 event')")
        con.commit()
        con.close()

    def test_update_keeps_settings_and_history_and_makes_a_backup(self):
        self.make_v1_database()
        db = Database(self.path)                             # "install the new version"
        self.addCleanup(db.close)
        self.assertIsNotNone(db.backup_path)
        self.assertTrue(Path(db.backup_path).is_file())
        self.assertEqual(db.query("PRAGMA user_version")[0][0], SCHEMA_VERSION)
        migrate_settings(db)
        self.assertEqual(db.get_setting("strict_lock_threshold"), 88)
        self.assertEqual(db.get_setting("high_alarm_sound_path"), "C:\\my.wav")
        self.assertFalse(db.get_setting("sound_enabled"))
        self.assertEqual(db.query("SELECT count(*) FROM samples")[0][0], 1)
        self.assertEqual(db.get_setting("wake_interval_min"), 5)    # new keys: defaults, not written
        self.assertNotIn("wake_interval_min", db.saved_settings())
        db.close()
        db2 = Database(self.path)                            # second start: no new backup
        self.addCleanup(db2.close)
        self.assertIsNone(db2.backup_path)

    def test_corrupt_database_is_kept_and_replaced(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"this is not a database" * 100)
        db = Database(self.path)
        self.addCleanup(db.close)
        self.assertIn("damaged", db.recovery_message)
        self.assertTrue(list(self.path.parent.glob("*.corrupt-*.db")))
        db.set_setting("theme", "light")                     # usable again
        self.assertEqual(db.get_setting("theme"), "light")

    def test_set_settings_is_atomic_and_verified(self):
        db = Database(self.path)
        self.addCleanup(db.close)
        db.set_settings({"a_key": 1, "theme": "light"})
        self.assertEqual(db.saved_settings(), {"a_key": 1, "theme": "light"})
        with self.assertRaises(TypeError):                   # one bad value: nothing written
            db.set_settings({"theme": "dark", "bad": object()})
        self.assertEqual(db.get_setting("theme"), "light")
        # Something rewrites the value behind ARES' back: the read-back must notice.
        db.execute("CREATE TRIGGER t AFTER UPDATE ON settings BEGIN "
                   "UPDATE settings SET value='\"x\"' WHERE key=NEW.key AND NEW.key='theme'; END")
        with self.assertRaises(RuntimeError):
            db.set_settings({"theme": "dark"})

    def test_reset_settings_keeps_history(self):
        db = Database(self.path)
        self.addCleanup(db.close)
        db.set_settings({"strict_lock_threshold": 80, "theme": "light"})
        db.execute("INSERT INTO samples(ts, percent) VALUES (1, 50)")
        db.log_event("info", "keep me")
        db.set_state("settings_version", 3)
        n = db.reset_settings()
        self.assertEqual(n, 2)
        self.assertEqual(db.get_setting("strict_lock_threshold"), DEFAULT_SETTINGS["strict_lock_threshold"])
        self.assertEqual(db.query("SELECT count(*) FROM samples")[0][0], 1)
        self.assertTrue(any(r["message"] == "keep me" for r in db.recent_events()))
        self.assertEqual(db.get_state("settings_version"), 3)

    def test_saved_value_survives_abrupt_process_end(self):
        """Simulated Windows restart / power loss right after Save: the process ends without
        closing the database (os._exit). The committed transaction must still be there."""
        code = ("import sys, os; sys.path.insert(0, sys.argv[1]); from app.database.database import Database; "
                "db = Database(sys.argv[2]); db.set_settings({'wake_interval_min': 25, 'theme': 'light'}); "
                "os._exit(0)")
        subprocess.run([sys.executable, "-c", code, str(ROOT), str(self.path)], check=True, timeout=60)
        with Database(self.path) as db:
            self.assertEqual(db.get_setting("wake_interval_min"), 25)
            self.assertEqual(db.get_setting("theme"), "light")


# --------------------------------------------------------------------------------------------
# Alarm sound volume (the user's file is only read)
# --------------------------------------------------------------------------------------------
class VolumeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "cache"

    def test_16bit_scaled_copy_source_unchanged(self):
        src = Path(self.tmp.name) / "a.wav"
        write_wav(src, [1000, -2000, 30000, -32768])
        before = src.read_bytes()
        out, note = sound_service.scaled_copy(src, 50, self.cache)
        self.assertEqual(note, "")
        self.assertNotEqual(out, src)
        self.assertEqual(read_wav16(out), [500, -1000, 15000, -16384])
        self.assertEqual(src.read_bytes(), before)
        self.assertEqual(sound_service.scaled_copy(src, 50, self.cache)[0], out)   # cached
        self.assertEqual(sound_service.scaled_copy(src, 100, self.cache), (src, ""))

    def test_8bit_and_unsupported(self):
        src = Path(self.tmp.name) / "b.wav"
        write_wav(src, [128, 255, 0], width=1)
        out, _ = sound_service.scaled_copy(src, 50, self.cache)
        with wave.open(str(out), "rb") as w:
            self.assertEqual(list(w.readframes(3)), [128, 191, 64])
        bad = Path(self.tmp.name) / "c.wav"
        bad.write_bytes(b"RIFF\x00\x00\x00\x00WAVEjunk")
        out, note = sound_service.scaled_copy(bad, 50, self.cache)
        self.assertEqual(out, bad)                           # falls back to the file itself
        self.assertIn("volume not applied", note)

    def test_play_alarm_rules(self):
        played = []
        fake = mock.Mock()
        fake.SND_FILENAME, fake.SND_ASYNC, fake.SND_NODEFAULT, fake.SND_ALIAS = 1, 2, 4, 8
        fake.PlaySound.side_effect = lambda s, f: played.append(s)
        good = Path(self.tmp.name) / "good.wav"
        write_wav(good, [100, 200])
        with mock.patch.object(sound_service, "_winsound", return_value=fake), \
                mock.patch.object(sound_service.paths if hasattr(sound_service, "paths") else paths,
                                  "cache_dir", return_value=self.cache):
            ok, how = sound_service.play_alarm("high", str(good), 100)
            self.assertTrue(ok)
            self.assertEqual(played[-1], str(good))
            ok, how = sound_service.play_alarm("high", str(good), 0)
            self.assertFalse(ok)
            self.assertIn("muted", how)
            ok, how = sound_service.play_alarm("low", str(Path(self.tmp.name) / "missing.wav"), 100)
            self.assertTrue(ok)                              # an alarm is still heard...
            self.assertTrue(how.startswith("⚠"))             # ...and the problem is reported
            self.assertIn("missing.wav", how)


# --------------------------------------------------------------------------------------------
# Settings page: save model, persistence, restore defaults (Qt)
# --------------------------------------------------------------------------------------------
@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class SettingsSaveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "s.db")
        from app.core import startup_manager as sm
        from app.core import smart_wake as sw
        # The fake Run key starts in step with the default "start with Windows" preference (on),
        # as after ARES's launch check. On Windows the box shows the real registration, so a fake
        # that starts "off" would already show the CHANGED value (off) and no change would be saved.
        self.reg = {"on": True}

        def apply(db, wanted, source=""):
            self.reg["on"] = bool(wanted)
            db.set_state("startup_last_result", {"ok": True, "message": "test", "wanted": bool(wanted)})
            return True, "test"
        for p in (mock.patch.object(sm, "apply_preference", apply),
                  mock.patch.object(sm, "status", lambda: sm.StartupStatus(
                      self.reg["on"], "Registry Run key (current user)" if self.reg["on"] else "Disabled",
                      None, None, [], self.reg["on"])),
                  mock.patch.object(sw, "detect", lambda: sw.WakeSupport(windows=False))):
            p.start()
            self.addCleanup(p.stop)

    def page(self, db):
        from app.ui.settings import SettingsPage

        pg = SettingsPage(db, player=FakePlayer(), wake_tester=lambda: (True, ["fake"]))
        self.addCleanup(pg.deleteLater)
        return pg

    def open_db(self):
        db = Database(self.path)
        self.addCleanup(db.close)
        return db

    def set_all(self, pg):
        """Change every control the way a user would."""
        for key, value in CHANGED.items():
            if key == "autostart":
                # The field's loader shows the REAL registration on Windows (not the value passed
                # in), so tick/untick the box directly, as the user does.
                pg.autostart.setChecked(value)
            else:
                pg._fields[key][1](value)

    def test_every_setting_survives_restart(self):
        db = self.open_db()
        pg = self.page(db)
        self.set_all(pg)
        self.assertEqual({k: pg.values()[k] for k in CHANGED}, CHANGED)
        keys = []
        pg.setting_changed.connect(keys.append)
        self.assertTrue(pg.save(), pg.status_lbl.text())
        self.assertIn("All changes saved", pg.status_lbl.text())
        self.assertEqual(set(keys), set(CHANGED))
        db.close()                                           # exit ARES
        db2 = self.open_db()                                 # start again
        for k, v in CHANGED.items():
            self.assertEqual(db2.get_setting(k), v, k)
        pg2 = self.page(db2)
        self.assertEqual({k: pg2.values()[k] for k in CHANGED}, CHANGED)
        self.assertFalse(pg2.is_dirty())

    def test_opening_never_overwrites_saved_values(self):
        db = self.open_db()
        db.set_settings(CHANGED)
        before = db.saved_settings()
        pg = self.page(db)
        pg.load()
        pg.refresh_from_db()
        self.assertTrue(pg.confirm_close())                  # nothing dirty: closes, no prompt
        self.assertEqual(db.saved_settings(), before)

    def test_status_states(self):
        db = self.open_db()
        pg = self.page(db)
        self.assertIn("saved", pg.status_lbl.text())
        pg.wake_interval.setValue(20)
        self.assertIn("Unsaved changes", pg.status_lbl.text())
        self.assertTrue(pg.discard_btn.isEnabled())
        pg.discard_btn.click()
        self.assertEqual(pg.wake_interval.value(), 5)
        self.assertFalse(pg.is_dirty())

    def test_failed_write_is_reported_and_nothing_claimed(self):
        db = self.open_db()
        pg = self.page(db)
        keys = []
        pg.setting_changed.connect(keys.append)
        pg.lock_threshold.setValue(80)
        with mock.patch.object(db, "set_settings", side_effect=sqlite3.OperationalError("disk I/O error")):
            self.assertFalse(pg.save())
        self.assertIn("Save failed", pg.status_lbl.text())
        self.assertIn("disk I/O error", pg.status_lbl.text())
        self.assertNotIn("All changes saved", pg.status_lbl.text())
        self.assertEqual(keys, [])                           # services not told about unsaved values
        self.assertTrue(pg.is_dirty())
        self.assertEqual(db.get_setting("strict_lock_threshold"), 90)
        self.assertTrue(pg.save())                           # retry works
        self.assertEqual(db.get_setting("strict_lock_threshold"), 80)

    def test_missing_custom_wav_is_kept_and_warned(self):
        db = self.open_db()
        wav = Path(self.tmp.name) / "siren.wav"
        write_wav(wav, [1, 2, 3])
        pg = self.page(db)
        pg.high_alarm.set_path(str(wav))
        self.assertTrue(pg.save())
        wav.unlink()                                         # file moved/deleted later
        db.close()
        db2 = self.open_db()
        pg2 = self.page(db2)
        self.assertEqual(pg2.high_alarm.value(), str(wav))   # path kept, not replaced
        self.assertIn("not found", pg2.high_alarm.status.text())
        self.assertFalse(pg2.is_dirty())
        self.assertEqual(db2.get_setting("high_alarm_sound_path"), str(wav))

    def test_restore_defaults_asks_and_keeps_history(self):
        db = self.open_db()
        db.set_settings(CHANGED)
        db.execute("INSERT INTO samples(ts, percent) VALUES (1, 50)")
        pg = self.page(db)
        from PySide6.QtWidgets import QMessageBox
        with mock.patch("app.ui.settings.QMessageBox.question", return_value=QMessageBox.No):
            self.assertFalse(pg.restore_defaults(confirm=True))
        self.assertEqual(db.get_setting("theme"), "light")   # cancelled: nothing changed
        keys = []
        pg.setting_changed.connect(keys.append)
        with mock.patch("app.ui.settings.QMessageBox.question", return_value=QMessageBox.Yes):
            self.assertTrue(pg.restore_defaults(confirm=True))
        for k in CHANGED:
            self.assertEqual(db.get_setting(k), DEFAULT_SETTINGS[k], k)
            self.assertEqual(pg.values()[k] if k != "autostart" else self.reg["on"],
                             DEFAULT_SETTINGS[k] if k != "power_profile" else "", k)
        self.assertTrue(self.reg["on"])                      # default autostart re-applied
        self.assertEqual(db.query("SELECT count(*) FROM samples")[0][0], 1)
        self.assertIn("theme", keys)
        self.assertIn("Default settings restored", pg.status_lbl.text())

    def test_unsaved_changes_on_close(self):
        from app.ui import settings as st
        db = self.open_db()
        pg = self.page(db)
        pg.wake_interval.setValue(30)
        with mock.patch.object(pg, "ask_unsaved", return_value=st.CANCEL):
            self.assertFalse(pg.confirm_close(allow_cancel=True))
        self.assertTrue(pg.is_dirty())
        with mock.patch.object(pg, "ask_unsaved", return_value=st.DISCARD):
            self.assertTrue(pg.confirm_close())
        self.assertEqual(db.get_setting("wake_interval_min"), 5)
        pg.wake_interval.setValue(30)
        with mock.patch.object(pg, "ask_unsaved", return_value=st.SAVE):
            self.assertTrue(pg.confirm_close(allow_cancel=False))      # Exit ARES: saved first
        self.assertEqual(db.get_setting("wake_interval_min"), 30)

    def test_alarm_test_uses_selected_sound_and_volume(self):
        db = self.open_db()
        pg = self.page(db)
        pg.low_volume.setValue(20)
        pg.low_alarm.play_test()
        self.assertEqual(pg.player.played[-1], ("low", None, 20))
        self.assertEqual(db.get_setting("low_alarm_volume"), 100)       # test never saves

    def test_wake_section(self):
        db = self.open_db()
        pg = self.page(db)
        pg.refresh_wake_support()
        self.assertIn("needs Windows", pg.wake_support_lbl.text())
        self.assertIn("No wake check", pg.wake_last_lbl.text())
        pg.wake_interval.setValue(10)
        ok, lines = pg.test_wake()
        self.assertTrue(ok)
        self.assertTrue(any("unsaved" in x for x in lines))


# --------------------------------------------------------------------------------------------
# Whole app: protection uses saved alarm settings, theme, window state (Qt)
# --------------------------------------------------------------------------------------------
@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class AppPersistenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        use_fake_battery(self)            # never the laptop's real battery (see fake_hw.py)

    def make(self, path, player=None):
        from app.ui.background_app import AresBackgroundApp

        bg = AresBackgroundApp(path, power_probe=lambda: (None, None), alarm_player=player or FakePlayer())
        self.addCleanup(bg.shutdown)
        return bg

    def test_alarm_enable_and_volume_are_used(self):
        from app.core.battery_monitor import BatterySnapshot

        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "a.db")
            with Database(path) as pre:
                pre.set_settings({"high_alarm_enabled": False, "low_alarm_volume": 35})
            player = FakePlayer()
            bg = self.make(path, player)
            bg._on_snapshot(BatterySnapshot(time.time(), True, "ioctl", percent=95, ac_online=True,
                                            charging=True, discharging=False))
            self.assertTrue(bg.lock.active)
            self.assertEqual(player.played, [])              # high alarm disabled: screen, no sound
            bg._on_snapshot(BatterySnapshot(time.time(), True, "ioctl", percent=10, ac_online=False,
                                            charging=False, discharging=True))
            self.assertEqual(bg.lock.mode, "low")
            self.assertEqual(player.played[-1][0], "low")
            self.assertEqual(player.played[-1][2], 35)
            bg.shutdown()

    def test_theme_and_window_state_survive_restart(self):
        from app.ui.theme import THEMES, apply_theme

        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "w.db")
            bg = self.make(path)
            bg.open_dashboard(3)
            w = bg.window
            sp = w.settings_page
            sp.theme.setCurrentIndex(sp.theme.findData("light"))
            self.assertTrue(sp.save())
            self.assertEqual(QApplication.instance().palette().window().color().name(), THEMES["light"]["bg"])
            w.close()                                        # reopen under the new theme
            spin(50)
            bg.open_dashboard(3)
            w = bg.window
            w.resize(700, 520)
            spin(50)
            size = (w.width(), w.height())                   # what Qt actually allowed
            w.close()
            spin(50)
            bg.shutdown()
            bg2 = self.make(path)                            # restart
            self.assertEqual(bg2.db.get_setting("theme"), "light")
            bg2.open_dashboard(None)
            self.assertEqual(bg2.window.tabs.currentIndex(), 3)
            self.assertEqual((bg2.window.width(), bg2.window.height()), size)
            self.assertNotEqual(size, (1040, 720))              # not just the default size
            bg2.shutdown()
            self.assertEqual(apply_theme(QApplication.instance(), "nonsense"), "dark")


if __name__ == "__main__":
    unittest.main()
