"""SQLite storage: settings, controller state, samples, sessions, events, health.

There is ONE settings store: the `settings` table (key -> JSON value). Only values the user
saved are stored; a missing key means "use the default", so defaults are never written over
saved values and new settings added by an update simply appear with their default.

Safety
* Schema changes are versioned with `PRAGMA user_version`. Before upgrading an existing file,
  a backup copy is written to data/backups/ (online backup API), and the upgrade runs in one
  transaction.
* A damaged database is never deleted: it is renamed to `*.corrupt-<time>.db`, the saved
  settings are salvaged from it if possible, and a fresh database is started.
* `set_settings()` writes a batch in one transaction and reads it back before reporting success.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2          # 1 = v1.0-v1.2 layout (implicit, user_version 0), 2 = v1.3
BACKUPS_KEPT = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS controller_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS samples (
    ts REAL NOT NULL, percent REAL, ac_online INTEGER, charging INTEGER, discharging INTEGER,
    rate_mw INTEGER, remaining_mwh INTEGER, full_mwh INTEGER, design_mwh INTEGER,
    voltage_mv INTEGER, temp_c REAL
);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, start_ts REAL NOT NULL, end_ts REAL,
    start_pct REAL, end_pct REAL, start_mwh INTEGER, end_mwh INTEGER
);
CREATE INDEX IF NOT EXISTS idx_sessions_start ON sessions(start_ts);
CREATE TABLE IF NOT EXISTS events (ts REAL NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS health (
    day TEXT PRIMARY KEY, full_mwh INTEGER, design_mwh INTEGER, health_pct REAL, cycle_count INTEGER
);
"""

DEFAULT_SETTINGS: dict[str, Any] = {
    "max_charge": 90,
    "min_charge": 25,
    # Write the thresholds to the firmware automatically (startup and at the 90%/25% edges).
    # Writes still happen only when the firmware differs, and need admin + a supported interface.
    "auto_apply_thresholds": True,
    # Strict Charging Protection Lock (a full-screen REMINDER, not hardware control).
    "strict_lock_enabled": True,
    "strict_lock_threshold": 90,      # lock when >= this % with the charger connected
    "high_alarm_sound_path": "",      # custom .wav for the high-battery alarm; empty = Windows default
    "high_alarm_repeat_min": 3,       # repeat the alarm every N minutes while locked; 0 = once per session
    # Low Battery Protection (full-screen reminder to connect the charger).
    "low_lock_enabled": True,
    "low_lock_threshold": 25,         # lock when <= this % with the charger disconnected
    "low_alarm_sound_path": "",
    "low_alarm_repeat_min": 3,
    "sound_enabled": True,            # master switch for every ARES sound (alarms and alerts)
    # Smart Audio Override (V2 Beta): while a protection alarm sounds, un-mute the default playback
    # device and set it to 100%, then restore the user's volume/mute. Opt-in.
    "audio_override_enabled": False,
    # Legacy keys (v1.1): migrated by migrate_settings(); still read for compatibility.
    "sound_at_max": True,
    "alert_sound_path": "",
    "monitoring_enabled": True,
    "monitor_interval_s": 30,
    "notifications_enabled": True,
    "notify_low_pct": 20,
    "notify_health_drop_pct": 5,
    "temp_warn_c": 50,
    "autostart": True,                # start ARES when the user signs in to Windows
    "start_minimized": False,         # at sign-in: False = open the dashboard, True = tray only
    "power_profile": "",              # preferred profile (remembered; applied only on request)
    "apply_profile_on_startup": False,  # apply the preferred Windows power plan when ARES starts
    # Alarm details (v1.3)
    "high_alarm_enabled": True,
    "low_alarm_enabled": True,
    "high_alarm_volume": 100,         # % of the WAV's own loudness (Windows system volume still applies)
    "low_alarm_volume": 100,
    # Interface (v1.3)
    "theme": "dark",                  # "dark" or "light"
    # Smart Wake-Up Protection (v1.3). Opt-in: waking from Sleep costs battery.
    "wake_enabled": False,
    "wake_low_enabled": True,
    "wake_low_threshold": 25,         # wake check fires when <= this % on battery
    "wake_high_enabled": True,
    "wake_high_threshold": 89,        # wake check fires when >= this % on the charger
    "wake_interval_min": 5,           # base interval between wake checks (minimum 5)
    "cctk_path": "",
    "sample_retention_days": 90,
}


