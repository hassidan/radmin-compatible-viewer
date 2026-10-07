"""Nonmodal, one-shot power UI; the caller must confirm before construction.

Connect authenticated(), retry_requested(entry, 'power:'+action.value, message)
and optionally session_error(message), then show(). Startup is deferred one GUI
turn. Only an authentication failure requests another login, after teardown.
The manager must reconfirm that new attempt. No result offers an action retry.

worker is retained until window destruction; closing is set before dismissal.
Cancel requests cooperative cancellation and leaves the result visible. Close
(including Escape) waits asynchronously for cleanup before deleting the window.
"""
from __future__ import annotations

import threading

from PySide6.QtCore import QThread, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import QProgressBar

from .channel import EncryptedChannel
from .power import POWER_DESCRIPTORS, PowerAction, PowerClient, PowerFailure, PowerOutcome, PowerResult
from .protocol import AuthenticationError, RadminSession
from .ui_dialogs import ModernDialog, RoundedMenu
from .ui_icons import icon


AUTH_MESSAGE = "Authentication failed. Verify the login before confirming a new power request."


def create_power_menu(parent, callback):
    """One action list for the manager, session toolbar and fullscreen dashboard."""
    menu = RoundedMenu("Power", parent)
    menu.setIcon(icon("power"))
    menu.setToolTipsVisible(True)
    for action in PowerAction:
        if action == PowerAction.SLEEP:
            menu.addSeparator()
        descriptor = POWER_DESCRIPTORS[action]
        image = {PowerAction.RESTART: "restart", PowerAction.SLEEP: "sleep",
                 PowerAction.HIBERNATE: "hibernate"}.get(action, "power")
        item = menu.addAction(icon(image), descriptor.label + "…")
        item.setData(action.value)
        item.setToolTip(descriptor.permission)
        item.triggered.connect(lambda checked=False, selected=action: callback(selected))
    return menu


def result_message(result: PowerResult) -> str:
    """Display only typed fields, never backend details or exception text."""
    messages = {
        PowerOutcome.ACCEPTED: "Server acknowledged the request. The current machine power state is unverified.",
        PowerOutcome.UNKNOWN: "Unknown outcome: the request may have been dispatched, but no conclusive acknowledgement was received. Do not retry automatically.",
        PowerOutcome.CANCELLED: "Cancelled before power dispatch. No power request was sent.",
        PowerOutcome.NOT_SENT: "Not sent: connection or encrypted-channel setup failed before power dispatch.",
        PowerOutcome.UNAVAILABLE: "Unavailable: the power feature, Windows power state, PowerShell or required API is unsupported or unavailable.",
        PowerOutcome.REJECTED: "Rejected: the server or Windows refused the power request.",
    }
    message = messages[result.outcome]
    if result.failure == PowerFailure.PERMISSION:
        message += " Permission denied: check Radmin permissions and the Windows shutdown privilege."
    # Native Radmin error numbers have no verified Win32 mapping. Preserve the
    # numeric code without inventing a permission/unsupported classification.
    if type(result.remote_code) is int and 0 <= result.remote_code <= 0xffffffff:
        message += f" (Code 0x{result.remote_code:08x}.)"
    return message


class PowerWorker(QThread):
    """Single I/O owner. stop() only sets an Event; it never closes sockets.

    A normally returning execute owns channel/session teardown. Before that
    handoff, or on an unexpected execute exception, this worker closes them.
    Unexpected execute exceptions are conservatively UNKNOWN, never retryable.
    Read result_value/authentication_failed only after finished().
    """
    authenticated = Signal()
    status = Signal(str)

    def __init__(self, entry, password, timeout, action, parent=None):
        super().__init__(parent)
        self.entry = dict(entry)
        self.password = password
        self.timeout = timeout
        self.action = PowerAction(action)
        self.cancel = threading.Event()
        self.result_value = None
        self.authentication_failed = False

    def stop(self):
        self.cancel.set()

    def _result(self, outcome, dispatched=False):
        return PowerResult(self.action, POWER_DESCRIPTORS[self.action].method,
                           outcome, dispatched, "")

    def run(self):
        session = channel = None
        executing = completed = authenticating = False
        self.result_value = self._result(PowerOutcome.CANCELLED)
        try:
            if self.cancel.is_set():
                return
            self.status.emit("Authenticating a dedicated power connection…")
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
            self.status.emit("Authenticated · Starting encrypted channel…")
            channel = EncryptedChannel(session)
            if self.cancel.is_set():
                return
            channel.start()
            if self.cancel.is_set():
                return
            client = PowerClient(channel)
            if self.cancel.is_set():
                return
            self.status.emit("Waiting for the server acknowledgement…")
            executing = True
            self.result_value = client.execute(self.action, cancel=self.cancel)
            completed = True
        except Exception as error:
            if executing:
                self.result_value = self._result(PowerOutcome.UNKNOWN, True)
            elif self.cancel.is_set():
                self.result_value = self._result(PowerOutcome.CANCELLED)
            else:
                self.authentication_failed = authenticating and isinstance(error, AuthenticationError)
                self.result_value = self._result(PowerOutcome.NOT_SENT)
        finally:
            self.password = ""
            if not completed:
                # Channel.close cascades to session.close. Both are idempotent;
                # a failed channel cleanup still gets a session-level fallback.
                try:
                    if channel is not None:
                        channel.close()
                    elif session is not None:
                        session.close()
                except Exception:
                    if session is not None:
                        try:
                            session.close()
                        except Exception:
                            pass


