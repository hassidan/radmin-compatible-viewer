"""Session presentation and single-owner worker integration boundary."""
from __future__ import annotations

import queue
import threading

from PySide6.QtCore import QThread, Signal, Slot, Qt, QSize, QTimer, QEvent
from PySide6.QtGui import QImage, QPainter, QAction, QShortcut, QKeySequence
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QScrollArea, QLabel,
                               QToolBar, QMenu, QStackedWidget, QVBoxLayout, QProgressBar,
                               QPushButton)

from .protocol import RadminSession, AuthenticationError, RadminTimeout, TransportError, UnsupportedProtocolError
from .clipboard_ui import ClipboardController
from .ui_dialogs import RoundedMenu as QMenu
from .power_ui import create_power_menu
from .ui_icons import application_icon
from .fullscreen_controls import FullscreenControls, compact_toolbar, action_button, icon


class ConnectionWorker(QThread):
    """All session and optional adapter calls happen on this worker thread.

    adapter_factory(session, mode) -> adapter; adapter.poll(timeout) returns
    QImage or None; adapter.handle_input(dict); adapter.close(); adapter.can_control.
    poll must return within timeout, or raise on disconnection. No Qt widgets here.
    """
    status = Signal(str)
    frame_ready = Signal(QImage)
    capabilities = Signal(bool)
    failed = Signal(str)
    authentication_failed = Signal(str)
    authenticated = Signal()
    remote_closed = Signal(str)
    clipboard_reply = Signal(int, object)

    def __init__(self, entry, password, timeout, mode="view", adapter_factory=None, parent=None):
        super().__init__(parent)
        self.entry = dict(entry)
        self.password = password
        self.timeout = timeout
        self.mode = mode
        self.adapter_factory = adapter_factory
        self.stop_event = threading.Event()
        self.inputs = queue.Queue(maxsize=256)
        self.frame_consumed = threading.Event()
        self.frame_consumed.set()

    def stop(self):
        self.stop_event.set()

    def enqueue_input(self, event):
        if not self.stop_event.is_set():
            try:
                self.inputs.put_nowait(dict(event))
            except queue.Full:
                # Never silently lose a key release and leave a remote key held.
                self.stop()

    def run(self):
        session = adapter = None
        try:
            if self.stop_event.is_set():
                return
            self.status.emit("Connecting and authenticating…")
            session = RadminSession(self.entry["host"], self.entry["port"],
                                    self.entry["username"], self.password, self.timeout)
            self.password = ""
            session.connect()
            if self.stop_event.is_set():
                return
            if not session.authenticated:
                raise AuthenticationError()
            self.authenticated.emit()
            if self.adapter_factory is None:
                self.status.emit("Authenticated · Desktop transport is not integrated. No remote image or input is active.")
                self.stop_event.wait()
            else:
                self.status.emit("Authenticated · Starting desktop transport…")
                adapter = self.adapter_factory(session, self.mode)
                self.capabilities.emit(self.mode == "control" and bool(adapter.can_control))
                pending_image = None
                while not self.stop_event.is_set():
                    for _ in range(64):
                        try:
                            event = self.inputs.get_nowait()
                        except queue.Empty:
                            break
                        if event["type"] == "show_cursor":
                            if hasattr(adapter, "set_show_cursor"):
                                adapter.set_show_cursor(event["enabled"])
                        elif self.mode == "control" and adapter.can_control:
                            adapter.handle_input(event)
                    image = adapter.poll(0.05)
                    if hasattr(adapter, "take_clipboard_replies"):
                        for request_id, text in adapter.take_clipboard_replies():
                            self.clipboard_reply.emit(request_id, text)
                    if image is not None:
                        if not isinstance(image, QImage) or image.isNull():
                            raise ValueError("Invalid frame")
                        pending_image = image.copy()
                    if pending_image is not None and self.frame_consumed.is_set():
                        self.frame_consumed.clear()
                        self.frame_ready.emit(pending_image)
                        pending_image = None
        except Exception as error:
            if not self.stop_event.is_set():
                message = "Session failed. Check the server and protocol compatibility."
                if isinstance(error, AuthenticationError):
                    message = "Authentication rejected or server proof invalid."
                    self.authentication_failed.emit(message)
                    return
                elif isinstance(error, RadminTimeout):
                    message = "Connection/authentication timed out."
                elif isinstance(error, UnsupportedProtocolError):
                    message = "The server sent an unsupported protocol or desktop update."
                elif isinstance(error, TransportError):
                    message = "Network connection failed or was closed by the server."
                    self.remote_closed.emit(message)
                    return
                self.failed.emit(message)  # Never expose arbitrary exception text/credentials.
        finally:
            self.password = ""
            self.capabilities.emit(False)
            try:
                if adapter is not None:
                    adapter.close()
            except Exception:
                self.failed.emit("Desktop adapter cleanup failed; closing the underlying session.")
            finally:
                if session is not None:
                    try:
                        session.close()
                    except Exception:
                        self.failed.emit("Session cleanup failed.")


