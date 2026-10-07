"""Small bundled vector icon set and shared Qt styling; no network assets."""
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt, QDir
from PySide6.QtGui import QIcon, QPixmap, QPainter, QPalette, QColor
from PySide6.QtSvg import QSvgRenderer


_PATHS = {
    "monitor": '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8M12 17v4"/>',
    "terminal": '<rect x="2" y="3" width="20" height="18" rx="2"/><path d="m6 8 4 4-4 4m7 0h5"/>',
    "control": '<rect x="2" y="3" width="19" height="13" rx="2"/><path d="M7 21h7m-4-5v5M14 11l2 11 2-4 4-2z"/>',
    "folder": '<path d="M3 7V5a1 1 0 0 1 1-1h5l2 3h9a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "edit": '<path d="M4 16L16 4l4 4L8 20H4zM13 7l4 4"/>',
    "trash": '<path d="M4 6h16M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7"/>',
    "settings": '<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3"/><circle cx="15" cy="17" r="3"/>',
    "scan": '<circle cx="11" cy="11" r="7"/><path d="M16 16l5 5M11 7v8M7 11h8"/>',
    "refresh": '<path d="M20 4v5h-5M4 20v-5h5M20 9a8 8 0 0 0-14-3M4 15a8 8 0 0 0 14 3"/>',
    "stop": '<rect x="5" y="5" width="14" height="14" rx="2"/>',
    "disconnect": '<path d="M10 4H4v16h6M10 12h11m-4-4 4 4-4 4"/>',
    "power": '<path d="M12 3v9M6 5a9 9 0 1 0 12 0"/>',
    "restart": '<path d="M20 4v5h-5M20 9a8 8 0 1 0 0 6"/>',
    "sleep": '<path d="M20 15A9 9 0 0 1 9 4a9 9 0 1 0 11 11z"/>',
    "hibernate": '<path d="M12 2v20M3 7l18 10M3 17 21 7M9 4l3 3 3-3M9 20l3-3 3 3M3 10l4-1-1-4M18 19l-1-4 4-1M3 14l4 1-1 4M18 5l-1 4 4 1"/>',
    "fullscreen": '<path d="M8 3H3v5m13-5h5v5M3 16v5h5m8 0h5v-5"/>',
    "fit": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 8l4 4-4 4m10-8l-4 4 4 4"/>',
    "cursor": '<path d="M5 3l2 17 4-6 7-2zM11 15l4 6"/>',
    "clipboard": '<rect x="5" y="5" width="14" height="16" rx="2"/><rect x="9" y="2" width="6" height="5" rx="1"/><path d="M9 12h6m-6 4h6"/>',
    "upload": '<path d="M12 16V3M7 8l5-5 5 5M4 15v6h16v-6"/>',
    "download": '<path d="M12 3v13m-5-5l5 5 5-5M4 17v4h16v-4"/>',
    "pin": '<path d="M8 3h8l-1 7 4 4H5l4-4zM12 14v8"/>',
    "unpin": '<path d="M8 3h8l-1 7 4 4H5l4-4M12 14v8M3 3l18 18"/>',
    "more": '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
    "lock": '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3M12 14v3"/>',
    "retry": '<path d="M20 7v5h-5M20 12a8 8 0 1 0-2 6"/>',
    "close": '<path d="M5 5l14 14M19 5L5 19"/>',
    "minimize": '<path d="M5 12h14"/>',
    "maximize": '<rect x="5" y="5" width="14" height="14" rx="1"/>',
    "restore": '<rect x="4" y="8" width="12" height="12" rx="1"/><path d="M8 8V4h12v12h-4"/>',
    "eye": '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    "grid": '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    "list": '<path d="M8 5h13M8 12h13M8 19h13M3 5h1M3 12h1M3 19h1"/>',
}


@lru_cache(maxsize=1)
def application_icon():
    """Branded icon with raster sizes for native taskbars, tray hosts and dialogs."""
    renderer = QSvgRenderer(str(Path(__file__).with_name("assets") / "app-icon.svg"))
    result = QIcon()
    for size in (16, 20, 22, 24, 32, 48, 64, 96, 128, 256):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        result.addPixmap(pixmap)
    return result


@lru_cache(maxsize=160)
def icon(name, color="#475569", rotation=0):
    body = _PATHS.get(name, _PATHS["monitor"])
    if rotation:
        body = f'<g transform="rotate({rotation} 12 12)">{body}</g>'
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">{body}</svg>'
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    result = QIcon()
    for size in (20, 24, 32, 48, 64, 96, 128):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        result.addPixmap(pixmap)
    return result


def refresh_icon(frame=0, color="#475569"):
    """Same vector scale, stroke and palette as toolbar icons, also when spinning."""
    return icon("refresh", color, (frame % 30) * 12)


def availability_color(status):
    if status in ("Online", "Port open"):
        return "#22a06b"
    if status in ("Unreachable", "DNS error"):
        return "#d64a54"
    return "#94a3b8"


@lru_cache(maxsize=128)
def computer_icon(status="Not checked", frame=0):
    pixmap = QPixmap(96, 96)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#eaf1ff"))
    painter.drawRoundedRect(4, 4, 88, 88, 22, 22)
    painter.drawPixmap(23, 23, icon("monitor", "#3865c7").pixmap(50, 50))
    painter.setBrush(QColor("white"))
    painter.drawEllipse(71, 69, 23, 23)
    if status in ("Queued", "Checking…"):
        painter.drawPixmap(69, 67, refresh_icon(frame, "#3865c7").pixmap(27, 27))
    else:
        painter.setBrush(QColor(availability_color(status)))
        painter.drawEllipse(75, 73, 15, 15)
    painter.end()
    return QIcon(pixmap)


