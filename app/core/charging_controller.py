"""Hysteresis charge controller.

How it works with real firmware (e.g. Dell):
* The thresholds are written ONCE to the BIOS (Custom start/stop). From then on
  the embedded controller stops charging at the stop value and resumes below the
  start value by itself, even when ARES is not running or the PC is off.
* ARES therefore does not toggle charging on and off. On each tick it compares the
  battery level with the thresholds, makes sure (only if the user enabled it) that
  the firmware still holds the requested values, and reports the charging state
  ONLY from hardware telemetry (battery driver CHARGING / ON_LINE flags).
* Commands are never repeated while the firmware already matches; failures back off
  exponentially; a missing password or missing admin rights stops retries entirely.
* All state is persisted in SQLite, and on start-up the firmware is re-read and is
  treated as the source of truth, so a crash or power loss can't leave things in a
  half-applied state.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from app.core.battery_monitor import BatterySnapshot
from app.database.database import Database
from app.hardware.base_controller import BaseChargeController, ChargeConfig, ControllerCapabilities, WriteResult
from app.utils.calculations import ThresholdPlan, validate_user_thresholds

log = logging.getLogger(__name__)

# Controller states
MONITORING_ONLY = "monitoring_only"
NO_BATTERY = "no_battery"
ON_BATTERY = "on_battery"
CHARGING = "charging"
PAUSED = "paused"              # confirmed by telemetry: AC online, battery not charging, thresholds active
PAUSE_PENDING = "pause_pending"
RESUMED = "resumed"            # confirmed by telemetry: charging after reaching the start threshold
RESUME_PENDING = "resume_pending"
IDLE_AC = "idle_ac"            # AC online, not charging, cause not attributable to ARES thresholds

FIRMWARE_REREAD_S = 15 * 60
BACKOFF_START_S = 5 * 60
BACKOFF_MAX_S = 6 * 3600
UNCONFIRMED_WARN_S = 10 * 60


@dataclass
class ControllerStatus:
    state: str
    headline: str
    detail: str
    firmware: ChargeConfig | None
    plan: ThresholdPlan | None
    capabilities: ControllerCapabilities
    blocked_reason: str | None = None
    messages: list[str] = field(default_factory=list)


EventSink = Callable[[str, str], None]   # (kind, message)


class ChargingController:
    def __init__(self, db: Database, hw: BaseChargeController, on_event: EventSink | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.db = db
        self.hw = hw
        self.on_event = on_event or (lambda k, m: None)
        self.clock = clock
        self.caps = hw.capabilities
        self.firmware: ChargeConfig | None = None
        self._fw_read_ts = 0.0
        self._password: str | None = None              # held in memory only, never stored
        self._state: str = db.get_state("last_state", "") or ""
        self._unconfirmed_since: float | None = None
        self._unconfirmed_warned = False
        self.blocked_reason: str | None = db.get_state("blocked_reason")
        self._startup_check_done = False   # with auto-apply, also check/apply once right after start
        self._startup_reconcile()

    # ---- persistence / recovery ------------------------------------------
    def _startup_reconcile(self) -> None:
        if not self.caps.can_read:
            return
        self._reread_firmware(force=True)
        if self.firmware and self.db.get_state("original_config") is None:
            # Remember the configuration we found before ARES ever changed anything, for "Restore".
            self.db.set_state("original_config", self.firmware.to_dict())
            self._event("info", f"Saved original firmware charge setting: {self.firmware.describe()}")
        last_applied = ChargeConfig.from_dict(self.db.get_state("last_applied"))
        if last_applied and self.firmware and not self.firmware.matches(last_applied.start or -1,
                                                                        last_applied.stop or -1):
            self._event("warning", f"Firmware setting is now {self.firmware.describe()}, which differs from "
                                   f"what ARES last applied ({last_applied.describe()}). It may have been changed "
                                   f"in BIOS Setup or reset.")

    def _reread_firmware(self, force: bool = False) -> None:
        now = self.clock()
        if not self.caps.can_read:
            return
        if not force and now - self._fw_read_ts < FIRMWARE_REREAD_S:
            return
        cfg = self.hw.read_config()
        self._fw_read_ts = now
        if cfg is not None:
            self.firmware = cfg
            self.db.set_state("last_firmware", cfg.to_dict())

    def _event(self, kind: str, msg: str) -> None:
        log.info("[%s] %s", kind, msg)
        self.db.log_event(kind, msg)
        self.on_event(kind, msg)

    def _set_state(self, state: str) -> bool:
        changed = state != self._state
        if changed:
            self._state = state
            self.db.set_state("last_state", state)
        return changed

    # ---- settings ----------------------------------------------------------
    def user_thresholds(self) -> tuple[int, int]:
        return int(self.db.get_setting("min_charge")), int(self.db.get_setting("max_charge"))

    def plan(self) -> ThresholdPlan | None:
        start, stop = self.user_thresholds()
        return self.hw.plan(start, stop)

    def set_password(self, password: str | None) -> None:
        self._password = password or None
        if self.blocked_reason == "password":
            self._clear_block()

    def _clear_block(self) -> None:
        self.blocked_reason = None
        self.db.set_state("blocked_reason", None)
        self.db.set_state("write_failures", 0)
        self.db.set_state("next_write_ts", 0)

    # ---- writes ------------------------------------------------------------
    def control_active(self) -> bool:
        """True when hardware charge control is supported AND the firmware holds the planned thresholds."""
        return bool(self.caps.supported and self.firmware_matches_plan())

    def firmware_matches_plan(self) -> bool:
        p = self.plan()
        return bool(p and self.firmware and self.firmware.matches(p.start, p.stop))

    def apply_thresholds(self, manual: bool = False) -> WriteResult | None:
        """Write the planned thresholds if (and only if) the firmware differs. Returns None if nothing sent."""
        if not self.caps.supported:
            return WriteResult(False, False, "Hardware charge control is not available; monitoring only.")
        start, stop = self.user_thresholds()
        errs = validate_user_thresholds(start, stop)
        if errs:
            return WriteResult(False, False, " ".join(errs))
        plan = self.plan()
        if plan is None:
            return WriteResult(False, False, "Firmware limits unknown; refusing to write.")
        self._reread_firmware(force=manual)
        if self.firmware and self.firmware.matches(plan.start, plan.stop):
            return None if not manual else WriteResult(True, True, "Firmware already holds these thresholds.",
                                                       self.firmware)
        now = self.clock()
        if manual:
            self._clear_block()
        else:
            if self.blocked_reason:
                return None
            if now < float(self.db.get_state("next_write_ts", 0) or 0):
                return None
        res = self.hw.write_thresholds(plan.start, plan.stop, self._password)
        self.db.set_state("last_write_ts", now)
        if res.config_after:
            self.firmware = res.config_after
            self._fw_read_ts = now
            self.db.set_state("last_firmware", res.config_after.to_dict())
        if res.success and res.verified:
            self.db.set_state("last_applied", ChargeConfig("Custom", plan.start, plan.stop).to_dict())
            self._clear_block()
            self._event("applied", f"Firmware confirmed charge thresholds: start {plan.start}% / stop {plan.stop}%.")
        else:
            fails = int(self.db.get_state("write_failures", 0) or 0) + 1
            self.db.set_state("write_failures", fails)
            delay = min(BACKOFF_MAX_S, BACKOFF_START_S * (2 ** (fails - 1)))
            self.db.set_state("next_write_ts", now + delay)
            if res.needs_password:
                self.blocked_reason = "password"
            elif res.needs_admin:
                self.blocked_reason = "admin"
            if self.blocked_reason:
                self.db.set_state("blocked_reason", self.blocked_reason)
            self._event("error", f"Could not apply thresholds: {res.message}")
        return res

    def restore_original(self) -> WriteResult:
        orig = ChargeConfig.from_dict(self.db.get_state("original_config"))
        if orig is None:
            orig = ChargeConfig("Standard")
        res = self.hw.restore_mode(orig, self._password)
        if res.config_after:
            self.firmware = res.config_after
        self._event("applied" if res.verified else "error",
                    f"Restore {orig.describe()}: {res.message}")
        if res.verified:
            self.db.set_state("last_applied", None)
            self.db.set_setting("auto_apply_thresholds", False)
        return res

    # ---- main tick ---------------------------------------------------------
    def evaluate(self, s: BatterySnapshot) -> ControllerStatus:
        plan = self.plan()
        msgs = list(plan.messages) if plan else []
        if not s.available or s.percent is None:
            self._set_state(NO_BATTERY)
            return self._status(NO_BATTERY, "No battery data", "The battery driver returned no data.", plan, msgs)

        if not self.caps.supported:
            return self._monitoring_only(s, plan, msgs)

        self._reread_firmware()
        user_min, user_max = self.user_thresholds()
        fw_ok = self.firmware_matches_plan()
        stop = plan.stop if plan else user_max
        start = plan.start if plan else user_min
        auto = bool(self.db.get_setting("auto_apply_thresholds"))
        pct = s.percent

        # Commands are sent only at the hysteresis edges (>= max, <= min), once at start-up,
        # or on explicit user action, and only if the firmware does not already match.
        at_edge = pct >= user_max or pct <= user_min or not self._startup_check_done
        self._startup_check_done = True
        if auto and at_edge and not fw_ok:
            self.apply_thresholds()
            fw_ok = self.firmware_matches_plan()

        if s.ac_online is False or s.discharging:
            state, head = ON_BATTERY, "Discharging (on battery)"
            detail = "AC adapter not supplying power." if s.ac_online is False else "Battery is discharging."
        elif fw_ok and pct >= stop - 1:
            if s.charging:
                state, head = PAUSE_PENDING, "Stop requested, still charging"
                detail = (f"Firmware stop threshold is {stop}%, but the battery driver still reports charging. "
                          f"Waiting for hardware confirmation.")
            else:
                state, head = PAUSED, "Charging Paused"
                detail = (f"Confirmed by battery telemetry: AC online, battery not charging. "
                          f"Firmware will resume charging below {start}%.")
        elif fw_ok and pct <= start:
            if s.charging:
                state, head = RESUMED, "Charging Resumed"
                detail = f"Confirmed by battery telemetry: charging until {stop}%."
            else:
                state, head = RESUME_PENDING, "Resume expected, not charging yet"
                detail = f"Battery is at or below the {start}% start threshold but the driver reports no charging."
        elif s.charging:
            state, head = CHARGING, "Charging"
            detail = f"Firmware will stop at {stop}%." if fw_ok else "Firmware thresholds not active."
        elif fw_ok:
            state, head = PAUSED, "Charging Paused"
            detail = (f"Confirmed by battery telemetry: AC online, battery not charging. Between thresholds "
                      f"({start}-{stop}%); firmware resumes below {start}%.")
        else:
            state, head = IDLE_AC, "Not charging (on AC)"
            detail = "The firmware chose not to charge; ARES thresholds are not active."

        if not fw_ok:
            if self.firmware is None:
                msgs.append("Current firmware setting could not be read.")
            else:
                msgs.append(f"Firmware currently: {self.firmware.describe()}.")
            if self.blocked_reason == "password":
                msgs.append("BIOS admin password required to change settings. Enter it in Settings.")
            elif self.blocked_reason == "admin":
                msgs.append("Run ARES as administrator to change settings.")
            elif not self.caps.can_write:
                msgs.append("Writing needs administrator rights.")

        self._track_confirmation(state, pct)
        if self._set_state(state):
            if state == PAUSED:
                self._event("charging_paused", f"Charging paused at {pct:.0f}% (confirmed by battery telemetry).")
            elif state == RESUMED:
                self._event("charging_resumed", f"Charging resumed at {pct:.0f}% (confirmed by battery telemetry).")
        return self._status(state, head, detail, plan, msgs)

    def _track_confirmation(self, state: str, pct: float) -> None:
        now = self.clock()
        if state in (PAUSE_PENDING, RESUME_PENDING):
            if self._unconfirmed_since is None:
                self._unconfirmed_since = now
            elif not self._unconfirmed_warned and now - self._unconfirmed_since > UNCONFIRMED_WARN_S:
                self._unconfirmed_warned = True
                what = "stop" if state == PAUSE_PENDING else "resume"
                self._event("warning", f"Firmware has not confirmed charge {what} after 10 minutes at {pct:.0f}%. "
                                       "Some Dell batteries ignore custom thresholds; check BIOS Setup.")
        else:
            self._unconfirmed_since = None
            self._unconfirmed_warned = False

    def _monitoring_only(self, s: BatterySnapshot, plan, msgs) -> ControllerStatus:
        self._set_state(MONITORING_ONLY)
        head = s.charging_state_text()
        detail = ("Monitoring only: no supported charge-control interface, so ARES cannot stop or resume "
                  "charging. States shown come from the battery driver.")
        msgs = msgs + list(self.caps.notes)
        return self._status(MONITORING_ONLY, head, detail, plan, msgs)

    def _status(self, state, head, detail, plan, msgs) -> ControllerStatus:
        return ControllerStatus(state, head, detail, self.firmware, plan, self.caps, self.blocked_reason, msgs)
