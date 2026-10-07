"""Offscreen fake-peer tests: no remote connections or local shell execution."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import queue
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from radmin_viewer.protocol import AuthenticationError, TransportError
from radmin_viewer.terminal_ui import TerminalWindow, TerminalWorker, ShellText


class TerminalUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.windows = []
        self.events = []
        self.sent = []
        self.replies = queue.Queue()
        self.hooks = {}
        owner = self

        def record(name):
            owner.events.append((name, threading.get_ident()))
            if name in owner.hooks:
                owner.hooks[name]()

        class Session:
            def __init__(self, host, port, username, password, timeout):
                self.timeout = timeout
                self.authenticated = False
                record("session")

            def connect(self):
                record("auth")
                self.authenticated = True

            def close(self):
                record("session_close")

        class Channel:
            def __init__(self, session):
                self.session = session
                record("channel")

            def start(self):
                record("channel_start")

            def close(self):
                record("channel_close")
                self.session.close()

        class Client:
            def __init__(self, channel, **kwargs):
                owner.assertEqual(kwargs, {"operation_timeout": 2., "io_timeout": 2.})
                self.channel = channel
                record("client")

            def start(self, cancel=None):
                record("client_start")

            def exchange(self, data=b"", cancel=None):
                record("exchange")
                owner.sent.append(data)
                try:
                    result = owner.replies.get_nowait()
                except queue.Empty:
                    return b""
                if isinstance(result, Exception):
                    raise result
                return result

            def close(self):
                record("client_close")
                self.channel.close()

        for name, replacement in (("RadminSession", Session), ("EncryptedChannel", Channel), ("TelnetClient", Client)):
            patcher = patch("radmin_viewer.terminal_ui." + name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        for window in self.windows:
            window.close()
            window.worker.wait(3000)
            self.app.processEvents()
            window.close()
            window.deleteLater()
        self.app.processEvents()

    def pump(self, predicate, timeout=3):
        end = time.monotonic() + timeout
        while not predicate() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(.002)
        self.assertTrue(predicate(), self.events)

    def window(self):
        window = TerminalWindow({"host": "fake.invalid", "port": 4899,
                                 "username": "fake", "name": "Fake"},
                                "SECRET", {"timeout": .5})
        window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.windows.append(window)
        window.show()
        return window

    def fail(self, stage, error):
        def fail():
            raise error
        self.hooks[stage] = fail

    def test_deferred_close_before_start(self):
        window = self.window()
        self.assertEqual(self.events, [])
        window.close()
        self.app.processEvents()
        self.assertEqual(self.events, [])
        self.assertFalse(window.worker.isRunning())
        self.assertEqual(window.worker.password, "")
        self.assertTrue(window.closing)

    def test_idle_keeps_alive_and_resources_are_worker_owned(self):
        window = self.window()
        auth = []
        window.authenticated.connect(lambda: auth.append(True))
        self.assertFalse(window.power_button.isEnabled())
        self.pump(lambda: len(self.sent) >= 3)
        self.assertEqual(auth, [True])
        self.assertTrue(window.connected)
        self.assertTrue(window.power_button.isEnabled())
        self.assertEqual(window.output.toPlainText(), "")
        window.close()
        self.pump(lambda: window._finished)
        self.assertFalse(window.isVisible())
        self.assertIn("client_close", [name for name, _ in self.events])
        self.assertEqual(len({tid for _, tid in self.events}), 1)
        self.assertNotEqual(self.events[0][1], threading.get_ident())

    def test_crlf_commands_history_draft_and_queue_rejection(self):
        window = self.window()
        self.pump(lambda: window.connected)
        self.assertIs(window.input, window.output)
        self.assertFalse(hasattr(window, "send_button"))
        self.assertTrue(window.output.hasFocus())
        window.input.setText("echo one")
        QTest.keyClick(window.input, Qt.Key.Key_Return)
        self.pump(lambda: b"echo one\r\n" in self.sent)
        self.assertEqual(self.sent.count(b"echo one\r\n"), 1)
        window.input.setText("draft")
        QTest.keyClick(window.input, Qt.Key.Key_Up)
        self.assertEqual(window.input.text(), "echo one")
        QTest.keyClick(window.input, Qt.Key.Key_Down)
        self.assertEqual(window.input.text(), "draft")
        with patch.object(window.worker, "submit", return_value=False):
            window.send_command()
        self.assertEqual(window.input.text(), "draft")
        self.assertEqual(window.input.history, ["echo one"])
        window.close()
        self.pump(lambda: window._finished)
        fresh = self.window()
        self.assertEqual(fresh.input.history, [])
        self.assertTrue(fresh.worker.commands.empty())

    def test_incremental_encoding_plaintext_and_controls(self):
        window = self.window()
        self.pump(lambda: window.connected)
        window.encoding.setCurrentIndex(1)
        window.worker.output.put(b"\xe2\x82")
        window._drain()
        self.assertEqual(window.output.toPlainText(), "")
        window.worker.output.put(b"\xac <b>literal</b>\r")
        window._drain()
        window.worker.output.put(b"\nabc\bD\rX")
        window._drain()
        self.assertEqual(window.output.toPlainText(), "€ <b>literal</b>\nXbD")
        window.clear_display()
        self.assertEqual(window.output.toPlainText(), "")
        window.encoding.setCurrentIndex(2)
        window.worker.output.put("שלום".encode("cp862"))
        window._drain()
        self.assertEqual(window.output.toPlainText(), "שלום")
        window.input.setText("שלום")
        window.send_command()
        self.pump(lambda: "שלום".encode("cp862") + b"\r\n" in self.sent)
        window.encoding.setCurrentIndex(3)
        window.input.setText("שלום")
        window.send_command()
        self.assertEqual(window.input.text(), "שלום")
        self.assertIn("cannot be encoded", window.status_label.text())

    def test_copy_and_interrupt(self):
        window = self.window()
        self.pump(lambda: window.connected)
        window._render("select me")
        cursor = window.output.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        window.output.setTextCursor(cursor)
        QTest.keyClick(window.output, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.app.clipboard().text(), "select me")
        self.assertNotIn(b"\x03", self.sent)
        window.input.setText("copy input")
        cursor = window.input.textCursor()
        cursor.setPosition(window.input.boundary)
        cursor.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
        window.input.setTextCursor(cursor)
        QTest.keyClick(window.input, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.app.clipboard().text(), "copy input")
        window.input.deselect()
        QTest.keyClick(window.input, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        self.pump(lambda: b"\x03" in self.sent)
        self.assertEqual(window.input.text(), "")
        window.interrupt_button.click()
        self.pump(lambda: self.sent.count(b"\x03") == 2)

    def test_output_limit_and_queue_limits(self):
        text = ShellText()
        result = text.feed("x" * (4 * text.LIMIT))
        self.assertEqual(len(result), text.LIMIT)
        result = text.feed("\n" + "line\n" * 40000)
        self.assertLessEqual(len(result), text.LIMIT)
        self.assertTrue(result.endswith("line\n"))
        worker = TerminalWorker({}, "", 1)
        worker.accepting.set()
        for _ in range(32):
            self.assertTrue(worker.submit(b"echo test\r\n"))
        self.assertFalse(worker.submit(b"overflow"))
        self.assertFalse(worker.submit(b"x" * 65537))
        worker.stop()
        self.assertFalse(worker.submit(b"after stop"))

    def test_auth_retry_is_after_cleanup_and_close(self):
        self.fail("auth", AuthenticationError("SECRET"))
        window = self.window()
        retry = []
        window.retry_requested.connect(lambda *args: retry.append(
            (args, window.worker.isRunning(), window.isVisible(), list(self.events))))
        self.pump(lambda: bool(retry))
        args, running, visible, events = retry[0]
        self.assertEqual(args[1], "terminal")
        self.assertNotIn("SECRET", args[2])
        self.assertFalse(running)
        self.assertFalse(visible)
        self.assertIn("session_close", [name for name, _ in events])
        self.assertEqual(len(retry), 1)

    def test_pre_active_failure_stays_visible_and_does_not_retry(self):
        self.fail("client_start", AuthenticationError("SECRET"))
        window = self.window()
        retries, errors = [], []
        window.retry_requested.connect(lambda *args: retries.append(args))
        window.session_error.connect(errors.append)
        self.pump(lambda: window._finished)
        self.assertTrue(window.isVisible())
        self.assertFalse(window.connected)
        self.assertEqual(retries, [])
        self.assertEqual(len(errors), 1)
        self.assertNotIn("SECRET", errors[0])

    def test_active_failure_closes_with_notice_after_cleanup(self):
        window = self.window()
        errors = []
        window.session_error.connect(lambda message: errors.append((message, window.worker.isRunning())))
        self.pump(lambda: window.connected)
        self.replies.put(TransportError("SECRET"))
        self.pump(lambda: window._finished)
        self.assertFalse(window.isVisible())
        self.assertEqual(len(errors), 1)
        self.assertFalse(errors[0][1])
        self.assertNotIn("SECRET", errors[0][0])

    def test_cancel_during_auth_and_output_backpressure(self):
        entered, release = threading.Event(), threading.Event()

        def gate():
            entered.set()
            release.wait(2)
        self.hooks["auth"] = gate
        window = self.window()
        retries = []
        window.retry_requested.connect(lambda *args: retries.append(args))
        self.pump(entered.is_set)
        window.close()
        self.assertTrue(window.worker.isRunning())
        release.set()
        self.pump(lambda: window._finished)
        self.assertEqual(retries, [])
        self.assertNotIn("channel", [name for name, _ in self.events])
        self.hooks.clear()
        fresh = self.window()
        self.pump(lambda: fresh.connected)
        fresh._drain_timer.stop()
        for _ in range(20):
            self.replies.put(b"bounded output")
            fresh.worker.submit(b"echo test\r\n")
        self.pump(lambda: fresh.worker.output.full())
        fresh.close()
        self.pump(lambda: fresh._finished)
        self.assertTrue(fresh.worker.commands.empty())

    def test_power_menu_only_routes_signal(self):
        window = self.window()
        routed = []
        window.power_requested.connect(lambda *args: routed.append(args))
        self.pump(lambda: window.connected)
        action = next(a for a in window.power_button.menu().actions() if not a.isSeparator())
        action.trigger()
        self.assertEqual(routed, [(window.entry, action.data())])
        self.assertFalse(any(self.sent))

    def test_immediate_command_reply_is_rendered(self):
        window = self.window()
        self.pump(lambda: window.connected)
        self.replies.put(b"echo result\r\nresult\r\n")
        window.input.setText("echo result")
        QTest.keyClick(window.output, Qt.Key.Key_Return)
        self.pump(lambda: "result" in window.output.toPlainText().splitlines())
        self.assertIn(b"echo result\r\n", self.sent)

    def test_cancel_in_flight_exchange_suppresses_errors(self):
        window = self.window()
        self.pump(lambda: window.connected)
        entered, release = threading.Event(), threading.Event()

        def blocked_exchange():
            entered.set()
            release.wait(2)
            raise TransportError("SECRET")
        self.hooks["exchange"] = blocked_exchange
        errors = []
        window.session_error.connect(errors.append)
        self.pump(entered.is_set)
        window.close()
        self.assertTrue(window.worker.isRunning())
        release.set()
        self.pump(lambda: window._finished)
        self.assertEqual(errors, [])
        self.assertFalse(window.isVisible())

    def test_generic_connect_failure_never_requests_password_retry(self):
        self.fail("auth", TransportError("SECRET"))
        window = self.window()
        retries = []
        window.retry_requested.connect(lambda *args: retries.append(args))
        self.pump(lambda: window._finished)
        self.assertEqual(retries, [])
        self.assertTrue(window.isVisible())
        self.assertFalse(window.power_button.isEnabled())
        self.assertNotIn("SECRET", window.status_label.text())


if __name__ == "__main__":
    unittest.main()
