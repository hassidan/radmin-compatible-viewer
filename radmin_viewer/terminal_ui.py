"""Line-oriented remote command shell; no VT/PTY or fullscreen TUI emulation.

TerminalWindow(entry, password, settings) defers startup one GUI turn. Connect
authenticated, retry_requested(entry, 'terminal', message), session_error, and
power_requested(entry, action.value) before that turn. Close waits asynchronously
for worker cleanup. A new window always starts with empty command/history queues.
"""
from __future__ import annotations

import codecs
import queue
import threading

from PySide6.QtCore import Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QProgressBar, QToolButton,
)

from .auxiliary import TelnetClient
from .channel import EncryptedChannel
from .protocol import AuthenticationError, RadminSession
from .power_ui import create_power_menu
from .ui_dialogs import StyledComboBox
from .ui_icons import icon, application_icon
from .terminal_editor import TerminalEditor


ENCODINGS = (("CP437", "cp437"), ("UTF-8", "utf-8"),
             ("CP862", "cp862"), ("CP1252", "cp1252"))
AUTH_MESSAGE = "Authentication failed. Verify the terminal login."


class TerminalWorker(QThread):
    authenticated = Signal()
    ready = Signal()

    def __init__(self, entry, password, timeout, parent=None):
        super().__init__(parent)
        self.entry = dict(entry)
        self.password = password
        self.timeout = timeout
        self.cancel = threading.Event()
        self.commands = queue.Queue(maxsize=32)
        # Backpressure also bounds data waiting for the GUI event loop.
        self.output = queue.Queue(maxsize=8)
        self.authentication_failed = False
        self.active = False
        self.error = ""
        self.accepting = threading.Event()

    def submit(self, data):
        if (not self.accepting.is_set() or self.cancel.is_set()
                or not isinstance(data, bytes) or len(data) > 65536):
            return False
        try:
            self.commands.put_nowait(data)
            return True
        except queue.Full:
            return False

    def stop(self):
        self.accepting.clear()
        self.cancel.set()

    def run(self):
        session = channel = client = None
        authenticating = False
        try:
            if self.cancel.is_set():
                return
            session = RadminSession(self.entry["host"], self.entry["port"],
                                    self.entry["username"], self.password, self.timeout)
            self.password = ""
            if self.cancel.is_set():
                return
            authenticating = True
            session.connect()
            if self.cancel.is_set():
                return
            if not session.authenticated:
                raise AuthenticationError()
            authenticating = False
            self.authenticated.emit()
            if self.cancel.is_set():
                return
            session.timeout = min(session.timeout, 2.0)
            channel = EncryptedChannel(session)
            channel.start()
            if self.cancel.is_set():
                return
            client = TelnetClient(channel, operation_timeout=2.0, io_timeout=2.0)
            client.start(cancel=self.cancel)
            if self.cancel.is_set():
                return
            self.active = True
            self.accepting.set()
            self.ready.emit()
            while not self.cancel.is_set():
                try:
                    data = self.commands.get(timeout=.15)
                except queue.Empty:
                    data = b""
                if self.cancel.is_set():
                    break
                block = client.exchange(data, cancel=self.cancel)
                # Empty is an idle reply, never EOF.
                if block:
                    while not self.cancel.is_set():
                        try:
                            self.output.put(block, timeout=.1)
                            break
                        except queue.Full:
                            pass
        except Exception as error:
            if not self.cancel.is_set():
                self.authentication_failed = authenticating and isinstance(error, AuthenticationError)
                self.error = (AUTH_MESSAGE if self.authentication_failed else
                              "Terminal connection closed. Reconnect to open a new shell."
                              if self.active else
                              "Could not start the remote terminal. Check the connection and terminal permissions.")
        finally:
            self.accepting.clear()
            self.password = ""
            for resource in (client, channel, session):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        pass
            while True:
                try:
                    self.commands.get_nowait()
                except queue.Empty:
                    break


