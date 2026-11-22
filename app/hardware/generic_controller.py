"""Monitoring-only controller for machines without a supported charge-control interface.

It never pretends to control charging: every write returns a clear failure.
"""
from __future__ import annotations

from app.hardware.base_controller import (BaseChargeController, ChargeConfig, ControllerCapabilities,
                                          WriteResult)


class GenericController(BaseChargeController):
    def __init__(self, vendor: str | None = None, reason: str | None = None) -> None:
        self.vendor = vendor or "Unknown"
        self._reason = reason or ("No documented charge-threshold interface is available for this "
                                  "manufacturer/model in ARES Battery Guardian.")

    def probe(self) -> ControllerCapabilities:
        return ControllerCapabilities(
            vendor=self.vendor, interface=None, supported=False,
            notes=[self._reason, "Battery monitoring, history, health analysis and notifications still work.",
                   "Charging is entirely managed by the laptop firmware."],
        )

    def read_config(self) -> ChargeConfig | None:
        return None

    def write_thresholds(self, start: int, stop: int, password: str | None = None) -> WriteResult:
        return WriteResult(False, False, "Charge-threshold control is not supported on this hardware. "
                                         "No command was sent.")

    def restore_mode(self, config: ChargeConfig, password: str | None = None) -> WriteResult:
        return WriteResult(False, False, "Not supported on this hardware. No command was sent.")
