"""Look and feel: light / dark palettes, one stylesheet built from tokens, and line icons."""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

LIGHT = {
    "bg": "#f3f5f8", "surface": "#ffffff", "surface2": "#f8f9fb", "border": "#dfe3ea", "text": "#18202b",
    "muted": "#5d6b7e", "accent": "#1f66d1", "accent_hover": "#1757b8", "accent_soft": "#e6efff",
    "ok": "#1f8a57", "ok_soft": "#e3f4ea", "fail": "#c93a3a", "fail_soft": "#fbe6e6", "warn": "#b9770e",
    "warn_soft": "#fcf1dc", "run": "#1f66d1", "run_soft": "#e6efff", "sidebar": "#0f1b2d", "sidebar_text": "#c9d4e5",
    "sidebar_active": "#1f66d1",
}
DARK = {
    "bg": "#12161c", "surface": "#1a2029", "surface2": "#151a22", "border": "#2b3340", "text": "#e5eaf1",
    "muted": "#93a0b3", "accent": "#4d8dff", "accent_hover": "#6aa0ff", "accent_soft": "#1c2b47",
    "ok": "#43c086", "ok_soft": "#173426", "fail": "#f06b6b", "fail_soft": "#3a1d1f", "warn": "#e2a43a",
    "warn_soft": "#3a2e17", "run": "#4d8dff", "run_soft": "#1c2b47", "sidebar": "#0b0f15", "sidebar_text": "#aab6c8",
    "sidebar_active": "#4d8dff",
}

_current = dict(LIGHT)


def tokens() -> dict:
    return _current


def system_prefers_dark() -> bool:
    try:
        return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except AttributeError:  # Qt < 6.5
        return False


STYLE = """
* {{ font-size: 13px; }}
QMainWindow, QDialog {{ background: {bg}; }}
QWidget {{ color: {text}; }}
QWidget#page {{ background: {bg}; }}
QLabel[role="h1"] {{ font-size: 20px; font-weight: 600; }}
QLabel[role="h2"] {{ font-size: 15px; font-weight: 600; }}
QLabel[role="muted"] {{ color: {muted}; }}
QLabel[role="mono"] {{ font-family: Menlo, Consolas, monospace; color: {muted}; }}

/* sidebar */
QFrame#sidebar {{ background: {sidebar}; border: none; }}
QFrame#sidebar QLabel {{ color: #ffffff; }}
QFrame#sidebar QLabel[role="muted"] {{ color: {sidebar_text}; }}
QPushButton[nav="true"] {{ background: transparent; color: {sidebar_text}; border: none; border-radius: 8px;
    text-align: left; padding: 9px 12px; font-size: 14px; }}
QPushButton[nav="true"]:hover {{ background: rgba(255,255,255,0.07); color: #ffffff; }}
QPushButton[nav="true"]:checked {{ background: {sidebar_active}; color: #ffffff; }}

/* buttons */
QPushButton {{ background: {surface}; border: 1px solid {border}; border-radius: 7px; padding: 6px 12px; }}
QPushButton:hover {{ border-color: {accent}; }}
QPushButton:disabled {{ color: {muted}; background: {surface2}; }}
QPushButton[kind="primary"] {{ background: {accent}; color: #ffffff; border: none; font-weight: 600; padding: 8px 18px; }}
QPushButton[kind="primary"]:hover {{ background: {accent_hover}; }}
QPushButton[kind="primary"]:disabled {{ background: {border}; color: {muted}; }}
QPushButton[kind="danger"] {{ color: {fail}; border-color: {fail}; }}
QPushButton[kind="ghost"] {{ background: transparent; border: none; color: {accent}; padding: 4px 6px; }}
QPushButton[kind="ghost"]:hover {{ text-decoration: underline; }}
QToolButton {{ background: transparent; border: none; border-radius: 6px; padding: 4px; }}
QToolButton:hover {{ background: {accent_soft}; }}

/* cards */
QFrame#card {{ background: {surface}; border: 1px solid {border}; border-radius: 12px; }}
QFrame#card[drop="true"] {{ border: 2px dashed {accent}; background: {accent_soft}; }}
QFrame#card[selected="true"] {{ border: 2px solid {accent}; }}
QLabel[pill] {{ border-radius: 10px; padding: 2px 10px; font-size: 12px; font-weight: 600; }}
QLabel[pill="idle"] {{ background: {surface2}; color: {muted}; border: 1px solid {border}; }}
QLabel[pill="running"] {{ background: {run_soft}; color: {run}; }}
QLabel[pill="passed"] {{ background: {ok_soft}; color: {ok}; }}
QLabel[pill="failed"] {{ background: {fail_soft}; color: {fail}; }}
QLabel[pill="badge"] {{ background: {accent_soft}; color: {accent}; font-weight: 500; }}

QProgressBar {{ background: {surface2}; border: 1px solid {border}; border-radius: 6px; height: 12px;
    text-align: center; font-size: 11px; color: {muted}; }}
QProgressBar::chunk {{ background: {accent}; border-radius: 5px; }}
QProgressBar[state="passed"]::chunk {{ background: {ok}; }}
QProgressBar[state="failed"]::chunk {{ background: {fail}; }}

/* inputs, lists, tabs */
QLineEdit, QComboBox, QPlainTextEdit, QSpinBox {{ background: {surface}; border: 1px solid {border};
    border-radius: 6px; padding: 5px 7px; selection-background-color: {accent}; }}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {{ border-color: {accent}; }}
QComboBox QAbstractItemView {{ background: {surface}; border: 1px solid {border}; selection-background-color: {accent_soft};
    selection-color: {text}; }}
QTreeWidget, QTableWidget, QListWidget {{ background: {surface}; border: 1px solid {border}; border-radius: 8px;
    alternate-background-color: {surface2}; selection-background-color: {accent_soft}; selection-color: {text}; }}
QListWidget::item {{ padding: 7px 8px; border-radius: 6px; margin: 1px 2px; }}
QListWidget::item:selected {{ background: {accent_soft}; color: {text}; }}
QHeaderView::section {{ background: {surface2}; color: {muted}; border: none; border-bottom: 1px solid {border};
    padding: 6px 8px; font-weight: 600; }}
QTabWidget::pane {{ border: 1px solid {border}; border-radius: 8px; background: {surface}; top: -1px; }}
QTabBar::tab {{ background: transparent; color: {muted}; padding: 8px 14px; border: none;
    border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {text}; border-bottom: 2px solid {accent}; font-weight: 600; }}
QGroupBox {{ background: {surface}; border: 1px solid {border}; border-radius: 10px; margin-top: 18px;
    padding: 12px 10px 8px 10px; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; color: {muted}; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QSplitter::handle {{ background: {bg}; }}
QStatusBar {{ background: {surface}; border-top: 1px solid {border}; color: {muted}; }}
QToolTip {{ background: {surface}; color: {text}; border: 1px solid {border}; padding: 4px; }}
QMenu {{ background: {surface}; border: 1px solid {border}; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 4px; }}
QMenu::item:selected {{ background: {accent_soft}; color: {text}; }}
"""


