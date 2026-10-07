"""Optional runtime tray lifecycle, independent of sessions and window closing."""
from PySide6.QtCore import QObject, QTimer, Qt, Signal
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import ui_icons


class SystemTrayController(QObject):
    """Own a tray icon until ``stop``; the caller owns graceful shutdown.

    Construction starts availability monitoring. Call ``hide_to_tray`` from the
    manager's close handler and use its boolean result to choose a close fallback.
    Connect ``quit_requested`` to graceful quit, call ``begin_shutdown`` when
    shutdown starts, and ``stop`` only once cleanup has finished.
    """

    quit_requested = Signal()
    availability_changed = Signal(bool)

    def __init__(self, window, *, tray_factory=QSystemTrayIcon,
                 availability=QSystemTrayIcon.isSystemTrayAvailable):
        super().__init__(window)
        self.window = window
        self._availability = availability
        self._available = False
        self._shutdown = False
        self._stopped = False
        self._quit_requested = False
        self._tooltip = QApplication.applicationDisplayName() or "Viewer"

        self.tray = tray_factory(self)
        application_icon = getattr(ui_icons, "application_icon", None)
        self.tray.setIcon(application_icon() if application_icon else ui_icons.icon("monitor"))
        self.tray.setToolTip(self._tooltip)
        self.menu = QMenu(window)
        self.menu.setStyleSheet("QMenu { border-radius: 8px; padding: 5px; }"
                                "QMenu::item { border-radius: 5px; padding: 8px 24px 8px 10px; }")
        self.open_action = self.menu.addAction("Open viewer")
        self.open_action.setIcon(application_icon() if application_icon else ui_icons.icon("monitor"))
        self.hide_action = self.menu.addAction("Hide viewer")
        self.hide_action.setIcon(ui_icons.icon("minimize"))
        self.menu.addSeparator()
        self.quit_action = self.menu.addAction("Quit")
        self.quit_action.setIcon(ui_icons.icon("disconnect"))
        self.open_action.triggered.connect(self.restore)
        self.hide_action.triggered.connect(self.hide_to_tray)
        self.quit_action.triggered.connect(self._request_quit)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._activated)

        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self.check_availability)
        self.check_availability()
        self._timer.start()

    @property
    def available(self):
        """Availability from the latest poll (hide always checks again)."""
        return self._available

    @property
    def can_hide(self):
        return self._available and not self._shutdown and not self._stopped

    def check_availability(self):
        """Refresh desktop support and recover a hidden manager if it vanishes."""
        if self._stopped:
            return
        was_available = self._available
        self._available = bool(self._availability())
        if self._available and not was_available:
            self.tray.show()
        if self._available != was_available:
            self.availability_changed.emit(self._available)
        self.hide_action.setEnabled(self.can_hide)
        if not self._available and not self.window.isVisible() and not self._shutdown:
            self.restore()

    def hide_to_tray(self):
        """Hide only when reachable through a currently available system tray."""
        if self._shutdown or self._stopped:
            return False
        self.check_availability()
        if not self.can_hide:
            return False
        self.window.hide()
        return True

    def restore(self):
        """Restore without discarding maximized/full-screen window state."""
        if self._shutdown or self._stopped:
            return
        state = self.window.windowState()
        if state & Qt.WindowState.WindowMinimized:
            self.window.setWindowState(state & ~Qt.WindowState.WindowMinimized)
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def _activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.restore()
        # QSystemTrayIcon owns native context-menu delivery through setContextMenu.

    def _request_quit(self):
        if self._quit_requested or self._shutdown or self._stopped:
            return
        self._quit_requested = True
        self.quit_action.setEnabled(False)
        self.quit_requested.emit()

    def begin_shutdown(self):
        """Disable interaction while keeping the icon registered during cleanup."""
        if self._stopped or self._shutdown:
            return
        self._shutdown = True
        self.open_action.setEnabled(False)
        self.hide_action.setEnabled(False)
        self.quit_action.setEnabled(False)
        self.menu.hide()
        self.tray.setToolTip(f"{self._tooltip} — Shutting down…")

    def stop(self):
        """End monitoring and remove the icon; safe to call repeatedly."""
        if self._stopped:
            return
        self.begin_shutdown()
        self._stopped = True
        self._available = False
        self._timer.stop()
        self.tray.activated.disconnect(self._activated)
        self.menu.hide()
        self.tray.setContextMenu(None)
        self.tray.hide()