class ShellText:
    """Bounded transcript with CR overwrite, LF and destructive backspace."""
    LIMIT = 131072

    def __init__(self):
        self.prefix = ""
        self.line = []
        self.column = 0

    def feed(self, text):
        for char in text:
            if char == "\r":
                self.column = 0
            elif char == "\n":
                self.prefix = (self.prefix + "".join(self.line) + "\n")[-self.LIMIT:]
                self.line = []
                self.column = 0
            elif char == "\b":
                if self.column:
                    self.column -= 1
                    del self.line[self.column]
            elif char == "\t" or ord(char) >= 32:
                if self.column < len(self.line):
                    self.line[self.column] = char
                else:
                    self.line.append(char)
                self.column += 1
                if len(self.line) > 2 * self.LIMIT:
                    del self.line[:len(self.line) - self.LIMIT]
                    self.column = min(self.column, self.LIMIT)
            elif char == "\x1b":
                # Make unsupported escapes visible; never interpret remote HTML.
                self.feed("␛")
        if len(self.line) > self.LIMIT:
            removed = len(self.line) - self.LIMIT
            del self.line[:removed]
            self.column = max(0, self.column - removed)
        self.prefix = self.prefix[-max(0, self.LIMIT - len(self.line)):] if len(self.line) < self.LIMIT else ""
        return self.prefix + "".join(self.line)


def _button(image, label):
    button = QPushButton(icon(image), "")
    button.setToolTip(label)
    button.setAccessibleName(label)
    return button


