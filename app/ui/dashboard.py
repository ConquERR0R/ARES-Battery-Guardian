"""Dashboard, history and health views."""
from __future__ import annotations

import bisect
import time
from datetime import datetime

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import (QComboBox, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
                               QSizePolicy, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from app.core import charging_controller as cc
from app.core.battery_monitor import BatterySnapshot
from app.utils import calculations as calc

ACCENT = QColor("#3ddc97")
WARN = QColor("#f5b942")
BAD = QColor("#ff5c5c")
MUTED = QColor("#8b93a7")
GRID = QColor("#2a3040")
# Shown on the dashboard instead of the long compatibility notice when the firmware cannot be controlled.
CONTROL_UNAVAILABLE = "Not available on this PC (monitoring only)"

STATE_COLORS = {
    cc.PAUSED: "#3ddc97", cc.RESUMED: "#4fa8ff", cc.CHARGING: "#4fa8ff", cc.ON_BATTERY: "#f5b942",
    cc.PAUSE_PENDING: "#f5b942", cc.RESUME_PENDING: "#f5b942", cc.IDLE_AC: "#c0c6d4",
    cc.MONITORING_ONLY: "#c0c6d4", cc.NO_BATTERY: "#ff5c5c",
}


class ChargeRing(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.percent: float | None = None
        self.charging = False
        self.stop: int | None = None
        self.start: int | None = None
        self.setMinimumSize(190, 190)

    def set_values(self, percent, charging, start=None, stop=None) -> None:
        self.percent, self.charging, self.start, self.stop = percent, charging, start, stop
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        side = min(self.width(), self.height()) - 16
        rect = QRectF((self.width() - side) / 2, (self.height() - side) / 2, side, side)
        p.setPen(QPen(self.palette().color(QPalette.Mid), 14, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(rect, 0, 360 * 16)
        if self.percent is not None:
            col = BAD if self.percent <= 15 else WARN if self.percent <= 30 else ACCENT
            p.setPen(QPen(col, 14, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(rect, 90 * 16, int(-self.percent / 100 * 360 * 16))
        fg = self.palette().color(QPalette.WindowText)          # follows the dark / light theme
        for mark, col in ((self.stop, fg), (self.start, QColor("#4fa8ff"))):
            if mark is not None:
                p.save()
                p.translate(rect.center())
                p.rotate(mark / 100 * 360)
                p.setPen(QPen(col, 3))
                p.drawLine(QPointF(0, -side / 2 - 9), QPointF(0, -side / 2 + 9))
                p.restore()
        p.setPen(fg)
        f = QFont(self.font())
        f.setPointSize(30)
        f.setBold(True)
        p.setFont(f)
        txt = "N/A" if self.percent is None else f"{self.percent:.0f}%"
        p.drawText(rect, Qt.AlignCenter, txt)
        if self.charging:
            f.setPointSize(10)
            f.setBold(False)
            p.setFont(f)
            p.setPen(QColor("#4fa8ff"))
            p.drawText(rect.adjusted(0, side * 0.35, 0, 0), Qt.AlignHCenter | Qt.AlignTop, "⚡ charging")


class InfoCard(QFrame):
    def __init__(self, title: str, rows: list[tuple[str, str]], parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        t = QLabel(title)
        t.setObjectName("cardTitle")
        lay.addWidget(t)
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        self.values: dict[str, QLabel] = {}
        for i, (key, label) in enumerate(rows):
            k = QLabel(label)
            k.setObjectName("key")
            v = QLabel("N/A")
            v.setObjectName("value")
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            v.setWordWrap(True)
            grid.addWidget(k, i, 0, Qt.AlignTop)
            grid.addWidget(v, i, 1)
            self.values[key] = v
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)
        lay.addStretch(1)

    def set(self, key: str, text: str, color: str | None = None) -> None:
        lbl = self.values[key]
        lbl.setText(text)
        lbl.setStyleSheet(f"color:{color};" if color else "")


def yes_no(v) -> str:
    return "N/A" if v is None else ("Yes" if v else "No")


class DashboardWidget(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        top = QHBoxLayout()
        self.ring = ChargeRing()
        top.addWidget(self.ring)
        head = QVBoxLayout()
        self.headline = QLabel("Starting…")
        self.headline.setObjectName("headline")
        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setObjectName("detail")
        self.machine = QLabel("")
        self.machine.setObjectName("muted")
        self.notes = QLabel("")
        self.notes.setWordWrap(True)
        self.notes.setObjectName("notes")
        head.addWidget(self.headline)
        head.addWidget(self.detail)
        head.addWidget(self.machine)
        head.addWidget(self.notes)
        head.addStretch(1)
        top.addLayout(head, 1)
        root.addLayout(top)

        grid = QGridLayout()
        self.status_card = InfoCard("Power", [
            ("charging", "Battery driver state"), ("ac", "AC adapter"), ("rate", "Power flow"),
            ("voltage", "Voltage"), ("runtime", "Est. remaining runtime"), ("temp", "Temperature")])
        self.battery_card = InfoCard("Battery", [
            ("manufacturer", "Manufacturer"), ("model", "Model"), ("chem", "Chemistry"),
            ("cycles", "Cycle count"), ("mdate", "Manufactured"), ("source", "Data source")])
        self.capacity_card = InfoCard("Capacity & health", [
            ("design", "Design capacity"), ("full", "Full charge capacity"), ("current", "Current capacity"),
            ("health", "Battery health"), ("wear", "Wear")])
        self.control_card = InfoCard("Charge control", [
            ("hw", "Hardware control"), ("iface", "Interface"), ("user", "Your thresholds"),
            ("plan", "Firmware target"), ("fw", "Firmware now")])
        grid.addWidget(self.status_card, 0, 0)
        grid.addWidget(self.battery_card, 0, 1)
        grid.addWidget(self.capacity_card, 1, 0)
        grid.addWidget(self.control_card, 1, 1)
        root.addLayout(grid, 1)

    def set_profile(self, prof) -> None:
        m = prof.machine
        self.machine.setText(f"{prof.display_name}   ·   BIOS {m.bios_version or 'N/A'}   ·   "
                             f"{m.os_name or ''}")

    def update_snapshot(self, s: BatterySnapshot) -> None:
        c = self.status_card
        c.set("charging", s.charging_state_text())
        c.set("ac", "Connected" if s.ac_online else ("Disconnected" if s.ac_online is False else "N/A"))
        if s.rate_mw is None:
            c.set("rate", "N/A")
        elif s.rate_mw > 0:
            c.set("rate", f"+{s.rate_mw/1000:.2f} W into battery")
        elif s.rate_mw < 0:
            c.set("rate", f"{s.rate_mw/1000:.2f} W from battery")
        else:
            c.set("rate", "0 W (battery idle)")
        c.set("voltage", calc.fmt(None if s.voltage_mv is None else s.voltage_mv / 1000, "V", 2))
        c.set("runtime", calc.format_minutes(s.runtime_min))
        if s.temperature_c is None:
            c.set("temp", f"N/A ({s.temperature_note})" if s.temperature_note else "N/A")
        else:
            c.set("temp", f"{s.temperature_c:.1f} °C", "#ff5c5c" if s.temperature_c >= 50 else None)
        b = self.battery_card
        b.set("manufacturer", s.manufacturer or "N/A")
        b.set("model", s.model or "N/A")
        b.set("chem", s.chemistry or "N/A")
        b.set("cycles", calc.fmt(s.cycle_count))
        b.set("mdate", s.manufacture_date or "N/A")
        b.set("source", {"ioctl": "Windows battery driver", "power_status": "GetSystemPowerStatus (limited)",
                         "none": "None"}.get(s.source, s.source))
        k = self.capacity_card
        k.set("design", calc.fmt(s.design_mwh, "mWh"))
        k.set("full", calc.fmt(s.full_charge_mwh, "mWh"))
        k.set("current", calc.fmt(s.remaining_mwh, "mWh"))
        h = s.health_pct
        k.set("health", calc.fmt(h, "%", 1), None if h is None else ("#3ddc97" if h >= 80 else "#f5b942"
                                                                      if h >= 60 else "#ff5c5c"))
        k.set("wear", calc.fmt(s.wear_pct, "%", 1))
        self.ring.set_values(s.percent, bool(s.charging), self.ring.start, self.ring.stop)

    def update_status(self, st: cc.ControllerStatus, user: tuple[int, int]) -> None:
        self.headline.setText(st.headline)
        self.headline.setStyleSheet(f"color:{STATE_COLORS.get(st.state, '#ffffff')};")
        caps = st.capabilities
        monitoring_only = st.state == cc.MONITORING_ONLY or not caps.supported
        # Without firmware charge control the long compatibility notice and the interface-detection
        # notes (Dell BIOS WMI / Dell Command | Configure) are not shown here: they are in
        # Settings > I. Hardware Diagnostics. The card below still says plainly that charge control
        # is not available, so the dashboard never suggests that ARES controls charging.
        self.detail.setText("" if monitoring_only else st.detail)
        self.detail.setVisible(bool(self.detail.text()))
        msgs = [] if monitoring_only else st.messages
        self.notes.setText("\n".join(f"• {m}" for m in msgs))
        self.notes.setVisible(bool(msgs))
        k = self.control_card
        if monitoring_only:
            k.set("hw", CONTROL_UNAVAILABLE, "#f5b942")
            k.set("iface", "None found (details: Settings > Hardware Diagnostics)")
        else:
            k.set("hw", caps.summary, "#3ddc97" if caps.can_write else "#f5b942")
            k.set("iface", caps.interface or "None")
        k.set("user", f"Resume {user[0]}% / stop {user[1]}%")
        if st.plan and not monitoring_only:
            k.set("plan", f"Start {st.plan.start}% / stop {st.plan.stop}%"
                          + ("" if st.plan.exact else "  (closest supported)"))
        else:
            k.set("plan", "N/A (no hardware control)")
        k.set("fw", st.firmware.describe() if st.firmware else "N/A")
        if st.plan:
            self.ring.start, self.ring.stop = st.plan.start, st.plan.stop
        self.ring.update()


def _num(row, key):
    """A numeric column of a samples row, or None when it is missing, NULL or not a number."""
    try:
        v = row[key]
    except (KeyError, IndexError, TypeError):
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v


def charge_state_text(row) -> str:
    """'Charging' / 'Discharging' / 'On AC, not charging' / 'Unknown' for one recorded sample."""
    if row is None:
        return "Unknown"
    if _num(row, "charging") == 1:
        return "Charging"
    if _num(row, "discharging") == 1:
        return "Discharging"
    if _num(row, "ac_online") == 1:
        return "On AC, not charging"
    if _num(row, "ac_online") == 0:
        return "Discharging"            # on battery, even if the driver left the flag empty
    return "Unknown"


def format_value(v: float, unit: str = "%") -> str:
    """Exact value as recorded: whole numbers without decimals, others with one or two."""
    if abs(v - round(v)) < 1e-9:
        return f"{round(v):.0f}{unit}"
    return f"{v:.2f}".rstrip("0").rstrip(".") + unit


def point_tooltip_lines(t: float, v: float, row=None, unit: str = "%",
                        time_format: str = "%Y-%m-%d %H:%M:%S", value_name: str = "Battery") -> list[str]:
    """Text lines of the hover tooltip for one data point. Voltage and capacity appear only when the
    sample recorded them (many drivers report neither); a missing state shows as 'Unknown'."""
    lines = [f"{value_name}: {format_value(v, unit)}", datetime.fromtimestamp(t).strftime(time_format)]
    if row is None:
        return lines
    lines.append(f"State: {charge_state_text(row)}")
    mv = _num(row, "voltage_mv")
    if mv is not None and mv > 0:
        lines.append(f"Voltage: {mv / 1000:.2f} V")
    rem, full = _num(row, "remaining_mwh"), _num(row, "full_mwh")
    rem = rem if rem is not None and rem >= 0 else None
    full = full if full is not None and full > 0 else None
    if rem is not None and full is not None:
        lines.append(f"Capacity: {rem / 1000:.1f} of {full / 1000:.1f} Wh")
    elif rem is not None:
        lines.append(f"Remaining capacity: {rem / 1000:.1f} Wh")
    elif full is not None:
        lines.append(f"Full-charge capacity: {full / 1000:.1f} Wh")
    return lines


def nearest_index(times: list[float], t: float) -> int | None:
    """Index of the point whose time is closest to t (times sorted ascending); None if empty."""
    if not times:
        return None
    i = bisect.bisect_left(times, t)
    if i <= 0:
        return 0
    if i >= len(times):
        return len(times) - 1
    return i if times[i] - t < t - times[i - 1] else i - 1


class LineChart(QWidget):
    """Lightweight QPainter line chart (avoids loading the heavy QtCharts module).

    Hover: moving the mouse over the plot selects the nearest recorded point (by time), draws a
    vertical guide line and a marker on it, and paints a tooltip box next to it. The tooltip is
    painted by the chart itself (not a QToolTip pop-up), so it moves with the point without
    flicker and uses the theme's tooltip colours. The chart repaints only when the selected
    point changes."""

    MARGINS = (44, 12, 10, 26)          # left, right, top, bottom

    def __init__(self, y_label: str = "%", y_min: float = 0, y_max: float = 100, parent=None) -> None:
        super().__init__(parent)
        self.points: list[tuple[float, float]] = []
        self.details: list | None = None             # optional samples row per point (same order)
        self.shade: list[tuple[float, float]] = []   # charging spans (t0, t1)
        self.y_min, self.y_max, self.y_label = y_min, y_max, y_label
        self.hlines: list[tuple[float, QColor]] = []
        self.empty_text = "No data recorded yet"
        self.value_name = "Battery"
        self.time_format = "%Y-%m-%d %H:%M:%S"
        self.hover_index: int | None = None
        self._hover_pos: QPointF | None = None
        self._times: list[float] = []
        self.setMinimumHeight(220)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)

    def set_data(self, points, shade=(), hlines=(), details=None) -> None:
        self.points, self.shade, self.hlines = list(points), list(shade), list(hlines)
        self.details = list(details) if details is not None and len(details) == len(self.points) else None
        self._times = [t for t, _ in self.points]
        # Keep the tooltip on the point nearest the mouse when the data refreshes under it.
        self.hover_index = self._index_at(self._hover_pos) if self._hover_pos is not None else None
        self.update()

    # -- geometry shared by painting and hover ------------------------------------------------
    def plot_rect(self) -> QRectF:
        L, R, T, B = self.MARGINS
        return QRectF(L, T, max(1, self.width() - L - R), max(1, self.height() - T - B))

    def _span(self) -> tuple[float, float]:
        t0, t1 = self.points[0][0], self.points[-1][0]
        return t0, max(1.0, t1 - t0)

    def x_of(self, t: float) -> float:
        r = self.plot_rect()
        t0, span = self._span()
        return r.left() + (t - t0) / span * r.width()

    def y_of(self, v: float) -> float:
        r = self.plot_rect()
        v = max(self.y_min, min(self.y_max, v))
        return r.top() + (self.y_max - v) / (self.y_max - self.y_min) * r.height()

    def _index_at(self, pos: QPointF | None) -> int | None:
        if pos is None or len(self.points) < 2:
            return None
        r = self.plot_rect()
        if not (r.left() - 4 <= pos.x() <= r.right() + 4 and r.top() <= pos.y() <= r.bottom()):
            return None
        t0, span = self._span()
        t = t0 + (pos.x() - r.left()) / r.width() * span
        return nearest_index(self._times, t)

    def hover_lines(self) -> list[str]:
        if self.hover_index is None:
            return []
        t, v = self.points[self.hover_index]
        row = self.details[self.hover_index] if self.details is not None else None
        return point_tooltip_lines(t, v, row, self.y_label, self.time_format, self.value_name)

    # -- hover events ------------------------------------------------------------------------
    def set_hover_pos(self, pos: QPointF | None) -> None:
        self._hover_pos = pos
        idx = self._index_at(pos)
        if idx != self.hover_index:           # repaint only when the selected point changes
            self.hover_index = idx
            self.update()

    def mouseMoveEvent(self, e) -> None:
        self.set_hover_pos(e.position())
        super().mouseMoveEvent(e)

    def leaveEvent(self, e) -> None:
        self.set_hover_pos(None)
        super().leaveEvent(e)

    def resizeEvent(self, e) -> None:
        if self._hover_pos is not None:
            self.hover_index = self._index_at(self._hover_pos)
        super().resizeEvent(e)

    # -- painting ----------------------------------------------------------------------------
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        L, R, T, B = self.MARGINS
        w, h = self.width() - L - R, self.height() - T - B
        p.fillRect(self.rect(), self.palette().color(QPalette.AlternateBase))
        GRID = self.palette().color(QPalette.Mid)
        p.setPen(QPen(GRID, 1))
        f = p.font()
        f.setPointSize(8)
        p.setFont(f)
        for i in range(5):
            y = T + h * i / 4
            p.setPen(QPen(GRID, 1))
            p.drawLine(L, int(y), L + w, int(y))
            p.setPen(MUTED)
            val = self.y_max - (self.y_max - self.y_min) * i / 4
            p.drawText(0, int(y) - 7, L - 6, 14, Qt.AlignRight | Qt.AlignVCenter, f"{val:.0f}{self.y_label}")
        if len(self.points) < 2:
            p.setPen(MUTED)
            p.drawText(self.rect(), Qt.AlignCenter, self.empty_text)
            return
        t0, span = self._span()
        t1 = self.points[-1][0]
        X, Y = self.x_of, self.y_of

        for a, b in self.shade:
            p.fillRect(QRectF(X(max(a, t0)), T, max(1.0, X(min(b, t1)) - X(max(a, t0))), h), QColor(79, 168, 255, 40))
        for val, col in self.hlines:
            p.setPen(QPen(col, 1, Qt.DashLine))
            p.drawLine(QPointF(L, Y(val)), QPointF(L + w, Y(val)))
        path = QPainterPath(QPointF(X(self.points[0][0]), Y(self.points[0][1])))
        for t, v in self.points[1:]:
            path.lineTo(X(t), Y(v))
        p.setPen(QPen(ACCENT, 2))
        p.drawPath(path)
        p.setPen(MUTED)
        fmt = "%H:%M" if span <= 86400 else "%d %b"
        for i in range(5):
            t = t0 + span * i / 4
            x = X(t)
            p.drawText(int(x) - 40, T + h + 4, 80, 18, Qt.AlignCenter, datetime.fromtimestamp(t).strftime(fmt))
        if self.hover_index is not None and self.hover_index < len(self.points):
            self._paint_hover(p)

    def tooltip_rect(self, lines: list[str], anchor: QPointF, fm) -> QRectF:
        """Box beside the marker, flipped to the left / clamped so it always stays inside the chart."""
        pad = 6
        tw = max(fm.horizontalAdvance(s) for s in lines) + 2 * pad
        th = fm.height() * len(lines) + 2 * pad
        x = anchor.x() + 12
        if x + tw > self.width() - 2:
            x = anchor.x() - 12 - tw
        x = max(2.0, x)
        y = min(max(2.0, anchor.y() - th / 2), max(2.0, self.height() - th - 2))
        return QRectF(x, y, tw, th)

    def _paint_hover(self, p: QPainter) -> None:
        pal = self.palette()
        r = self.plot_rect()
        t, v = self.points[self.hover_index]
        pt = QPointF(self.x_of(t), self.y_of(v))
        guide = QColor(pal.color(QPalette.WindowText))
        guide.setAlpha(110)
        p.setPen(QPen(guide, 1, Qt.DashLine))
        p.drawLine(QPointF(pt.x(), r.top()), QPointF(pt.x(), r.bottom()))
        p.setPen(QPen(pal.color(QPalette.Base), 2))
        p.setBrush(ACCENT)
        p.drawEllipse(pt, 4.5, 4.5)
        lines = self.hover_lines()
        fm = p.fontMetrics()
        box = self.tooltip_rect(lines, pt, fm)
        p.setPen(QPen(pal.color(QPalette.Mid), 1))
        p.setBrush(pal.color(QPalette.ToolTipBase))
        p.drawRoundedRect(box, 4, 4)
        p.setPen(pal.color(QPalette.ToolTipText))
        for i, s in enumerate(lines):
            p.drawText(QRectF(box.left() + 6, box.top() + 6 + i * fm.height(), box.width() - 12, fm.height()),
                       Qt.AlignLeft | Qt.AlignVCenter, s)
        p.setBrush(Qt.NoBrush)


class HistoryPage(QWidget):

    RANGES = {"Last 6 hours": 6 * 3600, "Last 24 hours": 86400, "Last 7 days": 7 * 86400, "Last 30 days": 30 * 86400}

    def __init__(self, history_provider, parent=None) -> None:
        super().__init__(parent)
        self.provider = history_provider   # callable returning (BatteryHistory)
        lay = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.range = QComboBox()
        self.range.addItems(list(self.RANGES))
        self.range.setCurrentIndex(1)
        self.range.currentIndexChanged.connect(self.refresh)
        bar.addWidget(QLabel("Range:"))
        bar.addWidget(self.range)
        bar.addStretch(1)
        # (The CSV export buttons were removed from the UI in v1.2. The samples, sessions and events
        # tables are untouched; report_service.export_*_csv still exists for scripted exports.)
        lay.addLayout(bar)
        self.chart = LineChart()
        lay.addWidget(self.chart, 2)
        self.drain = QLabel("")
        self.drain.setObjectName("detail")
        lay.addWidget(self.drain)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Session", "Start", "End", "Duration", "From %", "To %"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        lay.addWidget(self.table, 1)
        self.thresholds: tuple[int, int] | None = None

    def refresh(self) -> None:
        hist = self.provider()
        if hist is None:
            return
        since = time.time() - self.RANGES[self.range.currentText()]
        rows = hist.samples_since(since)
        kept = [r for r in rows if _num(r, "percent") is not None and _num(r, "ts") is not None]
        pts = [(r["ts"], r["percent"]) for r in kept]
        shade, start = [], None
        for r in rows:
            if r["charging"] == 1 and start is None:
                start = r["ts"]
            elif r["charging"] != 1 and start is not None:
                shade.append((start, r["ts"]))
                start = None
        if start is not None and rows:
            shade.append((start, rows[-1]["ts"]))
        hl = []
        if self.thresholds:
            hl = [(self.thresholds[1], self.palette().color(QPalette.WindowText)), (self.thresholds[0], QColor("#4fa8ff"))]
        self.chart.set_data(pts, shade, hl, details=kept)
        ds = hist.drain_stats(3 * 3600)
        if ds.percent_per_hour is not None:
            self.drain.setText(f"Recent discharge rate: {ds.percent_per_hour:.1f} %/h"
                               + (f", average draw {ds.avg_power_w:.1f} W" if ds.avg_power_w else "")
                               + f" ({ds.samples} samples). Blue shading = charging. Hover over the line for details.")
        else:
            self.drain.setText("Discharge rate: N/A (needs at least two samples on battery in the last 3 h). "
                               "Blue shading = charging. Hover over the line for details.")
        sess = hist.sessions(100)
        self.table.setRowCount(len(sess))
        for i, r in enumerate(sess):
            dur = calc.format_minutes((r["end_ts"] - r["start_ts"]) / 60) if r["end_ts"] else "ongoing"
            vals = [r["kind"].capitalize(), datetime.fromtimestamp(r["start_ts"]).strftime("%Y-%m-%d %H:%M"),
                    datetime.fromtimestamp(r["end_ts"]).strftime("%Y-%m-%d %H:%M") if r["end_ts"] else "—",
                    dur, calc.fmt(r["start_pct"], "%"), calc.fmt(r["end_pct"], "%")]
            for j, v in enumerate(vals):
                self.table.setItem(i, j, QTableWidgetItem(str(v)))


class HealthPage(QWidget):
    def __init__(self, history_provider, parent=None) -> None:
        super().__init__(parent)
        self.provider = history_provider
        lay = QVBoxLayout(self)
        self.card = InfoCard("Battery Health Analyzer", [
            ("health", "Current health"), ("wear", "Wear"), ("cycles", "Cycle count"),
            ("trend", "Change since tracking began"), ("anomaly", "Capacity readings")])
        lay.addWidget(self.card)
        t = QLabel("Health over time (full-charge ÷ design capacity)")
        t.setObjectName("cardTitle")
        lay.addWidget(t)
        self.chart = LineChart("%", 40, 110)
        self.chart.empty_text = "Health history appears after two days of tracking"
        self.chart.value_name = "Health"
        self.chart.time_format = "%Y-%m-%d"
        lay.addWidget(self.chart, 1)
        self._snap: BatterySnapshot | None = None

    def update_snapshot(self, s: BatterySnapshot) -> None:
        self._snap = s

    def refresh(self) -> None:
        s = self._snap
        hist = self.provider()
        if s:
            self.card.set("health", calc.fmt(s.health_pct, "%", 1))
            self.card.set("wear", calc.fmt(s.wear_pct, "%", 1))
            self.card.set("cycles", calc.fmt(s.cycle_count))
            issues = calc.capacity_anomalies(s.full_charge_mwh, s.design_mwh, s.remaining_mwh)
            if s.design_mwh is None or s.full_charge_mwh is None:
                self.card.set("anomaly", "N/A (capacity not reported by driver)")
            else:
                self.card.set("anomaly", "\n".join(issues) if issues else "Normal", "#f5b942" if issues else None)
        if hist:
            tr = hist.health_trend()
            if tr.change is None:
                self.card.set("trend", "N/A")
            else:
                self.card.set("trend", f"{tr.change:+.1f} points ({tr.first_day} → {tr.last_day})")
            pts = []
            for r in hist.health_rows():
                if _num(r, "health_pct") is None:
                    continue
                ts = datetime.strptime(r["day"], "%Y-%m-%d").timestamp()
                pts.append((ts, r["health_pct"]))
            self.chart.set_data(pts, (), [(80, QColor("#f5b942"))])