class PowerWindow(ModernDialog):
    authenticated = Signal()
    retry_requested = Signal(object, str, str)
    session_error = Signal(str)
    operation_finished = Signal()

    def __init__(self, entry, password, settings, action):
        self.action = PowerAction(action)
        super().__init__(POWER_DESCRIPTORS[self.action].label)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.entry = dict(entry)
        self.mode = "power:" + self.action.value
        self.closing = False
        self.result_value = None
        self._started = self._finished = self._retry_emitted = False
        self.add_message(f"{entry.get('name') or entry['host']} · {POWER_DESCRIPTORS[self.action].label}")
        self.status_label = self.add_message("Preparing a dedicated power connection…")
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setAccessibleName("Power request progress")
        self.body_layout.addWidget(self.progress)
        self.cancel_button = self.add_button("Cancel", self._cancel_or_close)
        self.worker = PowerWorker(entry, password, settings["timeout"], self.action, self)
        self.worker.authenticated.connect(self._authenticated)
        self.worker.status.connect(self._status)
        self.worker.finished.connect(self._worker_finished)
        self._start_timer = QTimer(self)
        self._start_timer.setSingleShot(True)
        self._start_timer.timeout.connect(self._start)
        self._start_timer.start(0)

    @Slot()
    def _start(self):
        if not self.closing and not self._finished and not self._started:
            self._started = True
            self.worker.start()

    @Slot()
    def _authenticated(self):
        if not self.closing and not self.worker.cancel.is_set():
            self.authenticated.emit()

    @Slot(str)
    def _status(self, message):
        if not self.closing and not self.worker.cancel.is_set() and not self._finished:
            self.status_label.setText(message)

    def _show_result(self):
        self.status_label.setText(result_message(self.result_value))
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.cancel_button.setText("Close")
        self.cancel_button.setEnabled(True)
        self._queue_fit()

    @Slot()
    def _worker_finished(self):
        if self._finished:
            return
        self.worker.wait()  # finished was emitted; join final native-thread teardown.
        self._finished = True
        self.result_value = self.worker.result_value
        self._show_result()
        if self.worker.authentication_failed and not self.closing and not self.worker.cancel.is_set():
            self.closing = True
            self.close()
            if not self._retry_emitted:
                self._retry_emitted = True
                self.retry_requested.emit(dict(self.entry), self.mode, AUTH_MESSAGE)
        elif self.closing:
            if self.result_value.dispatched:
                self.session_error.emit(result_message(self.result_value))
            self.close()
        self.operation_finished.emit()

    @Slot()
    def _cancel_or_close(self):
        if self._finished:
            self.close()
            return
        self.worker.stop()
        self.cancel_button.setEnabled(False)
        self.status_label.setText("Cancelling… Waiting for connection cleanup. A dispatched request cannot be recalled.")
        if not self._started:
            self._start_timer.stop()
            self.worker.password = ""
            self._finished = True
            self.result_value = self.worker._result(PowerOutcome.CANCELLED)
            self._show_result()
            self.operation_finished.emit()

    def done(self, result):
        # QDialog.reject/accept/Escape otherwise bypass closeEvent's thread guard.
        self.close()

    def closeEvent(self, event):
        self.closing = True
        self._start_timer.stop()
        if self._started and not self._finished:
            self.worker.stop()
            self.cancel_button.setEnabled(False)
            self.status_label.setText("Closing… Waiting for connection cleanup.")
            event.ignore()
            return
        self.worker.password = ""
        if self.result_value is None:
            self.result_value = self.worker._result(PowerOutcome.CANCELLED)
        event.accept()