class TerminalWindow(QMainWindow):
    authenticated = Signal()
    retry_requested = Signal(object, str, str)
    session_error = Signal(str)
    power_requested = Signal(object, str)

    def __init__(self, entry, password, settings):
        super().__init__()
        self.entry = dict(entry)
        self.mode = "terminal"
        self.closing = False
        self._started = self._finished = self.connected = False
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(f"{entry.get('name') or entry['host']} — Terminal")
        self.setWindowIcon(application_icon())
        self.resize(900, 600)
        self.decoder = codecs.getincrementaldecoder("cp437")("replace")
        self.transcript = ShellText()
        central = QWidget()
        layout = QVBoxLayout(central)
        toolbar = QHBoxLayout()
        label = QLabel("Remote command shell")
        toolbar.addWidget(label)
        toolbar.addStretch()
        self.encoding = StyledComboBox()
        self.encoding.setAccessibleName("Terminal encoding")
        for name, codec in ENCODINGS:
            self.encoding.addItem(name, codec)
        toolbar.addWidget(self.encoding)
        self.clear_button = _button("trash", "Clear display")
        toolbar.addWidget(self.clear_button)
        self.interrupt_button = _button("stop", "Interrupt remote command (Ctrl+C)")
        toolbar.addWidget(self.interrupt_button)
        self.power_button = QToolButton()
        self.power_button.setIcon(icon("power"))
        self.power_button.setToolTip("Remote power actions")
        self.power_button.setAccessibleName("Remote power actions")
        self.power_button.setMenu(create_power_menu(self, self._power))
        self.power_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        toolbar.addWidget(self.power_button)
        self.disconnect_button = _button("disconnect", "Disconnect terminal")
        self.disconnect_button.clicked.connect(self.close)
        toolbar.addWidget(self.disconnect_button)
        layout.addLayout(toolbar)
        self.output = TerminalEditor()
        self.output.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        layout.addWidget(self.output, 1)
        self.input = self.output
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(5)
        layout.addWidget(self.progress)
        self.status_label = QLabel("Connecting to remote command shell…")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.setCentralWidget(central)
        self.worker = TerminalWorker(entry, password, settings["timeout"], self)
        self.worker.authenticated.connect(self._authenticated)
        self.worker.ready.connect(self._ready)
        self.worker.finished.connect(self._worker_finished)
        self.input.returnPressed.connect(self.send_command)
        self.input.interrupt_requested.connect(self.interrupt)
        self.output.notice.connect(self.status_label.setText)
        self.interrupt_button.clicked.connect(self.interrupt)
        self.clear_button.clicked.connect(self.clear_display)
        self.encoding.currentIndexChanged.connect(self._encoding_changed)
        self._controls(False)
        self._drain_timer = QTimer(self)
        self._drain_timer.setInterval(50)
        self._drain_timer.timeout.connect(self._drain)
        self._drain_timer.start()
        self._start_timer = QTimer(self)
        self._start_timer.setSingleShot(True)
        self._start_timer.timeout.connect(self._start)
        self._start_timer.start(0)

    def _controls(self, enabled):
        self.output.active = enabled
        for widget in (self.interrupt_button, self.power_button):
            widget.setEnabled(enabled)

    @Slot()
    def _start(self):
        if not self.closing and not self._started:
            self._started = True
            self.worker.start()

    @Slot()
    def _authenticated(self):
        if not self.closing:
            self.status_label.setText("Authenticated · Starting remote command shell…")
            self.authenticated.emit()

    @Slot()
    def _ready(self):
        if not self.closing and not self._finished:
            self.connected = True
            self._controls(True)
            self.progress.hide()
            self.status_label.setText("Connected · Line-oriented shell · Fullscreen terminal applications are unsupported")
            self.input.setFocus()

    def _power(self, action):
        if self.connected and not self.closing:
            self.power_requested.emit(dict(self.entry), action.value)

    @Slot()
    def send_command(self):
        if not self.connected or self.closing:
            return
        text = self.input.text()
        try:
            if any(ch in text for ch in "\r\n\0"):
                raise ValueError()
            data = text.encode(self.encoding.currentData()) + b"\r\n"
        except (UnicodeError, ValueError):
            self.status_label.setText("Command cannot be encoded. Choose the remote encoding and use a single line without NUL.")
            return
        if self.worker.submit(data):
            self.input.remember(text)
        else:
            self.status_label.setText("Command queue is full or disconnected; command was not queued.")

    @Slot()
    def interrupt(self):
        if self.connected and not self.closing:
            if self.worker.submit(b"\x03"):
                self.input.setText("")
            else:
                self.status_label.setText("Interrupt could not be queued.")

    def _render(self, text):
        if not text:
            return
        rendered = self.transcript.feed(text)
        self.output.set_remote_text(rendered)

    @Slot()
    def _drain(self):
        for _ in range(8):
            try:
                block = self.worker.output.get_nowait()
            except queue.Empty:
                break
            self._render(self.decoder.decode(block))

    @Slot(int)
    def _encoding_changed(self, index):
        self._drain()
        self._render(self.decoder.decode(b"", final=True))
        self.decoder = codecs.getincrementaldecoder(self.encoding.currentData())("replace")

    @Slot()
    def clear_display(self):
        self._drain()
        self.transcript = ShellText()
        self.output.set_remote_text("")

    @Slot()
    def _worker_finished(self):
        if self._finished:
            return
        self.worker.wait()
        self._finished = True
        self.connected = False
        self._controls(False)
        self.progress.hide()
        self._drain_timer.stop()
        self._drain()
        self._render(self.decoder.decode(b"", final=True))
        if self.closing:
            self.close()
        elif self.worker.authentication_failed:
            self.close()
            self.retry_requested.emit(dict(self.entry), self.mode, AUTH_MESSAGE)
        elif self.worker.error:
            self.status_label.setText(self.worker.error)
            self.session_error.emit(self.worker.error)
            if self.worker.active:
                self.close()

    def closeEvent(self, event):
        self.closing = True
        self.connected = False
        self._controls(False)
        self._start_timer.stop()
        self.worker.stop()
        if self._started and not self._finished:
            self.status_label.setText("Closing… Waiting for connection cleanup.")
            event.ignore()
            return
        self._drain_timer.stop()
        self.worker.password = ""
        event.accept()
