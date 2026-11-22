"""Test helper: keep the REAL battery out of tests that run the whole background app.

AresBackgroundApp's monitoring thread reads the battery through BatteryMonitor. On Linux that
reads nothing, but on a Windows laptop it returns the real battery: at 90 % or more on the charger
a real reading re-locks the protection screen in the middle of a test that has just "unplugged"
its fake charger (seen on the Latitude, 2026-11-21). Tests that drive the protection logic with
their own fake readings call use_fake_battery(self) so the result no longer depends on the
laptop's charge level or charger. It also replaces hardware detection with a non-Dell test machine,
so the monitoring thread never runs Dell Command | Configure (cctk) or WMI queries during a test.
"""
from __future__ import annotations

import time
import unittest
from unittest import mock

from app.core.battery_monitor import BatterySnapshot
from app.core.hardware_detector import HardwareProfile
from app.utils.system_info import MachineInfo


class NoBatteryMonitor:
    """Stands in for BatteryMonitor: every reading is 'no battery detected'."""

    reads = 0

    def read(self) -> BatterySnapshot:
        NoBatteryMonitor.reads += 1
        return BatterySnapshot(timestamp=time.time(), available=False, source="none")


def test_machine() -> HardwareProfile:
    return HardwareProfile(MachineInfo(manufacturer="Test", model="Fake laptop"), "other", False)


def use_fake_battery(tc: unittest.TestCase) -> None:
    for p in (mock.patch("app.core.monitor_service.BatteryMonitor", NoBatteryMonitor),
              mock.patch("app.core.monitor_service.detect", test_machine)):
        p.start()
        tc.addCleanup(p.stop)
