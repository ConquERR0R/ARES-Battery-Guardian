"""The long-lived ARES process: tray icon + background monitoring thread + database.

This object lives for the whole run of ARES. The dashboard window is a separate,
disposable view: clicking X destroys it (Qt.WA_DeleteOnClose) while this object,
the tray icon and the monitoring thread keep running. "Open Dashboard" builds a
fresh window that attaches to the SAME running backend, so no second monitoring
loop is ever created. Only "Exit ARES" (tray) stops everything.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QMetaObject, QObject, Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from app import APP_NAME
from app.core.monitor_service import MonitorWorker
from app.database.battery_history import BatteryHistory
from app.database.database import Database, migrate_settings
from app.core.power_probe import default_power_probe
from app.ui.lock_window import ProtectionManager
from app.ui.tray import TrayIcon

log = logging.getLogger(__name__)


class AresBackgroundApp(QObject):
    # Re-broadcast of worker signals so windows can attach/detach freely.
    snapshot = Signal(object)
    status = Signal(object)
    profile = Signal(object)
    action_result = Signal(str, object)
    monitoring_changed = Signal(bool)
    event_logged = Signal()

    # Requests to the worker thread (queued across threads).
    request_monitoring = Signal(bool)
    request_apply = Signal()
    request_restore = Signal()
    request_password = Signal(str)
    request_settings_changed = Signal()
    request_redetect = Signal()
    request_test_sound = Signal()

    def __init__(self, db_path: str, power_probe=default_power_probe, manage_startup: bool = False,
                 alarm_player=None, wake_scheduler=None, power=None) -> None:
        super().__init__()
        self.manage_startup = manage_startup      # only the real app (main.py) touches Windows startup
        self.db_path = db_path
        self.db = Database(db_path)                       # UI-thread connection (settings, queries)
        migrate_settings(self.db)                         # before the worker or any page reads settings
        # Smart Wake-Up task: only the real app (or a test that injects a fake) touches Task Scheduler.
        self.wake_scheduler = wake_scheduler
        self.wake_last_sync: tuple[bool, str] | None = None
        self.history = BatteryHistory(self.db, recover=False)
        self.window = None                                # current dashboard window, or None
        self.last_snapshot = None
        self.last_status = None
        self.last_profile = None
        self.monitoring = True
        self.worker_starts = 0                            # for tests: must stay 1 per process
        self._quitting = False
        self._closed = False

        # Battery protection screens (90% high / 25% low) live here, not in the dashboard, so they
        # work with the dashboard closed; exactly one manager -> at most one protection window.
        self.lock = ProtectionManager(self.db, power_probe=power_probe, player=alarm_player, power=power)
        self.lock.lock_changed.connect(self._on_lock_changed)

        self.tray = TrayIcon()
        self.tray.open_dashboard.connect(lambda: self.open_dashboard(None))      # last used tab
        self.tray.open_settings.connect(lambda: self.open_dashboard(4))
        self.tray.open_health.connect(lambda: self.open_dashboard(2))
        self.tray.monitoring_toggled.connect(self.request_monitoring)
        self.tray.smart_wake_toggled.connect(self.set_smart_wake)
        self.tray.set_smart_wake(bool(self.db.get_setting("wake_enabled")))
        self.tray.exit_requested.connect(self.exit_ares)
        self._tray_retry = QTimer(self)
        self._tray_retry.timeout.connect(self._show_tray)
        self._show_tray()

        self.thread = QThread(self)
        self.worker = MonitorWorker(db_path)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.start)
        self.worker.snapshot.connect(self._on_snapshot)
        self.worker.status.connect(self._on_status)
        self.worker.profile.connect(self._on_profile)
        self.worker.notify.connect(self.tray.notify)
        self.worker.action_result.connect(self.action_result)
        self.worker.monitoring_changed.connect(self._on_monitoring_changed)
        self.worker.event_logged.connect(self.event_logged)
        self.request_monitoring.connect(self.worker.set_monitoring)
        self.request_apply.connect(self.worker.apply_thresholds)
        self.request_restore.connect(self.worker.restore_original)
        self.request_password.connect(self.worker.set_password)
        self.request_settings_changed.connect(self.worker.settings_changed)
        self.request_redetect.connect(self.worker.redetect)
        self.request_test_sound.connect(self.worker.test_sound)

        # Settings edits are saved instantly by the Settings page; the worker is told after a short
        # pause so dragging a spin box doesn't trigger a burst of re-evaluations / firmware checks.
        self._worker_settings_timer = QTimer(self)
        self._worker_settings_timer.setSingleShot(True)
        self._worker_settings_timer.setInterval(700)
        self._worker_settings_timer.timeout.connect(self.request_settings_changed)
        self._wake_sync_pending = QTimer(self)
        self._wake_sync_pending.setSingleShot(True)
        self._wake_sync_pending.setInterval(300)
        self._wake_sync_pending.timeout.connect(self.sync_wake)
        # Self-heal the wake task every 30 min while ARES runs (e.g. if it was deleted by hand).
        self._wake_heal = QTimer(self)
        self._wake_heal.setInterval(30 * 60_000)
        self._wake_heal.timeout.connect(self.sync_wake)

    # ---- lifecycle ------------------------------------------------------------
    def start(self) -> None:
        if self.thread.isRunning():
            return                      # never start a second monitoring loop
        self.worker_starts += 1
        self.thread.start()
        self.lock.start()
        log.info("Background monitoring started")
        # Register / repair "start with Windows" a few seconds after start-up (keeps launch fast).
        if self.manage_startup:
            QTimer.singleShot(3000, self.sync_startup)
            QTimer.singleShot(4000, self.apply_profile_if_requested)
        if self._wake_managed():
            QTimer.singleShot(5000, self.sync_wake)
            self._wake_heal.start()

    def sync_startup(self) -> None:
        """Register / repair (or remove) ARES' own Windows startup entry to match the setting."""
        if self._closed:
            return
        from app.core import startup_manager

        ok, msg = startup_manager.apply_preference(self.db, bool(self.db.get_setting("autostart")), "launch check")
        log.info("Startup check: %s (%s)", msg, "ok" if ok else "not applied")

    # ---- Smart Wake-Up -------------------------------------------------------------
    def _wake_managed(self) -> bool:
        return self.manage_startup or self.wake_scheduler is not None

    def sync_wake(self) -> tuple[bool, str]:
        """Register / refresh / remove the wake-check task to match the settings."""
        if self._closed or not self._wake_managed():
            return False, "Smart Wake-Up task not managed in this run."
        from app.core import smart_wake

        ok, msg = smart_wake.ensure(self.db, self.wake_scheduler)
        if self.wake_last_sync != (ok, msg):
            self.db.log_event("smart_wake" if ok else "warning", f"Smart Wake-Up: {msg}")
        self.wake_last_sync = (ok, msg)
        self.tray.set_smart_wake(bool(self.db.get_setting("wake_enabled")))
        return ok, msg

    def set_smart_wake(self, on: bool) -> None:
        """Tray toggle (also the immediate 'turn it off' path)."""
        if self._closed:
            return
        self.db.set_setting("wake_enabled", bool(on))
        self.sync_wake()
        self.tray.set_smart_wake(bool(on))
        if self.window is not None:
            self.window.settings_page.load_key("wake_enabled")

    def test_wake(self) -> tuple[bool, list[str]]:
        """Settings 'Test wake configuration': bring the task in line first, then report."""
        from app.core import smart_wake

        if self._wake_managed():
            self.sync_wake()
        return smart_wake.test_configuration(self.db, self.wake_scheduler)

    def wake_trigger(self, mode: str, reason: str) -> None:
        """A wake check found a trigger: re-read the battery now and show protection if due."""
        if self._closed:
            return
        self.lock.wake_trigger(mode, reason)
        self.request_settings_changed.emit()          # makes the worker take a fresh reading now

    def apply_profile_if_requested(self) -> None:
        """Apply the preferred power plan at startup ONLY if the user enabled that option."""
        if self._closed or not self.db.get_setting("apply_profile_on_startup"):
            return
        from app.services import power_profile

        name = self.db.get_setting("power_profile")
        if name in power_profile.PROFILES and power_profile.PROFILES[name]:
            ok, msg = power_profile.apply_profile(name)
            self.db.log_event("info" if ok else "warning", f"Power profile at startup: {msg}")

    def _show_tray(self) -> None:
        # At Windows logon the taskbar may not be ready yet; retry for up to ~2 minutes.
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
            self._tray_retry.stop()
        elif not self._tray_retry.isActive():
            self._tray_retry.start(3000)
            QTimer.singleShot(120_000, self._tray_retry.stop)

    def exit_ares(self) -> None:
        """The ONLY user path that terminates ARES: stop monitoring, close any window, remove tray."""
        if self._quitting:
            return
        self._quitting = True
        log.info("Exit ARES requested")
        self.shutdown()
        QApplication.instance().quit()

    def shutdown(self) -> None:
        """Release every resource (thread, DB connections, tray). Safe to call more than once."""
        self._quitting = True
        if self._closed:
            return
        self._closed = True                  # from here on, late worker signals are ignored
        self._tray_retry.stop()
        self._worker_settings_timer.stop()
        self._wake_heal.stop()
        self._wake_sync_pending.stop()
        self.lock.shutdown()
        if self.window is not None:
            self.window.close()
        if self.thread.isRunning():
            # Blocking call: the worker stops its timer and closes its own SQLite connection
            # on its own thread before we stop the thread.
            QMetaObject.invokeMethod(self.worker, "shutdown", Qt.BlockingQueuedConnection)
            self.thread.quit()
            if not self.thread.wait(10000):
                log.error("Monitoring thread did not stop within 10 s")
        else:
            self.worker.shutdown()               # never started: nothing running, just close
        self.tray.hide()
        self.db.close()
        log.info("Background app shut down")

    # ---- dashboard window (create / destroy on demand) ------------------------
    def open_dashboard(self, tab: int | None = 0) -> None:
        """tab None = the tab the user had open last time."""
        if self._quitting:
            return
        if self.window is None:
            from app.ui.main_window import MainWindow   # imported lazily: no UI cost while in background

            self.window = MainWindow(self)
            self.window.destroyed.connect(self._window_destroyed)
            log.info("Dashboard window created")
        if tab is None:
            tab = int(self.db.get_setting("ui_last_tab", 0) or 0)
        self.window.show_tab(tab)
        if self.lock.active:                 # lock stays in front; dashboard not usable underneath
            self.window.setEnabled(False)
            self.lock.window.cover_screen()

    def _on_lock_changed(self, locked: bool) -> None:
        if self.window is not None:
            self.window.setEnabled(not locked)
            if not locked:
                self.window.raise_()         # back to the dashboard if it was open

    def _window_destroyed(self, *_):
        self.window = None
        log.info("Dashboard window closed and destroyed; background monitoring continues")

    def close_dashboard(self) -> None:
        if self.window is not None:
            self.window.close()

    # ---- worker → cache → window ---------------------------------------------
    # Worker signals are queued across threads, so a reading taken just before Exit can be
    # delivered after shutdown() closed the database. Those late deliveries are dropped here.
    def _on_snapshot(self, s) -> None:
        if self._closed:
            return
        self.last_snapshot = s
        self.lock.on_snapshot(s)
        self.snapshot.emit(s)

    def _on_status(self, st) -> None:
        from app.core import charging_controller as cc

        if self._closed:
            return
        self.last_status = st
        s = self.last_snapshot
        if s is not None:
            self.tray.update_state(s.percent, bool(s.charging), st.state == cc.PAUSED, st.headline)
        self.status.emit(st)

    def _on_profile(self, prof) -> None:
        if self._closed:
            return
        self.last_profile = prof
        self.profile.emit(prof)

    def _on_monitoring_changed(self, on: bool) -> None:
        if self._closed:
            return
        self.monitoring = on
        self.tray.set_monitoring(on)
        if not on:
            self.tray.notify(APP_NAME, "Monitoring paused. Firmware charge thresholds (if applied) stay active.",
                             "info")
        self.monitoring_changed.emit(on)

    # ---- live settings ------------------------------------------------------------
    WAKE_KEYS = {"wake_enabled", "wake_low_enabled", "wake_low_threshold", "wake_high_enabled",
                 "wake_high_threshold", "wake_interval_min"}
    PROTECTION_KEYS = {"strict_lock_enabled", "strict_lock_threshold", "high_alarm_repeat_min",
                       "low_lock_enabled", "low_lock_threshold", "low_alarm_repeat_min", "sound_enabled"}
    WORKER_KEYS = {"monitor_interval_s", "max_charge", "min_charge", "auto_apply_thresholds", "cctk_path"}

    def on_setting_changed(self, key: str) -> None:
        """A setting was saved (Settings page). Push it to the service that uses it."""
        if self._closed:
            return
        if key in self.PROTECTION_KEYS:
            self.lock.settings_changed()            # e.g. turning protection off releases its screen now
        if key in self.WAKE_KEYS:
            self._wake_sync_pending.start()         # one Task Scheduler update for a whole Save
        if key == "theme":
            from app.ui.theme import apply_theme

            app = QApplication.instance()
            if isinstance(app, QApplication):
                apply_theme(app, self.db.get_setting("theme"))
        if key == "monitoring_enabled":
            self.request_monitoring.emit(bool(self.db.get_setting("monitoring_enabled")))
        elif key == "cctk_path":
            self.request_redetect.emit()
        elif key in self.WORKER_KEYS:
            self._worker_settings_timer.start()
        # Notification thresholds, sound paths and the notification/sound switches are read from
        # the database at the moment they are used, so they apply immediately without a push.

    def capabilities(self):
        ctl = getattr(self.worker, "controller", None)
        return ctl.caps if ctl is not None else None
