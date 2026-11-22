"""Detects the machine and picks the right charge controller."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.hardware.base_controller import BaseChargeController
from app.hardware.dell_controller import DellController
from app.hardware.generic_controller import GenericController
from app.utils import system_info as si

log = logging.getLogger(__name__)


@dataclass
class HardwareProfile:
    machine: si.MachineInfo
    vendor_key: str            # "dell" or "other"
    is_target_e7470: bool

    @property
    def display_name(self) -> str:
        parts = [p for p in (self.machine.manufacturer, self.machine.model) if p]
        return " ".join(parts) if parts else "Unknown computer"


def detect() -> HardwareProfile:
    m = si.get_machine_info()
    manu = (m.manufacturer or "").lower()
    vendor = "dell" if ("dell" in manu or "alienware" in manu) else "other"
    is_e7470 = vendor == "dell" and "e7470" in (m.model or "").lower()
    log.info("Detected %s %s (BIOS %s)", m.manufacturer, m.model, m.bios_version)
    return HardwareProfile(m, vendor, is_e7470)


def create_controller(profile: HardwareProfile, cctk_path: str | None = None) -> BaseChargeController:
    if profile.vendor_key == "dell":
        return DellController(profile.machine.model, cctk_path=cctk_path)
    return GenericController(profile.machine.manufacturer,
                             reason=f"{profile.display_name}: no supported charge-threshold interface "
                                    "is implemented for this manufacturer.")
