"""Compact, local window chrome with native move/resize and portable fallbacks."""
from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QMainWindow, QSizePolicy, QToolButton,
    QWidget,
)

from .ui_icons import icon


def _system_operation(window, operation, *args):
    """Some platform plugins expose the API but cannot implement it."""
    handle = window.windowHandle()
    if handle is None:
        return False
    try:
        return bool(getattr(handle, operation)(*args))
    except (AttributeError, NotImplementedError, RuntimeError):
        return False


class _DragArea(QWidget):
    def __init__(self, window, parent):
        super().__init__(parent)
        self._window = window
        self._press = None
        self._offset = QPoint()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(24)
        self.setAccessibleName("Window title; drag to move")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 8, 0)
        self.logo = QLabel(self)
        self.logo.setFixedSize(20, 20)
        self.logo.setPixmap(window.windowIcon().pixmap(20, 20))
        self.logo.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        window.windowIconChanged.connect(lambda image: self.logo.setPixmap(image.pixmap(20, 20)))
        layout.addWidget(self.logo)
        self.title = QLabel(window.windowTitle(), self)
        self.title.setMinimumWidth(0)
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.title)
        window.windowTitleChanged.connect(self.title.setText)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self._window.isFullScreen():
            return super().mousePressEvent(event)
        self._press = event.globalPosition().toPoint()
        self._offset = self._press - self._window.pos()
        # A maximized window must restore only once an actual drag begins.
        if not self._window.isMaximized():
            if _system_operation(self._window, "startSystemMove"):
                self._press = None
        event.accept()

    def mouseMoveEvent(self, event):
        if self._press is None or not event.buttons() & Qt.MouseButton.LeftButton:
            return super().mouseMoveEvent(event)
        window = self._window
        if window.isFullScreen():
            self._press = None
            return
        point = event.globalPosition().toPoint()
        if window.isMaximized():
            if (point - self._press).manhattanLength() < QApplication.startDragDistance():
                return
            fraction = self._offset.x() / max(1, window.width())
            window.showNormal()
            self._offset = QPoint(round(window.width() * fraction), self._offset.y())
            self._move_bounded(point - self._offset)
            if _system_operation(window, "startSystemMove"):
                self._press = None
                return
        self._move_bounded(point - self._offset)
        event.accept()

    def _move_bounded(self, position):
        window = self._window
        screen = QApplication.screenAt(position + self._offset) or window.screen()
        if screen is not None:
            area = screen.availableGeometry()
            # Keep a usable piece of the title strip reachable, even for windows
            # larger than the screen; negative multi-monitor coordinates are valid.
            visible = min(80, window.width(), area.width())
            position.setX(max(area.left() - window.width() + visible,
                              min(position.x(), area.right() - visible + 1)))
            position.setY(max(area.top(), min(position.y(), area.bottom() - self.height() + 1)))
        window.move(position)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = None
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = None
            self._window.toggle_maximized()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def hideEvent(self, event):
        self._press = None
        super().hideEvent(event)


class _ResizeGrip(QWidget):
    def __init__(self, window, edges, cursor):
        super().__init__(window)
        self._window = window
        self.edges = edges
        self._press = None
        self.setCursor(cursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def mousePressEvent(self, event):
        window = self._window
        if (event.button() != Qt.MouseButton.LeftButton or
                window.isMaximized() or window.isFullScreen()):
            return super().mousePressEvent(event)
        self._press = None
        if not _system_operation(window, "startSystemResize", self.edges):
            self._press = event.globalPosition().toPoint()
            self._geometry = window.geometry()
        event.accept()

    def mouseMoveEvent(self, event):
        window = self._window
        if window.isMaximized() or window.isFullScreen():
            self._press = None
        if self._press is None or not event.buttons() & Qt.MouseButton.LeftButton:
            return super().mouseMoveEvent(event)
        delta = event.globalPosition().toPoint() - self._press
        rect = QRect(self._geometry)
        minimum = window.minimumSize().expandedTo(window.minimumSizeHint())
        maximum = window.maximumSize()
        left = bool(self.edges & Qt.Edge.LeftEdge)
        top = bool(self.edges & Qt.Edge.TopEdge)
        if self.edges & (Qt.Edge.LeftEdge | Qt.Edge.RightEdge):
            width = max(minimum.width(), min(maximum.width(),
                        rect.width() + (-delta.x() if left else delta.x())))
            if left:
                rect.setLeft(rect.right() - width + 1)
            else:
                rect.setWidth(width)
        if self.edges & (Qt.Edge.TopEdge | Qt.Edge.BottomEdge):
            height = max(minimum.height(), min(maximum.height(),
                         rect.height() + (-delta.y() if top else delta.y())))
            if top:
                rect.setTop(rect.bottom() - height + 1)
            else:
                rect.setHeight(height)
        window.setGeometry(rect)
        event.accept()

    def mouseReleaseEvent(self, event):
        self._press = None
        super().mouseReleaseEvent(event)

    def hideEvent(self, event):
        self._press = None
        super().hideEvent(event)


class _WindowChrome(QWidget):
    def __init__(self, window, menu_bar):
        super().__init__(window)
        self.setObjectName("WindowChrome")
        self.setStyleSheet("""
            QWidget#WindowChrome { background: #f5f7fb; }
            QWidget#WindowChrome QLabel { color: #69768b; background: transparent; }
            QWidget#WindowChrome QMenuBar { background: transparent; border: none; }
            QWidget#WindowChrome QToolButton {
                background: transparent; border: none; border-radius: 4px; padding: 0;
            }
            QWidget#WindowChrome QToolButton:hover { background: #e5ecf8; }
            QWidget#WindowChrome QToolButton:pressed { background: #dbe8ff; }
            QWidget#WindowChrome QToolButton:focus { border: 1px solid #6188da; }
            QWidget#WindowChrome QToolButton#WindowClose:hover { background: #fbe2e5; }
        """)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 2)
        layout.setSpacing(0)
        menu_bar.setNativeMenuBar(False)
        layout.addWidget(menu_bar)
        self.drag_area = _DragArea(window, self)
        layout.addWidget(self.drag_area, 1)
        self.minimize_button = self._button("minimize", "Minimize", layout)
        self.maximize_button = self._button("maximize", "Maximize", layout)
        self.close_button = self._button("close", "Close", layout)
        self.close_button.setObjectName("WindowClose")
        self.minimize_button.clicked.connect(window.showMinimized)
        self.maximize_button.clicked.connect(window.toggle_maximized)
        # Defer until the button event finishes, and cancel automatically if the
        # header is destroyed. Calling close() honors the subclass's closeEvent.
        self._close_timer = QTimer(self)
        self._close_timer.setSingleShot(True)
        self._close_timer.timeout.connect(window.close)
        self.close_button.clicked.connect(lambda: self._close_timer.start(0))

    def _button(self, name, label, layout):
        button = QToolButton(self)
        button.setIcon(icon(name))
        button.setIconSize(QSize(16, 16))
        button.setFixedSize(34, 28)
        button.setToolTip(label)
        button.setAccessibleName(label)
        button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        layout.addWidget(button)
        return button

    def sync_state(self, window):
        restore = window.isMaximized()
        label = "Restore" if restore else "Maximize"
        self.maximize_button.setIcon(icon("restore" if restore else "maximize"))
        self.maximize_button.setToolTip(label)
        self.maximize_button.setAccessibleName(label)
        self.maximize_button.setEnabled(not window.isFullScreen())


