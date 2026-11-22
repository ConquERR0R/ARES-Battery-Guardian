"""Windows power plans via powercfg. Never changes anything without an explicit call
from the UI, which asks the user for confirmation first."""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.utils import system_info as si

# Built-in Windows power scheme GUIDs
SCHEME_POWER_SAVER = "a1841308-3d3b-4a61-9e4f-b8d0de4e2d6f"
SCHEME_BALANCED = "381b4222-f694-41f0-9685-ff5bb260df2e"
SCHEME_HIGH_PERF = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"

PROFILES = {
    "Battery Saver": SCHEME_POWER_SAVER,
    "Balanced": SCHEME_BALANCED,
    "Performance": SCHEME_HIGH_PERF,
    "Custom Charging": None,   # not a Windows plan: opens ARES charge-threshold settings
}

_LINE = re.compile(r"([0-9a-fA-F-]{36})\s+\((.*?)\)\s*(\*)?")


@dataclass
class PowerScheme:
    guid: str
    name: str
    active: bool


def list_schemes() -> list[PowerScheme]:
    if not si.IS_WINDOWS:
        return []
    res = si.run_hidden(["powercfg", "/list"], timeout=15)
    out = []
    for m in _LINE.finditer(res.stdout):
        out.append(PowerScheme(m.group(1).lower(), m.group(2), bool(m.group(3))))
    return out


def active_scheme() -> PowerScheme | None:
    for s in list_schemes():
        if s.active:
            return s
    return None


def profile_for_guid(guid: str | None) -> str | None:
    for name, g in PROFILES.items():
        if g and guid and g == guid.lower():
            return name
    return None


def apply_profile(profile: str) -> tuple[bool, str]:
    """Activate a Windows power plan. If the built-in plan is hidden (common on Windows 11),
    it is restored with `powercfg -duplicatescheme` and that copy is activated."""
    guid = PROFILES.get(profile)
    if guid is None:
        return False, f"'{profile}' is not a Windows power plan."
    if not si.IS_WINDOWS:
        return False, "Not running on Windows."
    schemes = {s.guid: s for s in list_schemes()}
    target = guid
    if guid not in schemes:
        res = si.run_hidden(["powercfg", "-duplicatescheme", guid], timeout=20)
        m = re.search(r"([0-9a-fA-F-]{36})", res.stdout)
        if not res.ok or not m:
            return False, f"Windows does not provide the '{profile}' plan on this PC: {res.stderr.strip()}"
        target = m.group(1)
    res = si.run_hidden(["powercfg", "/setactive", target], timeout=20)
    if not res.ok:
        return False, (res.stderr or res.stdout).strip() or "powercfg failed"
    now = active_scheme()
    if now and now.guid == target.lower():
        return True, f"Windows power plan set to {now.name}."
    return False, "powercfg ran but the active plan did not change."