class Framebuffer(QWidget):
    input_event = Signal(dict)

    def __init__(self):
        super().__init__()
        self.image = QImage()
        self.scaling = "fit"
        self.control = False
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

    def sizeHint(self):
        return self.image.size() if not self.image.isNull() else QSize(800, 500)

    def target(self):
        if self.image.isNull():
            return self.rect()
        size = self.image.size()
        if self.scaling == "fit":
            size.scale(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        from PySide6.QtCore import QRect
        return QRect((self.width() - size.width()) // 2, (self.height() - size.height()) // 2,
                     size.width(), size.height())

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.GlobalColor.black)
        if self.image.isNull():
            painter.setPen(Qt.GlobalColor.lightGray)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "No remote framebuffer received")
        else:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(self.target(), self.image)

    def pointer(self, event, kind):
        if not self.control or self.image.isNull():
            return
        rect = self.target()
        point = event.position()
        # Clamp releases outside the image so a drag cannot leave buttons held.
        x = max(0, min(self.image.width() - 1, int((point.x() - rect.x()) * self.image.width() / max(1, rect.width()))))
        y = max(0, min(self.image.height() - 1, int((point.y() - rect.y()) * self.image.height() / max(1, rect.height()))))
        payload = {"type": kind, "x": x, "y": y, "modifiers": event.modifiers().value}
        if kind == "wheel":
            payload.update(dx=event.angleDelta().x(), dy=event.angleDelta().y())
        else:
            payload.update(button=event.button().value, buttons=event.buttons().value)
        self.input_event.emit(payload)

    def mousePressEvent(self, event):
        self.setFocus()
        self.pointer(event, "pointer_press")

    def mouseReleaseEvent(self, event):
        self.pointer(event, "pointer_release")

    def mouseMoveEvent(self, event):
        self.pointer(event, "pointer_move")

    def wheelEvent(self, event):
        self.pointer(event, "wheel")

    def key(self, event, kind):
        if self.control and not self.image.isNull():
            self.input_event.emit({"type": kind, "key": event.key(), "text": event.text(),
                                   "modifiers": event.modifiers().value,
                                   "scan_code": event.nativeScanCode(), "auto_repeat": event.isAutoRepeat()})

    def keyPressEvent(self, event):
        local_keys = getattr(self.window(), "local_keys", set())
        if event.key() in local_keys:
            if event.isAutoRepeat():
                return
            # A shortcut release may have gone to a toolbar button instead.
            # A fresh press must never lose its corresponding remote release.
            local_keys.discard(event.key())
        self.key(event, "key_press")

    def keyReleaseEvent(self, event):
        if event.key() in getattr(self.window(), "local_keys", set()):
            self.window().local_keys.discard(event.key())
            return
        self.key(event, "key_release")

    def focusOutEvent(self, event):
        if self.control:
            self.input_event.emit({"type": "release_all"})
        super().focusOutEvent(event)


