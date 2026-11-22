"""Smart notifications with de-duplication. Display is delegated to a sink
(the system tray's Windows toast via QSystemTrayIcon.showMessage)."""
from __future__ import annotations

import time
from typing import Callable

from app.core.battery_monitor import BatterySnapshot
from app.database.database import Database
from app.services import sound_service

Sink = Callable[[str, str, str], None]  # (title, message, level: info|warning|critical)
SoundPlayer = Callable[[str | None], tuple[bool, str]]   # (wav path or None) -> (played, description)

# The "reached maximum" alert re-arms once the battery has dropped this many points below the maximum,
# so it sounds once per charge cycle, not on every reading while the battery sits at 90%.
MAX_ALERT_REARM_POINTS = 5

COOLDOWN_S = {"low": 15 * 60, "temp": 15 * 60, "health": 7 * 86400, "unconfirmed": 3600}


class NotificationService:
    def __init__(self, db: Database, sink: Sink | None = None, clock: Callable[[], float] = time.time,
                 sound: SoundPlayer = sound_service.play_alert) -> None:
        self.db = db
        self.sink = sink
        self.clock = clock
        self.sound = sound
        self._last: dict[str, float] = {}
        self._low_armed = True

    def enabled(self) -> bool:
        return bool(self.db.get_setting("notifications_enabled"))

    def _send(self, key: str, title: str, msg: str, level: str = "info", cooldown: float = 0) -> bool:
        now = self.clock()
        if cooldown and now - self._last.get(key, 0) < cooldown:
            return False
        self._last[key] = now
        if self.enabled() and self.sink:
            self.sink(title, msg, level)
        return True

    # ---- controller events ---------------------------------------------------
    def controller_event(self, kind: str, message: str) -> None:
        if kind == "charging_paused":
            self._send(kind, "Charging paused", message, "info")
        elif kind == "charging_resumed":
            self._send(kind, "Charging resumed", message, "info")
        elif kind == "applied":
            self._send(kind + message, "Charge thresholds applied", message, "info")
        elif kind == "error":
            self._send("error" + message, "Charge control problem", message, "warning", cooldown=3600)
        elif kind == "warning":
            self._send("warn" + message, "ARES Battery Guardian", message, "warning", cooldown=3600)

    # ---- telemetry checks ------------------------------------------------------
    # ---- maximum-charge alert (sound + notification, once per charge cycle) ----------
    def check_max_reached(self, s: BatterySnapshot, control_active: bool) -> bool:
        """Alert when the battery reaches the user's maximum while on AC. Returns True if it alerted.

        The armed/disarmed flag is stored in the database so a restart at 92% doesn't alert again.
        """
        if not s.available or s.percent is None:
            return False
        max_pct = int(self.db.get_setting("max_charge"))
        armed = self.db.get_state("max_alert_armed", True)
        if s.percent < max_pct - MAX_ALERT_REARM_POINTS:
            if not armed:
                self.db.set_state("max_alert_armed", True)
            return False
        if not armed or s.percent < max_pct or not s.ac_online:
            return False
        self.db.set_state("max_alert_armed", False)
        if control_active:
            msg = (f"Battery reached {s.percent:.0f}%. Firmware charge limit is active, so the battery stops "
                   f"charging at {max_pct}% while the laptop keeps running on AC.")
        else:
            msg = (f"Battery reached {s.percent:.0f}%. Hardware charge control is NOT active on this PC, so the "
                   f"battery may keep charging. Unplug the charger if you want to stop at {max_pct}%.")
        # With High Battery Protection on, its full-screen alarm plays the sound (once, controlled
        # repeats); playing here too would double it.
        if self.db.get_setting("sound_enabled") and not self.db.get_setting("strict_lock_enabled"):
            played, how = self.sound(self.db.get_setting("high_alarm_sound_path") or None)
            self.db.log_event("info", f"Maximum-charge sound: {how}")
        if self.enabled() and self.sink:
            self.sink(f"Battery at {max_pct}%", msg, "info" if control_active else "warning")
        self.db.log_event("max_reached", msg)
        return True

    def check_snapshot(self, s: BatterySnapshot) -> None:
        if not s.available or s.percent is None:
            return
        low = int(self.db.get_setting("notify_low_pct"))
        on_batt = s.ac_online is False or bool(s.discharging)
        if on_batt and s.percent <= low and self._low_armed:
            if self._send("low", "Battery low", f"Battery at {s.percent:.0f}%. Connect the charger.",
                          "critical", cooldown=COOLDOWN_S["low"]):
                self._low_armed = False
        if s.percent > low + 5 or not on_batt:
            self._low_armed = True
        warn_t = float(self.db.get_setting("temp_warn_c"))
        if s.temperature_c is not None and s.temperature_c >= warn_t:
            self._send("temp", "Battery temperature high",
                       f"Battery sensor reports {s.temperature_c:.1f} °C (warning level {warn_t:.0f} °C).",
                       "warning", cooldown=COOLDOWN_S["temp"])

    def check_health(self, first_health: float | None, current_health: float | None) -> None:
        drop_limit = float(self.db.get_setting("notify_health_drop_pct"))
        if first_health is None or current_health is None:
            return
        drop = first_health - current_health
        if drop >= drop_limit:
            self._send("health", "Battery health decreased",
                       f"Battery health fell from {first_health:.1f}% to {current_health:.1f}% since tracking began.",
                       "warning", cooldown=COOLDOWN_S["health"])
