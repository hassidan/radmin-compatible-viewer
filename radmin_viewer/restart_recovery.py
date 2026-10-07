"""Observe an already-dispatched restart; never dispatch power or authenticate.

Main constructs RestartRecoveryWindow only after ACCEPTED, or UNKNOWN with
dispatched=True, connects ready(entry, password, mode), registers destroyed,
then show(). Startup is deferred one GUI turn. The monitor alone has explicit
start()/cancel(); all methods and callbacks belong to its owning GUI thread.

WAIT_OFFLINE -> WAIT_ONLINE -> READY requires Unreachable/DNS error followed
by two consecutive verified Online samples. Port open does not prove offline.
These observations cannot prove a reboot: filtering/DNS failures can look like
an outage, and a fast reboot between polls can be missed (then timeout safely).
Credentials are never passed to the scanner. Clearing Python string references
is lifetime minimization, not guaranteed memory zeroization.
"""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QObject, QTimer, Qt, Signal
from PySide6.QtWidgets import QProgressBar

from .scanner import ConnectionScanner
from .ui_dialogs import ModernDialog


class RestartMonitor(QObject):
    ready = Signal()
    failure = Signal(str)
    state_changed = Signal(str)
    remaining_changed = Signal(int)  # Seconds, rounded up.

    IDLE = "IDLE"
    WAIT_OFFLINE = "WAIT_OFFLINE"
    WAIT_ONLINE = "WAIT_ONLINE"
    READY = "READY"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"

    def __init__(self, entry, parent=None, *, timeout_ms=300000,
                 poll_interval_ms=1500, scan_factory=ConnectionScanner,
                 clock=time.monotonic):
        super().__init__(parent)
        if not 1 <= timeout_ms <= 2147483647 or not 1 <= poll_interval_ms <= 2147483647:
            raise ValueError("Recovery timing must be positive Qt timer intervals")
        self._target = {key: entry[key] for key in ("id", "host", "port")}
        self.timeout_ms = timeout_ms
        self.poll_interval_ms = poll_interval_ms
        self._scan_factory = scan_factory
        self._clock = clock
        self.state = self.IDLE
        self._deadline = None
        self._scanner = None
        self._sample = None
        self._online_streak = 0
        self._poll_timer = self._timer(self._poll)
        self._deadline_timer = self._timer(self._check_deadline)
        self._check_timer = self._timer(self._check_expired)
        self._countdown_timer = self._timer(self._check_deadline, single=False)

    def _timer(self, callback, single=True):
        timer = QTimer(self)
        timer.setSingleShot(single)
        timer.setTimerType(Qt.TimerType.PreciseTimer)
        timer.timeout.connect(callback)
        return timer

    @property
    def running(self):
        return self.state in (self.WAIT_OFFLINE, self.WAIT_ONLINE)

    def start(self):
        if self.state != self.IDLE:
            return
        self._deadline = self._clock() + self.timeout_ms / 1000
        self.state = self.WAIT_OFFLINE
        self._deadline_timer.start(self.timeout_ms)
        self._countdown_timer.start(250)
        self.state_changed.emit(self.state)
        self._poll()

    def cancel(self):
        if self.state == self.IDLE or self.running:
            self._stop(self.CANCELLED)

    def _stop(self, state):
        self.state = state  # Invalidate callbacks before cancel emits signals.
        for timer in (self._poll_timer, self._deadline_timer,
                      self._check_timer, self._countdown_timer):
            timer.stop()
        self._release_scan(cancel=True)
        self.state_changed.emit(state)

    def _release_scan(self, *, cancel=False):
        scanner, self._scanner = self._scanner, None
        self._check_timer.stop()
        if scanner is not None:
            if cancel:
                scanner.cancel()
            scanner.deleteLater()

    def _check_deadline(self):
        if not self.running:
            return False
        remaining = self._deadline - self._clock()
        if remaining <= 0:
            offline_missing = self.state == self.WAIT_OFFLINE
            self._stop(self.TIMED_OUT)
            self.failure.emit(
                "Timed out: no offline transition was observed. No reconnection was attempted."
                if offline_missing else
                "Timed out waiting for two consecutive verified Radmin responses.")
            return False
        self.remaining_changed.emit(math.ceil(remaining))
        # Re-arm against the monotonic deadline, including an early timer wakeup.
        if self.running:
            self._deadline_timer.start(max(1, math.ceil(remaining * 1000)))
        return self.running

    def _poll(self):
        if not self._check_deadline() or self._scanner is not None:
            return
        self._poll_timer.stop()
        budget = max(1, min(1500, math.ceil((self._deadline - self._clock()) * 1000)))
        scanner = self._scan_factory(self, concurrency=1, timeout_ms=budget)
        self._scanner = scanner
        self._sample = None
        scanner.result.connect(lambda result, s=scanner: self._result(s, result))
        scanner.finished.connect(lambda cancelled, s=scanner: self._finished(s, cancelled))
        # Allow the scanner's DNS/connect deadline to classify the result first.
        self._check_timer.start(budget + 50)
        scanner.start([dict(self._target)])

    def _result(self, scanner, result):
        if scanner is not self._scanner or not self._check_deadline():
            return
        if (result.entry_id, result.host, result.port) != tuple(
                self._target[key] for key in ("id", "host", "port")):
            return
        if result.status in ("Online", "Unreachable", "DNS error", "Port open"):
            self._sample = result.status

    def _finished(self, scanner, cancelled):
        if scanner is not self._scanner or not self._check_deadline():
            return
        sample = None if cancelled else self._sample
        self._release_scan()
        self._consume(sample)

    def _check_expired(self):
        if not self._check_deadline() or self._scanner is None:
            return
        # Watchdog is deliberately not outage evidence. The scanner's own
        # DNS/connect timer supplies the authoritative Unreachable result.
        sample = self._sample
        self._release_scan(cancel=True)
        self._consume(sample)

    def _consume(self, sample):
        if self.state == self.WAIT_OFFLINE:
            if sample in ("Unreachable", "DNS error"):
                self.state = self.WAIT_ONLINE
                self.state_changed.emit(self.state)
        elif self.state == self.WAIT_ONLINE:
            self._online_streak = self._online_streak + 1 if sample == "Online" else 0
            if self._online_streak == 2:
                self._stop(self.READY)
                self.ready.emit()
                return
        if self.running:
            self._poll_timer.start(self.poll_interval_ms)


