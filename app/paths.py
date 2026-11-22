"""Filesystem locations for the database, logs and caches.

All user data lives in ONE stable, writable, per-user folder that survives updates, rebuilds
and reinstalls of ARES (nothing is ever stored next to the program, in the PyInstaller
extraction folder or in a temp directory):

    Windows : %LOCALAPPDATA%\\ARES Battery Guardian\\{data,logs,cache}
    other   : $XDG_DATA_HOME/ARES Battery Guardian  (default ~/.local/share/...)
    override: ARES_DATA_DIR (used by the tests and for portable copies)

LOCALAPPDATA (not the roaming APPDATA) is used on purpose: the SQLite history grows over time
and must not be synced between PCs, and packaged ARES builds already stored their data here.

Before v1.2 a copy run from source kept its data in the project folder (data\\ next to
main.py). `migrate_legacy_data()` copies that database once into the new folder; the old file
is left untouched. It does nothing when ARES_DATA_DIR is set (tests and portable copies stay
isolated).
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys
from pathlib import Path

from app import APP_NAME

log = logging.getLogger(__name__)

DB_NAME = "battery_guardian.db"


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def user_data_root() -> Path:
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(local) / APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(xdg) / APP_NAME


def base_dir() -> Path:
    override = os.environ.get("ARES_DATA_DIR")
    if override:
        return Path(override)
    return user_data_root()


def _sub(name: str) -> Path:
    d = base_dir() / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def data_dir() -> Path:
    return _sub("data")


def logs_dir() -> Path:
    return _sub("logs")


def cache_dir() -> Path:
    """Regenerable files only (e.g. volume-adjusted copies of alarm sounds)."""
    return _sub("cache")


def database_path() -> Path:
    return data_dir() / DB_NAME


def legacy_database_path() -> Path:
    return project_root() / "data" / DB_NAME


def migrate_legacy_data(target: Path | None = None, legacy: Path | None = None) -> str | None:
    """Copy the pre-v1.2 project-folder database into the per-user folder, once.

    Only when the new database does not exist yet, so existing user data is never overwritten.
    Uses SQLite's online backup API (consistent even if the old file has a WAL). Returns a
    message if something was copied, else None. Never raises.
    """
    if target is None and os.environ.get("ARES_DATA_DIR"):
        return None                              # test run / portable copy: keep it isolated
    target = target or database_path()
    legacy = legacy or legacy_database_path()
    try:
        if target.exists() or not legacy.is_file() or legacy.resolve() == target.resolve():
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".migrating")
        src = sqlite3.connect(f"file:{legacy}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        os.replace(tmp, target)                  # atomic: either the full copy or nothing
        msg = f"Copied existing ARES data from {legacy} to {target} (the old file was left untouched)."
        log.info(msg)
        return msg
    except Exception as exc:
        log.exception("Legacy data migration failed")
        return f"Could not copy old ARES data from {legacy}: {exc}"