SETTINGS_VERSION = 3


def migrate_settings(db: "Database") -> None:
    """One-time upgrade of settings saved by older ARES versions (idempotent)."""
    if int(db.get_state("settings_version", 0) or 0) >= SETTINGS_VERSION:
        return
    saved = {r["key"] for r in db.query("SELECT key FROM settings")}
    if "alert_sound_path" in saved and "high_alarm_sound_path" not in saved:
        db.set_setting("high_alarm_sound_path", db.get_setting("alert_sound_path") or "")
    if "sound_at_max" in saved and "sound_enabled" not in saved:
        db.set_setting("sound_enabled", bool(db.get_setting("sound_at_max")))
    db.set_state("settings_version", SETTINGS_VERSION)


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self.recovery_message: str | None = None      # set if a damaged file was replaced
        self.backup_path: str | None = None           # set if a pre-upgrade backup was written
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        try:
            self._open()
        except sqlite3.DatabaseError as exc:
            if self.path == ":memory:":
                raise
            self._recover_from_corruption(str(exc))

    def _open(self) -> None:
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            if self.path != ":memory:":
                ok = conn.execute("PRAGMA quick_check").fetchone()[0]
                if ok != "ok":
                    raise sqlite3.DatabaseError(f"integrity check failed: {ok}")
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            old = conn.execute("PRAGMA user_version").fetchone()[0]
            has_data = bool(conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0])
        except sqlite3.DatabaseError:
            conn.close()
            raise
        self._conn = conn
        with self._lock:
            if has_data and old < SCHEMA_VERSION and self.path != ":memory:":
                self.backup_path = self.backup(f"pre-v{SCHEMA_VERSION}")
            if old > SCHEMA_VERSION:
                log.warning("Database %s is from a newer ARES (schema %s > %s); opening without changes",
                            self.path, old, SCHEMA_VERSION)
            conn.execute("BEGIN IMMEDIATE")
            try:
                for stmt in SCHEMA.split(";"):
                    if stmt.strip():
                        conn.execute(stmt)
                if old < SCHEMA_VERSION:
                    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    # ---- backups / recovery ---------------------------------------------------
    def backup(self, tag: str) -> str | None:
        """Consistent copy of the whole database to data/backups/ (keeps the newest few)."""
        if self.path == ":memory:" or self._conn is None:
            return None
        folder = Path(self.path).parent / "backups"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            dest = folder / f"{Path(self.path).stem}.{tag}.{time.strftime('%Y%m%d-%H%M%S')}.db"
            out = sqlite3.connect(dest)
            try:
                self._conn.backup(out)
            finally:
                out.close()
            old = sorted(folder.glob(f"{Path(self.path).stem}.*.db"), key=lambda p: p.stat().st_mtime)
            for extra in old[:-BACKUPS_KEPT]:
                extra.unlink(missing_ok=True)
            return str(dest)
        except (OSError, sqlite3.Error) as exc:
            log.warning("Database backup failed: %s", exc)
            return None

    def _recover_from_corruption(self, reason: str) -> None:
        """Keep the damaged file as a backup, salvage settings, start a fresh database."""
        src = Path(self.path)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        kept = src.with_name(f"{src.stem}.corrupt-{stamp}.db")
        for suffix in ("", "-wal", "-shm"):
            f = Path(str(src) + suffix)
            if f.exists():
                os.replace(f, Path(str(kept) + suffix))
        salvaged: list[tuple[str, str]] = []
        try:
            old = sqlite3.connect(f"file:{kept}?mode=ro", uri=True)
            try:
                salvaged = [(k, v) for k, v in old.execute("SELECT key, value FROM settings")]
            finally:
                old.close()
        except sqlite3.Error:
            pass
        self._open()
        for k, v in salvaged:
            try:
                json.loads(v)
            except (TypeError, ValueError):
                continue
            self.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, v))
        self.recovery_message = (f"The ARES database was damaged ({reason}). It was kept as {kept.name}, "
                                 f"{len(salvaged)} saved settings were recovered and a new database was started.")
        log.error(self.recovery_message)
        self.log_event("warning", self.recovery_message)

    def close(self) -> None:
        """Close the connection (idempotent). Checkpoints WAL so no -wal/-shm files stay open."""
        with self._lock:
            if self._conn is None:
                return
            try:
                if self.path != ":memory:":
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            # A clear error instead of "'NoneType' object has no attribute 'execute'".
            raise sqlite3.ProgrammingError(f"Database {self.path} is closed")
        return self._conn

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._connection().execute(sql, params)
            self._conn.commit()
            return cur

    def query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._connection().execute(sql, params).fetchall()

    # ---- settings ----------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        rows = self.query("SELECT value FROM settings WHERE key=?", (key,))
        if not rows:
            return DEFAULT_SETTINGS.get(key, default) if default is None else default
        return json.loads(rows[0]["value"])

    def set_setting(self, key: str, value: Any) -> None:
        self.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(value)))

    def set_settings(self, values: dict[str, Any]) -> None:
        """Save several settings atomically (all or nothing), then verify by reading them back.
        Raises sqlite3.Error / RuntimeError on failure; the caller reports "Save failed"."""
        if not values:
            return
        with self._lock:
            conn = self._connection()
            conn.execute("BEGIN IMMEDIATE")
            try:
                for key, value in values.items():
                    conn.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            stored = {r["key"]: json.loads(r["value"]) for r in conn.execute(
                f"SELECT key, value FROM settings WHERE key IN ({','.join('?' * len(values))})", list(values))}
        bad = [k for k, v in values.items() if stored.get(k) != json.loads(json.dumps(v))]
        if bad:
            raise RuntimeError("Settings did not read back correctly: " + ", ".join(bad))

    def saved_settings(self) -> dict[str, Any]:
        """Only the values the user saved (no defaults)."""
        return {r["key"]: json.loads(r["value"]) for r in self.query("SELECT key, value FROM settings")}

    def reset_settings(self) -> int:
        """Restore defaults: delete saved preferences ONLY (history, sessions, samples, events and
        controller state are untouched). Returns how many saved values were removed."""
        with self._lock:
            conn = self._connection()
            conn.execute("BEGIN IMMEDIATE")
            try:
                n = conn.execute("DELETE FROM settings").rowcount
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return n

    def all_settings(self) -> dict[str, Any]:
        out = dict(DEFAULT_SETTINGS)
        for r in self.query("SELECT key, value FROM settings"):
            out[r["key"]] = json.loads(r["value"])
        return out

    # ---- controller state (survives restarts / crashes) --------------------
    def get_state(self, key: str, default: Any = None) -> Any:
        rows = self.query("SELECT value FROM controller_state WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set_state(self, key: str, value: Any) -> None:
        self.execute("INSERT INTO controller_state(key,value,updated) VALUES(?,?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
                     (key, json.dumps(value), time.time()))

    # ---- events -------------------------------------------------------------
    def log_event(self, kind: str, message: str) -> None:
        self.execute("INSERT INTO events(ts,kind,message) VALUES(?,?,?)", (time.time(), kind, message))

    def recent_events(self, limit: int = 200) -> list[sqlite3.Row]:
        return self.query("SELECT ts, kind, message FROM events ORDER BY ts DESC LIMIT ?", (limit,))
