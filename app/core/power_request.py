"""Keep Windows awake while ARES needs it (documented SetThreadExecutionState API).

Used only while a protection screen is showing (so it is seen and the alarm is heard) and,
after an 89% Smart Wake-Up on the charger, until the battery reaches the 90% protection
threshold. ARES never forces Sleep: releasing the request simply lets Windows apply its own
idle / unattended-wake sleep rules again.

Must be called from one long-lived thread (the GUI thread): the request belongs to the thread.
"""
from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


class PowerRequest:
    def __init__(self) -> None:
        self.system = False
        self.display = False
        self.calls: list[int] = []          # for tests / diagnostics

    def set(self, system: bool, display: bool) -> None:
        system = system or display
        if (system, display) == (self.system, self.display):
            return
        self.system, self.display = system, display
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if system else 0) | (ES_DISPLAY_REQUIRED if display else 0)
        self.calls.append(flags)
        self._apply(flags)

    def release(self) -> None:
        self.set(False, False)

    def _apply(self, flags: int) -> None:
        if sys.platform != "win32":
            return
        try:
            import ctypes

            if not ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint32(flags)):
                log.warning("SetThreadExecutionState(0x%08x) failed", flags)
        except Exception:
            log.exception("SetThreadExecutionState failed")
