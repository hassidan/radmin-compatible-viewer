import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time
import unittest

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from radmin_viewer.restart_recovery import RestartMonitor, RestartRecoveryWindow
from radmin_viewer.scanner import ScanResult


ENTRY = {"id": "one", "host": "saved.example", "port": 4899,
         "username": "user", "name": "Saved"}


class FakeScanner(QObject):
    result = Signal(object)
    finished = Signal(bool)

    def __init__(self, parent, **bounds):
        super().__init__(parent)
        self.bounds = bounds
        self.cancelled = False
        self.targets = None

    def start(self, entries):
        self.targets = entries

    def send(self, status):
        self.result.emit(ScanResult(ENTRY["id"], ENTRY["host"], ENTRY["port"], status))

    def complete(self, status):
        self.send(status)
        self.finished.emit(False)

    def cancel(self):
        self.cancelled = True
        self.send("Cancelled")
        self.finished.emit(True)


class RestartRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.now = 10.0
        self.scans = []
        self.monitors = []
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            window.close()
        for monitor in self.monitors:
            monitor.cancel()
        self.app.processEvents()

    def factory(self, parent, **kwargs):
        scanner = FakeScanner(parent, **kwargs)
        self.scans.append(scanner)
        return scanner

    def monitor(self, **kwargs):
        monitor = RestartMonitor(ENTRY, scan_factory=self.factory,
                                 clock=lambda: self.now, **kwargs)
        self.monitors.append(monitor)
        monitor.start()
        return monitor

    def window(self, **kwargs):
        window = RestartRecoveryWindow(ENTRY, "secret", scan_factory=self.factory,
                                       clock=lambda: self.now, **kwargs)
        self.windows.append(window)
        return window

    def sample(self, monitor, status):
        monitor._poll()
        self.scans[-1].complete(status)

    def test_initial_online_requires_outage_and_exact_target(self):
        monitor = self.monitor()
        ready = []
        monitor.ready.connect(lambda: ready.append(True))
        for status in ("Online", "Online", "Port open", "Online"):
            self.sample(monitor, status)
        self.assertEqual(monitor.state, monitor.WAIT_OFFLINE)
        self.assertEqual(ready, [])
        for scanner in self.scans:
            self.assertEqual(scanner.targets, [{k: ENTRY[k] for k in ("id", "host", "port")}])
            self.assertEqual(scanner.bounds, {"concurrency": 1, "timeout_ms": 1500})
        self.sample(monitor, "Unreachable")
        self.assertEqual(monitor.state, monitor.WAIT_ONLINE)
        self.sample(monitor, "Online")
        self.assertEqual(ready, [])
        self.sample(monitor, "Online")
        self.assertEqual(ready, [True])
        self.assertFalse(monitor.running)
        self.assertIsNone(monitor._scanner)
        self.assertFalse(monitor._deadline_timer.isActive())
        self.scans[-1].complete("Online")
        monitor.start()
        monitor._poll()
        self.assertEqual(ready, [True])

    def test_unstable_online_resets_and_duplicate_result_is_one_sample(self):
        monitor = self.monitor()
        self.sample(monitor, "DNS error")
        for status in ("Online", "Port open", "Online", "Unreachable", "Online"):
            self.sample(monitor, status)
            self.assertEqual(monitor.state, monitor.WAIT_ONLINE)
        monitor._poll()
        scanner = self.scans[-1]
        scanner.send("Online")
        scanner.send("Online")
        self.assertTrue(monitor.running)
        scanner.finished.emit(False)
        self.assertEqual(monitor.state, monitor.READY)

    def test_no_overlap_wrong_endpoint_and_late_previous_scan(self):
        monitor = self.monitor()
        old = self.scans[-1]
        for _ in range(5):
            monitor._poll()
        self.assertEqual(len(self.scans), 1)
        old.result.emit(ScanResult("one", "other.example", 4899, "Unreachable"))
        old.finished.emit(False)
        self.assertEqual(monitor.state, monitor.WAIT_OFFLINE)
        monitor._poll()
        old.complete("Unreachable")
        self.scans[-1].complete("Online")
        self.assertEqual(monitor.state, monitor.WAIT_OFFLINE)

    def test_expiry_pending_dns_and_late_results(self):
        monitor = self.monitor(timeout_ms=100)
        failures, ready = [], []
        monitor.failure.connect(failures.append)
        monitor.ready.connect(lambda: ready.append(True))
        scanner = self.scans[-1]
        scanner.send("Checking…")
        self.now += .101
        monitor._check_deadline()
        self.assertEqual(monitor.state, monitor.TIMED_OUT)
        self.assertTrue(scanner.cancelled)
        self.assertIn("no offline transition", failures[0])
        scanner.complete("Unreachable")
        scanner.complete("Online")
        monitor._check_deadline()
        self.assertEqual(len(failures), 1)
        self.assertEqual(ready, [])

    def test_deadline_wins_over_second_online_callback(self):
        monitor = self.monitor(timeout_ms=100)
        self.sample(monitor, "Unreachable")
        self.sample(monitor, "Online")
        monitor._poll()
        self.now += .101
        self.scans[-1].complete("Online")
        self.assertEqual(monitor.state, monitor.TIMED_OUT)

    def test_real_deadline_timer_fires_while_scan_pending(self):
        monitor = RestartMonitor(ENTRY, timeout_ms=25, scan_factory=self.factory)
        self.monitors.append(monitor)
        monitor.start()
        end = time.monotonic() + 1
        while monitor.running and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(.002)
        self.assertEqual(monitor.state, monitor.TIMED_OUT)
        self.assertTrue(self.scans[-1].cancelled)

    def test_watchdog_is_not_offline_evidence_and_resets_streak(self):
        monitor = self.monitor()
        monitor._check_expired()
        self.assertEqual(monitor.state, monitor.WAIT_OFFLINE)
        self.sample(monitor, "Unreachable")
        self.sample(monitor, "Online")
        monitor._poll()
        monitor._check_expired()
        self.sample(monitor, "Online")
        self.assertEqual(monitor.state, monitor.WAIT_ONLINE)

    def test_cancel_aborts_timers_and_ignores_late_callbacks(self):
        monitor = self.monitor()
        scanner = self.scans[-1]
        monitor.cancel()
        scanner.complete("Unreachable")
        scanner.complete("Online")
        monitor._poll()
        self.assertEqual(monitor.state, monitor.CANCELLED)
        self.assertTrue(scanner.cancelled)
        self.assertEqual(len(self.scans), 1)
        self.assertFalse(any(t.isActive() for t in (monitor._poll_timer,
                         monitor._deadline_timer, monitor._check_timer, monitor._countdown_timer)))

    def test_window_ready_preserves_modes_and_clears_password_before_emit(self):
        for mode in ("view", "control", "terminal"):
            window = self.window(mode=mode)
            received = []
            window.ready.connect(lambda entry, password, mode, w=window:
                                 received.append((entry, password, mode, w.password, w.monitor.running)))
            window._start()
            for status in ("Unreachable", "Online", "Online"):
                self.sample(window.monitor, status)
            window._ready()
            self.assertEqual(received, [(ENTRY, "secret", mode, "", False)])
            self.assertTrue(window.closing)
            self.assertEqual(window.password, "")

    def test_window_timeout_visible_and_credentials_cleared(self):
        window = self.window(timeout_ms=100)
        window.show()
        window._start()
        failures = []
        window.failure.connect(failures.append)
        self.now += .101
        window.monitor._check_deadline()
        self.assertEqual(window.password, "")
        self.assertEqual(window.cancel_button.text(), "Close")
        self.assertTrue(window.isVisible())
        self.assertFalse(window.closing)
        self.assertEqual(len(failures), 1)

    def test_window_starts_on_next_gui_turn_and_cancel_button_cleans_up(self):
        window = self.window()
        self.assertEqual(self.scans, [])
        self.assertEqual(window.monitor.state, RestartMonitor.IDLE)
        self.app.processEvents()
        self.assertEqual(len(self.scans), 1)
        self.assertEqual(window.monitor.state, RestartMonitor.WAIT_OFFLINE)
        window.cancel_button.click()
        self.assertTrue(self.scans[-1].cancelled)
        self.assertEqual(window.password, "")
        self.assertTrue(window.closing)

    def test_close_before_deferred_start_and_escape_cleanup(self):
        window = self.window()
        window.close()
        window._start()
        self.assertEqual(self.scans, [])
        self.assertEqual(window.password, "")
        self.assertEqual(window.monitor.state, RestartMonitor.CANCELLED)
        other = self.window()
        other._start()
        other.reject()
        self.assertTrue(self.scans[-1].cancelled)
        self.assertEqual(other.password, "")
        self.assertTrue(other.closing)

    def test_invalid_mode_and_bounds(self):
        with self.assertRaises(ValueError):
            RestartRecoveryWindow(ENTRY, "secret", "power:restart")
        for kwargs in ({"timeout_ms": 0}, {"poll_interval_ms": 0}):
            with self.assertRaises(ValueError):
                RestartMonitor(ENTRY, **kwargs)


if __name__ == "__main__":
    unittest.main()
