"""Battery telemetry acquisition.

Primary source: the Windows battery class driver (IOCTL_BATTERY_QUERY_*), which
gives mWh capacities, charge/discharge rate in mW, voltage, and power-state flags
(ON_LINE / CHARGING / DISCHARGING) straight from the ACPI battery driver.
Secondary source: GetSystemPowerStatus (always available, coarse).
Static fallback: WMI root\\wmi via PowerShell, only if the IOCTL path fails.

Every field is Optional. A missing value stays None and is shown as "N/A".
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field

from app.utils import calculations as calc
from app.utils import system_info as si

log = logging.getLogger(__name__)

# Temperatures outside this window are treated as sensor errors, not real readings.
PLAUSIBLE_TEMP_C = (-20.0, 90.0)


@dataclass
class BatterySnapshot:
    timestamp: float
    available: bool                      # a battery was detected and read
    source: str                          # "ioctl", "power_status", or "none"
    percent: float | None = None
    ac_online: bool | None = None
    charging: bool | None = None         # hardware says current is flowing INTO the battery
    discharging: bool | None = None
    critical: bool | None = None
    rate_mw: int | None = None           # + charging, - discharging
    voltage_mv: int | None = None
    remaining_mwh: int | None = None
    full_charge_mwh: int | None = None
    design_mwh: int | None = None
    cycle_count: int | None = None
    manufacturer: str | None = None
    model: str | None = None
    serial: str | None = None
    chemistry: str | None = None
    manufacture_date: str | None = None
    temperature_c: float | None = None
    temperature_note: str | None = None
    windows_runtime_min: float | None = None
    battery_saver_on: bool | None = None
    errors: list[str] = field(default_factory=list)

    # ---- derived values -------------------------------------------------- #
    @property
    def health_pct(self) -> float | None:
        return calc.health_percent(self.full_charge_mwh, self.design_mwh)

    @property
    def wear_pct(self) -> float | None:
        return calc.wear_percent(self.full_charge_mwh, self.design_mwh)

    @property
    def runtime_min(self) -> float | None:
        r = calc.runtime_minutes(self.remaining_mwh, self.rate_mw)
        return r if r is not None else (self.windows_runtime_min if self.discharging else None)

    @property
    def power_w(self) -> float | None:
        return None if self.rate_mw is None else round(abs(self.rate_mw) / 1000.0, 2)

    @property
    def idle_on_ac(self) -> bool:
        """AC present, battery neither charging nor discharging, per hardware flags."""
        return bool(self.ac_online) and self.charging is False and not self.discharging

    def charging_state_text(self) -> str:
        if not self.available:
            return "No battery detected"
        if self.charging:
            return "Charging"
        if self.discharging or self.ac_online is False:
            return "Discharging (on battery)"
        if self.idle_on_ac:
            if self.percent is not None and self.percent >= 99:
                return "Fully charged"
            return "Not charging (on AC)"
        return "Unknown"

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(health_pct=self.health_pct, wear_pct=self.wear_pct, runtime_min=self.runtime_min)
        return d


class BatteryMonitor:
    """Reads battery telemetry. Cheap enough to call every few seconds."""

    STATIC_REFRESH_S = 600

    def __init__(self) -> None:
        self._device_paths: list[str] | None = None
        self._static_cache: dict = {}
        self._static_ts = 0.0
        self._wmi_static: dict | None = None

    def _paths(self) -> list[str]:
        if self._device_paths is None or not self._device_paths:
            try:
                self._device_paths = si.list_battery_device_paths()
            except OSError as exc:
                log.warning("Battery enumeration failed: %s", exc)
                self._device_paths = []
        return self._device_paths

    def read(self) -> BatterySnapshot:
        now = time.time()
        if not si.IS_WINDOWS:
            return BatterySnapshot(now, False, "none", errors=["Not running on Windows: no battery API."])

        ps = si.get_power_status()
        snap = BatterySnapshot(now, False, "none")
        if ps is not None:
            snap.ac_online = ps.ac_online
            snap.percent = ps.percent
            snap.charging = ps.charging
            snap.battery_saver_on = ps.battery_saver_on
            snap.windows_runtime_min = round(ps.seconds_remaining / 60.0, 1) if ps.seconds_remaining else None
            if not ps.no_battery and ps.percent is not None:
                snap.available = True
                snap.source = "power_status"

        data = None
        for path in self._paths():
            try:
                data = si.read_battery_ioctl(path)
            except OSError as exc:
                snap.errors.append(f"IOCTL error: {exc}")
                data = None
            if data and data.power_state is not None:
                break  # E7470 has a single internal battery; use the first that answers
        if data is None or data.power_state is None:
            self._device_paths = None  # re-enumerate next time (battery may have been re-seated)
        else:
            self._apply_ioctl(snap, data)

        self._apply_static_fallback(snap, now)
        self._check_temperature(snap)
        return snap

    def _apply_ioctl(self, snap: BatterySnapshot, d: si.BatteryIoctlData) -> None:
        snap.available = True
        snap.source = "ioctl"
        st = d.power_state or 0
        snap.ac_online = bool(st & si.BATTERY_POWER_ON_LINE)
        snap.charging = bool(st & si.BATTERY_CHARGING)
        snap.discharging = bool(st & si.BATTERY_DISCHARGING)
        snap.critical = bool(st & si.BATTERY_CRITICAL)
        snap.voltage_mv = d.voltage_mv
        snap.rate_mw = d.rate_mw
        if not d.relative_units:
            snap.remaining_mwh = d.capacity_mwh
            snap.full_charge_mwh = d.full_charged_capacity_mwh
            snap.design_mwh = d.designed_capacity_mwh
            if d.capacity_mwh is not None and d.full_charged_capacity_mwh:
                # Windows' own % is computed the same way; keep the integer from
                # GetSystemPowerStatus if present to match the taskbar exactly.
                if snap.percent is None:
                    snap.percent = round(d.capacity_mwh / d.full_charged_capacity_mwh * 100.0, 1)
        snap.cycle_count = d.cycle_count
        snap.manufacturer = d.manufacturer
        snap.model = d.device_name
        snap.serial = d.serial_number
        snap.chemistry = d.chemistry
        snap.manufacture_date = d.manufacture_date
        snap.temperature_c = d.temperature_c
        snap.errors.extend(d.errors)

    def _apply_static_fallback(self, snap: BatterySnapshot, now: float) -> None:
        """Fill static fields from WMI only if the driver didn't provide them."""
        needs = snap.available and (snap.design_mwh is None or snap.full_charge_mwh is None
                                    or snap.manufacturer is None)
        if not needs:
            return
        if self._wmi_static is None or now - self._static_ts > self.STATIC_REFRESH_S:
            self._wmi_static = si.read_wmi_battery_static()
            self._static_ts = now
        w = self._wmi_static or {}
        snap.design_mwh = snap.design_mwh or w.get("designed_capacity_mwh")
        snap.full_charge_mwh = snap.full_charge_mwh or w.get("full_charged_capacity_mwh")
        snap.manufacturer = snap.manufacturer or w.get("manufacturer")
        snap.model = snap.model or w.get("device_name")
        snap.serial = snap.serial or w.get("serial_number")
        snap.cycle_count = snap.cycle_count or w.get("cycle_count")

    @staticmethod
    def _check_temperature(snap: BatterySnapshot) -> None:
        t = snap.temperature_c
        if t is None:
            snap.temperature_note = "No battery temperature sensor exposed by the driver"
            return
        lo, hi = PLAUSIBLE_TEMP_C
        if not (lo <= t <= hi):
            snap.temperature_note = f"Sensor returned implausible value ({t} °C); ignored"
            snap.temperature_c = None
        else:
            snap.temperature_note = "Battery class driver (ACPI)"
