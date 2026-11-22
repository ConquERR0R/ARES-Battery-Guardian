"""CSV exports and the plain-text hardware compatibility report."""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from app import APP_DISPLAY_VERSION, APP_NAME
from app.database.database import Database


def _iso(ts) -> str:
    return "" if ts is None else datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def export_samples_csv(db: Database, path: Path | str, since_ts: float = 0) -> int:
    rows = db.query("SELECT * FROM samples WHERE ts >= ? ORDER BY ts", (since_ts,))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["time", "percent", "ac_online", "charging", "discharging", "rate_mw", "remaining_mwh",
                    "full_charge_mwh", "design_mwh", "voltage_mv", "temperature_c"])
        for r in rows:
            w.writerow([_iso(r["ts"]), r["percent"], r["ac_online"], r["charging"], r["discharging"], r["rate_mw"],
                        r["remaining_mwh"], r["full_mwh"], r["design_mwh"], r["voltage_mv"], r["temp_c"]])
    return len(rows)


def export_sessions_csv(db: Database, path: Path | str) -> int:
    rows = db.query("SELECT * FROM sessions WHERE kind != 'idle' ORDER BY start_ts")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["kind", "start", "end", "duration_min", "start_pct", "end_pct", "energy_mwh"])
        for r in rows:
            dur = round((r["end_ts"] - r["start_ts"]) / 60, 1) if r["end_ts"] else ""
            energy = (r["end_mwh"] - r["start_mwh"]) if (r["end_mwh"] is not None and r["start_mwh"] is not None) else ""
            w.writerow([r["kind"], _iso(r["start_ts"]), _iso(r["end_ts"]), dur, r["start_pct"], r["end_pct"], energy])
    return len(rows)


def export_events_csv(db: Database, path: Path | str) -> int:
    rows = db.query("SELECT * FROM events ORDER BY ts")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["time", "kind", "message"])
        for r in rows:
            w.writerow([_iso(r["ts"]), r["kind"], r["message"]])
    return len(rows)


def compatibility_report(diag: dict) -> str:
    """Human-readable report from main.run_diagnostics() output."""
    m = diag.get("machine", {})
    c = diag.get("capabilities", {})
    b = diag.get("battery", {})
    fw = diag.get("firmware_config")
    lines = [
        f"{APP_NAME} {APP_DISPLAY_VERSION} - Hardware compatibility report",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "MACHINE",
        f"  Manufacturer : {m.get('manufacturer') or 'N/A'}",
        f"  Model        : {m.get('model') or 'N/A'}",
        f"  BIOS         : {m.get('bios_version') or 'N/A'} ({m.get('bios_date') or 'N/A'})",
        f"  OS           : {m.get('os_name') or 'N/A'} build {m.get('os_build') or 'N/A'}",
        f"  Running as administrator: {diag.get('is_admin')}",
        "",
        "CHARGE CONTROL",
        f"  Result       : {c.get('summary')}",
        f"  Interface    : {c.get('interface') or 'none'}",
        f"  Can read     : {c.get('can_read')}",
        f"  Can write    : {c.get('can_write')}",
        f"  Limits       : {json.dumps(c.get('limits')) if c.get('limits') else 'N/A'}",
        f"  Firmware now : {fw.get('describe') if fw else 'N/A (could not read)'}",
    ]
    for n in c.get("notes", []):
        lines.append(f"  - {n}")
    plan = diag.get("plan_for_25_90")
    if plan:
        lines += ["", "REQUESTED 25% / 90%",
                  f"  Closest supported: start {plan['start']}% / stop {plan['stop']}% "
                  f"({'exact' if plan['exact'] else 'adjusted'})"]
        for msg in plan["messages"]:
            lines.append(f"  - {msg}")
    lines += ["", "BATTERY TELEMETRY"]
    keys = ["source", "percent", "ac_online", "charging", "discharging", "rate_mw", "voltage_mv", "remaining_mwh",
            "full_charge_mwh", "design_mwh", "health_pct", "wear_pct", "cycle_count", "manufacturer", "model",
            "chemistry", "manufacture_date", "temperature_c", "temperature_note"]
    for k in keys:
        v = b.get(k)
        lines.append(f"  {k:<17}: {'N/A' if v is None else v}")
    if b.get("errors"):
        lines.append(f"  errors           : {'; '.join(b['errors'])}")
    return "\n".join(lines) + "\n"
