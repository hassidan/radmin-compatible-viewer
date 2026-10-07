"""Bounded, asynchronous checks of explicit saved endpoints using Qt networking.

No authentication: only the already recovered Radmin preamble is exchanged.
A TCP listener is distinct from a recognized Radmin service; no ICMP privileges
or subnet discovery are required. All methods run on the owning Qt thread.
"""
from collections import deque
from dataclasses import dataclass
from datetime import datetime
import time

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QAbstractSocket, QTcpSocket

from .protocol import PREAMBLE, PREAMBLE_ACK


@dataclass(frozen=True)
class ScanResult:
    entry_id: str
    host: str
    port: int
    status: str
    detail: str = ""
    checked_at: str = ""
    latency_ms: int | None = None


class ConnectionScanner(QObject):
    result = Signal(object)
    progress = Signal(int, int)
    finished = Signal(bool)  # True if cancelled

    def __init__(self, parent=None, *, concurrency=16, timeout_ms=3000):
        super().__init__(parent)
        if not 1 <= concurrency <= 64 or not 1 <= timeout_ms <= 120000:
            raise ValueError("Invalid scan bounds")
        self.concurrency = concurrency
        self.timeout_ms = timeout_ms
        self.running = False
        self._pending = deque()
        self._active = {}
        self._total = self._done = 0

    def start(self, entries):
        if self.running:
            raise RuntimeError("Scan already running")
        # Snapshot only identity and destination, never credentials or mutable UI data.
        targets = [(entry["id"], entry["host"], entry["port"]) for entry in entries]
        self._pending = deque(targets)
        self._total, self._done = len(targets), 0
        self.running = True
        for target in targets:
            self.result.emit(ScanResult(*target, "Queued"))
        self.progress.emit(0, self._total)
        QTimer.singleShot(0, self._pump)

    def _pump(self):
        if not self.running:
            return
        while self._pending and len(self._active) < self.concurrency:
            target = self._pending.popleft()
            sock = QTcpSocket(self)
            sock.setReadBufferSize(len(PREAMBLE_ACK) + 1)
            timer = QTimer(sock)
            timer.setSingleShot(True)
            self._active[sock] = {"target": target, "timer": timer, "start": time.monotonic(),
                                  "connected": False, "latency": None, "received": bytearray()}
            sock.connected.connect(lambda s=sock: self._connected(s))
            sock.readyRead.connect(lambda s=sock: self._read(s))
            sock.errorOccurred.connect(lambda error, s=sock: self._error(s, error))
            timer.timeout.connect(lambda s=sock: self._timeout(s))
            self.result.emit(ScanResult(*target, "Checking…"))
            timer.start(self.timeout_ms)  # Includes DNS, connect and greeting.
            sock.connectToHost(target[1], target[2])
        if not self._pending and not self._active:
            self.running = False
            self.finished.emit(False)

    def _connected(self, sock):
        state = self._active.get(sock)
        if state is None:
            return
        state["connected"] = True
        state["latency"] = round((time.monotonic() - state["start"]) * 1000)
        sock.write(PREAMBLE)

    def _read(self, sock):
        state = self._active.get(sock)
        if state is None:
            return
        if sock.bytesAvailable() <= 0:
            return
        state["received"].extend(bytes(sock.read(len(PREAMBLE_ACK) - len(state["received"]))))
        data = state["received"]
        if not PREAMBLE_ACK.startswith(data):
            self._finish(sock, "Port open", "TCP connection succeeded; reply was not the supported Radmin greeting.")
        elif len(data) == len(PREAMBLE_ACK):
            self._finish(sock, "Online", "Radmin greeting recognized. Authentication and session permissions were not tested.")

    def _error(self, sock, error):
        # A final valid greeting may be buffered when the peer closes immediately.
        self._read(sock)
        state = self._active.get(sock)
        if state is None:
            return
        if state["connected"]:
            self._finish(sock, "Port open", "TCP connection succeeded, but the Radmin greeting was not completed.")
        elif error == QAbstractSocket.SocketError.HostNotFoundError:
            self._finish(sock, "DNS error", "Hostname could not be resolved.")
        else:
            self._finish(sock, "Unreachable", "Configured TCP port refused the connection or could not be reached. The computer may still be running.")

    def _timeout(self, sock):
        state = self._active.get(sock)
        if state is None:
            return
        if state["connected"]:
            self._finish(sock, "Port open", "TCP connected, but no supported Radmin greeting arrived before the deadline.")
        else:
            self._finish(sock, "Unreachable", "DNS/connect deadline expired. The computer may be offline, filtered, or slow to respond.")

    def _dispose(self, sock):
        state = self._active.pop(sock)
        state["timer"].stop()
        sock.abort()
        sock.deleteLater()
        return state

    def _finish(self, sock, status, detail):
        if sock not in self._active:
            return
        state = self._dispose(sock)
        self._done += 1
        self.result.emit(ScanResult(*state["target"], status, detail,
                                   datetime.now().astimezone().isoformat(timespec="seconds"), state["latency"]))
        self.progress.emit(self._done, self._total)
        QTimer.singleShot(0, self._pump)

    def cancel(self):
        if not self.running:
            return
        self.running = False
        targets = list(self._pending)
        self._pending.clear()
        for sock in list(self._active):
            targets.append(self._dispose(sock)["target"])
        for target in targets:
            self.result.emit(ScanResult(*target, "Cancelled", "No completed result for this entry."))
        self.finished.emit(True)
