"""Offscreen transfer lifecycle tests with gated, worker-owned fake peers."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from radmin_viewer import transfer_ui
from radmin_viewer.protocol import AuthenticationError, TransportError


class TransferLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.auth_gate = threading.Event()
        self.channel_gate = threading.Event()
        self.service_gate = threading.Event()
        self.cleanup_gate = threading.Event()
        for gate in (self.auth_gate, self.channel_gate, self.service_gate, self.cleanup_gate):
            gate.set()
        self.auth_error = None
        self.verified = True
        self.operation_error = None
        self.closed = []
        self.calls = []
        self.windows = []
        test = self

        def record(name):
            test.calls.append((name, threading.get_ident()))

        class Session:
            authenticated = False

            def __init__(self, *args):
                record("session")

            def connect(self):
                record("connect")
                test.auth_gate.wait(3)
                if test.auth_error:
                    raise test.auth_error
                self.authenticated = test.verified

            def close(self):
                record("close session")
                test.cleanup_gate.wait(3)
                test.closed.append("session")

        class Channel:
            def __init__(self, session):
                record("channel")

            def start(self):
                record("start channel")
                test.channel_gate.wait(3)

            def close(self):
                record("close channel")
                test.cleanup_gate.wait(3)
                test.closed.append("channel")

        class Client:
            def __init__(self, channel, **kwargs):
                record("client")

            def start(self, **kwargs):
                record("start client")
                test.service_gate.wait(3)

            def list_directory(self, path, **kwargs):
                record("list")
                if test.operation_error:
                    raise test.operation_error
                return ()

            def close(self):
                record("close client")
                test.cleanup_gate.wait(3)
                test.closed.append("client")

        for name, fake in (("RadminSession", Session), ("EncryptedChannel", Channel),
                           ("FileTransferClient", Client)):
            mock = patch.object(transfer_ui, name, fake)
            mock.start()
            self.addCleanup(mock.stop)

    def tearDown(self):
        for gate in (self.auth_gate, self.channel_gate, self.service_gate, self.cleanup_gate):
            gate.set()
        for window in self.windows:
            if isValid(window):
                window.close()
                self.assertTrue(window.worker.wait(3000), "Transfer worker leaked")
                self.app.processEvents()
                window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def wait(self, predicate):
        end = time.monotonic() + 3
        while not predicate() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(.005)
        self.assertTrue(predicate())

    def window(self):
        window = transfer_ui.FileTransferWindow(
            {"name": "Lab", "host": "localhost", "port": 4899, "username": "user"},
            "secret-password", {"timeout": 1})
        window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.windows.append(window)
        window.show()
        return window

    def active_window(self):
        window = self.window()
        self.wait(lambda: window.remote_directory is not None)
        return window

    def test_wrong_password_retries_once_after_cleanup(self):
        self.auth_error = AuthenticationError("secret-password")
        self.cleanup_gate.clear()
        window = self.window()
        retries, authenticated = [], []
        window.authenticated.connect(lambda: authenticated.append(True))
        window.retry_requested.connect(lambda entry, mode, message: retries.append(
            (entry, mode, message, list(self.closed), window.worker.isRunning())))
        self.wait(lambda: window.retry_pending)
        window.request_retry()
        self.assertEqual(retries, [])
        self.assertTrue(window.isVisible())
        self.cleanup_gate.set()
        self.wait(lambda: not window.isVisible())
        self.assertEqual(len(retries), 1)
        entry, mode, message, closed, running = retries[0]
        self.assertEqual(entry, window.entry)
        self.assertIsNot(entry, window.entry)
        self.assertEqual(mode, "transfer")
        self.assertIn("Authentication", message)
        self.assertNotIn("secret-password", message)
        self.assertEqual(closed, ["session"])
        self.assertFalse(running)
        self.assertEqual(authenticated, [])
        window.request_retry()
        self.assertEqual(len(retries), 1)

    def test_verified_authentication_and_all_connection_stages(self):
        for gate in (self.auth_gate, self.channel_gate, self.service_gate):
            gate.clear()
        window = self.window()
        authenticated = []
        window.authenticated.connect(lambda: authenticated.append(True))
        self.assertFalse(window.worker.isRunning())
        self.wait(lambda: "Authenticating" in window.status_label.text())
        self.assertEqual(authenticated, [])
        self.assertTrue(window.progress.isVisible())
        self.assertEqual(window.progress.maximum(), 0)
        self.auth_gate.set()
        self.wait(lambda: "encrypted channel" in window.status_label.text())
        self.assertEqual(authenticated, [True])
        self.assertEqual(window.progress.maximum(), 0)
        self.channel_gate.set()
        self.wait(lambda: "file-transfer service" in window.status_label.text())
        self.assertEqual(window.progress.maximum(), 0)
        self.service_gate.set()
        self.wait(lambda: window.remote_directory is not None)
        self.assertEqual(authenticated, [True])
        self.assertEqual(window.worker.password, "")
        window.disconnect()
        self.wait(lambda: not window.isVisible())
        # Construction, all I/O, and cleanup stay on a single non-GUI thread.
        owners = {owner for _, owner in self.calls}
        self.assertEqual(len(owners), 1)
        self.assertNotIn(threading.get_ident(), owners)

    def test_unverified_connect_does_not_emit_save_success(self):
        self.verified = False
        window = self.window()
        authenticated, retries = [], []
        window.authenticated.connect(lambda: authenticated.append(True))
        window.retry_requested.connect(lambda *args: retries.append(args))
        self.wait(lambda: not window.isVisible())
        self.assertEqual(authenticated, [])
        self.assertEqual(len(retries), 1)
        self.assertEqual(self.closed, ["session"])

    def test_disconnect_waits_for_cleanup_then_closes(self):
        window = self.active_window()
        self.cleanup_gate.clear()
        window.cancel_button.click()
        self.wait(lambda: any(name == "close client" for name, _ in self.calls))
        self.assertTrue(window.isVisible())
        self.assertEqual(self.closed, [])
        self.cleanup_gate.set()
        self.wait(lambda: not window.isVisible())
        self.assertEqual(self.closed, ["client", "channel", "session"])
        self.assertFalse(window.worker.isRunning())

    def test_close_before_start_never_starts_worker(self):
        window = self.window()
        with patch.object(window.worker, "start", wraps=window.worker.start) as start:
            window.close()
            QTest.qWait(50)
            start.assert_not_called()
        self.assertEqual(self.calls, [])
        self.assertFalse(window.worker.isRunning())
        self.assertEqual(window.worker.password, "")

    def test_cancel_during_authentication_suppresses_success_and_retry(self):
        self.auth_gate.clear()
        window = self.window()
        events = []
        window.authenticated.connect(lambda: events.append("authenticated"))
        window.retry_requested.connect(lambda *args: events.append("retry"))
        self.wait(lambda: any(name == "connect" for name, _ in self.calls))
        window.close()
        self.assertTrue(window.isVisible())
        self.auth_gate.set()
        self.wait(lambda: not window.isVisible())
        self.assertEqual(events, [])
        self.assertEqual(self.closed, ["session"])

    def test_nonfatal_operation_error_keeps_session_usable(self):
        window = self.active_window()
        self.operation_error = ValueError("secret-password")
        window.browse_remote("invalid")
        self.wait(lambda: window.last_error)
        self.assertTrue(window.isVisible())
        self.assertTrue(window.connected)
        self.assertFalse(window.retry_button.isVisible())
        self.assertNotIn("secret-password", window.status_label.text())
        self.operation_error = None
        window.browse_remote("C:\\")
        self.wait(lambda: not window.busy)
        self.assertTrue(window.connected)

    def test_fatal_transport_retry_only_after_stopped(self):
        window = self.active_window()
        self.operation_error = TransportError("secret-password")
        self.cleanup_gate.clear()
        retries = []
        window.retry_requested.connect(lambda *args: retries.append(
            (args, list(self.closed), window.worker.isRunning())))
        window.browse_remote("C:\\")
        self.wait(lambda: window.last_error)
        self.assertTrue(window.retry_button.isVisible())
        self.assertFalse(window.retry_button.isEnabled())
        self.assertNotIn("secret-password", window.status_label.text())
        self.cleanup_gate.set()
        self.wait(lambda: window.worker_finished)
        self.assertTrue(window.isVisible())
        window.retry_button.click()
        self.assertFalse(window.isVisible())
        self.assertEqual(len(retries), 1)
        self.assertEqual(retries[0][1:], (["client", "channel", "session"], False))


if __name__ == "__main__":
    unittest.main()