class SessionWindow(QMainWindow):
    retry_requested = Signal(object, str, str)
    authenticated = Signal()
    session_error = Signal(str)
    power_requested = Signal(object, str)
    terminal_requested = Signal(object)
    transfer_requested = Signal(object)

    def __init__(self, entry, password, settings, mode="view", adapter_factory=None):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowIcon(application_icon())
        self.setWindowTitle(f"{entry['name']} — {'Control' if mode == 'control' else 'View'}")
        self.resize(1000, 700)
        self.closing = False
        self.failed_session = False
        self.entry, self.mode = dict(entry), mode
        self.active_session = False
        self.worker_stopped = False
        self.retry_pending = False
        self.retry_emitted = False
        self.error_message = ""
        self.local_keys = set()
        self.surface = Framebuffer()
        self.surface.show_remote_cursor = True
        self.scroll = QScrollArea()
        self.scroll.viewport().setMouseTracking(True)
        self.scroll.setWidget(self.surface)
        self.pages = QStackedWidget()
        self.pages.addWidget(self.scroll)
        self.panel = QWidget()
        panel_layout = QVBoxLayout(self.panel)
        panel_layout.addStretch()
        card = QWidget()
        card.setObjectName("ConnectionCard")
        card.setMaximumWidth(420)
        card.setMinimumWidth(300)
        card.setStyleSheet("QWidget#ConnectionCard { background: white; border: 1px solid #e2e7ef; border-radius: 14px; }")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(16)
        mark = QLabel()
        mark.setPixmap(icon("monitor").pixmap(48, 48))
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(mark)
        target = QLabel(entry["name"])
        target.setTextFormat(Qt.TextFormat.PlainText)
        target.setWordWrap(True)
        target.setAlignment(Qt.AlignmentFlag.AlignCenter)
        target.setStyleSheet("font-size: 20px; font-weight: 600;")
        layout.addWidget(target)
        self.loader_label = QLabel("Connecting and authenticating…")
        self.loader_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loader_label.setWordWrap(True)
        self.loader_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.loader_label)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setAccessibleName("Connecting to remote desktop")
        layout.addWidget(self.progress)
        self.retry_button = QPushButton(icon("retry"), "Retry")
        self.retry_button.clicked.connect(self.request_retry)
        layout.addWidget(self.retry_button)
        self.retry_button.hide()
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.disconnect)
        layout.addWidget(self.cancel_button)
        panel_layout.addWidget(card, 0, Qt.AlignmentFlag.AlignHCenter)
        panel_layout.addStretch()
        self.pages.addWidget(self.panel)
        self.pages.setCurrentWidget(self.panel)
        self.setCentralWidget(self.pages)
        toolbar = self.toolbar = QToolBar("Session")
        compact_toolbar(toolbar)
        self.addToolBar(toolbar)
        self.disconnect_action = QAction(icon("disconnect"), "Disconnect", self)
        self.disconnect_action.triggered.connect(self.disconnect)
        fit = self.fit_action = QAction(icon("fit"), "Fit to window", self, checkable=True)
        fit.setChecked(settings["scaling"] == "fit")
        fit.toggled.connect(self.set_scaling)
        self.fullscreen_action = QAction(icon("fullscreen"), "Fullscreen (F11)", self)
        self.fullscreen_action.triggered.connect(self.toggle_fullscreen)
        self.fullscreen_shortcut = QShortcut(QKeySequence("F11"), self)
        self.fullscreen_shortcut.setAutoRepeat(False)
        self.fullscreen_shortcut.activated.connect(self.fullscreen_key)
        self.escape_shortcut = QShortcut(QKeySequence("Escape"), self)
        self.escape_shortcut.setAutoRepeat(False)
        self.escape_shortcut.setEnabled(False)
        self.escape_shortcut.activated.connect(self.escape_fullscreen)
        self.cursor_action = QAction(icon("cursor"), "Show remote cursor", self, checkable=True)
        self.cursor_action.setChecked(True)
        self.cursor_action.toggled.connect(self.set_remote_cursor)
        self.clipboard_menu = QMenu("Clipboard", self)
        self.send_clipboard_action = self.clipboard_menu.addAction(icon("upload"), "Send clipboard")
        self.receive_clipboard_action = self.clipboard_menu.addAction(icon("download"), "Receive clipboard")
        self.auto_clipboard_action = QAction(icon("clipboard"), "Share clipboard automatically", self, checkable=True)
        self.auto_clipboard_action.setToolTip("Text only. Enabling sends current local text; then shares changes in both directions.")
        self.clipboard_menu.addAction(self.auto_clipboard_action)
        self.clipboard_action = QAction(icon("clipboard"), "Clipboard", self)
        self.display_menu = QMenu("Display options", self)
        self.display_menu.addAction(fit)
        self.display_menu.addAction(self.cursor_action)
        self.display_action = QAction(icon("settings"), "Display options", self)
        self.power_menu = create_power_menu(self, lambda action: self.power_requested.emit(dict(self.entry), action.value))
        self.power_action = QAction(icon("power"), "Power options", self)
        self.power_action.setEnabled(False)
        self.terminal_action = QAction(icon("terminal"), "Open terminal", self)
        self.terminal_action.setEnabled(False)
        self.terminal_action.triggered.connect(lambda: self.terminal_requested.emit(dict(self.entry)))
        self.transfer_action = QAction(icon("folder"), "Open file transfer", self)
        self.transfer_action.setEnabled(False)
        self.transfer_action.triggered.connect(lambda: self.transfer_requested.emit(dict(self.entry)))
        controls = [(self.display_action, self.display_menu), (self.clipboard_action, self.clipboard_menu),
                    (self.terminal_action, None),
                    (self.transfer_action, None),
                    (self.power_action, self.power_menu),
                    (self.fullscreen_action, None), (self.disconnect_action, None)]
        for action, menu in controls:
            action_button(toolbar, action, menu)
        self.fullscreen_controls = FullscreenControls(self, controls, [self.display_menu, self.clipboard_menu, self.power_menu])
        self.clipboard_actions = (self.send_clipboard_action, self.receive_clipboard_action, self.auto_clipboard_action)
        for action in self.clipboard_actions:
            action.setEnabled(False)
        self.status_label = QLabel("Starting…")
        self.status_label.setWordWrap(True)
        self.statusBar().addWidget(self.status_label, 1)
        self.worker = ConnectionWorker(entry, password, settings["timeout"], mode, adapter_factory, self)
        self.clipboard_controller = ClipboardController(QApplication.clipboard(), self.worker.enqueue_input,
                                                        self.clipboard_status, self)
        self.send_clipboard_action.triggered.connect(self.clipboard_controller.send_local)
        self.receive_clipboard_action.triggered.connect(self.clipboard_controller.receive_remote)
        self.auto_clipboard_action.toggled.connect(self.clipboard_controller.set_automatic)
        self.clipboard_controller.automatic_changed.connect(self.auto_clipboard_action.setChecked)
        self.worker.clipboard_reply.connect(self.clipboard_controller.received)
        self.clipboard_label = QLabel()
        self.clipboard_label.setTextFormat(Qt.TextFormat.PlainText)
        self.statusBar().addWidget(self.clipboard_label)
        self.worker.status.connect(self.set_status)
        self.worker.failed.connect(self.failure)
        self.worker.authentication_failed.connect(self.authentication_failure)
        self.worker.authenticated.connect(self.authenticated)
        self.worker.authenticated.connect(self.enable_power)
        self.worker.remote_closed.connect(self.remote_closed)
        self.worker.frame_ready.connect(self.deliver_frame)
        self.worker.capabilities.connect(self.set_control)
        self.worker.finished.connect(self.finished)
        self.surface.input_event.connect(self.worker.enqueue_input)
        self.set_scaling(fit.isChecked())
        self.start_timer = QTimer(self)
        self.start_timer.setSingleShot(True)
        self.start_timer.timeout.connect(self.start_worker)
        self.start_timer.start(0)
        if settings["fullscreen"]:
            self.showFullScreen()

    @Slot(QImage)
    def deliver_frame(self, image):
        if not self.worker.stop_event.is_set() and not self.failed_session:
            self.active_session = True
            self.pages.setCurrentWidget(self.scroll)
            self.surface.image = image.copy()
            self.set_scaling(self.surface.scaling == "fit")
            self.status_label.setText("Desktop active · " + ("Control enabled" if self.surface.control else "View only"))
        self.worker.frame_consumed.set()

    @Slot(bool)
    def set_control(self, enabled):
        enabled = enabled and not (self.closing or self.retry_pending or self.failed_session
                                   or self.worker.stop_event.is_set())
        self.surface.control = enabled
        self.surface.setCursor(Qt.CursorShape.BlankCursor if enabled and self.cursor_action.isChecked() else Qt.CursorShape.ArrowCursor)
        self.clipboard_controller.set_available(enabled)
        for action in self.clipboard_actions:
            action.setEnabled(enabled)

    def clipboard_status(self, message):
        self.clipboard_label.setText(message)

    def enable_power(self):
        enabled = not self.closing and not self.failed_session and not self.worker.stop_event.is_set()
        self.power_action.setEnabled(enabled)
        self.terminal_action.setEnabled(enabled)
        self.transfer_action.setEnabled(enabled)

    def set_remote_cursor(self, enabled):
        self.surface.show_remote_cursor = enabled
        self.surface.setCursor(Qt.CursorShape.BlankCursor if enabled and self.surface.control else Qt.CursorShape.ArrowCursor)
        self.worker.enqueue_input({"type": "show_cursor", "enabled": enabled})

    def set_scaling(self, fit):
        self.surface.scaling = "fit" if fit else "actual"
        self.scroll.setWidgetResizable(fit)
        self.surface.setMinimumSize(0, 0)
        if not fit:
            self.surface.resize(self.surface.sizeHint())
        self.surface.update()

    def toggle_fullscreen(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def fullscreen_key(self):
        self.local_keys.add(Qt.Key.Key_F11)
        self.toggle_fullscreen()

    def escape_fullscreen(self):
        self.local_keys.add(Qt.Key.Key_Escape)
        self.showNormal()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and hasattr(self, "fullscreen_controls"):
            fullscreen = self.isFullScreen()
            self.toolbar.setVisible(not fullscreen)
            self.statusBar().setVisible(not fullscreen)
            self.escape_shortcut.setEnabled(fullscreen)
            self.fullscreen_controls.set_active(fullscreen)

    def start_worker(self):
        if not self.closing and not self.worker.stop_event.is_set():
            self.worker.start()

    def set_status(self, message):
        if not self.closing and not self.failed_session:
            self.status_label.setText(message)
            self.loader_label.setText(message + "\nWaiting for the initial desktop frame…")

    def failure(self, message):
        self.failed_session = True
        self.power_action.setEnabled(False)
        self.terminal_action.setEnabled(False)
        self.transfer_action.setEnabled(False)
        self.set_control(False)
        self.error_message = message
        self.status_label.setText(message)
        self.loader_label.setText(message)
        self.progress.hide()
        self.retry_button.show()
        self.cancel_button.setText("Close")
        self.pages.setCurrentWidget(self.panel)

    def authentication_failure(self, message):
        self.failure(message)
        self.request_retry()

    def remote_closed(self, message):
        if self.active_session:
            self.session_error.emit(message)
            self.closing = True
        else:
            self.failure(message)

    def request_retry(self):
        if self.retry_pending or self.retry_emitted or self.closing:
            return
        self.retry_pending = True
        self.retry_button.setEnabled(False)
        self.worker.stop()
        if self.worker_stopped:
            self.complete_retry()

    def complete_retry(self):
        if not self.retry_emitted:
            self.retry_emitted = True
            self.retry_requested.emit(dict(self.entry), self.mode, self.error_message)
        self.close()

    def disconnect(self):
        self.closing = True
        self.power_action.setEnabled(False)
        self.terminal_action.setEnabled(False)
        self.transfer_action.setEnabled(False)
        self.start_timer.stop()
        self.set_control(False)
        self.disconnect_action.setEnabled(False)
        self.status_label.setText("Disconnecting… Waiting for the current network operation to finish.")
        self.loader_label.setText(self.status_label.text())
        self.cancel_button.setEnabled(False)
        self.worker.stop()
        if not self.worker.isRunning():
            self.finished()

    def finished(self):
        # finished is queued to the GUI; join the last QThread teardown before
        # handing a retry to main (which may immediately create another worker).
        self.worker.wait()
        self.worker_stopped = True
        self.power_action.setEnabled(False)
        self.terminal_action.setEnabled(False)
        self.transfer_action.setEnabled(False)
        self.set_control(False)
        self.clipboard_controller.close()
        self.disconnect_action.setEnabled(False)
        if not self.failed_session:
            self.status_label.setText("Disconnected")
        if self.retry_pending:
            self.complete_retry()
        elif self.closing or not self.failed_session:
            self.close()

    def closeEvent(self, event):
        self.start_timer.stop()
        self.closing = True
        self.worker.stop()
        if self.worker.isRunning():
            self.disconnect()
            event.ignore()
        else:
            self.worker.password = ""
            self.clipboard_controller.close()
            self.fullscreen_controls.dispose()
            event.accept()
