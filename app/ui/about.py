"""About page: version, build and runtime information, data location, Beta notes."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFormLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from app import paths
from app.build_info import version_info

BETA_NOTES = (
    "This is a Beta build. Please report anything unexpected; the Events tab and the log file have "
    "the details.\n\n"
    "Smart Wake-Up Protection is EXPERIMENTAL: waking a sleeping laptop with a Windows wake timer has "
    "not yet been confirmed on any Dell Latitude. Keep relying on the 90% / 25% protection screens and, "
    "where supported, the firmware charge threshold.\n\n"
    "ARES never physically stops charging by itself. Only the Dell firmware threshold (section A of "
    "Settings) controls charging; the protection screens are reminders.")


def _value(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("value")
    lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
    lbl.setWordWrap(True)
    return lbl


class AboutPage(QWidget):
    def __init__(self, db=None, parent=None) -> None:
        super().__init__(parent)
        self.info = version_info()
        lay = QVBoxLayout(self)
        title = QLabel(f"{self.info['name']}")
        title.setObjectName("headline")
        lay.addWidget(title)
        badge = QLabel(f"Version {self.info['display_version']}")
        badge.setObjectName("warn" if self.info["channel"] else "ok")
        lay.addWidget(badge)

        g = QGroupBox("Version information")
        f = QFormLayout(g)
        rows = [("Version", self.info["version"]),
                ("Release channel", self.info["channel"] or "Release"),
                ("Build", self.info["build_date"] + (" (packaged)" if self.info["packaged"] else "")),
                ("Python", self.info["python"]),
                ("PySide6 / Qt", f"{self.info['pyside6']} / Qt {self.info['qt']}"),
                ("Program", self.info["executable"]),
                ("Data folder", str(paths.base_dir()))]
        if self.info["build_pyinstaller"]:
            rows.insert(3, ("PyInstaller", self.info["build_pyinstaller"]))
        self.values: dict[str, QLabel] = {}
        for k, v in rows:
            self.values[k] = _value(v)
            f.addRow(k, self.values[k])
        lay.addWidget(g)

        g = QGroupBox("Beta notes")
        v = QVBoxLayout(g)
        notes = QLabel(BETA_NOTES)
        notes.setObjectName("detail")
        notes.setWordWrap(True)
        v.addWidget(notes)
        lay.addWidget(g)

        row = QHBoxLayout()
        self.copy_btn = QPushButton("Copy version information")
        self.copy_btn.clicked.connect(self.copy)
        row.addWidget(self.copy_btn)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)

    def text(self) -> str:
        return "\n".join(f"{k}: {lbl.text()}" for k, lbl in self.values.items())

    def copy(self) -> None:
        QGuiApplication.clipboard().setText(self.text())
