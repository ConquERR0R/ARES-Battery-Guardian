"""ARES themes (dark and light; the choice is saved in the "theme" setting).

Why this module exists: the original theme put `QWidget { background: ... }` and border rules
on QSpinBox / QComboBox / QCheckBox into one style sheet. A style sheet rule that matches an
input widget makes Qt draw that widget with its style-sheet renderer, and with the Windows 11
style this loses spin-box arrows, combo-box arrows and check-box indicators (they draw dark on
dark or not at all). Disabled controls also looked exactly like enabled ones, so a checkbox
that was disabled (e.g. "write thresholds to the firmware" on a PC without cctk) looked
clickable but did nothing.

Fix: use the cross-platform Fusion style with a dark QPalette for all colours, and keep the
style sheet to containers, labels and buttons only. Input widgets are drawn by Fusion from the
palette, so their indicators and arrows are always visible, and disabled widgets are visibly
greyed out.
"""
from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory

THEMES = {
    "dark": dict(bg="#0f1218", card="#161a23", card2="#1e2330", border="#232938", border2="#2f3647",
                 text="#e6e9f0", strong="#ffffff", muted="#8b93a7", detail="#c0c6d4", accent="#3ddc97",
                 warn="#f5b942", base="#0b0e13", button="#232938", hover="#2c3446", pressed="#1b202b",
                 disabled="#5b6273", disabled_bg="#1a1e27", indicator="#7d8598", mid="#262c3a",
                 light="#3a4256", dark="#0a0c10", primary="#1f7a55", primary_border="#27a06e",
                 primary_disabled="#1a2a24", check_disabled="#2c5a47", disabled_base="#141820"),
    "light": dict(bg="#f3f5f9", card="#ffffff", card2="#e9edf4", border="#d5dbe5", border2="#c3cad6",
                  text="#1b2130", strong="#0b0f18", muted="#5c6578", detail="#39414f", accent="#13885a",
                  warn="#a86600", base="#ffffff", button="#e9edf4", hover="#dde3ec", pressed="#cfd6e1",
                  disabled="#9aa2b1", disabled_bg="#eef1f5", indicator="#6f788a", mid="#c9d0db",
                  light="#ffffff", dark="#a7afbd", primary="#178a5e", primary_border="#13774f",
                  primary_disabled="#b9d9cb", check_disabled="#a9cdbd", disabled_base="#f0f2f6"),
}
DEFAULT_THEME = "dark"


def style_sheet(name: str = DEFAULT_THEME) -> str:
    t = THEMES.get(name, THEMES[DEFAULT_THEME])
    return f"""
QTabWidget::pane {{ border: 0; }}
QTabBar::tab {{ background: {t['card']}; color: {t['muted']}; padding: 8px 18px; border: 0; margin-right: 2px; }}
QTabBar::tab:selected {{ background: {t['card2']}; color: {t['strong']}; border-bottom: 2px solid {t['accent']}; }}
QFrame#card, QGroupBox {{ background: {t['card']}; border: 1px solid {t['border']}; border-radius: 10px; }}
QGroupBox {{ margin-top: 14px; padding: 14px 10px 10px 10px; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; color: {t['accent']}; }}
QLabel {{ background: transparent; }}
QLabel:disabled {{ color: {t['disabled']}; }}
QLabel#cardTitle {{ color: {t['accent']}; font-weight: 600; font-size: 11pt; }}
QLabel#key {{ color: {t['muted']}; }}
QLabel#value {{ color: {t['strong']}; }}
QLabel#headline {{ font-size: 20pt; font-weight: 700; }}
QLabel#detail {{ color: {t['detail']}; }}
QLabel#muted {{ color: {t['muted']}; }}
QLabel#notes {{ color: {t['warn']}; }}
QLabel#ok {{ color: {t['accent']}; }}
QLabel#warn {{ color: {t['warn']}; }}
QLabel#bad {{ color: #e5484d; }}
QPushButton {{ background: {t['button']}; color: {t['text']}; border: 1px solid {t['border2']}; padding: 6px 14px;
               border-radius: 6px; }}
QPushButton:hover {{ background: {t['hover']}; }}
QPushButton:pressed {{ background: {t['pressed']}; }}
QPushButton:disabled {{ color: {t['disabled']}; background: {t['disabled_bg']}; }}
QPushButton#primary {{ background: {t['primary']}; border-color: {t['primary_border']}; color: white; }}
QPushButton#primary:disabled {{ background: {t['primary_disabled']}; color: {t['disabled']}; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 14px; height: 14px; border: 2px solid {t['indicator']}; border-radius: 4px;
                       background: {t['base']}; }}
QCheckBox::indicator:hover {{ border-color: {t['accent']}; }}
QCheckBox::indicator:checked {{ background: {t['accent']}; border-color: {t['accent']}; }}
QCheckBox::indicator:disabled {{ border-color: {t['border2']}; background: {t['disabled_base']}; }}
QCheckBox::indicator:checked:disabled {{ background: {t['check_disabled']}; border-color: {t['check_disabled']}; }}
QTableWidget {{ background: {t['card']}; gridline-color: {t['border']}; border: 0; }}
QHeaderView::section {{ background: {t['card2']}; color: {t['muted']}; border: 0; padding: 4px; }}
QScrollArea {{ border: 0; }}
QMenu {{ background: {t['card']}; border: 1px solid {t['border2']}; }}
QMenu::item:selected {{ background: {t['hover']}; }}
"""


STYLE_SHEET = style_sheet(DEFAULT_THEME)          # kept for older imports


def palette(name: str = DEFAULT_THEME) -> QPalette:
    t = THEMES.get(name, THEMES[DEFAULT_THEME])
    p = QPalette()
    c = QColor
    for role, key in ((QPalette.Window, "bg"), (QPalette.WindowText, "text"), (QPalette.Base, "base"),
                      (QPalette.AlternateBase, "card"), (QPalette.Text, "text"), (QPalette.Button, "button"),
                      (QPalette.ButtonText, "text"), (QPalette.ToolTipBase, "card"), (QPalette.ToolTipText, "text"),
                      (QPalette.Highlight, "primary"), (QPalette.Link, "accent"),
                      (QPalette.PlaceholderText, "muted"), (QPalette.Light, "light"), (QPalette.Midlight, "border2"),
                      (QPalette.Mid, "mid"), (QPalette.Dark, "dark")):
        p.setColor(role, c(t[key]))
    p.setColor(QPalette.HighlightedText, c("#ffffff"))
    p.setColor(QPalette.Shadow, c("#000000"))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, c(t["disabled"]))
    p.setColor(QPalette.Disabled, QPalette.Base, c(t["disabled_base"]))
    return p


def dark_palette() -> QPalette:
    return palette("dark")


def apply_theme(app: QApplication, name: str | None = DEFAULT_THEME) -> str:
    """Apply a theme to the whole app (also used to switch live after Save). Returns the name used."""
    name = name if name in THEMES else DEFAULT_THEME
    if "Fusion" in QStyleFactory.keys():
        app.setStyle("Fusion")
    app.setPalette(palette(name))
    f = QFont("Segoe UI")
    f.setPointSize(10)
    app.setFont(f)                      # via the font, not a `*` style-sheet rule (see module doc)
    app.setStyleSheet(style_sheet(name))
    return name
