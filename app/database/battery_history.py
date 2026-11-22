"""Battery history: samples, charge/discharge sessions, daily health, drain analysis."""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date

from app.core.battery_monitor import BatterySnapshot
from app.database.database import Database
from app.utils import calculations as calc


def _b(v):
    return None if v is None else int(bool(v))


@dataclass
class HealthTrend:
    first_day: str | None
    last_day: str | None
    first_health: float | None
    last_health: float | None

    @property
    def change(self) -> float | None:
        if self.first_health is None or self.last_health is None:
            return None
        return round(self.last_health - self.first_health, 1)


class BatteryHistory:
    # store a sample when percent changes, state changes, or at least this often
    MAX_SAMPLE_GAP_S = 300

    def __init__(self, db: Database, recover: bool = True) -> None:
        self.db = db
        self._last: BatterySnapshot | None = None
        self._last_store_ts = 0.0
        self._session_id: int | None = None
        self._session_kind: str | None = None
        if recover:
            self._recover_open_session()

    # ---- sessions -----------------------------------------------------------
    def _recover_open_session(self) -> None:
        """Close sessions left open by a crash or power loss (end = last sample before restart)."""
        rows = self.db.query("SELECT id, start_ts FROM sessions WHERE end_ts IS NULL")
        for r in rows:
            last = self.db.query("SELECT ts, percent, remaining_mwh FROM samples WHERE ts>=? ORDER BY ts DESC LIMIT 1",
                                 (r["start_ts"],))
            if last:
                self.db.execute("UPDATE sessions SET end_ts=?, end_pct=?, end_mwh=? WHERE id=?",
                                (last[0]["ts"], last[0]["percent"], last[0]["remaining_mwh"], r["id"]))
            else:
                self.db.execute("UPDATE sessions SET end_ts=start_ts, end_pct=start_pct WHERE id=?", (r["id"],))

    @staticmethod
    def _kind(s: BatterySnapshot) -> str:
        if s.charging:
            return "charge"
        if s.discharging or s.ac_online is False:
            return "discharge"
        return "idle"

    def _update_session(self, s: BatterySnapshot) -> tuple[str | None, str]:
        kind = self._kind(s)
        prev = self._session_kind
        if kind == prev:
            return prev, kind
        if self._session_id is not None:
            self.db.execute("UPDATE sessions SET end_ts=?, end_pct=?, end_mwh=? WHERE id=?",
                            (s.timestamp, s.percent, s.remaining_mwh, self._session_id))
        cur = self.db.execute("INSERT INTO sessions(kind,start_ts,start_pct,start_mwh) VALUES(?,?,?,?)",
                              (kind, s.timestamp, s.percent, s.remaining_mwh))
        self._session_id = cur.lastrowid
        self._session_kind = kind
        return prev, kind

    # ---- recording ----------------------------------------------------------
    def record(self, s: BatterySnapshot) -> tuple[str | None, str]:
        """Record a snapshot. Returns (previous_session_kind, current_session_kind)."""
        if not s.available:
            return None, "none"
        transition = self._update_session(s)
        last = self._last
        changed = (last is None or last.percent != s.percent or last.charging != s.charging
                   or last.ac_online != s.ac_online)
        if changed or s.timestamp - self._last_store_ts >= self.MAX_SAMPLE_GAP_S:
            self.db.execute(
                "INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (s.timestamp, s.percent, _b(s.ac_online), _b(s.charging), _b(s.discharging), s.rate_mw,
                 s.remaining_mwh, s.full_charge_mwh, s.design_mwh, s.voltage_mv, s.temperature_c))
            self._last_store_ts = s.timestamp
        self._last = s
        self._record_health(s)
        return transition

    def _record_health(self, s: BatterySnapshot) -> None:
        h = s.health_pct
        if h is None:
            return
        self.db.execute("INSERT INTO health(day,full_mwh,design_mwh,health_pct,cycle_count) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(day) DO UPDATE SET full_mwh=excluded.full_mwh, design_mwh=excluded.design_mwh,"
                        " health_pct=excluded.health_pct, cycle_count=excluded.cycle_count",
                        (date.today().isoformat(), s.full_charge_mwh, s.design_mwh, h, s.cycle_count))

    def prune(self, retention_days: int) -> None:
        cutoff = time.time() - retention_days * 86400
        self.db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        self.db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        self.db.execute("DELETE FROM sessions WHERE end_ts IS NOT NULL AND end_ts < ?", (cutoff,))

    # ---- queries ------------------------------------------------------------
    def samples_since(self, since_ts: float):
        return self.db.query("SELECT * FROM samples WHERE ts >= ? ORDER BY ts", (since_ts,))

    def sessions(self, limit: int = 100):
        return self.db.query("SELECT * FROM sessions WHERE kind != 'idle' ORDER BY start_ts DESC LIMIT ?", (limit,))

    def health_rows(self):
        return self.db.query("SELECT * FROM health ORDER BY day")

    def health_trend(self) -> HealthTrend:
        rows = self.health_rows()
        if not rows:
            return HealthTrend(None, None, None, None)
        return HealthTrend(rows[0]["day"], rows[-1]["day"], rows[0]["health_pct"], rows[-1]["health_pct"])

    def drain_stats(self, window_s: float = 3600) -> calc.DrainStats:
        """Discharge rate over the most recent continuous discharge period inside the window."""
        rows = self.db.query("SELECT ts, percent, rate_mw, ac_online, discharging FROM samples "
                             "WHERE ts >= ? ORDER BY ts", (time.time() - window_s,))
        seg: list = []
        for r in rows:
            on_batt = r["discharging"] == 1 or r["ac_online"] == 0
            if on_batt:
                seg.append(r)
            else:
                seg = []
        return calc.drain_rate([(r["ts"], r["percent"]) for r in seg], [r["rate_mw"] for r in seg])
