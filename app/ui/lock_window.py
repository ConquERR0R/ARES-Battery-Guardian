"""Battery protection screens: one shared full-screen window class and its manager.

Two modes use the same window (see app/core/strict_lock.py for the rules):
  * HIGH - Strict Charging Protection: battery >= 90% (default) with the charger connected.
  * LOW  - Low Battery Protection: battery <= 25% (default) with the charger disconnected.

What this is: an always-on-top, full-screen ARES window that blocks interaction with ARES'
own windows (application-modal) until the charger condition changes, plus an alarm sound.

What this is NOT: an operating-system lock. It installs no keyboard hooks, does not touch
Ctrl+Alt+Del, Task Manager, shutdown, the secure desktop or any Windows policy, and the
ARES process can always be ended normally. Windows shortcuts such as Ctrl+Alt+Del, Win+L
and Alt+Tab keep working. It does not stop or start charging.

Emergency release (documented in README and shown on the window):
  * Ctrl+Alt+Shift+U on the protection screen -> released until the charger state changes
  * `python main.py --unlock` (or `ARES-Battery-Guardian.exe --unlock`) from a terminal
  * `--disable-lock` turns both protection screens off entirely
  * Ctrl+Alt+Del -> Task Manager -> end "ARES Battery Guardian" / python, as with any app
  * Automatic fail-safe: if the charger state cannot be read for 10 s, the screen releases itself
"""
from __future__ import annotations

import logging
import time
from typing import Callable

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from app.core.strict_lock import (EVENT_KIND, HIGH, LOCK, LOW, MODE_NAME, UNLOCK, LockDecision,
                                  ProtectionPolicy)
from app.core.power_request import PowerRequest
from app.database.database import Database
from app.services.sound_service import AlarmPlayer

log = logging.getLogger(__name__)

WATCH_INTERVAL_MS = 1000     # charger check while a protection screen is shown
IDLE_POLL_MS = 5000          # cheap power check while no screen is shown (GetSystemPowerStatus, no WMI)
FAILSAFE_UNKNOWN_S = 10      # charger state unreadable this long while locked -> release
EMERGENCY_SHORTCUT = "Ctrl+Alt+Shift+U"

ALARM_PATH_KEY = {HIGH: "high_alarm_sound_path", LOW: "low_alarm_sound_path"}
ALARM_ENABLED_KEY = {HIGH: "high_alarm_enabled", LOW: "low_alarm_enabled"}
ALARM_VOLUME_KEY = {HIGH: "high_alarm_volume", LOW: "low_alarm_volume"}
SCREEN_HOLD_ON_BATTERY_S = 15 * 60   # keep a LOW screen's display awake at most this long on battery
WAKE_HOLD_MAX_S = 60 * 60            # after an 89% wake on the charger, stay awake at most this long
ALARM_REPEAT_KEY = {HIGH: "high_alarm_repeat_min", LOW: "low_alarm_repeat_min"}

# The probe lives in a Qt-free module so the --wake-check process can use it without the GUI;
# re-exported here for existing imports.
from app.core.power_probe import PowerReading, default_power_probe  # noqa: E402,F401
from app.services.audio_override import AudioOverride  # noqa: E402


# Per-mode look and text. All visible strings live here so both modes share one window class.
STYLE = {
    HIGH: dict(bg="#120607", accent="#ff2d2d", dim="#5a1414", headline="#ff5c5c", headline_dim="#ff9b9b",
               icon="⚠",
               headline_fmt="BATTERY LIMIT REACHED",
               action="PLEASE REMOVE YOUR CHARGER",
               info="Your battery has reached the configured limit. Disconnect the charger to continue "
                    "using your computer.",
               waiting="Waiting for charger disconnection..."),
    LOW: dict(bg="#140d02", accent="#ff9f1a", dim="#5c3a06", headline="#ffb547", headline_dim="#ffd699",
              icon="⚠",
              headline_fmt="LOW BATTERY – {thr}%",
              action="PLEASE CONNECT YOUR CHARGER",
              info="Your battery is running low. Connect your charger to continue using your computer.",
              waiting="Waiting for charger connection..."),
}