def apply_theme(app):
    QDir.setSearchPaths("viewericons", [str(Path(__file__).with_name("assets"))])
    app.setStyle("Fusion")
    palette = QPalette()
    for role, color in ((QPalette.ColorRole.Window, "#f5f7fb"), (QPalette.ColorRole.WindowText, "#253247"),
                        (QPalette.ColorRole.Base, "#ffffff"), (QPalette.ColorRole.AlternateBase, "#f6f8fc"),
                        (QPalette.ColorRole.Text, "#253247"), (QPalette.ColorRole.Button, "#ffffff"),
                        (QPalette.ColorRole.ButtonText, "#253247"), (QPalette.ColorRole.Highlight, "#dbe8ff"),
                        (QPalette.ColorRole.HighlightedText, "#234eac")):
        palette.setColor(role, QColor(color))
    app.setPalette(palette)
    app.setStyleSheet('''
        QMainWindow, QDialog { background: #f5f7fb; }
        QToolBar { background: #ffffff; border: none; spacing: 6px; padding: 7px; }
        QToolButton { border: none; border-radius: 7px; padding: 8px; }
        QToolButton:hover { background: #edf2fb; }
        QToolButton:checked { background: #dbe8ff; }
        QToolButton:disabled { background: transparent; }
        QLineEdit, QSpinBox, QComboBox { background: #fff; border: 1px solid #dbe1ec; border-radius: 7px; padding: 8px; }
        QLineEdit:focus { border-color: #6188da; }
        QPlainTextEdit#TerminalOutput { background: #111827; color: #dbeafe;
            border: 1px solid #263247; border-radius: 10px; padding: 12px;
            selection-background-color: #3865c7; selection-color: white; }
        QComboBox { padding-right: 32px; }
        QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: top right;
                              width: 28px; border: none; background: transparent; }
        QComboBox::down-arrow { image: url(viewericons:chevron-down.svg); width: 14px; height: 14px; }
        QComboBox QAbstractItemView { background: white; border: 1px solid #dbe1ec;
                                    padding: 4px; selection-background-color: #eaf1ff;
                                    selection-color: #234eac; outline: none; }
        QToolButton::menu-indicator { image: url(viewericons:chevron-down.svg);
                                     width: 12px; height: 12px; right: 1px; bottom: 1px; }
        QToolButton#AddButton { padding-right: 22px; }
        QToolButton#AddButton::menu-button { subcontrol-origin: border; subcontrol-position: right;
                                           width: 18px; border: none; border-radius: 6px; background: transparent; }
        QToolButton#AddButton::menu-button:hover { background: #c9d7ee; }
        QToolButton#AddButton::menu-button:pressed { background: #afc3e4; }
        QToolButton#AddButton::menu-arrow { image: url(viewericons:chevron-down.svg); width: 12px; height: 12px; }
        QSpinBox { padding-right: 28px; }
        QSpinBox::up-button { subcontrol-origin: border; subcontrol-position: top right;
                             width: 24px; border: none; background: transparent; }
        QSpinBox::down-button { subcontrol-origin: border; subcontrol-position: bottom right;
                               width: 24px; border: none; background: transparent; }
        QSpinBox::up-arrow { image: url(viewericons:chevron-up.svg); width: 12px; height: 12px; }
        QSpinBox::down-arrow { image: url(viewericons:chevron-down.svg); width: 12px; height: 12px; }
        QPushButton { border: 1px solid #dbe1ec; border-radius: 7px; padding: 8px 15px; background: white; }
        QPushButton:hover { background: #edf2fb; border-color: #b2c6eb; }
        QPushButton:default { background: #3865c7; color: white; border-color: #3865c7; }
        QTreeWidget, QListWidget { border: 1px solid #e2e7ef; border-radius: 10px; background: white; outline: none; }
        QListWidget::item { border-radius: 10px; padding: 6px; }
        QListWidget::item:selected { background: #eaf1ff; color: #234eac; }
        QTreeWidget::item { padding: 6px; }
        QHeaderView::section { background: #f6f8fc; color: #69768b; border: none; padding: 8px; }
        QHeaderView::up-arrow { image: url(viewericons:chevron-up.svg); width: 12px; height: 12px; }
        QHeaderView::down-arrow { image: url(viewericons:chevron-down.svg); width: 12px; height: 12px; }
        QMenu { background: #fff; border: 1px solid #dbe1ec; padding: 5px; }
        QMenu::item { padding: 8px 24px 8px 10px; border-radius: 5px; }
        QMenu::item:selected { background: #eaf1ff; color: #234eac; }
        QMenu::right-arrow { image: url(viewericons:chevron-right.svg); width: 12px; height: 12px; }
        QToolTip { background: #253247; color: white; border: none; padding: 6px; }
        QProgressBar { border: none; border-radius: 4px; background: #e5ecf8; min-height: 5px; max-height: 7px; }
        QProgressBar::chunk { background: #4b7df2; border-radius: 4px; }
        QStatusBar { color: #69768b; }
        QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
        QScrollBar::handle:vertical { background: #c8d3e3; min-height: 24px; border-radius: 3px; }
        QScrollBar::handle:vertical:hover { background: #9cacbf; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; border: none; }
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
        QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
        QScrollBar::handle:horizontal { background: #c8d3e3; min-width: 24px; border-radius: 3px; }
        QScrollBar::handle:horizontal:hover { background: #9cacbf; }
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; border: none; }
        QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }
        QLabel#Heading { font-size: 25px; font-weight: 600; color: #253247; }
    ''')
