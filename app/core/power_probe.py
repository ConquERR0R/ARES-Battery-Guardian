"""Battery percent + charger probe with no Qt dependency.

Used by the lightweight `--wake-check` process (started by Task Scheduler after a wake timer)
and by the protection screens in the GUI process. It only calls the documented Win32
GetSystemPowerStatus API (via app.utils.system_info, plain ctypes), so importing this module
never loads PySide6, Qt GUI libraries, WMI or PowerShell.
"""
from __future__ import annotations

from app.utils import system_info as si

PowerReading = tuple  # (percent: float | None, ac_online: bool | None)


def default_power_probe() -> PowerReading | None:
    """Fast, read-only power check. None = could not be read at all. Never raises OSError."""
    try:
        ps = si.get_power_status()
    except OSError:
        return None
    return None if ps is None else (ps.percent, ps.ac_online)