def apply_theme(app: QApplication, mode: str = "system") -> str:
    """mode: light | dark | system. Returns the mode actually applied (light/dark)."""
    dark = system_prefers_dark() if mode == "system" else mode == "dark"
    _current.clear()
    _current.update(DARK if dark else LIGHT)
    t = _current
    app.setStyle("Fusion")
    pal = QPalette()
    for role, key in ((QPalette.ColorRole.Window, "bg"), (QPalette.ColorRole.Base, "surface"),
                      (QPalette.ColorRole.AlternateBase, "surface2"), (QPalette.ColorRole.Text, "text"),
                      (QPalette.ColorRole.WindowText, "text"), (QPalette.ColorRole.Button, "surface"),
                      (QPalette.ColorRole.ButtonText, "text"), (QPalette.ColorRole.Highlight, "accent"),
                      (QPalette.ColorRole.ToolTipBase, "surface"), (QPalette.ColorRole.ToolTipText, "text"),
                      (QPalette.ColorRole.PlaceholderText, "muted")):
        pal.setColor(role, QColor(t[key]))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    app.setPalette(pal)
    app.setStyleSheet(STYLE.format(**t))
    return "dark" if dark else "light"


def repolish(widget) -> None:
    """Re-apply the stylesheet after a dynamic property changed."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)


# -- icons: 24x24 line icons drawn for this app ----------------------------------------
_ICONS = {
    "flash": '<path d="M13 2 4 14h7l-1 8 9-12h-7l1-8z"/>',
    "config": '<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/>'
              '<circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
    "trace": '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
    "report": '<path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5M10 13h6M10 17h6"/>',
    "chip": '<rect x="7" y="7" width="10" height="10" rx="1"/><path d="M10 3v4M14 3v4M10 17v4M14 17v4M3 10h4M3 14h4'
            'M17 10h4M17 14h4"/>',
    "open": '<path d="M3 7h6l2 2h10v10H3z"/>',
    "save": '<path d="M5 3h11l3 3v15H5z"/><path d="M8 3v5h7V3M8 21v-6h8v6"/>',
    "new": '<path d="M12 5v14M5 12h14"/>',
    "play": '<path d="M7 4l13 8-13 8z"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="1"/>',
    "upload": '<path d="M12 16V4M7 9l5-5 5 5M4 16v4h16v-4"/>',
    "package": '<path d="M3 7l9-4 9 4v10l-9 4-9-4z"/><path d="M3 7l9 4 9-4M12 11v10"/>',
    "edit": '<path d="M4 20h4L19 9l-4-4L4 16z"/>',
    "trash": '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
    "moon": '<path d="M20 14A8 8 0 1 1 10 4a6 6 0 0 0 10 10z"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19'
           'M5 19l1.5-1.5M17.5 6.5 19 5"/>',
    "doc": '<path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5"/>',
    "tools": '<path d="M14.5 6.5a4 4 0 0 0-5.3 5.3L3.5 17.5l3 3 5.7-5.7a4 4 0 0 0 5.3-5.3l-2.4 2.4-2.6-.6-.6-2.6z"/>',
}


def icon(name: str, color: str | None = None, size: int = 18) -> QIcon:
    stroke = color or _current["text"]
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{stroke}" '
           f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{_ICONS[name]}</svg>')
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    pix = QPixmap(QSize(size * 2, size * 2))
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    renderer.render(painter)
    painter.end()
    return QIcon(pix)
