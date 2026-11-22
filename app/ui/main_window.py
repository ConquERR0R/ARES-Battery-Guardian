"""Dashboard window: a disposable view attached to the running background app.

Closing it (X) destroys the window object; monitoring keeps running in
AresBackgroundApp. Reopening from the tray builds a new window."""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QByteArray, Qt, QTimer
from PySide6.QtWidgets import (QHeaderView, QLabel, QMainWindow, QMessageBox,
                               QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from app import APP_DISPLAY_VERSION, APP_NAME
from app.core import charging_controller as cc
from app.ui.dashboard import DashboardWidget, HealthPage, HistoryPage
from app.ui.about import AboutPage
from app.ui.settings import SettingsPage
from app.ui.tray import battery_icon
from app.utils import system_info as si


class EventsPage(QWidget):
    def __init__(self, db_provider, parent=None) -> None:
        super().__init__(parent)
        self.provider = db_provider
        lay = QVBoxLayout(self)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Time", "Type", "Message"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setWordWrap(True)
        lay.addWidget(self.table)

    def refresh(self) -> None:
        db = self.provider()
        rows = db.recent_events(300)
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            for j, v in enumerate((datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M:%S"), r["kind"],
                                   r["message"])):
                self.table.setItem(i, j, QTableWidgetItem(v))
        self.table.resizeRowsToContents()


class MainWindow(QMainWindow):
    TAB_DASH, TAB_HISTORY, TAB_HEALTH, TAB_EVENTS, TAB_SETTINGS, TAB_ABOUT = range(6)

    def __init__(self, bg) -> None:
        super().__init__()
        self.bg = bg                      # AresBackgroundApp (owns tray, worker thread, database)
        self.setAttribute(Qt.WA_DeleteOnClose, True)   # X = destroy this window, not hide it
        self.setWindowTitle(f"{APP_NAME} {APP_DISPLAY_VERSION}" + ("  [Administrator]" if si.is_admin() else ""))
        self.setWindowIcon(battery_icon(100))
        self.resize(1040, 720)
        self.db = bg.db
        self.history = bg.history

        self.tabs = QTabWidget()
        self.dashboard = DashboardWidget()
        self.history_page = HistoryPage(lambda: self.history)
        self.health_page = HealthPage(lambda: self.history)
        self.events_page = EventsPage(lambda: self.db)
        self.settings_page = SettingsPage(self.db, player=bg.lock.player, wake_tester=bg.test_wake)
        self.tabs.addTab(self.dashboard, "Dashboard")
        self.tabs.addTab(self.history_page, "History")
        self.tabs.addTab(self.health_page, "Health")
        self.tabs.addTab(self.events_page, "Events")
        self.tabs.addTab(self.settings_page, "Settings")
        self.about_page = AboutPage(self.db)
        self.tabs.addTab(self.about_page, "About")
        self.tabs.currentChanged.connect(lambda _i: self._refresh_visible_page())
        self.setCentralWidget(self.tabs)
        self.status_lbl = QLabel()
        self.statusBar().addWidget(self.status_lbl)

        # Attach to the running backend. Connections to a destroyed receiver are
        # removed by Qt automatically, so closing the window leaves no dangling slots.
        bg.snapshot.connect(self._on_snapshot)
        bg.status.connect(self._on_status)
        bg.profile.connect(self._on_profile)
        bg.action_result.connect(self._on_action_result)
        bg.monitoring_changed.connect(self._on_monitoring_changed)
        bg.event_logged.connect(self._refresh_events_if_visible)

        sp = self.settings_page
        sp.setting_changed.connect(bg.on_setting_changed)     # announced only after a confirmed Save
        sp.apply_requested.connect(bg.request_apply)
        sp.restore_requested.connect(bg.request_restore)
        sp.password_entered.connect(bg.request_password)
        sp.redetect_requested.connect(bg.request_redetect)
        sp.quit_requested.connect(bg.exit_ares)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh_visible_page)
        self._refresh_timer.start(60_000)

        # Fill immediately from the backend's latest readings (no new hardware calls).
        if bg.last_profile is not None:
            self._on_profile(bg.last_profile)
        if bg.last_snapshot is not None:
            self._on_snapshot(bg.last_snapshot)
        if bg.last_status is not None:
            self._on_status(bg.last_status)
        self._on_monitoring_changed(bg.monitoring)
        self._restore_geometry()            # last: after all widgets exist (size limits are known)

    # ---- backend → window -----------------------------------------------------
    def _on_snapshot(self, s) -> None:
        self.dashboard.update_snapshot(s)
        self.health_page.update_snapshot(s)
        self.status_lbl.setText(f"Last reading {datetime.fromtimestamp(s.timestamp).strftime('%H:%M:%S')}"
                                f"  ·  source: {s.source}"
                                + ("" if self.bg.monitoring else "  ·  monitoring paused"))

    def _on_status(self, st: cc.ControllerStatus) -> None:
        user = (int(self.db.get_setting("min_charge")), int(self.db.get_setting("max_charge")))
        self.dashboard.update_status(st, user)
        self.settings_page.update_diagnostics(st)
        if st.plan:
            self.history_page.thresholds = (st.plan.start, st.plan.stop)

    def _on_profile(self, prof) -> None:
        self.dashboard.set_profile(prof)
        self.settings_page.set_profile(prof)
        caps = self.bg.capabilities()
        if caps is not None:
            self.settings_page.set_capabilities(caps)

    def _on_action_result(self, action: str, res) -> None:
        if res is None:
            return
        title = "Charge thresholds" if action == "apply" else "Restore firmware setting"
        text = res.message
        if res.needs_password:
            text += "\n\nEnter the BIOS admin password in Settings and try again."
        if res.needs_admin:
            text += "\n\nUse 'Restart as administrator'."
        (QMessageBox.information if res.success else QMessageBox.warning)(self, title, text)
        self.settings_page.refresh_from_db()        # keeps any unsaved edits

    def _on_monitoring_changed(self, on: bool) -> None:
        if not on:
            self.status_lbl.setText("Monitoring paused. Firmware thresholds (if applied) stay active.")

    # ---- pages -------------------------------------------------------------------
    def show_tab(self, idx: int) -> None:
        self.tabs.setCurrentIndex(idx)
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()
        self._refresh_visible_page()

    def _refresh_visible_page(self) -> None:
        i = self.tabs.currentIndex()
        if i == self.TAB_HISTORY:
            self.history_page.refresh()
        elif i == self.TAB_HEALTH:
            self.health_page.refresh()
        elif i == self.TAB_EVENTS:
            self.events_page.refresh()

    def _refresh_events_if_visible(self) -> None:
        if self.tabs.currentIndex() == self.TAB_EVENTS:
            self.events_page.refresh()

    # ---- close -------------------------------------------------------------------
    # ---- window size / position / tab (remembered across restarts) -----------------
    def _restore_geometry(self) -> None:
        data = self.db.get_setting("ui_window_geometry", "")
        if data:
            try:
                # Qt moves the window back onto a visible screen if that monitor is gone.
                self.restoreGeometry(QByteArray.fromBase64(data.encode("ascii")))
            except (ValueError, UnicodeError):
                pass

    def _save_ui_state(self) -> None:
        try:
            self.db.set_settings({
                "ui_window_geometry": bytes(self.saveGeometry().toBase64()).decode("ascii"),
                "ui_last_tab": int(self.tabs.currentIndex())})
        except Exception:                 # UI state must never block closing
            pass

    def closeEvent(self, ev) -> None:
        """X closes and destroys this window only. ARES keeps running in the background."""
        if not self.settings_page.confirm_close(allow_cancel=not self.bg._quitting):
            ev.ignore()                     # user chose Cancel on "unsaved changes"
            return
        self._save_ui_state()
        if not self.db.get_state("close_hint_shown"):
            self.db.set_state("close_hint_shown", True)
            self.bg.tray.notify(APP_NAME, "Dashboard closed. ARES keeps monitoring in the background; "
                                "use the tray icon to reopen it or choose Exit ARES to stop.", "info")
        ev.accept()
