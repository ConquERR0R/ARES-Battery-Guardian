"""Battery History hover tooltip: tooltip text, nearest-point selection and mouse interaction."""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QFont, QImage, QMouseEvent, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.database.battery_history import BatteryHistory  # noqa: E402
from app.database.database import Database  # noqa: E402
from app.ui import theme  # noqa: E402
from app.ui.dashboard import (HealthPage, HistoryPage, LineChart, charge_state_text,  # noqa: E402
                              format_value, nearest_index, point_tooltip_lines)

APP = QApplication.instance() or QApplication([])

T0 = 1_763_700_000.0           # a fixed local time; text is compared via datetime.fromtimestamp


def row(**kw):
    base = dict(ts=T0, percent=50.0, ac_online=None, charging=None, discharging=None, rate_mw=None,
                remaining_mwh=None, full_mwh=None, design_mwh=None, voltage_mv=None, temp_c=None)
    base.update(kw)
    return base


def move(widget, x, y):
    pos = QPointF(x, y)
    ev = QMouseEvent(QEvent.MouseMove, pos, widget.mapToGlobal(pos.toPoint()).toPointF(),
                     Qt.NoButton, Qt.NoButton, Qt.NoModifier)
    QApplication.sendEvent(widget, ev)


def render(widget) -> QImage:
    img = QImage(widget.size(), QImage.Format_ARGB32)
    img.fill(0)
    p = QPainter(img)
    widget.render(p, QPoint())
    p.end()
    return img


class TooltipTextTest(unittest.TestCase):
    def test_percentage_and_timestamp(self):
        lines = point_tooltip_lines(T0, 87.0)
        self.assertEqual(lines[0], "Battery: 87%")
        self.assertEqual(lines[1], datetime.fromtimestamp(T0).strftime("%Y-%m-%d %H:%M:%S"))
        self.assertEqual(len(lines), 2)                     # no row -> no state/voltage/capacity

    def test_exact_value_keeps_decimals(self):
        self.assertEqual(format_value(87.5), "87.5%")
        self.assertEqual(format_value(87.25), "87.25%")
        self.assertEqual(format_value(100.0), "100%")

    def test_charging_state(self):
        self.assertEqual(charge_state_text(row(charging=1, ac_online=1)), "Charging")
        self.assertEqual(charge_state_text(row(discharging=1, ac_online=0)), "Discharging")
        self.assertEqual(charge_state_text(row(ac_online=0)), "Discharging")
        self.assertEqual(charge_state_text(row(ac_online=1, charging=0)), "On AC, not charging")
        self.assertEqual(charge_state_text(row()), "Unknown")
        self.assertEqual(charge_state_text(None), "Unknown")

    def test_voltage_and_capacity_only_when_available(self):
        lines = point_tooltip_lines(T0, 60, row(charging=1, voltage_mv=12345, remaining_mwh=31200, full_mwh=52000))
        self.assertIn("State: Charging", lines)
        self.assertIn("Voltage: 12.35 V", lines)
        self.assertIn("Capacity: 31.2 of 52.0 Wh", lines)
        bare = point_tooltip_lines(T0, 60, row(discharging=1))
        self.assertEqual(bare[2:], ["State: Discharging"])
        self.assertFalse(any("Voltage" in s or "Capacity" in s or "capacity" in s for s in bare))

    def test_partial_and_invalid_readings(self):
        self.assertIn("Remaining capacity: 31.2 Wh", point_tooltip_lines(T0, 60, row(remaining_mwh=31200)))
        self.assertIn("Full-charge capacity: 52.0 Wh", point_tooltip_lines(T0, 60, row(full_mwh=52000)))
        junk = point_tooltip_lines(T0, 60, row(voltage_mv=0, full_mwh=0, remaining_mwh=-1))
        self.assertFalse(any("Voltage" in s or "apacity" in s for s in junk))
        odd = point_tooltip_lines(T0, 60, {"ts": T0, "percent": 60, "voltage_mv": "n/a"})  # missing columns
        self.assertEqual(odd[2:], ["State: Unknown"])


