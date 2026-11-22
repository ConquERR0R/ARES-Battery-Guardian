"""The compatibility notice is off the Dashboard and shown in Settings > I. Hardware Diagnostics."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core import charging_controller as cc  # noqa: E402
from app.core.hardware_detector import HardwareProfile  # noqa: E402
from app.database.database import Database  # noqa: E402
from app.hardware.base_controller import ChargeConfig, ControllerCapabilities  # noqa: E402
from app.ui.dashboard import CONTROL_UNAVAILABLE, DashboardWidget  # noqa: E402
from app.ui.settings import SettingsPage  # noqa: E402
from app.utils.calculations import ThresholdLimits, plan_thresholds  # noqa: E402
from app.utils.system_info import MachineInfo  # noqa: E402

APP = QApplication.instance() or QApplication([])

WMI_NOTE = ("Dell BIOS WMI namespace (root\\dcim\\sysman\\biosattributes) not present. "
            "This is normal on older models.")
CCTK_NOTE = "Dell Command | Configure (cctk.exe) is not installed. Install it from Dell."
DETAIL = ("Monitoring only: no supported charge-control interface, so ARES cannot stop or resume "
          "charging. States shown come from the battery driver.")
LIMITS = ThresholdLimits(50, 95, 55, 100, 5)


class FakePlayer:
    def play(self, *a, **k):
        return True

    def stop(self):
        pass

    def is_playing(self):
        return False


def monitoring_status():
    caps = ControllerCapabilities("Dell", None, False, notes=[WMI_NOTE, CCTK_NOTE])
    plan = plan_thresholds(25, 90, LIMITS)
    return cc.ControllerStatus(cc.MONITORING_ONLY, "Charging", DETAIL, None, plan, caps, None,
                               list(plan.messages) + caps.notes)


def supported_status():
    caps = ControllerCapabilities("Dell", "Dell Command | Configure (cctk.exe)", True, can_read=True,
                                  can_write=True, requires_admin=True, limits=LIMITS, notes=[CCTK_NOTE])
    plan = plan_thresholds(50, 90, LIMITS)
    return cc.ControllerStatus(cc.PAUSE_PENDING, "Charging", "Waiting for the firmware to stop charging.",
                               ChargeConfig("Custom", 50, 90), plan, caps, "BIOS password required",
                               ["BIOS password required"])


def visible_text(w) -> str:
    from PySide6.QtWidgets import QLabel
    return "\n".join(lbl.text() for lbl in w.findChildren(QLabel) if not lbl.isHidden())


class DashboardNoticeTest(unittest.TestCase):
    def setUp(self):
        self.d = DashboardWidget()
        self.d.resize(900, 700)
        self.d.show()

    def tearDown(self):
        self.d.close()

    def test_monitoring_only_notice_removed(self):
        self.d.update_status(monitoring_status(), (25, 90))
        text = visible_text(self.d)
        for gone in ("Monitoring only: no supported", "no supported charge-control interface", "BIOS WMI",
                     "Command | Configure", "cctk"):
            self.assertNotIn(gone, text)
        self.assertTrue(self.d.detail.isHidden())
        self.assertTrue(self.d.notes.isHidden())
        self.assertEqual(self.d.headline.text(), "Charging")          # real driver state still shown

    def test_never_claims_support(self):
        self.d.update_status(monitoring_status(), (25, 90))
        text = visible_text(self.d)
        self.assertIn(CONTROL_UNAVAILABLE, text)
        self.assertIn("Settings > Hardware Diagnostics", text)
        self.assertIn("N/A (no hardware control)", text)
        self.assertNotIn("Supported via", text)
        self.assertNotIn("Start 50% / stop 90%", text)                 # no firmware target implied

    def test_supported_mode_unchanged(self):
        self.d.update_status(supported_status(), (50, 90))
        text = visible_text(self.d)
        self.assertIn("Waiting for the firmware to stop charging.", text)
        self.assertIn("• BIOS password required", text)
        self.assertIn("Supported via Dell Command | Configure (cctk.exe)", text)
        self.assertIn("Start 50% / stop 90%", text)

    def test_switch_back_and_forth(self):
        self.d.update_status(supported_status(), (50, 90))
        self.d.update_status(monitoring_status(), (25, 90))
        self.assertTrue(self.d.notes.isHidden())
        self.d.update_status(supported_status(), (50, 90))
        self.assertFalse(self.d.notes.isHidden())
        self.assertFalse(self.d.detail.isHidden())


class HardwareDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.addCleanup(self.db.close)
        self.page = SettingsPage(self.db, player=FakePlayer(), wake_tester=lambda: (True, ["fake"]))
        self.addCleanup(self.page.deleteLater)

    def test_section_shows_details(self):
        st = monitoring_status()
        self.page.set_profile(HardwareProfile(MachineInfo(manufacturer="Dell Inc.", model="Latitude E7470",
                                                          bios_version="1.36.3"), "dell", True))
        self.page.set_capabilities(st.capabilities)
        self.page.update_diagnostics(st)
        d = self.page.diag
        self.assertEqual(self.page.diag_group.title(), "I. Hardware Diagnostics")
        self.assertEqual(d["machine"].text(), "Dell Inc. Latitude E7470")
        self.assertEqual(d["bios"].text(), "1.36.3")
        self.assertTrue(d["control"].text().startswith("Not available"))
        self.assertEqual(d["iface"].text(), "None found")
        self.assertEqual(d["status"].text(), DETAIL)
        notes = self.page.diag_notes.text()
        self.assertIn(WMI_NOTE, notes)
        self.assertIn(CCTK_NOTE, notes)
        self.assertEqual(notes.count(CCTK_NOTE), 1)                    # caps notes and messages deduplicated

    def test_supported_details(self):
        st = supported_status()
        self.page.set_capabilities(st.capabilities)
        self.page.update_diagnostics(st)
        d = self.page.diag
        self.assertEqual(d["control"].text(), "Supported")
        self.assertIn("needs administrator rights", d["access"].text())
        self.assertEqual(d["limits"].text(), "start 50-95%, stop 55-100%, gap at least 5%")
        self.assertEqual(d["firmware"].text(), "Custom: start 50% / stop 90%")
        self.assertIn("BIOS password required", self.page.diag_notes.text())

    def test_redetect_button_and_no_new_settings(self):
        before = self.page.keys()
        got = []
        self.page.redetect_requested.connect(lambda: got.append(1))
        self.page.redetect_btn.click()
        self.assertEqual(got, [1])
        self.assertEqual(self.page.keys(), before)
        self.assertFalse(self.page.is_dirty())                         # viewing diagnostics changes nothing


if __name__ == "__main__":
    unittest.main()
