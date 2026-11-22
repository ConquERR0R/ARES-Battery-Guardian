"""Smart Audio Override (V2 Beta): mocked Windows Core Audio.

Every test uses FakeAudio, an in-memory stand-in for the default playback device(s); nothing here
touches the real Windows volume. Real-hardware behaviour is checked manually (see
docs/V2_SMART_AUDIO_OVERRIDE.md).
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.battery_monitor import BatterySnapshot  # noqa: E402
from app.database.database import Database  # noqa: E402
from app.services import audio_override as ao  # noqa: E402

try:
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:                                     # pragma: no cover
    HAVE_QT = False


class FakeAudio:
    """Several playback devices; one is the default. Records every call in `calls`."""

    def __init__(self, volume=0.3, mute=False):
        self.devices = {"dev-speakers": {"volume": volume, "mute": mute}}
        self.default = "dev-speakers"
        self.calls: list[tuple] = []
        self.unavailable = False          # no audio device / API failure on open()
        self.fail_set = False             # SetMasterVolumeLevelScalar fails
        self.open_count = self.close_count = 0

    def open(self, device_id=None):
        self.open_count += 1
        if self.unavailable:
            raise ao.AudioUnavailable("no default playback device (E_NOTFOUND)")
        ident = device_id or self.default
        if ident not in self.devices:
            raise ao.AudioUnavailable("the saved audio device is not available (E_NOTFOUND)")
        return FakeEndpoint(self, ident)

    def state(self, ident="dev-speakers"):
        d = self.devices[ident]
        return round(d["volume"], 3), d["mute"]


class FakeEndpoint:
    def __init__(self, audio: FakeAudio, ident: str):
        self.a, self.device_id, self.name = audio, ident, ident

    def get_volume(self):
        return self.a.devices[self.device_id]["volume"]

    def get_mute(self):
        return self.a.devices[self.device_id]["mute"]

    def set_volume(self, v):
        self.a.calls.append(("set_volume", self.device_id, v))
        if self.a.fail_set:
            raise OSError("[WinError -2147024891] Access is denied")
        self.a.devices[self.device_id]["volume"] = v

    def set_mute(self, m):
        self.a.calls.append(("set_mute", self.device_id, m))
        self.a.devices[self.device_id]["mute"] = m

    def close(self):
        self.a.close_count += 1


def new_db(tc):
    tmp = tempfile.TemporaryDirectory()
    tc.addCleanup(tmp.cleanup)
    db = Database(Path(tmp.name) / "a.db")
    tc.addCleanup(db.close)
    return db, Path(tmp.name) / "a.db"


def events(db):
    return [r["message"] for r in db.recent_events() if "Smart Audio Override" in r["message"]]


class OverrideLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.db, self.path = new_db(self)
        self.db.set_setting(ao.SETTING_KEY, True)
        self.audio = FakeAudio(volume=0.3, mute=True)
        self.ov = ao.AudioOverride(self.db, self.audio)

    def test_off_by_default_changes_nothing(self):
        db, _ = new_db(self)
        self.assertFalse(db.get_setting(ao.SETTING_KEY))
        ov = ao.AudioOverride(db, self.audio)
        self.assertEqual(ov.begin("High Battery Protection alarm"), "")
        self.assertEqual(self.audio.calls, [])
        self.assertEqual(self.audio.open_count, 0)           # the audio API is not even opened
        self.assertEqual(ov.end("released"), "")

    def test_begin_unmutes_and_sets_100_then_restores_exactly(self):
        msg = self.ov.begin("High Battery Protection alarm")
        self.assertIn("un-muted", msg)
        self.assertEqual(self.audio.state(), (1.0, False))
        rec = self.db.get_state(ao.STATE_KEY)                # saved BEFORE the change, for recovery
        self.assertEqual((rec["device_id"], rec["volume"], rec["mute"]), ("dev-speakers", 0.3, True))
        self.assertEqual(self.audio.calls[0][0], "set_volume")
        msg = self.ov.end("protection released")
        self.assertIn("restored volume 30%", msg)
        self.assertEqual(self.audio.state(), (0.3, True))      # exact original volume AND mute
        self.assertIsNone(self.db.get_state(ao.STATE_KEY))
        self.assertFalse(self.ov.active)
        self.assertEqual(self.audio.open_count, self.audio.close_count)   # every handle released

    def test_unmuted_volume_only_changes_volume(self):
        self.audio.devices["dev-speakers"] = {"volume": 0.55, "mute": False}
        self.ov.begin("Low Battery Protection alarm")
        self.assertNotIn(("set_mute", "dev-speakers", False), self.audio.calls)
        self.ov.end("released")
        self.assertEqual(self.audio.state(), (0.55, False))

    def test_already_audible_is_left_alone(self):
        self.audio.devices["dev-speakers"] = {"volume": 1.0, "mute": False}
        self.assertIn("nothing changed", self.ov.begin("alarm"))
        self.assertEqual(self.audio.calls, [])
        self.assertIsNone(self.db.get_state(ao.STATE_KEY))
        self.assertEqual(self.ov.end("released"), "")

    def test_full_volume_but_muted_is_only_unmuted_and_muted_again(self):
        self.audio.devices["dev-speakers"] = {"volume": 1.0, "mute": True}
        self.ov.begin("alarm")
        self.assertEqual(self.audio.state(), (1.0, False))
        self.ov.end("released")
        self.assertEqual(self.audio.state(), (1.0, True))

    def test_no_second_override_in_one_session(self):
        self.ov.begin("alarm")
        self.audio.devices["dev-speakers"]["volume"] = 0.4        # user turns it down during the alarm
        self.assertEqual(self.ov.begin("alarm repeat"), "")      # repeat alarm: no override again
        self.assertEqual(self.audio.state(), (0.4, False))

    def test_user_change_during_alarm_is_kept(self):
        self.ov.begin("alarm")
        self.audio.devices["dev-speakers"]["volume"] = 0.5        # user's own change
        msg = self.ov.end("released")
        self.assertIn("your setting is kept", msg)
        self.assertEqual(self.audio.state(), (0.5, False))        # not forced back to 30% muted
        self.assertIsNone(self.db.get_state(ao.STATE_KEY))

    def test_user_mutes_during_alarm_is_kept(self):
        self.ov.begin("alarm")
        self.audio.devices["dev-speakers"]["mute"] = True
        self.ov.end("released")
        self.assertEqual(self.audio.state(), (1.0, True))

    def test_tiny_rounding_still_counts_as_unchanged(self):
        self.ov.begin("alarm")
        self.audio.devices["dev-speakers"]["volume"] = 0.995       # driver rounding, not the user
        self.ov.end("released")
        self.assertEqual(self.audio.state(), (0.3, True))

    def test_no_audio_device_is_safe(self):
        self.audio.unavailable = True
        msg = self.ov.begin("alarm")
        self.assertIn("not applied", msg)
        self.assertIn("plays at the current volume", msg)
        self.assertFalse(self.ov.active)
        self.assertIsNone(self.db.get_state(ao.STATE_KEY))
        self.assertEqual(self.ov.end("released"), "")
        self.assertTrue(any("not applied" in e for e in events(self.db)))

    def test_api_failure_while_setting_puts_back_and_records_nothing(self):
        self.audio.fail_set = True
        msg = self.ov.begin("alarm")
        self.assertIn("could not change the Windows volume", msg)
        self.assertEqual(self.audio.state(), (0.3, True))
        self.assertIsNone(self.db.get_state(ao.STATE_KEY))
        self.assertFalse(self.ov.active)

    def test_restore_targets_the_saved_device_after_default_changes(self):
        self.ov.begin("alarm")
        self.audio.devices["dev-headphones"] = {"volume": 0.2, "mute": False}
        self.audio.default = "dev-headphones"                      # user plugs in headphones
        self.ov.end("released")
        self.assertEqual(self.audio.state("dev-speakers"), (0.3, True))
        self.assertEqual(self.audio.state("dev-headphones"), (0.2, False))   # never touched

    def test_device_gone_at_restore_is_retried_later_then_given_up(self):
        self.ov.begin("alarm")
        speakers = self.audio.devices.pop("dev-speakers")          # unplugged during the alarm
        msg = self.ov.end("released")
        self.assertIn("tries again at its next start", msg)
        self.assertEqual(self.db.get_state(ao.STATE_KEY)["attempts"], 1)
        self.audio.devices["dev-speakers"] = speakers               # back at the next start
        ao.AudioOverride(self.db, self.audio).recover()
        self.assertEqual(self.audio.state(), (0.3, True))
        self.assertIsNone(self.db.get_state(ao.STATE_KEY))
        # never back: give up after MAX_RECOVERY_ATTEMPTS and tell the user
        self.ov.begin("alarm")
        self.audio.devices.pop("dev-speakers")
        self.ov.end("released")
        for _ in range(ao.MAX_RECOVERY_ATTEMPTS):
            ao.AudioOverride(self.db, self.audio).recover()
        self.assertIsNone(self.db.get_state(ao.STATE_KEY))
        self.assertTrue(any("Please set it yourself" in e for e in events(self.db)))

    def test_events_are_logged(self):
        self.ov.begin("High Battery Protection alarm")
        self.ov.end("protection released")
        ev = events(self.db)
        self.assertTrue(any("volume 30% → 100%" in e for e in ev))
        self.assertTrue(any("restored volume 30% and mute" in e for e in ev))


class CrashRecoveryTest(unittest.TestCase):
    """The override record survives an unexpected exit; the next start restores (best effort)."""

    def setUp(self):
        self.db, self.path = new_db(self)
        self.db.set_setting(ao.SETTING_KEY, True)
        self.audio = FakeAudio(volume=0.25, mute=False)
        ao.AudioOverride(self.db, self.audio).begin("alarm")       # ...then ARES is killed
        self.assertEqual(self.audio.state(), (1.0, False))

    def test_next_start_restores(self):
        self.db.close()
        db2 = Database(self.path)                                   # "next start of ARES"
        self.addCleanup(db2.close)
        msg = ao.AudioOverride(db2, self.audio).recover()
        self.assertIn("restored volume 25%", msg)
        self.assertIn("did not exit normally", msg)
        self.assertEqual(self.audio.state(), (0.25, False))
        self.assertIsNone(db2.get_state(ao.STATE_KEY))

    def test_next_start_keeps_a_volume_the_user_set_meanwhile(self):
        self.audio.devices["dev-speakers"]["volume"] = 0.6
        ao.AudioOverride(self.db, self.audio).recover()
        self.assertEqual(self.audio.state(), (0.6, False))
        self.assertIsNone(self.db.get_state(ao.STATE_KEY))

    def test_recovery_even_if_the_setting_was_turned_off(self):
        self.db.set_setting(ao.SETTING_KEY, False)
        ao.AudioOverride(self.db, self.audio).recover()
        self.assertEqual(self.audio.state(), (0.25, False))

    def test_corrupt_record_is_dropped(self):
        self.db.set_state(ao.STATE_KEY, {"nonsense": 1})
        self.assertEqual(ao.AudioOverride(self.db, self.audio).recover(), "")
        self.assertIsNone(self.db.get_state(ao.STATE_KEY))

    def test_begin_restores_a_leftover_first(self):
        fresh = ao.AudioOverride(self.db, self.audio)           # new process, alarm starts again
        fresh.begin("alarm")
        self.assertEqual(fresh.saved.volume, 0.25)              # the user's real level, not 100%
        fresh.end("released")
        self.assertEqual(self.audio.state(), (0.25, False))


class BackendTest(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "checks the non-Windows fallback")
    def test_off_windows_audio_is_unavailable(self):
        self.assertIsInstance(ao.default_backend(), ao.NoAudio)
        with self.assertRaises(ao.AudioUnavailable):
            ao.WindowsCoreAudio().open()
        db, _ = new_db(self)
        db.set_setting(ao.SETTING_KEY, True)
        ov = ao.AudioOverride(db)
        self.assertIn("not applied", ov.begin("alarm"))          # safe: logged, alarm still plays

    def test_guid_layout(self):
        if sys.platform != "win32":
            self.skipTest("ctypes.WINFUNCTYPE is Windows only")
        g = ao._guid(ao.IID_IAudioEndpointVolume)
        self.assertEqual(hex(g.Data1), "0x5cdf2c82")


def snap(pct, ac=True):
    return BatterySnapshot(time.time(), True, "ioctl", percent=pct, ac_online=ac,
                           charging=bool(ac) and pct < 100, discharging=(ac is False))


class FakePlayer:
    def __init__(self, log):
        self.log = log

    def play(self, kind, path, volume=100):
        self.log.append(("play", kind))
        return True, "played"

    def stop(self):
        self.log.append(("stop",))


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class ProtectionIntegrationTest(unittest.TestCase):
    """The override follows the existing alarm lifecycle of the ProtectionManager."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        from app.ui import lock_window as lw

        self.db, _ = new_db(self)
        self.db.set_setting(ao.SETTING_KEY, True)
        self.audio = FakeAudio(volume=0.2, mute=True)
        self.order: list = []
        orig_set = FakeEndpoint.set_volume

        def spy(ep, v, _orig=orig_set):
            self.order.append(("set_volume", v))
            _orig(ep, v)
        FakeEndpoint.set_volume = spy
        self.addCleanup(setattr, FakeEndpoint, "set_volume", orig_set)
        self.reading = [(None, True)]
        self.m = lw.ProtectionManager(self.db, power_probe=lambda: self.reading[0],
                                      player=FakePlayer(self.order), audio=ao.AudioOverride(self.db, self.audio))
        self.addCleanup(self.m.shutdown)
        self.m.start()

    def flush(self):
        QCoreApplication.sendPostedEvents(None, 0)

    def test_override_before_first_alarm_and_restore_on_unplug(self):
        self.m.on_snapshot(snap(90, True))
        self.assertTrue(self.m.active)
        self.assertEqual(self.order[:2], [("set_volume", 1.0), ("play", "high")])   # audible from the first sound
        self.assertEqual(self.audio.state(), (1.0, False))
        self.reading[0] = (90, False)
        self.m._watch_tick()                                    # charger unplugged -> released
        self.flush()
        self.assertFalse(self.m.active)
        self.assertEqual(self.audio.state(), (0.2, True))

    def test_repeat_alarm_does_not_override_again(self):
        self.db.set_setting("high_alarm_repeat_min", 1)
        self.m.on_snapshot(snap(90, True))
        self.audio.devices["dev-speakers"]["volume"] = 0.35     # user turns it down
        self.m._alarm_repeat()                                  # the 1-minute repeat
        self.assertEqual(self.audio.state(), (0.35, False))
        self.m.emergency_release("test")
        self.assertEqual(self.audio.state(), (0.35, False))     # the user's choice is kept

    def test_low_battery_alarm_and_mode_switch(self):
        self.m.on_snapshot(snap(20, False))
        self.assertEqual(self.m.mode, "low")
        self.assertEqual(self.audio.state(), (1.0, False))
        self.m.emergency_release("test")
        self.assertEqual(self.audio.state(), (0.2, True))

    def test_no_override_when_the_alarm_is_silent(self):
        self.db.set_setting("high_alarm_enabled", False)
        self.m.on_snapshot(snap(90, True))
        self.assertTrue(self.m.active)
        self.assertEqual(self.audio.state(), (0.2, True))
        self.db.set_setting("high_alarm_enabled", True)
        self.m.emergency_release("test")
        self.db.set_setting("sound_enabled", False)
        self.reading[0] = (90, False)
        self.m._watch_tick()
        self.reading[0] = (90, True)
        self.m.on_snapshot(snap(91, True))
        self.assertEqual(self.audio.state(), (0.2, True))

    def test_exit_restores(self):
        self.m.on_snapshot(snap(90, True))
        self.m.shutdown()                                       # Exit ARES while the alarm is active
        self.assertEqual(self.audio.state(), (0.2, True))
        self.assertIsNone(self.db.get_state(ao.STATE_KEY))

    def test_start_recovers_a_leftover(self):
        from app.ui import lock_window as lw

        self.m.on_snapshot(snap(90, True))
        self.m.audio.saved = None                               # simulate a crash: record left in the DB
        m2 = lw.ProtectionManager(self.db, power_probe=lambda: (None, None), player=FakePlayer([]),
                                  audio=ao.AudioOverride(self.db, self.audio))
        self.addCleanup(m2.shutdown)
        m2.start()
        self.assertEqual(self.audio.state(), (0.2, True))

    def test_override_failure_never_blocks_the_alarm(self):
        self.audio.unavailable = True
        self.m.on_snapshot(snap(90, True))
        self.assertTrue(self.m.active)
        self.assertIn(("play", "high"), self.order)


if __name__ == "__main__":
    unittest.main()
