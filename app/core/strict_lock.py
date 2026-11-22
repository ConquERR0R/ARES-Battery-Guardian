"""Battery protection decision logic (pure Python, no Qt).

Two protection modes share one full-screen reminder window (see app/ui/lock_window.py):

* HIGH ("Strict Charging Protection"): battery >= high threshold (default 90%) while the
  charger is connected -> ask the user to remove the charger.
* LOW ("Low Battery Protection"): battery <= low threshold (default 25%) while the charger
  is disconnected -> ask the user to connect the charger.

These are REMINDERS. They do not stop or start charging. The Dell firmware thresholds
(app/core/charging_controller.py) are a separate, independent mechanism.

Rules
-----
* Priority: LOW is checked before HIGH. Only one mode is ever active.
* HIGH session = the period the charger stays connected; it ends when the hardware reports
  the charger disconnected. LOW session = the period on battery; it ends when the charger is
  reported connected. Each mode fires at most once per session.
* HIGH releases ONLY on a confirmed unplug; LOW releases ONLY on a confirmed plug-in. A
  changing percentage alone never releases a lock.
* Unknown charger state (None) never locks. If it stays unknown while locked, the manager
  releases the lock (fail-open) so nobody can be stuck behind it.
* "Lock active" is NOT persisted: after a restart it is recomputed from fresh telemetry, so
  a crash can never leave a stale lock. Only the per-mode emergency "suppressed for this
  session" flag is persisted, so a restart after an emergency release does not re-lock
  immediately.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.database.database import Database

LOCK = "lock"
UNLOCK = "unlock"

HIGH = "high"
LOW = "low"
MODES = (LOW, HIGH)        # priority order

# Settings / state keys per mode. The HIGH keys keep their original names so existing
# installs keep their configuration.
ENABLED_KEY = {HIGH: "strict_lock_enabled", LOW: "low_lock_enabled"}
THRESHOLD_KEY = {HIGH: "strict_lock_threshold", LOW: "low_lock_threshold"}
SUPPRESSED_KEY = {HIGH: "strict_lock_suppressed", LOW: "low_lock_suppressed"}
EVENT_KIND = {HIGH: "strict_lock", LOW: "low_battery_lock"}
MODE_NAME = {HIGH: "High Battery Protection", LOW: "Low Battery Protection"}


@dataclass
class LockDecision:
    action: str | None      # LOCK, UNLOCK or None (no change)
    reason: str = ""
    mode: str | None = None


class ProtectionPolicy:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.active: str | None = None                         # HIGH, LOW or None
        self._triggered = {HIGH: False, LOW: False}
        self.suppressed = {m: bool(db.get_state(SUPPRESSED_KEY[m], False)) for m in MODES}

    # ---- settings -------------------------------------------------------------
    def enabled(self, mode: str) -> bool:
        return bool(self.db.get_setting(ENABLED_KEY[mode]))

    def threshold(self, mode: str) -> int:
        return int(self.db.get_setting(THRESHOLD_KEY[mode]))

    # ---- core -------------------------------------------------------------------
    def update(self, percent: float | None, ac_online: bool | None) -> LockDecision:
        """One decision per call. After an UNLOCK, call again: the other mode may now apply."""
        if ac_online is None:
            return LockDecision(None)           # never lock or unlock on an unknown charger state

        # A confirmed charger change ends the opposite mode's session and re-arms it.
        if ac_online:
            self._end_session(LOW)
        else:
            self._end_session(HIGH)

        a = self.active
        if a == HIGH and ac_online is False:
            self.active = None
            return LockDecision(UNLOCK, "Charger disconnected", HIGH)
        if a == LOW and ac_online is True:
            self.active = None
            return LockDecision(UNLOCK, "Charger connected", LOW)
        if a is not None and not self.enabled(a):
            self.active = None
            return LockDecision(UNLOCK, f"{MODE_NAME[a]} was turned off in Settings", a)
        if a is not None or percent is None:
            return LockDecision(None)

        if ac_online is False and self._armed(LOW) and percent <= self.threshold(LOW):
            return self._lock(LOW, f"Battery at {percent:.0f}% with charger disconnected "
                                   f"(low limit {self.threshold(LOW)}%)")
        if ac_online is True and self._armed(HIGH) and percent >= self.threshold(HIGH):
            return self._lock(HIGH, f"Battery at {percent:.0f}% with charger connected "
                                    f"(limit {self.threshold(HIGH)}%)")
        return LockDecision(None)

    def release(self, reason: str) -> LockDecision:
        """Emergency / fail-safe release of the active mode. It stays released until the
        charger state changes (unplug for HIGH, plug-in for LOW)."""
        mode = self.active
        if mode is None:
            for m in MODES:                     # nothing showing: pre-emptively suppress both
                self._set_suppressed(m, True)
            return LockDecision(None, reason)
        self._set_suppressed(mode, True)
        self.active = None
        return LockDecision(UNLOCK, reason, mode)

    # ---- helpers ------------------------------------------------------------------
    def _armed(self, mode: str) -> bool:
        return self.enabled(mode) and not self._triggered[mode] and not self.suppressed[mode]

    def _lock(self, mode: str, reason: str) -> LockDecision:
        self.active = mode
        self._triggered[mode] = True
        return LockDecision(LOCK, reason, mode)

    def _end_session(self, mode: str) -> None:
        self._triggered[mode] = False
        if self.suppressed[mode]:
            self._set_suppressed(mode, False)

    def _set_suppressed(self, mode: str, value: bool) -> None:
        self.suppressed[mode] = value
        self.db.set_state(SUPPRESSED_KEY[mode], value)
