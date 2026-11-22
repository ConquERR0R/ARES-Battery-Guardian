"""System tray icon and menu."""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from app import APP_DISPLAY_VERSION, APP_NAME


def battery_icon(percent: float | None, charging: bool = False, paused: bool = False) -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QColor("#e6e9f0"))
    p.setBrush(Qt.NoBrush)
    body = QRectF(4, 16, 50, 32)
    p.drawRoundedRect(body, 5, 5)
    p.fillRect(QRectF(55, 26, 6, 12), QColor("#e6e9f0"))
    if percent is not None:
        col = "#ff5c5c" if percent <= 15 else "#f5b942" if percent <= 30 else (
            "#3ddc97" if paused else "#4fa8ff" if charging else "#3ddc97")
        p.fillRect(QRectF(8, 20, 42 * max(0.0, min(100.0, percent)) / 100, 24), QColor(col))
    f = QFont()
    f.setPixelSize(22)
    f.setBold(True)
    p.setFont(f)
    p.setPen(QColor("#ffffff"))
    p.drawText(body, Qt.AlignCenter, "?" if percent is None else ("⏸" if paused else f"{percent:.0f}"))
    p.end()
    return QIcon(pm)


class TrayIcon(QSystemTrayIcon):
    open_dashboard = Signal()
    open_settings = Signal()
    open_health = Signal()
    monitoring_toggled = Signal(bool)
    smart_wake_toggled = Signal(bool)
    exit_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(battery_icon(None), parent)
        # Left-click/double-click opens the dashboard; right-click shows this menu.
        menu = QMenu()
        self.a_open = QAction("Open Dashboard", menu)
        self.a_start = QAction("Start Monitoring", menu)
        self.a_pause = QAction("Pause Monitoring", menu)
        self.a_settings = QAction("Charging Settings", menu)
        self.a_health = QAction("Battery Health", menu)
        self.a_wake = QAction("Smart Wake-Up (experimental)", menu)
        self.a_wake.setCheckable(True)
        self.a_exit = QAction("Exit ARES", menu)
        for a in (self.a_open, self.a_start, self.a_pause):
            menu.addAction(a)
        menu.addSeparator()
        menu.addAction(self.a_settings)
        menu.addAction(self.a_health)
        menu.addAction(self.a_wake)
        menu.addSeparator()
        menu.addAction(self.a_exit)
        self.setContextMenu(menu)
        self._menu = menu
        self.a_open.triggered.connect(self.open_dashboard)
        self.a_start.triggered.connect(lambda: self.monitoring_toggled.emit(True))
        self.a_pause.triggered.connect(lambda: self.monitoring_toggled.emit(False))
        self.a_settings.triggered.connect(self.open_settings)
        self.a_health.triggered.connect(self.open_health)
        self.a_wake.triggered.connect(lambda checked: self.smart_wake_toggled.emit(bool(checked)))
        self.a_exit.triggered.connect(self.exit_requested)
        self.activated.connect(self._activated)
        self.setToolTip(f"{APP_NAME} {APP_DISPLAY_VERSION}")
        self.set_monitoring(True)

    def _activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.open_dashboard.emit()

    def set_smart_wake(self, on: bool) -> None:
        self.a_wake.setChecked(bool(on))

    def set_monitoring(self, on: bool) -> None:
        self.a_start.setEnabled(not on)
        self.a_pause.setEnabled(on)

    def update_state(self, percent, charging, paused, headline: str) -> None:
        self.setIcon(battery_icon(percent, charging, paused))
        pct = "N/A" if percent is None else f"{percent:.0f}%"
        self.setToolTip(f"ARES Battery Guardian\n{pct} · {headline}")

    def notify(self, title: str, msg: str, level: str) -> None:
        icon = {"warning": QSystemTrayIcon.Warning, "critical": QSystemTrayIcon.Critical}.get(
            level, QSystemTrayIcon.Information)
        self.showMessage(title, msg, icon, 8000)
