"""Bounded, window-scoped fullscreen dashboard; never consumes remote input."""
from PySide6.QtCore import QEvent, QPoint, QPropertyAnimation, QEasingCurve, QTimer, Qt, QSize
from PySide6.QtWidgets import QApplication, QToolBar, QToolButton, QStyle
from PySide6.QtGui import QCursor

try:
    from .ui_icons import icon
except ImportError:
    def icon(name):
        return QApplication.style().standardIcon({
            "disconnect": QStyle.StandardPixmap.SP_DialogCloseButton,
            "fullscreen": QStyle.StandardPixmap.SP_TitleBarMaxButton,
            "retry": QStyle.StandardPixmap.SP_BrowserReload,
        }.get(name, QStyle.StandardPixmap.SP_ComputerIcon))


def compact_toolbar(toolbar):
    toolbar.setMovable(False)
    toolbar.setFloatable(False)
    toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
    toolbar.setIconSize(QSize(20, 20))
    toolbar.setAccessibleName("Session controls")


def action_button(toolbar, action, menu=None):
    button = QToolButton(toolbar)
    button.setDefaultAction(action)
    button.setAccessibleName(action.text())
    button.setToolTip(action.toolTip())
    button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
    if menu is not None:
        button.setMenu(menu)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    toolbar.addWidget(button)
    return button


class FullscreenControls(QToolBar):
    """Child overlay with a six-pixel reveal lip and one reusable animation."""
    def __init__(self, window, actions, menus):
        super().__init__("Fullscreen session controls", window)
        self.setObjectName("FullscreenDashboard")
        self.setStyleSheet("QToolBar#FullscreenDashboard { border: 1px solid #c7d6ef; border-bottom: 3px solid #6188da; border-radius: 10px; padding: 7px 12px; background: #ffffff; }")
        self.window_owner = window
        self.active = False
        self.revealed = True
        self.disposed = False
        self.open_menus = set()
        compact_toolbar(self)
        self.setMouseTracking(True)
        for action, menu in actions:
            action_button(self, action, menu)
        from PySide6.QtGui import QAction
        self.pin_action = QAction(icon("pin"), "Pin fullscreen controls", self)
        self.pin_action.setCheckable(True)
        self.pin_action.toggled.connect(self.set_pinned)
        self.pin_button = action_button(self, self.pin_action)
        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.setInterval(1500)
        self.hide_timer.timeout.connect(self.hide_dashboard)
        self.animation = QPropertyAnimation(self, b"pos", self)
        self.animation.setDuration(180)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        for menu in menus:
            menu.aboutToShow.connect(lambda m=menu: self.menu_opened(m))
            menu.aboutToHide.connect(lambda m=menu: self.menu_closed(m))
        QApplication.instance().installEventFilter(self)
        self.hide()

    def set_active(self, active):
        self.active = active
        self.animation.stop()
        self.hide_timer.stop()
        self.setVisible(active)
        if active:
            self.revealed = True
            self.reposition()
            self.raise_()
            self.arm()

    def destination(self):
        return QPoint((self.window_owner.width() - self.width()) // 2,
                      0 if self.revealed else 6 - self.height())

    def reposition(self):
        self.animation.stop()
        self.resize(min(self.sizeHint().width(), self.window_owner.width()), self.sizeHint().height())
        self.move(self.destination())

    def slide(self, revealed):
        self.revealed = revealed
        self.animation.stop()
        self.animation.setStartValue(self.pos())
        self.animation.setEndValue(self.destination())
        self.animation.start()

    def arm(self):
        if self.active and not self.pin_action.isChecked() and not self.open_menus:
            self.hide_timer.start()

    def reveal(self):
        if self.active:
            if not self.revealed:
                self.slide(True)
            self.arm()

    def hide_dashboard(self):
        if self.active and not self.pin_action.isChecked() and not self.open_menus:
            if self.isVisible() and self.rect().contains(self.mapFromGlobal(QCursor.pos())):
                self.arm()
                return
            self.slide(False)

    def set_pinned(self, pinned):
        self.pin_action.setIcon(icon("unpin" if pinned else "pin"))
        self.pin_action.setText("Unpin fullscreen controls" if pinned else "Pin fullscreen controls")
        self.pin_button.setAccessibleName(self.pin_action.text())
        self.hide_timer.stop()
        self.reveal()

    def menu_opened(self, menu):
        self.open_menus.add(menu)
        self.hide_timer.stop()
        self.reveal()

    def menu_closed(self, menu):
        self.open_menus.discard(menu)
        self.arm()

    def eventFilter(self, watched, event):
        if self.active:
            if watched is self.window_owner and event.type() == QEvent.Type.Resize:
                self.reposition()
            elif event.type() == QEvent.Type.MouseMove and hasattr(watched, "window") and watched.window() is self.window_owner:
                point = self.window_owner.mapFromGlobal(event.globalPosition().toPoint())
                if point.y() < 6 or self.geometry().contains(point):
                    self.reveal()
        return False

    def dispose(self):
        if not self.disposed:
            self.disposed = True
            self.active = False
            self.hide_timer.stop()
            self.animation.stop()
            QApplication.instance().removeEventFilter(self)