class NearestIndexTest(unittest.TestCase):
    def test_nearest(self):
        ts = [0.0, 10.0, 20.0, 30.0]
        self.assertEqual(nearest_index(ts, -5), 0)
        self.assertEqual(nearest_index(ts, 4), 0)
        self.assertEqual(nearest_index(ts, 6), 1)
        self.assertEqual(nearest_index(ts, 20), 2)
        self.assertEqual(nearest_index(ts, 99), 3)
        self.assertIsNone(nearest_index([], 1))


class ChartHoverTest(unittest.TestCase):
    def make(self, n=11, step=60.0, details=True):
        c = LineChart()
        c.resize(600, 300)
        pts = [(T0 + i * step, 40 + i) for i in range(n)]
        rows = [row(ts=t, percent=v, charging=1 if i % 2 else 0, ac_online=1) for i, (t, v) in enumerate(pts)]
        c.set_data(pts, (), (), details=rows if details else None)
        return c, pts

    def test_hover_selects_nearest_point(self):
        c, pts = self.make()
        x = c.x_of(pts[3][0]) + 0.3 * (c.x_of(pts[4][0]) - c.x_of(pts[3][0]))
        move(c, x, 100)
        self.assertEqual(c.hover_index, 3)
        self.assertEqual(c.hover_lines()[0], "Battery: 43%")
        self.assertIn("State: Charging", c.hover_lines())
        move(c, c.x_of(pts[4][0]) - 1, 250)                # y anywhere inside the plot
        self.assertEqual(c.hover_index, 4)
        self.assertIn("State: On AC, not charging", c.hover_lines())

    def test_no_repaint_while_on_same_point(self):
        c, pts = self.make()
        calls = []
        c.update = lambda *a: calls.append(1)              # count repaint requests
        x = c.x_of(pts[5][0])
        for dx in (-3, -1, 0, 1, 3):
            move(c, x + dx, 120)
        self.assertEqual(c.hover_index, 5)
        self.assertEqual(len(calls), 1)                    # one repaint when the point changed, none after

    def test_leave_and_outside_plot_hide_tooltip(self):
        c, pts = self.make()
        move(c, c.x_of(pts[2][0]), 100)
        self.assertIsNotNone(c.hover_index)
        move(c, c.x_of(pts[2][0]), c.height() - 3)         # over the time axis labels
        self.assertIsNone(c.hover_index)
        move(c, c.x_of(pts[2][0]), 100)
        QApplication.sendEvent(c, QEvent(QEvent.Leave))
        self.assertIsNone(c.hover_index)

    def test_too_few_points(self):
        c = LineChart()
        c.resize(600, 300)
        c.set_data([(T0, 50)])
        move(c, 300, 100)
        self.assertIsNone(c.hover_index)
        render(c)                                          # still paints the empty-text state

    def test_mismatched_details_are_ignored(self):
        c = LineChart()
        c.resize(600, 300)
        c.set_data([(T0, 50), (T0 + 60, 51)], details=[row()])
        move(c, c.x_of(T0 + 60), 100)
        self.assertEqual(len(c.hover_lines()), 2)          # value and time only, no crash

    def test_refresh_keeps_selection_under_mouse(self):
        c, pts = self.make()
        move(c, c.x_of(pts[-1][0]), 100)
        self.assertEqual(c.hover_index, len(pts) - 1)
        more = pts + [(pts[-1][0] + 60, 99)]
        c.set_data(more)                                   # live refresh, span grows
        self.assertIsNotNone(c.hover_index)
        self.assertLess(c.hover_index, len(more))

    def test_tooltip_stays_inside_widget(self):
        c, pts = self.make()
        fm = c.fontMetrics()
        lines = ["Battery: 100%", "2026-11-21 12:00:00", "Capacity: 52.0 of 52.0 Wh"]
        for anchor in (QPointF(c.width() - 5, 5), QPointF(5, c.height() - 5), QPointF(300, 150)):
            box = c.tooltip_rect(lines, anchor, fm)
            self.assertGreaterEqual(box.left(), 0)
            self.assertGreaterEqual(box.top(), 0)
            self.assertLessEqual(box.right(), c.width())
            self.assertLessEqual(box.bottom(), c.height())

    def test_marker_and_tooltip_painted_in_both_themes(self):
        for name in ("dark", "light"):
            c, pts = self.make()
            c.setPalette(theme.palette(name))
            base = render(c)
            move(c, c.x_of(pts[5][0]), 100)
            hovered = render(c)
            self.assertNotEqual(base, hovered, name)
            pal = theme.palette(name)
            bg, fg = pal.color(pal.ColorRole.ToolTipBase), pal.color(pal.ColorRole.ToolTipText)
            self.assertGreater(abs(bg.lightness() - fg.lightness()), 120, name)   # readable contrast
            box = c.tooltip_rect(c.hover_lines(), QPointF(c.x_of(pts[5][0]), c.y_of(pts[5][1])), c.fontMetrics())
            inside = hovered.pixelColor(int(box.left()) + 3, int(box.bottom()) - 3)
            self.assertEqual(inside.rgb(), bg.rgb(), name)                       # tooltip box uses theme colours


class HistoryPageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.addCleanup(self.db.close)
        now = time.time()
        rows = []
        # 30 days of samples every 2 h, plus incomplete ones (no percent, no voltage, NULL flags).
        for i in range(30 * 12):
            ts = now - 30 * 86400 + 600 + i * 7200
            rows.append((ts, 30 + (i % 60), 1, 1 if i % 3 == 0 else 0, 0 if i % 3 == 0 else 1,
                         None, 30000 if i % 2 else None, 52000 if i % 2 else None, 57000,
                         11800 if i % 4 == 0 else None, None))
        rows.append((now - 30, None, None, None, None, None, None, None, None, None, None))   # no percent
        rows.append((now - 20, 55.5, None, None, None, None, None, None, None, None, None))   # no flags
        for r in rows:
            self.db.execute("INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?)", r)
        self.hist = BatteryHistory(self.db)
        self.page = HistoryPage(lambda: self.hist)
        self.page.resize(800, 600)

    def test_every_range_has_tooltips(self):
        for i, name in enumerate(HistoryPage.RANGES):
            self.page.range.setCurrentIndex(i)
            self.page.refresh()
            c = self.page.chart
            self.assertGreaterEqual(len(c.points), 2, name)
            self.assertEqual(len(c.details), len(c.points), name)
            c.resize(700, 300)
            move(c, c.x_of(c.points[-1][0]), 80)
            lines = c.hover_lines()
            self.assertEqual(lines[0], "Battery: 55.5%", name)
            self.assertIn("State: Unknown", lines, name)
            move(c, c.x_of(c.points[0][0]), 80)
            self.assertTrue(c.hover_lines()[0].startswith("Battery: "), name)
            render(c)

    def test_history_records_unchanged(self):
        before = self.db.query("SELECT COUNT(*) AS n FROM samples")[0]["n"]
        for i in range(len(HistoryPage.RANGES)):
            self.page.range.setCurrentIndex(i)
            self.page.refresh()
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM samples")[0]["n"], before)

    def test_health_chart_tooltip(self):
        for r in [("2026-11-01", 52000, 57000, 91.2, 300), ("2026-11-02", None, 57000, None, 300),
                  ("2026-11-03", 51900, 57000, 91.05, 301)]:
            self.db.execute("INSERT INTO health VALUES (?,?,?,?,?)", r)
        hp = HealthPage(lambda: self.hist)
        hp.resize(700, 500)
        hp.refresh()
        c = hp.chart
        self.assertEqual(len(c.points), 2)                 # the NULL day is skipped, not a crash
        move(c, c.x_of(c.points[0][0]), 80)
        self.assertEqual(c.hover_lines(), ["Health: 91.2%", "2026-11-01"])


if __name__ == "__main__":
    unittest.main()
