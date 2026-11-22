"""Settings page (explicit Save model).

Edits are staged in the controls and written only when you click **Save settings**:
  * every value is validated first; nothing is written if any value is invalid;
  * all changed values are written in ONE database transaction and read back; the status line
    shows "✓ All changes saved" only after that read-back succeeded, otherwise "Save failed";
  * after a successful save each changed key is announced with `setting_changed(key)` so the
    background app pushes it to the service that uses it (protection, monitor, wake task, theme);
  * closing the window with unsaved changes asks Save / Discard (/ Cancel);
  * "Restore default settings…" (with confirmation) deletes saved preferences only; battery
    history, events and sessions are never touched.

Sections
  A. Charging thresholds        firmware (BIOS) charge control - the only part that changes charging
  B. Strict Charging Protection 90% full-screen reminder + alarm (charger connected)
  C. Low Battery Protection     25% full-screen reminder + alarm (charger disconnected)
  D. Monitoring & notifications
  E. Startup
  F. Power profile
  G. Smart Wake-Up Protection   periodic wake checks while the PC sleeps (Task Scheduler)
  H. Interface                  theme
  I. Hardware Diagnostics       read-only: detected computer, charge-control support and detection notes
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget)

from app.core import smart_wake, startup_manager
from app.database.database import DEFAULT_SETTINGS, Database
from app.hardware.base_controller import ControllerCapabilities
from app.services import power_profile, sound_service
from app.ui.theme import THEMES
from app.utils import system_info as si
from app.utils.calculations import plan_thresholds, validate_user_thresholds

log = logging.getLogger(__name__)

WINDOWS_MEDIA = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Media")

# Answers of the "unsaved changes" prompt.
SAVE, DISCARD, CANCEL = "save", "discard", "cancel"

WAKE_LIMITS_TEXT = (
    "How it works: a Windows Task Scheduler task wakes the PC from Sleep every few minutes, ARES reads "
    "the battery, shows the existing protection screen if a threshold is reached, and otherwise does "
    "nothing; Windows puts the PC back to Sleep by itself (ARES never forces Sleep). At the high wake "
    "threshold on the charger ARES keeps the PC awake until the 90% protection screen appears; the 90% "
    "threshold itself is unchanged.\n"
    "Limitations: no wake from Shut down; checks run only while you are signed in; waking from Hibernate is not guaranteed; Windows may have "
    "wake timers disabled (often on battery) and ARES never changes that setting; every wake uses some "
    "battery; Windows may delay or skip wakes; Modern Standby PCs decide themselves when tasks run. "
    "Wake-up is not confirmed on your hardware until you have done the sleep test in the documentation.")


def _note(text: str, name: str = "muted") -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName(name)
    lbl.setWordWrap(True)
    return lbl


def _spin(lo: int, hi: int, suffix: str) -> QSpinBox:
    s = QSpinBox()
    s.setRange(lo, hi)
    s.setSuffix(suffix)
    s.setMinimumWidth(110)
    return s


def _repolish(w) -> None:
    w.style().unpolish(w)
    w.style().polish(w)


def _set_kind(lbl: QLabel, kind: str, text: str) -> None:
    lbl.setObjectName(kind)
    lbl.setText(text)
    _repolish(lbl)


class AlarmRow(QWidget):
    """Sound file + Browse + Default + Test, with a status line. Used by sections B and C.

    The path is only staged here; it is written by Save. A missing/invalid file is shown as a
    warning but the path is kept as typed (never replaced silently). The user's file is only read.
    """

    def __init__(self, page: "SettingsPage", kind: str, volume: QSpinBox) -> None:
        super().__init__()
        self.page, self.kind, self.volume = page, kind, volume
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.path = QLineEdit()
        self.path.setPlaceholderText(sound_service.default_description(kind) + " — or choose a .wav file")
        self.browse = QPushButton("Browse…")
        self.clear = QPushButton("Default")
        self.clear.setToolTip("Use the Windows default sound")
        self.test = QPushButton("Test alarm")
        row.addWidget(self.path, 1)
        row.addWidget(self.browse)
        row.addWidget(self.clear)
        row.addWidget(self.test)
        lay.addLayout(row)
        self.status = _note("")
        lay.addWidget(self.status)
        self.path.textChanged.connect(lambda _t: self._changed())
        self.browse.clicked.connect(self.choose_file)
        self.clear.clicked.connect(lambda: self.set_path(""))
        self.test.clicked.connect(self.play_test)

    def value(self) -> str:
        return self.path.text().strip()

    def set_path(self, value: str) -> None:
        self.path.setText(value or "")

    def _changed(self) -> None:
        self.show_check()
        self.page.mark_dirty()

    def show_check(self) -> None:
        problem = sound_service.check_wav(self.value())
        if problem:
            _set_kind(self.status, "warn", "⚠ " + problem + " The chosen path is kept as it is (it is never "
                      "replaced silently); fix or re-select the file.")
        else:
            _set_kind(self.status, "muted", "Using " + (Path(self.value()).name if self.value()
                                                        else sound_service.default_description(self.kind)))

    def choose_file(self) -> None:
        cur = self.value()
        start = str(Path(cur).parent) if cur and Path(cur).parent.is_dir() else WINDOWS_MEDIA
        path, _ = QFileDialog.getOpenFileName(self, "Choose alarm sound", start, "WAV sound (*.wav)")
        if path:
            self.set_path(path)

    def play_test(self) -> tuple[bool, str]:
        """Plays the sound currently SELECTED (saved or not) at the volume currently shown."""
        played, how = self.page.player.play(self.kind, self.value() or None, volume=int(self.volume.value()))
        _set_kind(self.status, "warn" if (not played or how.startswith("⚠")) else "muted",
                  ("🔊 " if played else "⚠ ") + how)
        self.page.db.log_event("info", f"{'High' if self.kind == 'high' else 'Low'} battery alarm test: {how}")
        return played, how


class SettingsPage(QWidget):
    setting_changed = Signal(str)       # key of a setting that was just saved (after read-back)
    saved = Signal()                    # a Save completed successfully
    apply_requested = Signal()
    restore_requested = Signal()
    password_entered = Signal(str)
    redetect_requested = Signal()
    quit_requested = Signal()

    def __init__(self, db: Database, parent=None, player=None,
                 wake_tester: Callable[[], tuple[bool, list[str]]] | None = None,
                 wake_detector: Callable[[], smart_wake.WakeSupport] | None = None) -> None:
        super().__init__(parent)
        self.db = db
        self.player = player or sound_service.AlarmPlayer()
        self.wake_tester = wake_tester or (lambda: smart_wake.test_configuration(self.db))
        self.wake_detector = wake_detector or smart_wake.detect
        self.wake_support: smart_wake.WakeSupport | None = None
        self.caps: ControllerCapabilities | None = None
        self._loading = False
        self._fields: dict[str, tuple[Callable[[], Any], Callable[[Any], None]]] = {}
        self._checks: dict[str, QCheckBox] = {}         # key -> control (also used by the tests)
        self._spins: dict[str, QSpinBox] = {}
        self.baseline: dict[str, Any] = {}
        self.last_errors: list[str] = []
        self.last_save_ok: bool | None = None

        outer = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.status_lbl = _note("", "muted")
        self.save_btn = QPushButton("Save settings")
        self.save_btn.setObjectName("primary")
        self.discard_btn = QPushButton("Discard changes")
        self.defaults_btn = QPushButton("Restore default settings…")
        bar.addWidget(self.status_lbl, 1)
        bar.addWidget(self.discard_btn)
        bar.addWidget(self.defaults_btn)
        bar.addWidget(self.save_btn)
        outer.addLayout(bar)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        lay = QVBoxLayout(body)

        # ---- A. charging thresholds (firmware) --------------------------------------------
        g = QGroupBox("A. Charging thresholds (firmware charge control)")
        f = QFormLayout(g)
        f.addRow(_note("These two values are written to the laptop BIOS when this PC supports it. Only "
                       "the firmware can actually stop and resume charging; ARES itself never cuts power. "
                       "The protection screens in B and C are separate reminders with their own "
                       "thresholds and do not change charging."))
        self.max_spin = self._bind_spin("max_charge", _spin(50, 100, " %"))
        self.min_spin = self._bind_spin("min_charge", _spin(5, 95, " %"))
        f.addRow("Maximum charge warning / firmware stop", self.max_spin)
        f.addRow("Resume / low charging reminder (firmware start)", self.min_spin)
        self.plan_lbl = _note("", "notes")
        f.addRow(self.plan_lbl)
        self.auto_apply = self._bind_check("auto_apply_thresholds", QCheckBox(
            "Stop charging automatically: write these thresholds to the firmware and re-apply them if "
            "the BIOS setting changes"))
        f.addRow(self.auto_apply)
        self.fw_note = _note("")
        f.addRow(self.fw_note)
        self.pwd = QLineEdit()
        self.pwd.setEchoMode(QLineEdit.Password)
        self.pwd.setPlaceholderText("Only if a BIOS admin/setup password is set (kept in memory, never saved)")
        self.pwd.editingFinished.connect(self._password_done)
        f.addRow("BIOS password", self.pwd)
        row = QHBoxLayout()
        self.apply_btn = QPushButton("Apply to firmware now")
        self.restore_btn = QPushButton("Restore original firmware setting")
        self.admin_btn = QPushButton("Restart as administrator")
        for b in (self.apply_btn, self.restore_btn, self.admin_btn):
            row.addWidget(b)
        f.addRow(row)
        self.cctk = QLineEdit()
        self.cctk.setPlaceholderText("Auto-detect (C:\\Program Files (x86)\\Dell\\Command Configure\\X86_64\\cctk.exe)")
        self.cctk.textChanged.connect(lambda _t: self.mark_dirty())
        self._fields["cctk_path"] = (lambda: self.cctk.text().strip(), lambda v: self.cctk.setText(v or ""))
        self.cctk_browse = QPushButton("Browse…")
        crow = QHBoxLayout()
        crow.addWidget(self.cctk, 1)
        crow.addWidget(self.cctk_browse)
        f.addRow("cctk.exe path", crow)
        lay.addWidget(g)

        # ---- B. strict charging protection (high) ---------------------------------------------
        g = QGroupBox("B. Strict Charging Protection (high battery)")
        f = QFormLayout(g)
        self.lock_enabled = self._bind_check("strict_lock_enabled", QCheckBox(
            "Cover the screen, sound an alarm and ask me to remove the charger when the battery is full"))
        f.addRow(self.lock_enabled)
        self.lock_threshold = self._bind_spin("strict_lock_threshold", _spin(50, 100, " %"))
        f.addRow("Show the protection screen at or above", self.lock_threshold)
        self.high_alarm_on = self._bind_check("high_alarm_enabled", QCheckBox("Play the high-battery alarm"))
        f.addRow(self.high_alarm_on)
        self.high_volume = self._bind_spin("high_alarm_volume", self._volume_spin())
        f.addRow("Alarm volume", self.high_volume)
        self.high_alarm = AlarmRow(self, sound_service.HIGH, self.high_volume)
        self._fields["high_alarm_sound_path"] = (self.high_alarm.value, self.high_alarm.set_path)
        f.addRow("High-battery alarm sound", self.high_alarm)
        self.high_repeat = self._bind_spin("high_alarm_repeat_min", self._repeat_spin())
        f.addRow("Repeat the alarm", self.high_repeat)
        f.addRow(_note("Appears once per charging session while the charger is connected and disappears "
                       "as soon as the charger is unplugged. Emergency release: Ctrl+Alt+Shift+U."))
        lay.addWidget(g)

        # ---- C. low battery protection ------------------------------------------------------
        g = QGroupBox("C. Low Battery Protection")
        f = QFormLayout(g)
        self.low_enabled = self._bind_check("low_lock_enabled", QCheckBox(
            "Cover the screen, sound an alarm and ask me to connect the charger when the battery is low"))
        f.addRow(self.low_enabled)
        self.low_threshold = self._bind_spin("low_lock_threshold", _spin(5, 50, " %"))
        f.addRow("Show the protection screen at or below", self.low_threshold)
        self.low_alarm_on = self._bind_check("low_alarm_enabled", QCheckBox("Play the low-battery alarm"))
        f.addRow(self.low_alarm_on)
        self.low_volume = self._bind_spin("low_alarm_volume", self._volume_spin())
        f.addRow("Alarm volume", self.low_volume)
        self.low_alarm = AlarmRow(self, sound_service.LOW, self.low_volume)
        self._fields["low_alarm_sound_path"] = (self.low_alarm.value, self.low_alarm.set_path)
        f.addRow("Low-battery alarm sound", self.low_alarm)
        self.low_repeat = self._bind_spin("low_alarm_repeat_min", self._repeat_spin())
        f.addRow("Repeat the alarm", self.low_repeat)
        f.addRow(_note("Appears once per battery session while the charger is disconnected and disappears "
                       "as soon as the charger is connected. If no charger is at hand, save your work after "
                       "releasing it with Ctrl+Alt+Shift+U (it stays off until you next plug in)."))
        lay.addWidget(g)

        # ---- D. monitoring & notifications ------------------------------------------------
        g = QGroupBox("D. Monitoring && notifications")
        f = QFormLayout(g)
        self.monitoring = self._bind_check("monitoring_enabled", QCheckBox("Background monitoring enabled"))
        f.addRow(self.monitoring)
        self.interval = self._bind_spin("monitor_interval_s", _spin(5, 600, " s"))
        f.addRow("Monitoring interval", self.interval)
        self.notify = self._bind_check("notifications_enabled", QCheckBox("Show Windows notifications"))
        f.addRow(self.notify)
        self.sound_enabled = self._bind_check("sound_enabled", QCheckBox(
            "Sound (master switch for the protection alarms and the maximum-charge alert)"))
        f.addRow(self.sound_enabled)
        self.audio_override = self._bind_check("audio_override_enabled", QCheckBox(
            "Smart Audio Override: while a protection alarm sounds, un-mute Windows and set the volume to 100%"))
        f.addRow(self.audio_override)
        f.addRow(_note("Your volume and mute setting are put back when the alarm stops (also after a crash, at "
                       "the next start). If you change the volume yourself during the alarm, ARES keeps your "
                       "choice and does not change it again. Only the default playback device's master volume "
                       "is touched.", "detail"))
        self.low = self._bind_spin("notify_low_pct", _spin(5, 50, " %"))
        f.addRow("Low battery notification at", self.low)
        self.temp = self._bind_spin("temp_warn_c", _spin(35, 70, " °C"))
        f.addRow("Temperature warning at", self.temp)
        self.health_drop = self._bind_spin("notify_health_drop_pct", _spin(1, 30, " points"))
        f.addRow("Warn when health drops by", self.health_drop)
        lay.addWidget(g)

        # ---- E. startup -----------------------------------------------------------------
        g = QGroupBox("E. Startup")
        f = QFormLayout(g)
        self.autostart = QCheckBox("Start ARES automatically when I sign in to Windows")
        self.autostart.toggled.connect(lambda _v: self.mark_dirty())
        # On Windows the box shows the ACTUAL registration; elsewhere the saved preference.
        self._fields["autostart"] = (lambda: bool(self.autostart.isChecked()),
                                     lambda v: self.autostart.setChecked(self._startup_checked(bool(v))))
        f.addRow(self.autostart)
        self.start_minimized = self._bind_check("start_minimized", QCheckBox(
            "Start minimized in system tray (monitor in the background; don't open the dashboard)"))
        f.addRow(self.start_minimized)
        f.addRow(_note("Monitoring and the 90% / 25% protection start immediately either way. Only one ARES "
                       "runs at a time: if it is already running, the existing copy is used. The startup "
                       "entry is changed when you click Save settings."))
        self.startup_lbl = _note("")
        f.addRow("Status", self.startup_lbl)
        self.startup_result = _note("")
        f.addRow("Registration", self.startup_result)
        self.startup_test_btn = QPushButton("Test startup configuration")
        trow = QHBoxLayout()
        trow.addWidget(self.startup_test_btn)
        trow.addStretch(1)
        f.addRow(trow)
        lay.addWidget(g)

        # ---- F. power profiles ------------------------------------------------------------
        g = QGroupBox("F. Power profile")
        f = QFormLayout(g)
        self.profile = QComboBox()
        self.profile.addItem("Not set", "")
        for name in power_profile.PROFILES:
            self.profile.addItem(name, name)
        self.profile.currentIndexChanged.connect(lambda _i: self.mark_dirty())
        self._fields["power_profile"] = (lambda: self.profile.currentData() or "", self._set_profile_combo)
        self.profile_btn = QPushButton("Apply profile now…")
        prow = QHBoxLayout()
        prow.addWidget(self.profile, 1)
        prow.addWidget(self.profile_btn)
        f.addRow("Preferred profile", prow)
        self.profile_on_start = self._bind_check("apply_profile_on_startup", QCheckBox(
            "Apply the preferred profile when ARES starts (otherwise it is only remembered)"))
        f.addRow(self.profile_on_start)
        self.profile_lbl = _note("")
        f.addRow("Active Windows plan", self.profile_lbl)
        lay.addWidget(g)

        # ---- G. smart wake-up ---------------------------------------------------------------
        g = QGroupBox("G. Smart Wake-Up Protection (Experimental)")
        f = QFormLayout(g)
        self.wake_enabled = self._bind_check("wake_enabled", QCheckBox(
            "Wake the PC from Sleep periodically to check the battery (Task Scheduler wake timer)"))
        f.addRow(self.wake_enabled)
        f.addRow(_note("Experimental in this Beta: waking from Sleep has not yet been confirmed on any "
                       "laptop. Keep the 90% / 25% protection and the firmware threshold as your main protection.", "warn"))
        self.wake_low_on = self._bind_check("wake_low_enabled", QCheckBox("Low-battery wake protection (on battery)"))
        self.wake_low = self._bind_spin("wake_low_threshold", _spin(5, 50, " %"))
        wl = QHBoxLayout()
        wl.addWidget(self.wake_low_on, 1)
        wl.addWidget(QLabel("at or below"))
        wl.addWidget(self.wake_low)
        f.addRow(wl)
        self.wake_high_on = self._bind_check("wake_high_enabled", QCheckBox("High-battery wake protection (on charger)"))
        self.wake_high = self._bind_spin("wake_high_threshold", _spin(50, 100, " %"))
        wh = QHBoxLayout()
        wh.addWidget(self.wake_high_on, 1)
        wh.addWidget(QLabel("at or above"))
        wh.addWidget(self.wake_high)
        f.addRow(wh)
        self.wake_interval = self._bind_spin("wake_interval_min", _spin(smart_wake.MIN_INTERVAL_MIN,
                                                                        smart_wake.MAX_INTERVAL_MIN, " min"))
        self.wake_interval.setPrefix("every ")
        f.addRow("Check interval while asleep", self.wake_interval)
        self.wake_support_lbl = _note("Not checked yet.")
        f.addRow("Wake-timer support", self.wake_support_lbl)
        self.wake_sleep_lbl = _note("")
        f.addRow("Sleep state", self.wake_sleep_lbl)
        self.wake_problems_lbl = _note("", "warn")
        f.addRow(self.wake_problems_lbl)
        self.wake_last_lbl = _note("")
        f.addRow("Last wake check", self.wake_last_lbl)
        self.wake_test_btn = QPushButton("Test wake configuration")
        self.wake_refresh_btn = QPushButton("Re-check support")
        wrow = QHBoxLayout()
        wrow.addWidget(self.wake_test_btn)
        wrow.addWidget(self.wake_refresh_btn)
        wrow.addStretch(1)
        f.addRow(wrow)
        f.addRow(_note(WAKE_LIMITS_TEXT))
        f.addRow(_note("Turn it off at any time with the tray menu (Smart Wake-Up) or `ARES --disable-wake`.", "detail"))
        lay.addWidget(g)

        # ---- H. interface -------------------------------------------------------------------
        g = QGroupBox("H. Interface")
        f = QFormLayout(g)
        self.theme = QComboBox()
        for key in THEMES:
            self.theme.addItem(key.capitalize(), key)
        self.theme.currentIndexChanged.connect(lambda _i: self.mark_dirty())
        self._fields["theme"] = (lambda: self.theme.currentData(), self._set_theme_combo)
        f.addRow("Theme", self.theme)
        f.addRow(_note("Window size, position and the last open tab are remembered automatically."))
        lay.addWidget(g)

        # ---- I. hardware diagnostics (read-only) ---------------------------------------------
        g = QGroupBox("I. Hardware Diagnostics")
        self.diag_group = g
        f = QFormLayout(g)
        f.addRow(_note("What ARES detected on this computer. Nothing here is a setting, and nothing is "
                       "changed by viewing it."))
        self.diag: dict[str, QLabel] = {}
        for key, label in (("machine", "Computer"), ("bios", "BIOS version"), ("control", "Charge control"),
                           ("iface", "Interface"), ("access", "Access"), ("limits", "Firmware limits"),
                           ("firmware", "Firmware setting now"), ("status", "Status")):
            self.diag[key] = _note("N/A", "detail")
            self.diag[key].setTextInteractionFlags(Qt.TextSelectableByMouse)
            f.addRow(label, self.diag[key])
        self.diag_notes = _note("", "notes")
        self.diag_notes.setTextInteractionFlags(Qt.TextSelectableByMouse)
        f.addRow("Detection notes", self.diag_notes)
        self.redetect_btn = QPushButton("Re-detect hardware")
        self.redetect_btn.setToolTip("Run hardware detection again (read-only; changes no settings)")
        f.addRow(self.redetect_btn)
        lay.addWidget(g)
        lay.addStretch(1)
        self._diag_caps_notes: list[str] = []
        self._diag_status_msgs: list[str] = []

        self.max_spin.valueChanged.connect(self._update_plan)
        self.min_spin.valueChanged.connect(self._update_plan)
        self.save_btn.clicked.connect(self.save)
        self.discard_btn.clicked.connect(self.load)
        self.defaults_btn.clicked.connect(lambda: self.restore_defaults(confirm=True))
        self.apply_btn.clicked.connect(self._apply)
        self.restore_btn.clicked.connect(self._restore)
        self.admin_btn.clicked.connect(self._relaunch_admin)
        self.profile_btn.clicked.connect(self._apply_profile)
        self.cctk_browse.clicked.connect(self._browse_cctk)
        self.startup_test_btn.clicked.connect(self._test_startup)
        self.wake_test_btn.clicked.connect(self.test_wake)
        self.wake_refresh_btn.clicked.connect(self.refresh_wake_support)
        self.redetect_btn.clicked.connect(self.redetect_requested.emit)
        self.load()

    # ---- binding helpers ----------------------------------------------------------------
    @staticmethod
    def _repeat_spin() -> QSpinBox:
        s = _spin(0, 60, " min")
        s.setPrefix("every ")
        s.setSpecialValueText("once per session")
        return s

    @staticmethod
    def _volume_spin() -> QSpinBox:
        s = _spin(0, 100, " %")
        s.setSingleStep(10)
        s.setSpecialValueText("muted")
        return s

    def _bind_check(self, key: str, cb: QCheckBox) -> QCheckBox:
        self._checks[key] = cb
        self._fields[key] = (lambda: bool(cb.isChecked()), lambda v: cb.setChecked(bool(v)))
        cb.toggled.connect(lambda _v: self.mark_dirty())
        return cb

    def _bind_spin(self, key: str, sp: QSpinBox) -> QSpinBox:
        self._spins[key] = sp
        self._fields[key] = (lambda: int(sp.value()), lambda v: sp.setValue(int(v)))
        sp.valueChanged.connect(lambda _v: self.mark_dirty())
        return sp

    def _set_profile_combo(self, v) -> None:
        i = self.profile.findData(v if v in power_profile.PROFILES else "")
        self.profile.setCurrentIndex(max(0, i))

    def _set_theme_combo(self, v) -> None:
        i = self.theme.findData(v if v in THEMES else "dark")
        self.theme.setCurrentIndex(max(0, i))

    @staticmethod
    def _startup_checked(pref: bool) -> bool:
        return startup_manager.status().enabled if si.IS_WINDOWS else pref

    # ---- staged values ------------------------------------------------------------------
    def keys(self) -> list[str]:
        return list(self._fields)

    def values(self) -> dict[str, Any]:
        """What the controls show now (saved or not)."""
        return {k: get() for k, (get, _s) in self._fields.items()}

    def changes(self) -> dict[str, Any]:
        cur = self.values()
        return {k: v for k, v in cur.items() if self.baseline.get(k) != v}

    def is_dirty(self) -> bool:
        return bool(self.changes())

    def mark_dirty(self) -> None:
        if self._loading:
            return
        self._show_state()

    def _show_state(self) -> None:
        if self.is_dirty():
            _set_kind(self.status_lbl, "warn", "● Unsaved changes — click Save settings to keep them.")
        elif self.last_save_ok:
            pass                                    # keep "✓ All changes saved HH:MM:SS"
        else:
            _set_kind(self.status_lbl, "muted", "All settings are saved.")
        self.save_btn.setEnabled(True)
        self.discard_btn.setEnabled(self.is_dirty())

    def validate(self, v: dict[str, Any] | None = None) -> list[str]:
        v = v or self.values()
        errs = list(validate_user_thresholds(v["min_charge"], v["max_charge"]))
        if v["low_lock_threshold"] >= v["strict_lock_threshold"]:
            errs.append("Low Battery Protection threshold must be below the Strict Charging Protection threshold.")
        if v["wake_low_threshold"] >= v["wake_high_threshold"]:
            errs.append("Smart Wake-Up low threshold must be below the high threshold.")
        if not (smart_wake.MIN_INTERVAL_MIN <= v["wake_interval_min"] <= smart_wake.MAX_INTERVAL_MIN):
            errs.append(f"Wake check interval must be {smart_wake.MIN_INTERVAL_MIN}-{smart_wake.MAX_INTERVAL_MIN} min.")
        if v["theme"] not in THEMES:
            errs.append("Unknown theme.")
        return errs

    # ---- load / save --------------------------------------------------------------------
    def load(self) -> None:
        """Show the values that are actually stored and drop unsaved edits (never writes)."""
        self._loading = True
        try:
            s = self.db.all_settings()
            for key, (_get, put) in self._fields.items():
                put(s.get(key, DEFAULT_SETTINGS.get(key)))
            self.baseline = self.values()
        finally:
            self._loading = False
        self.high_alarm.show_check()
        self.low_alarm.show_check()
        self.last_save_ok = None
        self._show_state()
        self.refresh_status()
        self._update_plan()

    def load_key(self, key: str) -> None:
        """Re-read ONE saved value (changed elsewhere, e.g. the tray), keeping other unsaved edits."""
        if key not in self._fields:
            return
        _get, put = self._fields[key]
        self._loading = True
        try:
            put(self.db.get_setting(key))
            self.baseline[key] = self._fields[key][0]()
        finally:
            self._loading = False
        self._show_state()
        self.refresh_status()

    def refresh_from_db(self) -> None:
        """Reload every value the user has NOT edited (keeps unsaved edits)."""
        cur = self.values()
        for k in self._fields:
            if cur[k] == self.baseline.get(k):
                self.load_key(k)

    def save(self) -> bool:
        """Validate, write all changes in one transaction, verify, then apply. True on success."""
        self._password_done()
        values = self.values()
        errs = self.validate(values)
        self.last_errors = errs
        self._update_plan()
        if errs:
            self.last_save_ok = False
            _set_kind(self.status_lbl, "bad", "✗ Save failed: " + " ".join(errs) + " Nothing was saved.")
            return False
        changes = {k: v for k, v in values.items() if self.baseline.get(k) != v}
        if not changes:
            self.last_save_ok = True
            _set_kind(self.status_lbl, "ok", f"✓ All changes saved ({time.strftime('%H:%M:%S')}).")
            self.discard_btn.setEnabled(False)
            return True
        _set_kind(self.status_lbl, "muted", "Saving…")
        self.status_lbl.repaint()
        try:
            self.db.set_settings(changes)
        except Exception as exc:
            log.exception("Saving settings failed")
            self.last_save_ok = False
            _set_kind(self.status_lbl, "bad", f"✗ Save failed: {exc}. Your changes are still shown, not saved.")
            try:
                self.db.log_event("warning", f"Saving settings failed: {exc}")
            except Exception:
                pass
            return False
        self.baseline.update(changes)
        note = ""
        if "autostart" in changes:
            note = self._apply_autostart(bool(changes["autostart"]))
        for key in changes:
            self.setting_changed.emit(key)
        self.saved.emit()
        self.last_save_ok = True
        if note:
            _set_kind(self.status_lbl, "warn", f"✓ Settings saved ({time.strftime('%H:%M:%S')}), but: {note}")
        else:
            _set_kind(self.status_lbl, "ok", f"✓ All changes saved ({time.strftime('%H:%M:%S')}).")
        self.discard_btn.setEnabled(self.is_dirty())
        self.refresh_status()
        return True

    def _apply_autostart(self, want: bool) -> str:
        """Make Windows match the saved preference. Returns a problem text or ''."""
        ok, msg = startup_manager.apply_preference(self.db, want, "Settings")
        if si.IS_WINDOWS:
            actual = startup_manager.status().enabled
            if actual != want:                       # show the real state, not what was hoped for
                self._loading = True
                try:
                    self.autostart.setChecked(actual)
                finally:
                    self._loading = False
                return f"Windows startup registration failed ({msg}); the box shows the real state."
        return "" if ok or not si.IS_WINDOWS else msg

    def restore_defaults(self, confirm: bool = True) -> bool:
        """Delete saved preferences only (never history/events/sessions)."""
        if confirm and QMessageBox.question(
                self, "Restore default settings",
                "Reset every ARES setting to its default value?\n\nBattery history, health data and "
                "events are kept. Custom alarm sounds are forgotten (your .wav files are not touched). "
                "Firmware charge settings in the BIOS are not changed.") != QMessageBox.Yes:
            return False
        try:
            n = self.db.reset_settings()
        except Exception as exc:
            log.exception("Restoring default settings failed")
            _set_kind(self.status_lbl, "bad", f"✗ Restoring defaults failed: {exc}")
            return False
        self.db.log_event("info", f"Settings restored to defaults ({n} saved values removed; history kept).")
        self.load()
        startup_manager.apply_preference(self.db, bool(self.db.get_setting("autostart")), "defaults restored")
        self.load_key("autostart")
        for key in self._fields:
            self.setting_changed.emit(key)
        self.saved.emit()
        self.last_save_ok = True
        _set_kind(self.status_lbl, "ok", f"✓ Default settings restored ({time.strftime('%H:%M:%S')}).")
        self.refresh_status()
        return True

    def ask_unsaved(self, allow_cancel: bool) -> str:
        """Ask what to do with unsaved changes. Returns SAVE, DISCARD or CANCEL (tests replace this)."""
        buttons = QMessageBox.Save | QMessageBox.Discard | (QMessageBox.Cancel if allow_cancel else QMessageBox.NoButton)
        r = QMessageBox.question(self, "Unsaved settings", "You have unsaved settings changes. Save them?",
                                 buttons, QMessageBox.Save)
        return {QMessageBox.Save: SAVE, QMessageBox.Discard: DISCARD}.get(r, CANCEL if allow_cancel else DISCARD)

    def confirm_close(self, allow_cancel: bool = True) -> bool:
        """Called before the window closes. False = keep the window open."""
        if not self.is_dirty():
            return True
        answer = self.ask_unsaved(allow_cancel)
        if answer == SAVE:
            ok = self.save()
            if not ok and allow_cancel:
                QMessageBox.warning(self, "Settings not saved", self.status_lbl.text())
                return False
            return True
        if answer == DISCARD:
            self.load()
            return True
        return not allow_cancel

    # ---- status ---------------------------------------------------------------------------
    def set_capabilities(self, caps: ControllerCapabilities) -> None:
        self.caps = caps
        can = caps.supported
        for w in (self.apply_btn, self.restore_btn, self.auto_apply):
            w.setEnabled(can)
        why = ("" if can else "Unavailable on this PC: no supported firmware charge-control interface was "
               "detected (Dell Command | Configure / cctk with administrator rights). The thresholds above "
               "are still used for notifications and history.")
        self.fw_note.setText(why)
        self.fw_note.setVisible(bool(why))
        for w in (self.apply_btn, self.restore_btn, self.auto_apply):
            w.setToolTip(why)
        self.admin_btn.setVisible(si.IS_WINDOWS and not si.is_admin())
        self._update_plan()
        d = self.diag
        d["control"].setText("Supported" if caps.supported and caps.can_write else
                             "Read-only" if caps.supported and caps.can_read else
                             "Not available: monitoring only (ARES cannot stop or resume charging)")
        d["iface"].setText(caps.interface or "None found")
        access = [("read" if caps.can_read else "no read"), ("write" if caps.can_write else "no write")]
        if caps.requires_admin:
            access.append("needs administrator rights")
        if caps.password_required:
            access.append("BIOS password required")
        d["access"].setText(", ".join(access))
        lim = caps.limits
        d["limits"].setText(f"start {lim.start_min}-{lim.start_max}%, stop {lim.stop_min}-{lim.stop_max}%, "
                            f"gap at least {lim.min_gap}%" if lim else "N/A")
        self._diag_caps_notes = list(caps.notes)
        self._show_diag_notes()

    def set_profile(self, prof) -> None:
        """Computer details for I. Hardware Diagnostics (from hardware detection)."""
        m = prof.machine
        self.diag["machine"].setText(prof.display_name)
        self.diag["bios"].setText(m.bios_version or "N/A")

    def update_diagnostics(self, st) -> None:
        """Live controller status for I. Hardware Diagnostics: the detail text and messages that the
        dashboard no longer shows when charge control is not available."""
        self.diag["status"].setText(st.detail or st.headline or "N/A")
        self.diag["firmware"].setText(st.firmware.describe() if st.firmware else "N/A")
        msgs = list(st.messages)
        if st.blocked_reason:
            msgs.append(st.blocked_reason)
        self._diag_status_msgs = msgs
        self._show_diag_notes()

    def _show_diag_notes(self) -> None:
        seen: list[str] = []
        for m in self._diag_caps_notes + self._diag_status_msgs:
            if m and m not in seen:
                seen.append(m)
        self.diag_notes.setText("\n".join("• " + m for m in seen) if seen else "None")

    def refresh_status(self) -> None:
        st = startup_manager.status()
        if si.IS_WINDOWS:
            txt = ("Enabled" if st.enabled else "Disabled") + (f" — {st.method}" if st.enabled else "")
            if st.missing:
                txt += "\n⚠ Registered program not found: " + ", ".join(st.missing)
        else:
            txt = "Not available on this operating system (Windows only)."
        self.startup_lbl.setText(txt)
        res = self.db.get_state("startup_last_result")
        if not res:
            _set_kind(self.startup_result, "muted", "Not applied yet (ARES applies it a few seconds after it starts).")
        elif res.get("ok"):
            _set_kind(self.startup_result, "ok", "✓ Startup registration successfully applied. " + res.get("message", ""))
        else:
            _set_kind(self.startup_result, "warn", "✗ Failed: " + res.get("message", ""))
        act = power_profile.active_scheme()
        self.profile_lbl.setText(act.name if act else "N/A")
        self.wake_last_lbl.setText(smart_wake.last_check_text(self.db))

    def refresh_wake_support(self) -> smart_wake.WakeSupport:
        sup = self.wake_detector()
        self.wake_support = sup
        self.wake_support_lbl.setText(
            ("On charger: " + ("usable" if sup.usable_on_ac else "not usable") +
             " · on battery: " + ("usable" if sup.usable_on_battery else "not usable")) if sup.windows
            else sup.summary())
        self.wake_sleep_lbl.setText(f"{sup.sleep_state}; RTC wake from {sup.rtc_wake_state}"
                                    + ("; Hibernate available" if sup.hibernate else "") if sup.windows else "N/A")
        probs = sup.problems()
        self.wake_problems_lbl.setText("\n".join("⚠ " + p for p in probs))
        self.wake_problems_lbl.setVisible(bool(probs))
        return sup

    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        if self.wake_support is None:            # read once, when the page is first shown
            self.refresh_wake_support()

    def _update_plan(self) -> None:
        start, stop = self.min_spin.value(), self.max_spin.value()
        errs = validate_user_thresholds(start, stop)
        if errs:
            self.plan_lbl.setText("⚠ " + " ".join(errs))
            return
        if not self.caps or not self.caps.supported or not self.caps.limits:
            self.plan_lbl.setText("This computer has no supported charge-control interface, so these values are "
                                  "used for notifications and history only. ARES cannot stop charging.")
            return
        p = plan_thresholds(start, stop, self.caps.limits)
        txt = f"Firmware will be set to: start {p.start}% / stop {p.stop}%."
        if p.messages:
            txt += "\n" + "\n".join("• " + m for m in p.messages)
        self.plan_lbl.setText(txt)

    # ---- individual actions ------------------------------------------------------------
    def _password_done(self) -> None:
        if self.pwd.text():
            self.password_entered.emit(self.pwd.text())

    def _browse_cctk(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Locate cctk.exe", "C:\\Program Files (x86)\\Dell",
                                              "cctk.exe (cctk.exe)")
        if path:
            self.cctk.setText(path)

    def _test_startup(self) -> None:
        ok, lines = startup_manager.verify(bool(self.db.get_setting("autostart")))
        if self.changes().get("autostart") is not None:
            lines.append("Note: the startup checkbox has an unsaved change; this test checks the SAVED preference.")
        (QMessageBox.information if ok else QMessageBox.warning)(self, "Startup configuration", "\n".join(lines))
        self.refresh_status()

    def test_wake(self) -> tuple[bool, list[str]]:
        ok, lines = self.wake_tester()
        if any(k in self.changes() for k in ("wake_enabled", "wake_low_enabled", "wake_low_threshold",
                                             "wake_high_enabled", "wake_high_threshold", "wake_interval_min")):
            lines.append("Note: Smart Wake-Up has unsaved changes; this test checks the SAVED settings.")
        self.refresh_wake_support()
        self.refresh_status()
        if self.isVisible():
            (QMessageBox.information if ok else QMessageBox.warning)(self, "Smart Wake-Up configuration",
                                                                     "\n".join(lines))
        return ok, lines

    def _apply(self) -> None:
        if not self.save():
            QMessageBox.warning(self, "Settings not saved", "\n".join(self.last_errors) or self.status_lbl.text())
            return
        start, stop = self.min_spin.value(), self.max_spin.value()
        p = plan_thresholds(start, stop, self.caps.limits) if self.caps and self.caps.limits else None
        if p is None:
            return
        msg = (f"ARES will write this setting to the laptop BIOS:\n\n"
               f"    Primary Battery Charge Configuration = Custom\n"
               f"    Start {p.start}%  /  Stop {p.stop}%\n\n"
               "The battery firmware will then stop charging at the stop value and resume below the start "
               "value, even when ARES is closed. You can undo this with 'Restore original firmware setting'.\n\n"
               "Continue?")
        if QMessageBox.question(self, "Apply charge thresholds", msg) == QMessageBox.Yes:
            self.apply_requested.emit()

    def _restore(self) -> None:
        if QMessageBox.question(self, "Restore", "Restore the battery charge setting ARES found before it made "
                                "any change (usually ExpressCharge or Standard)?") == QMessageBox.Yes:
            self._password_done()
            self.restore_requested.emit()

    def _relaunch_admin(self) -> None:
        if self.is_dirty() and not self.save():
            QMessageBox.warning(self, "Settings not saved", self.status_lbl.text())
            return
        if si.relaunch_as_admin(["--after-elevation"]):
            self.quit_requested.emit()
        else:
            QMessageBox.information(self, "Administrator", "Elevation was cancelled or is not available.")

    def _remember_profile(self, name: str) -> None:
        self.db.set_settings({"power_profile": name})
        self.baseline["power_profile"] = name
        self.setting_changed.emit("power_profile")
        self._show_state()

    def _apply_profile(self) -> None:
        name = self.profile.currentData() or ""
        if not name:
            QMessageBox.information(self, "Power profile", "Choose a profile first.")
            return
        if name == "Custom Charging":
            QMessageBox.information(self, "Custom Charging", "Custom Charging uses your charge thresholds in "
                                    "section A. It does not change the Windows power plan.")
            self._remember_profile(name)
            self.max_spin.setFocus()
            return
        if QMessageBox.question(self, "Change Windows power plan",
                                f"Switch the Windows power plan to '{name}' now?") != QMessageBox.Yes:
            return
        ok, msg = power_profile.apply_profile(name)
        if ok:
            self._remember_profile(name)
            self.db.log_event("info", msg)
        (QMessageBox.information if ok else QMessageBox.warning)(self, "Power profile", msg)
        self.refresh_status()
