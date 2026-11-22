"""Pure battery maths. No I/O, fully unit-tested."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


def health_percent(full_mwh: int | float | None, design_mwh: int | float | None) -> float | None:
    """Full-charge capacity as a percentage of design capacity."""
    if not full_mwh or not design_mwh or design_mwh <= 0 or full_mwh <= 0:
        return None
    return round(full_mwh / design_mwh * 100.0, 1)


def wear_percent(full_mwh, design_mwh) -> float | None:
    h = health_percent(full_mwh, design_mwh)
    if h is None:
        return None
    return round(max(0.0, 100.0 - h), 1)


def capacity_anomalies(full_mwh, design_mwh, current_mwh=None) -> list[str]:
    """Flag readings that suggest the fuel gauge is miscalibrated or reporting garbage."""
    issues: list[str] = []
    if design_mwh is not None and design_mwh <= 0:
        issues.append("Design capacity reported as zero.")
    if full_mwh and design_mwh:
        ratio = full_mwh / design_mwh
        if ratio > 1.10:
            issues.append(f"Full-charge capacity is {ratio*100:.0f}% of design (above 110%): "
                          "fuel gauge may need calibration.")
        elif ratio < 0.30:
            issues.append(f"Full-charge capacity is only {ratio*100:.0f}% of design: battery is "
                          "heavily worn or the reading is invalid.")
    if current_mwh is not None and full_mwh and current_mwh > full_mwh * 1.05:
        issues.append("Remaining capacity is larger than full-charge capacity.")
    return issues


def runtime_minutes(remaining_mwh, rate_mw) -> float | None:
    """Remaining runtime from remaining energy and discharge rate (rate is negative when discharging)."""
    if remaining_mwh is None or rate_mw is None or rate_mw >= 0:
        return None
    draw = abs(rate_mw)
    if draw < 500:  # < 0.5 W is not a real laptop load, ignore unreliable readings
        return None
    return round(remaining_mwh / draw * 60.0, 1)


def time_to_target_minutes(remaining_mwh, full_mwh, target_pct, rate_mw) -> float | None:
    """Minutes until the charge reaches target_pct while charging (rate > 0)."""
    if None in (remaining_mwh, full_mwh, rate_mw) or rate_mw <= 300 or not full_mwh:
        return None
    target_mwh = full_mwh * target_pct / 100.0
    if remaining_mwh >= target_mwh:
        return 0.0
    return round((target_mwh - remaining_mwh) / rate_mw * 60.0, 1)


@dataclass
class DrainStats:
    percent_per_hour: float | None
    avg_power_w: float | None
    samples: int


def drain_rate(samples: Sequence[tuple[float, float]], powers_mw: Iterable[float | None] = ()) -> DrainStats:
    """Least-squares slope of (timestamp_seconds, percent) points while discharging.

    Returns positive %/hour for a draining battery.
    """
    pts = [(t, p) for t, p in samples if p is not None]
    pw = [abs(p) for p in powers_mw if p is not None and p < 0]
    avg_w = round(sum(pw) / len(pw) / 1000.0, 2) if pw else None
    if len(pts) < 2:
        return DrainStats(None, avg_w, len(pts))
    n = len(pts)
    mt = sum(t for t, _ in pts) / n
    mp = sum(p for _, p in pts) / n
    den = sum((t - mt) ** 2 for t, _ in pts)
    if den == 0:
        return DrainStats(None, avg_w, n)
    slope = sum((t - mt) * (p - mp) for t, p in pts) / den  # percent per second
    return DrainStats(round(-slope * 3600.0, 2), avg_w, n)


def format_minutes(minutes: float | None) -> str:
    if minutes is None:
        return "N/A"
    m = int(round(minutes))
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def fmt(value, unit: str = "", digits: int = 0) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float) and digits >= 0:
        value = f"{value:.{digits}f}"
    return f"{value}{(' ' + unit) if unit else ''}"


# --------------------------------------------------------------------------- #
# Threshold validation
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ThresholdLimits:
    """Firmware-imposed limits for custom charge thresholds."""
    start_min: int
    start_max: int
    stop_min: int
    stop_max: int
    min_gap: int


@dataclass
class ThresholdPlan:
    requested_start: int
    requested_stop: int
    start: int          # value the firmware will actually be given
    stop: int
    exact: bool         # True if the firmware can do exactly what was asked
    messages: list[str]


def validate_user_thresholds(start: int, stop: int) -> list[str]:
    """Basic sanity checks independent of hardware."""
    errs = []
    if not (0 <= start <= 100 and 0 <= stop <= 100):
        errs.append("Thresholds must be between 0 and 100%.")
    if stop <= start:
        errs.append("Maximum charge must be higher than the resume percentage.")
    if stop < 50:
        errs.append("Maximum charge below 50% is not allowed (it leaves too little usable battery).")
    if start < 5:
        errs.append("Resume percentage below 5% is not allowed (risk of deep discharge).")
    return errs


def plan_thresholds(start: int, stop: int, limits: ThresholdLimits) -> ThresholdPlan:
    """Map the user's desired thresholds onto the closest configuration the firmware accepts."""
    msgs: list[str] = []
    s = max(limits.stop_min, min(limits.stop_max, stop))
    if s != stop:
        msgs.append(f"Firmware accepts a stop threshold of {limits.stop_min}-{limits.stop_max}%; "
                    f"using {s}% instead of {stop}%.")
    st = max(limits.start_min, min(limits.start_max, start))
    if st != start:
        msgs.append(f"Firmware accepts a start threshold of {limits.start_min}-{limits.start_max}%; "
                    f"using {st}% instead of {start}%. Charging will resume at {st}%, not {start}%.")
    if s - st < limits.min_gap:
        new_st = s - limits.min_gap
        if new_st >= limits.start_min:
            msgs.append(f"Start must be at least {limits.min_gap}% below stop; using start {new_st}%.")
            st = new_st
        else:
            s = st + limits.min_gap
            msgs.append(f"Start must be at least {limits.min_gap}% below stop; using stop {s}%.")
    return ThresholdPlan(start, stop, st, s, exact=(st == start and s == stop), messages=msgs)
