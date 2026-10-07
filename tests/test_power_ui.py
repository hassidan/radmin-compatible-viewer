"""Offscreen, fake-only power UI contracts. No network or power commands."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from radmin_viewer.power import POWER_DESCRIPTORS, PowerAction, PowerFailure, PowerOutcome, PowerResult
from radmin_viewer.power_ui import PowerWindow
from radmin_viewer.protocol import AuthenticationError


class PowerUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.windows = []
        self.events = []
        self.hooks = {}
        self.releases = []
        self.outcome = PowerOutcome.ACCEPTED
        self.failure = None
        self.code = None
        self.dispatched = True
        self.auth_ok = True
        owner = self

        def record(name):
            owner.events.append((name, threading.get_ident()))
            hook = owner.hooks.get(name)
            if hook:
                hook()

        class Session:
            def __init__(self, host, port, username, password, timeout):
                record("session")
                self.authenticated = False

            def connect(self):
                record("auth")
                self.authenticated = owner.auth_ok

            def close(self):
                record("session_close")

        class Channel:
            def __init__(self, session):
                self.session = session
                self.closed = False
                record("channel")

            def start(self):
                record("channel_start")
                return self

            def close(self):
                if not self.closed:
                    self.closed = True
                    record("channel_close")
                    self.session.close()

        class Client:
            def __init__(self, channel):
                self.channel = channel
                record("client")

            def execute(self, action, cancel=None):
                try:
                    record("execute")
                    if cancel.is_set() and not owner.dispatched:
                        outcome = PowerOutcome.CANCELLED
                    else:
                        record("dispatch")
                        outcome = owner.outcome
                    return PowerResult(action, POWER_DESCRIPTORS[action].method,
                                       outcome, owner.dispatched, "SECRET <b>unsafe</b>",
                                       owner.failure, owner.code)
                finally:
                    self.channel.close()

        for name, replacement in (("RadminSession", Session), ("EncryptedChannel", Channel), ("PowerClient", Client)):
            patcher = patch("radmin_viewer.power_ui." + name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        for release in self.releases:
            release.set()
        for window in self.windows:
            window.worker.stop()
            window.worker.wait(2000)
            self.app.processEvents()
            window.close()
            window.deleteLater()
        self.app.processEvents()

    def pump(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.001)
        self.assertTrue(predicate(), self.events)

    def window(self, action=PowerAction.RESTART, name="Fake peer"):
        window = PowerWindow({"host": "invalid.example", "port": 4899,
                              "username": "fake-user", "name": name},
                             "SECRET", {"timeout": .1}, action)
        # Retain QObject for assertions after close. Production defaults to delete.
        window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.windows.append(window)
        window.show()
        return window

    def names(self):
        return [name for name, _ in self.events]

    def gate(self, name):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)

        def hook():
            entered.set()
            if not release.wait(2):
                raise RuntimeError("fake peer gate timed out")
        self.hooks[name] = hook
        return entered, release

    def fail(self, name, error):
        def hook():
            raise error
        self.hooks[name] = hook

    def test_verified_authentication_saved_before_finished_and_result_stays_open(self):
        window = self.window()
        signals = []
        window.authenticated.connect(lambda: signals.append("save"))
        window.worker.finished.connect(lambda: signals.append("finished"))
        self.assertEqual(self.events, [])
        self.pump(lambda: window._finished)
        self.assertEqual(signals, ["save", "finished"])
        self.assertEqual(window.result_value.outcome, PowerOutcome.ACCEPTED)
        self.assertTrue(window.isVisible())
        self.assertFalse(window.closing)
        self.assertEqual(window.windowModality(), Qt.WindowModality.NonModal)
        self.assertIn("Server acknowledged", window.status_label.text())
        self.assertIn("unverified", window.status_label.text())
        self.assertNotIn("SECRET", window.status_label.text())
        self.assertEqual(window.cancel_button.text(), "Close")
        self.assertEqual(self.names().count("execute"), 1)
        self.assertEqual(self.names()[-2:], ["channel_close", "session_close"])
        self.assertTrue(all(tid != threading.get_ident() for _, tid in self.events))
        self.assertEqual(len(set(tid for _, tid in self.events)), 1)
        window.cancel_button.click()
        self.assertFalse(window.isVisible())
        self.assertTrue(window.closing)
        self.assertFalse(window.worker.isRunning())

    def test_wrong_auth_cleanup_then_exactly_one_retry(self):
        for raises in (False, True):
            with self.subTest(raises=raises):
                self.events.clear()
                self.auth_ok = False
                if raises:
                    self.fail("auth", AuthenticationError("SECRET"))
                window = self.window(PowerAction.SLEEP)
                saved, retries = [], []
                window.authenticated.connect(lambda: saved.append(True))

                def retry(entry, mode, message):
                    self.assertFalse(window.worker.isRunning())
                    self.assertTrue(window.closing)
                    self.assertFalse(window.isVisible())
                    self.assertEqual(self.names()[-1], "session_close")
                    retries.append((entry, mode, message))
                window.retry_requested.connect(retry)
                self.pump(lambda: window._finished)
                self.assertEqual(saved, [])
                self.assertEqual(len(retries), 1)
                self.assertEqual(retries[0][1], "power:sleep")
                self.assertNotIn("SECRET", retries[0][2])
                self.assertNotIn("channel", self.names())
                window._worker_finished()
                self.assertEqual(len(retries), 1)

    def test_close_before_deferred_start_performs_no_operation(self):
        window = self.window()
        window.close()
        self.app.processEvents()
        self.assertEqual(self.events, [])
        self.assertFalse(window.worker.isRunning())
        self.assertEqual(window.worker.password, "")
        self.assertEqual(window.result_value.outcome, PowerOutcome.CANCELLED)

    def test_cancel_before_start_stays_open(self):
        window = self.window()
        window.cancel_button.click()
        self.app.processEvents()
        self.assertEqual(self.events, [])
        self.assertTrue(window.isVisible())
        self.assertEqual(window.result_value.outcome, PowerOutcome.CANCELLED)
        self.assertEqual(window.cancel_button.text(), "Close")

    def test_cancel_at_each_setup_boundary_prevents_execute(self):
        for boundary in ("session", "auth", "channel", "channel_start", "client"):
            with self.subTest(boundary=boundary):
                self.events.clear()
                self.hooks.clear()
                entered, release = self.gate(boundary)
                window = self.window()
                retries = []
                window.retry_requested.connect(lambda *args: retries.append(args))
                self.pump(entered.is_set)
                window.cancel_button.click()
                release.set()
                self.pump(lambda: window._finished)
                self.assertNotIn("execute", self.names())
                self.assertEqual(window.result_value.outcome, PowerOutcome.CANCELLED)
                self.assertEqual(self.names().count("session_close"), 1)
                self.assertEqual(retries, [])
                self.assertTrue(window.isVisible())

    def test_backend_gets_cancel_event_before_dispatch(self):
        self.dispatched = False
        entered, release = self.gate("execute")
        window = self.window()
        self.pump(entered.is_set)
        window.cancel_button.click()
        release.set()
        self.pump(lambda: window._finished)
        self.assertEqual(window.result_value.outcome, PowerOutcome.CANCELLED)
        self.assertNotIn("dispatch", self.names())

    def test_unknown_lost_ack_is_visible_and_never_retried(self):
        self.outcome = PowerOutcome.UNKNOWN
        window = self.window()
        retries = []
        window.retry_requested.connect(lambda *args: retries.append(args))
        self.pump(lambda: window._finished)
        self.assertIn("Unknown", window.status_label.text())
        self.assertTrue(window.isVisible())
        self.assertEqual(retries, [])
        self.assertEqual(self.names().count("execute"), 1)

    def test_cancel_after_dispatch_keeps_unknown_result_open(self):
        self.outcome = PowerOutcome.UNKNOWN
        entered, release = self.gate("dispatch")
        window = self.window()
        retries = []
        window.retry_requested.connect(lambda *args: retries.append(args))
        self.pump(entered.is_set)
        window.cancel_button.click()
        release.set()
        self.pump(lambda: window._finished)
        self.assertEqual(window.result_value.outcome, PowerOutcome.UNKNOWN)
        self.assertTrue(window.isVisible())
        self.assertEqual(window.cancel_button.text(), "Close")
        self.assertEqual(retries, [])

    def test_production_delete_on_close_waits_for_thread_and_destroys_both(self):
        entered, release = self.gate("dispatch")
        window = self.window()
        worker = window.worker
        self.pump(entered.is_set)
        window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        window.close()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertTrue(isValid(window))
        self.assertTrue(isValid(worker))
        self.assertTrue(worker.isRunning())
        release.set()
        worker.wait(2000)
        self.assertFalse(worker.isRunning())
        # Teardown owns only surviving objects; production close deletes these.
        self.windows.remove(window)
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(window))
        self.assertFalse(isValid(worker))
        self.assertEqual(self.names().count("session_close"), 1)

    def test_close_during_dispatch_waits_for_cleanup_and_reports_outcome(self):
        for outcome in (PowerOutcome.UNKNOWN, PowerOutcome.ACCEPTED):
            with self.subTest(outcome=outcome):
                self.events.clear()
                self.outcome = outcome
                entered, release = self.gate("dispatch")
                window = self.window()
                notices, retries = [], []
                window.session_error.connect(notices.append)
                window.retry_requested.connect(lambda *args: retries.append(args))
                self.pump(entered.is_set)
                window.reject()  # Escape/title-close uses this path too.
                self.assertTrue(window.closing)
                self.assertTrue(window.isVisible())
                self.assertTrue(window.worker.isRunning())
                self.assertNotIn("session_close", self.names())
                release.set()
                self.pump(lambda: window._finished)
                self.assertFalse(window.isVisible())
                self.assertFalse(window.worker.isRunning())
                self.assertEqual(len(notices), 1)
                self.assertNotIn("SECRET", notices[0])
                self.assertEqual(retries, [])
                self.assertEqual(window.result_value.outcome, outcome)
                self.assertEqual(self.names().count("session_close"), 1)

    def test_generic_setup_failure_is_sanitized_not_sent_without_retry(self):
        for boundary in ("session", "auth", "channel", "channel_start", "client"):
            with self.subTest(boundary=boundary):
                self.events.clear()
                self.hooks.clear()
                self.fail(boundary, RuntimeError("SECRET username payload"))
                window = self.window()
                retries = []
                window.retry_requested.connect(lambda *args: retries.append(args))
                self.pump(lambda: window._finished)
                self.assertEqual(window.result_value.outcome, PowerOutcome.NOT_SENT)
                self.assertNotIn("SECRET", window.status_label.text())
                self.assertNotIn("execute", self.names())
                self.assertEqual(retries, [])
                self.assertTrue(window.isVisible())

    def test_unexpected_backend_exception_cannot_claim_not_sent(self):
        self.fail("execute", RuntimeError("SECRET"))
        window = self.window()
        self.pump(lambda: window._finished)
        self.assertEqual(window.result_value.outcome, PowerOutcome.UNKNOWN)
        self.assertNotIn("SECRET", window.status_label.text())
        self.assertEqual(self.names().count("session_close"), 1)

    def test_permission_and_unsupported_codes_and_adaptive_layout(self):
        for outcome, code, failure, expected in (
                (PowerOutcome.REJECTED, 1300, PowerFailure.PERMISSION, "Permission denied"),
                (PowerOutcome.REJECTED, 50, PowerFailure.REMOTE_ERROR, "Rejected"),
                (PowerOutcome.UNAVAILABLE, None, PowerFailure.UNAVAILABLE, "unavailable")):
            with self.subTest(outcome=outcome, code=code):
                self.outcome, self.code, self.failure = outcome, code, failure
                window = self.window(PowerAction.HIBERNATE, "<b>long target</b> " * 400)
                self.pump(lambda: window._finished)
                window._fit_content()
                self.assertIn(expected, window.status_label.text())
                if code:
                    self.assertIn(f"0x{code:08x}", window.status_label.text())
                if failure == PowerFailure.REMOTE_ERROR:
                    self.assertNotIn("unsupported", window.status_label.text())
                bounds = window.screen().availableGeometry()
                self.assertLessEqual(window.height(), bounds.height())
                self.assertLessEqual(window.width(), bounds.width())
                self.assertTrue(window.rect().contains(window.cancel_button.mapTo(window, window.cancel_button.rect().center())))


if __name__ == "__main__":
    unittest.main()