class FramelessMainWindow(QMainWindow):
    """Call install_chrome() after creating menus; menuBar() remains usable.

    ``chrome`` exposes minimize_button, maximize_button, close_button and
    drag_area. ``toggle_maximized()`` is also available on this window.
    """

    def menuBar(self):
        # QMainWindow.menuBar() creates/replaces the menu widget when it is not a
        # QMenuBar. Keep the original accessor contract after embedding it.
        menu = getattr(self, "_chrome_menu_bar", None)
        return menu if menu is not None else super().menuBar()

    def install_chrome(self):
        """Install once, preserving menu objects, actions, shortcuts and focus."""
        if getattr(self, "chrome", None) is not None:
            return self.chrome
        visible = self.isVisible()
        state = self.windowState()
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self._chrome_menu_bar = super().menuBar()
        # Reparent before setMenuWidget: otherwise Qt schedules the old bar for
        # deletion. The header's layout takes ownership of it immediately.
        self._chrome_menu_bar.setParent(None)
        self.chrome = _WindowChrome(self, self._chrome_menu_bar)
        self.setMenuWidget(self.chrome)
        E, C = Qt.Edge, Qt.CursorShape
        specs = (
            (E.LeftEdge, C.SizeHorCursor), (E.RightEdge, C.SizeHorCursor),
            (E.TopEdge, C.SizeVerCursor), (E.BottomEdge, C.SizeVerCursor),
            (E.TopEdge | E.LeftEdge, C.SizeFDiagCursor),
            (E.TopEdge | E.RightEdge, C.SizeBDiagCursor),
            (E.BottomEdge | E.LeftEdge, C.SizeBDiagCursor),
            (E.BottomEdge | E.RightEdge, C.SizeFDiagCursor),
        )
        self._resize_grips = [_ResizeGrip(self, edges, cursor) for edges, cursor in specs]
        self._sync_chrome()
        if visible:
            self.setWindowState(state)
            self.show()
        return self.chrome

    def toggle_maximized(self):
        if not self.isFullScreen():
            self.showNormal() if self.isMaximized() else self.showMaximized()

    def _sync_chrome(self):
        if getattr(self, "chrome", None) is None:
            return
        self.chrome.sync_state(self)
        w, h, edge, corner = self.width(), self.height(), 4, 10
        rects = (
            (0, corner, edge, max(0, h - 2 * corner)),
            (w - edge, corner, edge, max(0, h - 2 * corner)),
            (corner, 0, max(0, w - 2 * corner), edge),
            (corner, h - edge, max(0, w - 2 * corner), edge),
            (0, 0, corner, corner), (w - corner, 0, corner, corner),
            (0, h - corner, corner, corner), (w - corner, h - corner, corner, corner),
        )
        enabled = not (self.isMaximized() or self.isFullScreen())
        for grip, rect in zip(getattr(self, "_resize_grips", ()), rects):
            grip.setGeometry(*rect)
            grip.setEnabled(enabled)
            grip.setVisible(enabled)
            grip.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_chrome()

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_chrome()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._sync_chrome()
