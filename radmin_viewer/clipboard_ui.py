"""GUI-thread text clipboard bridge; no transport or OS access in workers."""
from __future__ import annotations

from collections.abc import Callable
import weakref

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QClipboard
from PySide6.QtWidgets import QApplication

from .clipboard import ClipboardFormatError, encode_unicode_text


class ClipboardController(QObject):
    """One controller per session, constructed and called on the GUI thread.

    ``submit(event)`` queues a worker event and returns normally on acceptance;
    ``status(message)`` receives content-free diagnostics. Connect worker signals
    directly to received/failed (Qt queues cross-thread signal delivery).

    set_available(False) denotes a disconnected session and forgets its pending
    wire request. Set True only for a fresh/usable control session. Request IDs
    increase across reconnects. Timeout and failed() retain the pending request:
    the worker MUST retain its wire association too, until the original reply or
    reconnect. Neither disabling auto nor losing focus cancels that association.

    Automatic mode is exclusive process-wide, persists across availability
    changes, and sends local text before polling. close() is idempotent and must
    be called by the owning window during teardown, before QObject destruction.
    """

    automatic_changed = Signal(bool)
    POLL_INTERVAL_MS = 750
    RECEIVE_TIMEOUT_MS = 3000
    MAX_TEXT_BYTES = 1024 * 1024  # UTF-16 bytes including the terminating NUL.
    _automatic_owner: weakref.ReferenceType | None = None
    _remote_write_depth = 0

    def __init__(self, clipboard: QClipboard, submit: Callable[[dict], object],
                 status: Callable[[str], object], parent: QObject | None = None):
        self._require_gui_thread()
        super().__init__(parent)
        self._clipboard = clipboard
        self._submit = submit
        self._status = status
        self._available = False
        self._automatic = False
        self._closed = False
        self._revision = 0
        self._next_request_id = 0
        self._pending: tuple[int, int] | None = None
        self._receive_stalled = False
        self._queued_text: str | None = None
        self._poll = QTimer(self)
        self._poll.setInterval(self.POLL_INTERVAL_MS)
        self._poll.timeout.connect(self.receive_remote)
        self._deadline = QTimer(self)
        self._deadline.setSingleShot(True)
        self._deadline.setInterval(self.RECEIVE_TIMEOUT_MS)
        self._deadline.timeout.connect(self._timed_out)
        self._flush = QTimer(self)
        self._flush.setSingleShot(True)
        self._flush.setInterval(0)
        self._flush.timeout.connect(self._flush_local)
        clipboard.dataChanged.connect(self._local_changed)

    @staticmethod
    def _require_gui_thread() -> None:
        app = QApplication.instance()
        if app is None or QThread.currentThread() != app.thread():
            raise RuntimeError("ClipboardController requires the QApplication GUI thread")

    @property
    def automatic(self) -> bool:
        return self._automatic

    def _valid_text(self, text: object) -> bool:
        if not isinstance(text, str):
            self._status("Clipboard format is unsupported; Unicode text is required.")
            return False
        try:
            # Bound allocation before encoding, then account for surrogate pairs.
            if len(text) > (self.MAX_TEXT_BYTES - 2) // 2:
                raise ClipboardFormatError("size limit")
            if len(encode_unicode_text(text)) - 8 > self.MAX_TEXT_BYTES:
                raise ClipboardFormatError("size limit")
        except ClipboardFormatError:
            self._status("Clipboard text is invalid or exceeds the 1 MiB UTF-16 limit.")
            return False
        return True

    def _local_text(self) -> str | None:
        mime = self._clipboard.mimeData()
        if mime is None or not mime.hasText():
            self._status("Local clipboard has no supported text format.")
            return None
        text = self._clipboard.text()
        return text if self._valid_text(text) else None

    def _send_text(self, text: str) -> None:
        try:
            self._submit({"type": "clipboard_send", "text": text})
        except Exception:
            self._status("Clipboard send could not be queued.")
        else:
            self._status("Clipboard text queued for sending.")

    @Slot(bool)
    def set_available(self, available: bool) -> None:
        self._require_gui_thread()
        if self._closed or self._available == bool(available):
            return
        self._available = bool(available)
        self._revision += 1
        if not self._available:
            self._poll.stop()
            self._deadline.stop()
            self._flush.stop()
            self._queued_text = None
            self._pending = None
            self._receive_stalled = False
        elif self._automatic:
            self.send_local()
            self._poll.start()

    @Slot()
    def send_local(self) -> None:
        self._require_gui_thread()
        if self._closed:
            return
        if not self._available:
            self._status("Clipboard sharing is unavailable.")
            return
        self._flush.stop()
        self._queued_text = None
        text = self._local_text()
        if text is not None:
            self._send_text(text)

    @Slot()
    def receive_remote(self) -> None:
        self._require_gui_thread()
        if self._closed or self._pending is not None:
            return
        if not self._available:
            self._status("Clipboard sharing is unavailable.")
            return
        # A local change in this event-loop turn always goes out before a poll.
        self._flush_local()
        self._next_request_id += 1
        request_id = self._next_request_id
        self._pending = (request_id, self._revision)
        self._deadline.start()
        try:
            self._submit({"type": "clipboard_receive", "request_id": request_id})
        except Exception:
            self.failed(request_id, "")

    @Slot(bool)
    def set_automatic(self, automatic: bool) -> None:
        self._require_gui_thread()
        if self._closed or self._automatic == bool(automatic):
            return
        if automatic:
            owner_ref = ClipboardController._automatic_owner
            owner = owner_ref() if owner_ref is not None else None
            if owner is not None and owner is not self:
                owner.set_automatic(False)
            ClipboardController._automatic_owner = weakref.ref(self)
        else:
            owner_ref = ClipboardController._automatic_owner
            if owner_ref is not None and owner_ref() is self:
                ClipboardController._automatic_owner = None
        self._automatic = bool(automatic)
        self._revision += 1  # Relinquished requests cannot overwrite a new owner.
        self._poll.stop()
        self._flush.stop()
        self._queued_text = None
        if self._automatic and self._available:
            self.send_local()
            if not self._receive_stalled:
                self._poll.start()
        self.automatic_changed.emit(self._automatic)

    @Slot()
    def _local_changed(self) -> None:
        self._require_gui_thread()
        if self._closed:
            return
        self._revision += 1
        self._flush.stop()
        self._queued_text = None
        if (ClipboardController._remote_write_depth or not self._automatic
                or not self._available):
            return
        self._queued_text = self._local_text()
        if self._queued_text is not None:
            self._flush.start()

    @Slot()
    def _flush_local(self) -> None:
        self._require_gui_thread()
        self._flush.stop()
        text, self._queued_text = self._queued_text, None
        if not self._closed and self._available and self._automatic and text is not None:
            self._send_text(text)

    @Slot()
    def _timed_out(self) -> None:
        self._require_gui_thread()
        if not self._closed and self._pending is not None:
            self._receive_stalled = True
            self._poll.stop()
            self._status("Clipboard receive timed out; waiting for the original reply or reconnect.")

    @Slot(int, object)
    def received(self, request_id: int, text: object) -> None:
        self._require_gui_thread()
        if self._closed or self._pending is None or self._pending[0] != request_id:
            return
        _, revision = self._pending
        self._pending = None
        self._receive_stalled = False
        self._deadline.stop()
        if self._automatic and self._available:
            self._poll.start()
        if revision != self._revision:
            self._status("Stale clipboard reply discarded after a local change.")
            return
        if not self._valid_text(text):
            return
        mime = self._clipboard.mimeData()
        if mime is not None and mime.hasText() and self._clipboard.text() == text:
            # Repeated polls must not continually reclaim clipboard ownership or
            # turn an unchanged rich clipboard into plain text.
            return
        # dataChanged is synchronous on the GUI thread, including other sessions.
        # Increment their revisions, but never forward this remote write to them.
        ClipboardController._remote_write_depth += 1
        try:
            self._clipboard.setText(text)
        finally:
            ClipboardController._remote_write_depth -= 1
        self._status("Remote clipboard text received.")

    @Slot(int, str)
    def failed(self, request_id: int, message: str) -> None:
        self._require_gui_thread()
        if self._closed or self._pending is None or self._pending[0] != request_id:
            return
        self._deadline.stop()
        self._poll.stop()
        self._receive_stalled = True
        # Worker diagnostics may contain payloads. Never display them verbatim.
        self._status("Clipboard receive failed; waiting for the original reply or reconnect.")

    @Slot()
    def close(self) -> None:
        self._require_gui_thread()
        if self._closed:
            return
        self.set_automatic(False)
        self.set_available(False)
        self._closed = True
        self._poll.stop()
        self._deadline.stop()
        self._flush.stop()
        self._pending = None
        self._queued_text = None
        self._clipboard.dataChanged.disconnect(self._local_changed)