class ProtectionWindow(QWidget):
    """Shared full-screen protection window (HIGH or LOW mode)."""

    emergency_requested = Signal()

    def __init__(self, mode: str, threshold: int) -> None:
        super().__init__(None)
        self.mode = mode
        self.st = STYLE[mode]
        self._allow_close = False
        self.setObjectName("protectionScreen")
        self.setWindowTitle("ARES Battery Guardian - " + MODE_NAME[mode])
        # No title bar => no close/minimize buttons. Always on top. Application-modal so the
        # ARES dashboard cannot be used underneath. (Not system-modal: Windows security screens,
        # Ctrl+Alt+Del and Task Manager keep working.)
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setWindowModality(Qt.ApplicationModal)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setStyleSheet(f"""
            QWidget#protectionScreen {{ background: {self.st['bg']}; }}
            QLabel {{ color: #f3f4f6; background: transparent; }}
            QLabel#brand {{ color: #9aa3b5; letter-spacing: 4px; }}
            QLabel#action {{ color: #ffffff; }}
            QLabel#info {{ color: #c0c6d4; }}
            QLabel#status {{ color: #f5b942; }}
            QLabel#footer {{ color: #8b93a7; }}
        """)

        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignCenter)
        scr = QGuiApplication.primaryScreen()
        text_width = min(900, (scr.geometry().width() if scr else 1024) - 120)   # never wider than the screen
        lay.setSpacing(18)

        def label(text, name, size, bold=False):
            lbl = QLabel(text)
            lbl.setObjectName(name)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setWordWrap(True)
            lbl.setMinimumWidth(text_width)
            f = QFont(lbl.font())
            f.setPointSize(size)
            f.setBold(bold)
            lbl.setFont(f)
            lay.addWidget(lbl)
            return lbl

        self.brand = label("ARES BATTERY GUARDIAN", "brand", 14, True)
        self.icon = label(self.st["icon"], "icon", 96, True)
        self.headline = label("", "headline", 34, True)
        self.action = label(self.st["action"], "action", 24, True)
        self.info = label(self.st["info"], "info", 14)
        self.battery = label("Battery: N/A", "info", 18, True)
        self.charger = label("Charger: N/A", "info", 18, True)
        self.status = label(self.st["waiting"], "status", 16, True)
        self.footer = label("This screen is a reminder; it does not stop or start charging. "
                            f"Emergency release: click this screen, then press {EMERGENCY_SHORTCUT}  ·  "
                            "Ctrl+Alt+Del → Task Manager also works.", "footer", 11)
        self.set_threshold(threshold)

        self._pulse_on = False
        self.pulse = QTimer(self)
        self.pulse.timeout.connect(self._pulse)
        self.pulse.start(550)

        sc = QShortcut(QKeySequence(EMERGENCY_SHORTCUT), self)
        sc.setContext(Qt.WindowShortcut)
        sc.activated.connect(self.emergency_requested)

    # ---- display ------------------------------------------------------------------
    def set_threshold(self, threshold: int) -> None:
        self.headline.setText(self.st["headline_fmt"].format(thr=threshold))

    def _pulse(self) -> None:
        self._pulse_on = not self._pulse_on
        self.icon.setStyleSheet(f"color:{self.st['accent'] if self._pulse_on else self.st['dim']};")
        self.headline.setStyleSheet(f"color:{self.st['headline'] if self._pulse_on else self.st['headline_dim']};")
        self.update()                         # repaint the flashing border

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(self.st["bg"]))
        p.setPen(QPen(QColor(self.st["accent"] if self._pulse_on else self.st["dim"]), 16))
        p.drawRect(self.rect().adjusted(8, 8, -8, -8))
        p.end()

    def set_readings(self, percent: float | None, ac_online: bool | None,
                     charging: bool | None = None) -> None:
        txt = "Battery: N/A" if percent is None else f"Battery: {percent:.0f}%"
        if percent is not None:
            if ac_online is False:
                txt += "  ·  discharging"
            elif charging is True:
                txt += "  ·  charging"
            elif ac_online is True and charging is False:
                txt += "  ·  not charging"
        self.battery.setText(txt)
        self.charger.setText({True: "Charger: CONNECTED", False: "Charger: DISCONNECTED",
                              None: "Charger: unknown"}[ac_online])

    def cover_screen(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            self.setGeometry(screen.geometry())
        self.showFullScreen()
        self.raise_()
        self.activateWindow()

    # ---- no ordinary dismissal ---------------------------------------------------------
    def closeEvent(self, ev) -> None:
        if self._allow_close:
            ev.accept()
        else:
            ev.ignore()          # Alt+F4 / close requests are ignored while the condition lasts

    def keyPressEvent(self, ev) -> None:
        if ev.key() == Qt.Key_Escape:
            ev.ignore()
            return
        super().keyPressEvent(ev)

    def changeEvent(self, ev) -> None:
        super().changeEvent(ev)
        if self.windowState() & Qt.WindowMinimized and not self._allow_close:
            QTimer.singleShot(0, self.cover_screen)   # restore if minimized (e.g. Win+D)

    def force_close(self) -> None:
        self._allow_close = True
        self.pulse.stop()
        self.close()


class ProtectionManager(QObject):
    """Owns the single protection window and the alarm. Lives on the GUI thread inside the
    background app, so it works whether or not the dashboard is open."""

    lock_changed = Signal(bool)

    def __init__(self, db: Database, power_probe: Callable[[], PowerReading | None] = default_power_probe,
                 clock: Callable[[], float] = time.monotonic, player=None, power=None, audio=None) -> None:
        super().__init__()
        self.db = db
        # Smart Audio Override (optional setting): temporarily un-mute / 100% volume while an alarm sounds.
        self.audio = audio if audio is not None else AudioOverride(db)
        self.power = power or PowerRequest()      # keep-awake requests (SetThreadExecutionState)
        self._screen_since: float | None = None
        self._wake_hold_since: float | None = None
        self.power_probe = power_probe
        self.clock = clock
        self.player = player or AlarmPlayer()
        self.policy = ProtectionPolicy(db)
        self.window: ProtectionWindow | None = None
        self.windows_created = 0
        self.alarms_played = 0
        self._started = False
        self._closed = False                # after shutdown(): the database may be closed, ignore input
        self._percent: float | None = None
        self._ac: bool | None = None
        self._charging: bool | None = None
        self._snapshot_ac: bool | None = None
        self._unknown_since: float | None = None
        self.watch = QTimer(self)
        self.watch.timeout.connect(self._watch_tick)
        self.alarm_timer = QTimer(self)
        self.alarm_timer.timeout.connect(self._alarm_repeat)

    @property
    def active(self) -> bool:
        return self.window is not None

    @property
    def mode(self) -> str | None:
        return self.window.mode if self.window is not None else None

    def start(self) -> None:
        """Begin the cheap idle power check (so protection reacts within seconds, independent of
        the monitoring interval)."""
        self._started = True
        try:
            self.audio.recover()            # an override left behind by a crash / power loss
        except Exception:
            log.exception("Audio override recovery failed")
        if self.window is None:
            self.watch.start(IDLE_POLL_MS)

    # ---- inputs ---------------------------------------------------------------------------
    def on_snapshot(self, s) -> None:
        if self._closed:
            return
        if not s.available:
            self._snapshot_ac = None
            return
        self._snapshot_ac = s.ac_online
        self._percent = s.percent
        self._ac = s.ac_online
        self._charging = s.charging
        self._evaluate()

    def settings_changed(self) -> None:
        """Called when protection settings change: re-decide with the latest readings."""
        if self._closed:
            return
        if self.window is not None:
            self.window.set_threshold(self.policy.threshold(self.window.mode))
            self._schedule_alarm_repeat(self.window.mode)
        self._evaluate()

    def _watch_tick(self) -> None:
        if self._closed:
            self.watch.stop()
            return
        reading = self.power_probe()
        pct, ac = reading if reading is not None else (None, None)
        if pct is not None:
            self._percent = float(pct)
        if ac is None:
            ac = self._snapshot_ac          # fall back to the monitoring thread's last reading
        if self.window is None:
            self._ac = ac                   # None = unknown now (never act on a stale reading)
            if ac is not None:
                self._evaluate()
            self._update_power()
            return
        # A screen is shown: unlock quickly on the charger change; fail-safe on unknown.
        if ac is None:
            now = self.clock()
            if self._unknown_since is None:
                self._unknown_since = now
            elif now - self._unknown_since >= FAILSAFE_UNKNOWN_S:
                self.emergency_release("Charger status could not be read for "
                                       f"{FAILSAFE_UNKNOWN_S} s; protection screen released for safety")
                return
            self.window.set_readings(self._percent, None, None)
            self.window.status.setText("Charger status unavailable - releasing shortly for safety...")
            return
        self._unknown_since = None
        if ac != self._ac:
            self._charging = None           # stale once the charger state changed
        self._ac = ac
        self.window.status.setText(STYLE[self.window.mode]["waiting"])
        self._evaluate()
        self._update_power()

    # ---- Smart Wake-Up integration + keep-awake ----------------------------------------------
    def wake_trigger(self, mode: str, reason: str) -> None:
        """ARES was launched/activated by a Smart Wake-Up check that found `mode` conditions.
        Re-read the battery now; the normal policy decides whether a screen appears (so the 90%
        protection threshold is NOT changed by the 89% wake threshold)."""
        if self._closed:
            return
        if self.window is None and self._wake_hold_since is None:     # no log flood on repeats
            self.db.log_event("smart_wake", f"Smart Wake-Up trigger ({mode}): {reason}")
        self._watch_tick()
        if (self.window is None and self._wake_hold_since is None and mode == HIGH and self._ac is True
                and bool(self.db.get_setting("strict_lock_enabled"))):
            # 89% on the charger: stay awake (system only, screen may stay off) until the battery
            # reaches the protection threshold, the charger is unplugged or the hold times out.
            self._wake_hold_since = self.clock()
            self.db.log_event("smart_wake", "Keeping the laptop awake until the battery reaches "
                                            f"{self.policy.threshold(HIGH)}% or the charger is unplugged "
                                            f"(max {WAKE_HOLD_MAX_S // 60} min)")
        self._update_power()

    def _update_power(self) -> None:
        now = self.clock()
        if self._wake_hold_since is not None and (
                self.window is not None or self._ac is False or now - self._wake_hold_since > WAKE_HOLD_MAX_S):
            self._wake_hold_since = None
        screen = self.window is not None
        if screen and self._screen_since is None:
            self._screen_since = now
        if not screen:
            self._screen_since = None
        if screen and self._ac is False and now - self._screen_since > SCREEN_HOLD_ON_BATTERY_S:
            screen = False                   # battery safeguard: let Windows' own idle rules apply again
        self.power.set(system=screen or self._wake_hold_since is not None, display=screen)

    # ---- decisions -------------------------------------------------------------------------
    def _evaluate(self) -> None:
        for _ in range(2):                  # an UNLOCK may let the other mode apply right away
            d = self.policy.update(self._percent, self._ac)
            if d.action == LOCK:
                self._show(d)
                break
            if d.action == UNLOCK:
                self._hide(d.reason, d.mode)
                continue
            break
        if self.window is not None:
            self.window.set_readings(self._percent, self._ac, self._charging)

    def _show(self, d: LockDecision) -> None:
        if self.window is not None and self.window.mode != d.mode:
            self._hide("replaced by " + MODE_NAME[d.mode], self.window.mode)   # never two windows
        if self.window is None:
            self.window = ProtectionWindow(d.mode, self.policy.threshold(d.mode))
            self.window.emergency_requested.connect(
                lambda: self.emergency_release(f"Emergency release ({EMERGENCY_SHORTCUT})"))
            self.windows_created += 1
            self.db.log_event(EVENT_KIND[d.mode], f"{MODE_NAME[d.mode]} ON: {d.reason}")
            log.info("%s shown: %s", MODE_NAME[d.mode], d.reason)
            self._start_alarm(d.mode)
        self.window.set_readings(self._percent, self._ac, self._charging)
        self.window.cover_screen()
        self._unknown_since = None
        self.watch.start(WATCH_INTERVAL_MS)
        self._update_power()
        self.lock_changed.emit(True)

    def _hide(self, reason: str, mode: str | None, log_event: bool = True) -> None:
        self._stop_alarm(reason)
        if self._started:
            self.watch.start(IDLE_POLL_MS)
        else:
            self.watch.stop()
        if self.window is not None:
            w, self.window = self.window, None
            mode = mode or w.mode
            w.force_close()
            if log_event:
                self.db.log_event(EVENT_KIND[mode], f"{MODE_NAME[mode]} OFF: {reason}")
            log.info("%s removed: %s", MODE_NAME[mode], reason)
            if not self._closed and log_event:
                self._update_power()
            self.lock_changed.emit(False)

    def emergency_release(self, reason: str) -> None:
        if self._closed:
            return
        mode = self.mode
        d = self.policy.release(reason)
        if self.window is not None:
            what = "unplugged" if mode == HIGH else "connected"
            self._hide(reason + f" (stays off until the charger is {what})", d.mode)
        else:
            self.db.log_event("strict_lock", f"Protection screens suppressed: {reason}")

    # ---- alarm -----------------------------------------------------------------------------
    def _play(self, mode: str) -> tuple[bool, str] | None:
        if not self.db.get_setting("sound_enabled") or not self.db.get_setting(ALARM_ENABLED_KEY[mode]):
            return None
        self.alarms_played += 1
        return self.player.play(mode, self.db.get_setting(ALARM_PATH_KEY[mode]) or None,
                                volume=int(self.db.get_setting(ALARM_VOLUME_KEY[mode])))

    def _alarm_audible(self, mode: str) -> bool:
        return (bool(self.db.get_setting("sound_enabled")) and bool(self.db.get_setting(ALARM_ENABLED_KEY[mode]))
                and int(self.db.get_setting(ALARM_VOLUME_KEY[mode]) or 0) > 0)

    def _start_alarm(self, mode: str) -> None:
        if self._alarm_audible(mode):
            try:
                self.audio.begin(f"{MODE_NAME[mode]} alarm")   # before the first sound of this session
            except Exception:
                log.exception("Audio override failed")
        res = self._play(mode)
        if res is not None:
            kind = "warning" if str(res[1]).startswith("⚠") else EVENT_KIND[mode]
            self.db.log_event(kind, f"{MODE_NAME[mode]} alarm: {res[1]}")
        self._schedule_alarm_repeat(mode)

    def _schedule_alarm_repeat(self, mode: str) -> None:
        minutes = int(self.db.get_setting(ALARM_REPEAT_KEY[mode]) or 0)
        if minutes > 0:
            self.alarm_timer.start(minutes * 60_000)
        else:
            self.alarm_timer.stop()         # 0 = one alarm per protection session

    def _alarm_repeat(self) -> None:
        if self.window is None or self._closed:
            self.alarm_timer.stop()
            return
        self._play(self.window.mode)        # repeats are not logged (no event flooding)

    def _stop_alarm(self, reason: str = "alarm stopped") -> None:
        self.alarm_timer.stop()
        try:
            self.player.stop()
        except Exception:                   # sound problems must never block an unlock
            log.exception("Could not stop alarm")
        try:
            self.audio.end(reason)          # put the user's volume / mute back (if ARES changed it)
        except Exception:
            log.exception("Could not restore the audio state")

    def shutdown(self) -> None:
        """App exit: take the window down without recording a fake charger event."""
        self._started = False
        self._hide("ARES exiting", None, log_event=False)
        self.watch.stop()
        self._wake_hold_since = None
        self.power.release()                 # never leave a keep-awake request behind
        self._closed = True


# Backwards-compatible names (v1.1).
StrictLockWindow = ProtectionWindow
StrictLockManager = ProtectionManager
