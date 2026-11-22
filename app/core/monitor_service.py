"""Background monitoring worker. Runs in its own QThread so slow calls (cctk,
PowerShell) never freeze the UI. All hardware access happens on this thread."""
from __future__ import annotations

import logging
import time

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from app.core.battery_monitor import BatteryMonitor
from app.core.charging_controller import ChargingController
from app.core.hardware_detector import create_controller, detect
from app.database.battery_history import BatteryHistory
from app.database.database import Database
from app.services.notification_service import NotificationService

log = logging.getLogger(__name__)


class MonitorWorker(QObject):
    snapshot = Signal(object)          # BatterySnapshot
    status = Signal(object)            # ControllerStatus
    profile = Signal(object)           # HardwareProfile
    notify = Signal(str, str, str)     # title, message, level
    action_result = Signal(str, object)  # action name, WriteResult
    monitoring_changed = Signal(bool)
    event_logged = Signal()

    def __init__(self, db_path: str) -> None:
        super().__init__()
        self.db_path = db_path
        self.db: Database | None = None
        self.timer: QTimer | None = None
        self.monitoring = True
        self._last_maint = 0.0

    # ---- lifecycle (called on worker thread) ---------------------------------
    @Slot()
    def start(self) -> None:
        self.db = Database(self.db_path)
        self.monitor = BatteryMonitor()
        self.history = BatteryHistory(self.db)
        self.notifier = NotificationService(self.db, sink=lambda t, m, l: self.notify.emit(t, m, l))
        self._init_controller()
        if not self.db.get_state("auto_apply_default_v2"):
            # v1.0 shipped with automatic firmware apply OFF; the user asked for automatic stop-charging,
            # so turn it on once for existing installs. Unticking it in Settings afterwards is respected.
            self.db.set_setting("auto_apply_thresholds", True)
            self.db.set_state("auto_apply_default_v2", True)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.monitoring = bool(self.db.get_setting("monitoring_enabled"))
        self._apply_interval()
        if self.monitoring:
            self.timer.start()
        self.monitoring_changed.emit(self.monitoring)
        self.tick(force=True)

    def _init_controller(self) -> None:
        prof = detect()
        hw = create_controller(prof, cctk_path=self.db.get_setting("cctk_path") or None)
        self.controller = ChargingController(self.db, hw, on_event=self._on_event)
        self.profile.emit(prof)

    def _on_event(self, kind: str, msg: str) -> None:
        self.notifier.controller_event(kind, msg)
        self.event_logged.emit()

    def _apply_interval(self) -> None:
        sec = max(5, min(600, int(self.db.get_setting("monitor_interval_s"))))
        if self.timer:
            self.timer.setInterval(sec * 1000)

    @Slot()
    def shutdown(self) -> None:
        if self.timer:
            self.timer.stop()
        if self.db:
            self.db.close()
            self.db = None

    # ---- periodic work ------------------------------------------------------
    @Slot()
    def tick(self, force: bool = False) -> None:
        if not self.monitoring and not force:
            return
        try:
            snap = self.monitor.read()
            self.history.record(snap)
            st = self.controller.evaluate(snap)
            self.notifier.check_snapshot(snap)
            self.notifier.check_max_reached(snap, self.controller.control_active())
            self.snapshot.emit(snap)
            self.status.emit(st)
            now = time.time()
            if now - self._last_maint > 3600:
                self._last_maint = now
                self.history.prune(int(self.db.get_setting("sample_retention_days")))
                tr = self.history.health_trend()
                self.notifier.check_health(tr.first_health, tr.last_health)
        except Exception:  # keep monitoring alive whatever happens, but record it
            log.exception("Monitoring tick failed")

    # ---- commands from the UI -----------------------------------------------
    @Slot(bool)
    def set_monitoring(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self.monitoring and (self.timer.isActive() == enabled):
            return
        self.monitoring = enabled
        self.db.set_setting("monitoring_enabled", enabled)
        if enabled:
            self.timer.start()
            self.tick(force=True)
        else:
            self.timer.stop()
        self.db.log_event("info", "Monitoring started" if enabled else "Monitoring paused")
        self.monitoring_changed.emit(enabled)
        self.event_logged.emit()

    @Slot()
    def settings_changed(self) -> None:
        self._apply_interval()
        self.tick(force=True)

    @Slot(str)
    def set_password(self, pwd: str) -> None:
        self.controller.set_password(pwd)

    @Slot()
    def apply_thresholds(self) -> None:
        res = self.controller.apply_thresholds(manual=True)
        self.action_result.emit("apply", res)
        self.tick(force=True)

    @Slot()
    def restore_original(self) -> None:
        res = self.controller.restore_original()
        self.action_result.emit("restore", res)
        self.tick(force=True)

    @Slot()
    def test_sound(self) -> None:
        from app.services import sound_service

        played, how = sound_service.play_alarm(sound_service.HIGH,
                                               self.db.get_setting("high_alarm_sound_path") or None)
        self.db.log_event("info", f"Sound test: {how}")
        self.event_logged.emit()

    @Slot()
    def redetect(self) -> None:
        self._init_controller()
        self.tick(force=True)