class RestartRecoveryWindow(ModernDialog):
    ready = Signal(object, str, str)
    failure = Signal(str)

    def __init__(self, entry, password, mode="control", *, timeout_ms=300000,
                 poll_interval_ms=1500, scan_factory=ConnectionScanner,
                 clock=time.monotonic):
        if mode not in ("view", "control", "terminal"):
            raise ValueError("Recovery mode must be view, control or terminal")
        super().__init__("Restart recovery")
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.entry = dict(entry)
        self.password = password
        self.mode = mode
        self.closing = False
        self._ready_emitted = False
        self.add_message(f"{entry.get('name') or entry['host']} · Restart recovery")
        self.status_label = self.add_message("Restarting… Waiting for the endpoint to go offline.")
        self.countdown_label = self.add_message("")
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setAccessibleName("Restart recovery progress")
        self.body_layout.addWidget(self.progress)
        self.cancel_button = self.add_button("Cancel", self.close)
        self.monitor = RestartMonitor(entry, self, timeout_ms=timeout_ms,
                                      poll_interval_ms=poll_interval_ms,
                                      scan_factory=scan_factory, clock=clock)
        self.monitor.state_changed.connect(self._state_changed)
        self.monitor.remaining_changed.connect(self._remaining_changed)
        self.monitor.ready.connect(self._ready)
        self.monitor.failure.connect(self._failure)
        self._start_timer = QTimer(self)
        self._start_timer.setSingleShot(True)
        self._start_timer.timeout.connect(self._start)
        self._start_timer.start(0)

    def _start(self):
        if not self.closing:
            self.monitor.start()

    def _state_changed(self, state):
        if state == RestartMonitor.WAIT_ONLINE and not self.closing:
            self.status_label.setText("Offline observed. Waiting for two verified Radmin responses…")
        if state in (RestartMonitor.CANCELLED, RestartMonitor.TIMED_OUT):
            self.password = ""

    def _remaining_changed(self, seconds):
        if not self.closing:
            self.countdown_label.setText(f"Time remaining: {seconds // 60}:{seconds % 60:02d}")

    def _ready(self):
        if self.closing or self._ready_emitted or self.monitor.state != RestartMonitor.READY:
            return
        self._ready_emitted = True
        password, self.password = self.password, ""
        self.ready.emit(dict(self.entry), password, self.mode)
        self.close()

    def _failure(self, message):
        self.password = ""
        if self.closing:
            return
        self.status_label.setText(message)
        self.countdown_label.clear()
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.cancel_button.setText("Close")
        self.failure.emit(message)

    def done(self, result):
        self.close()

    def closeEvent(self, event):
        self.closing = True
        self.password = ""
        self._start_timer.stop()
        self.monitor.cancel()
        event.accept()
