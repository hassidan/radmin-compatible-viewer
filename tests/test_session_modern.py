"""Real offscreen Qt lifecycle, overlay and input tests; deterministic fake peers."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import queue
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QPoint, Qt, QEvent
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QToolButton
from shiboken6 import isValid

from radmin_viewer.protocol import AuthenticationError, ProtocolError, TransportError
from radmin_viewer.session_ui import SessionWindow


class ModernSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.auth_gate = threading.Event()
        self.cleanup_gate = threading.Event()
        self.auth_gate.set()
        self.cleanup_gate.set()
        self.auth_error = None
        self.closed = []
        self.events = []
        self.frames = queue.Queue()
        self.windows = []
        test = self

        class Peer:
            authenticated = False
            def __init__(self, *args): pass
            def connect(self):
                test.auth_gate.wait(3)
                if test.auth_error:
                    raise test.auth_error
                self.authenticated = True
            def close(self):
                test.cleanup_gate.wait(3)
                test.closed.append("session")

        class Adapter:
            can_control = True
            def poll(self, timeout):
                try:
                    frame = test.frames.get(timeout=timeout)
                except queue.Empty:
                    return None
                if isinstance(frame, Exception):
                    raise frame
                return frame
            def handle_input(self, event):
                test.events.append(event)
            def close(self):
                test.cleanup_gate.wait(3)
                test.closed.append("adapter")

        self.adapter = Adapter
        self.peer_patch = patch("radmin_viewer.session_ui.RadminSession", Peer)
        self.peer_patch.start()

    def tearDown(self):
        self.auth_gate.set()
        self.cleanup_gate.set()
        for window in self.windows:
            if isValid(window):
                window.close()
                window.worker.wait(3000)
                self.app.processEvents()
                window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.peer_patch.stop()

    def wait(self, predicate):
        end = time.monotonic() + 3
        while not predicate() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(.005)
        self.assertTrue(predicate())

    def window(self):
        window = SessionWindow(
            {"name": "Lab", "host": "localhost", "port": 4899, "username": "user"},
            "test-only", {"scaling": "fit", "timeout": 1, "fullscreen": False},
            "control", lambda s, m: self.adapter())
        # Retain the closed widget for post-cleanup state assertions.
        window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.windows.append(window)
        window.show()
        return window

    def image(self, color):
        image = QImage(320, 200, QImage.Format.Format_RGB32)
        image.fill(color)
        return image

    def active_window(self):
        window = self.window()
        self.frames.put(self.image(Qt.GlobalColor.blue))
        self.wait(lambda: window.active_session)
        return window

    def test_deferred_authentication_loader_and_initial_frame(self):
        self.auth_gate.clear()
        window = self.window()
        authenticated = []
        window.authenticated.connect(lambda: authenticated.append(True))
        self.assertFalse(window.worker.isRunning())
        self.assertIs(window.pages.currentWidget(), window.panel)
        self.assertTrue(window.progress.isVisible())
        self.assertTrue(window.cancel_button.isEnabled())
        self.wait(window.worker.isRunning)
        self.assertEqual(authenticated, [])
        self.auth_gate.set()
        self.wait(lambda: authenticated == [True])
        self.assertIs(window.pages.currentWidget(), window.panel)
        self.assertIn("Waiting for", window.loader_label.text())
        self.frames.put(self.image(Qt.GlobalColor.green))
        self.wait(lambda: window.active_session)
        self.assertIs(window.pages.currentWidget(), window.scroll)
        self.assertEqual(window.surface.image.pixelColor(0, 0), Qt.GlobalColor.green)

    def test_auth_failure_retries_once_only_after_cleanup(self):
        self.auth_error = AuthenticationError()
        self.cleanup_gate.clear()
        window = self.window()
        retries, authenticated = [], []
        window.authenticated.connect(lambda: authenticated.append(True))
        def retry(entry, mode, message):
            retries.append((entry, mode, message, list(self.closed), window.worker.isRunning()))
        window.retry_requested.connect(retry)
        self.wait(lambda: window.retry_pending)
        window.request_retry()
        self.assertEqual(retries, [])
        self.assertTrue(window.isVisible())
        self.cleanup_gate.set()
        self.wait(lambda: not window.isVisible())
        self.assertEqual(len(retries), 1)
        self.assertEqual(retries[0][0], window.entry)
        self.assertEqual(retries[0][1], "control")
        self.assertIn("Authentication", retries[0][2])
        self.assertEqual(retries[0][3:], (["session"], False))
        self.assertEqual(authenticated, [])
        window.request_retry()
        self.assertEqual(len(retries), 1)

    def test_disconnect_closes_only_after_adapter_and_session_cleanup(self):
        window = self.active_window()
        self.cleanup_gate.clear()
        window.disconnect_action.trigger()
        QTest.qWait(80)
        self.assertTrue(window.isVisible())
        self.assertFalse(window.surface.control)
        self.assertEqual(self.closed, [])
        self.cleanup_gate.set()
        self.wait(lambda: not window.isVisible())
        self.assertEqual(self.closed, ["adapter", "session"])
        self.assertTrue(window.fullscreen_controls.disposed)
        self.assertFalse(window.surface.image.isNull())

    def test_close_before_start_never_connects(self):
        window = self.window()
        window.close()
        QTest.qWait(50)
        self.assertFalse(window.worker.isRunning())
        self.assertEqual(self.closed, [])
        self.assertEqual(window.worker.password, "")
        self.assertTrue(window.fullscreen_controls.disposed)

    def test_cancel_during_authentication_waits_and_does_not_authenticate(self):
        self.auth_gate.clear()
        self.cleanup_gate.clear()
        window = self.window()
        authenticated = []
        window.authenticated.connect(lambda: authenticated.append(True))
        self.wait(window.worker.isRunning)
        window.cancel_button.click()
        self.assertTrue(window.isVisible())
        self.auth_gate.set()
        QTest.qWait(50)
        self.assertTrue(window.isVisible())
        self.assertEqual(authenticated, [])
        self.cleanup_gate.set()
        self.wait(lambda: not window.isVisible())
        self.assertEqual(self.closed, ["session"])

    def test_retry_after_worker_finished_and_real_delete_before_start(self):
        window = self.active_window()
        self.frames.put(ProtocolError())
        self.wait(lambda: window.worker_stopped)
        retries = []
        window.retry_requested.connect(lambda *args: retries.append(window.worker.isRunning()))
        window.retry_button.click()
        self.assertEqual(retries, [False])
        self.assertFalse(window.isVisible())
        early = self.window()
        early.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        early.close()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QTest.qWait(20)
        self.assertFalse(isValid(early))

    def test_generic_protocol_failure_stays_visible_and_retry_waits(self):
        window = self.active_window()
        self.cleanup_gate.clear()
        self.frames.put(ProtocolError("sensitive detail"))
        self.wait(lambda: window.failed_session)
        self.assertTrue(window.isVisible())
        self.assertIs(window.pages.currentWidget(), window.panel)
        self.assertFalse(window.progress.isVisible())
        self.assertNotIn("sensitive", window.loader_label.text())
        retries = []
        window.retry_requested.connect(lambda *args: retries.append((args, list(self.closed))))
        window.retry_button.click()
        window.request_retry()
        self.assertEqual(retries, [])
        self.cleanup_gate.set()
        self.wait(lambda: not window.isVisible())
        self.assertEqual(len(retries), 1)
        self.assertEqual(retries[0][1], ["adapter", "session"])

    def test_remote_close_after_active_closes_but_early_network_error_has_retry(self):
        window = self.active_window()
        errors = []
        window.session_error.connect(errors.append)
        self.frames.put(TransportError())
        self.wait(lambda: not window.isVisible())
        self.assertEqual(len(errors), 1)
        self.assertTrue(window.worker_stopped)
        early = self.window()
        self.frames.put(TransportError())
        self.wait(lambda: early.worker_stopped)
        self.assertTrue(early.isVisible())
        self.assertTrue(early.retry_button.isVisible())

    def test_latest_frame_retained_after_failure(self):
        window = self.active_window()
        # Backpressure retains the newest frame even when no further frame arrives.
        window.worker.frame_consumed.clear()
        self.frames.put(self.image(Qt.GlobalColor.red))
        self.frames.put(self.image(Qt.GlobalColor.green))
        self.wait(self.frames.empty)
        window.worker.frame_consumed.set()
        self.wait(lambda: window.surface.image.pixelColor(0, 0) == Qt.GlobalColor.green)
        self.frames.put(ProtocolError())
        self.wait(lambda: window.worker_stopped)
        self.assertEqual(window.surface.image.pixelColor(0, 0), Qt.GlobalColor.green)

    def test_fullscreen_hide_reveal_pin_menus_and_local_keys(self):
        window = self.active_window()
        window.surface.setFocus()
        QTest.keyClick(window.surface, Qt.Key.Key_F11)
        self.assertTrue(window.isFullScreen())
        overlay = window.fullscreen_controls
        self.assertEqual(overlay.hide_timer.interval(), 1500)
        QTest.qWait(220)
        geometry = window.scroll.geometry()
        target = window.surface.target()
        self.wait(lambda: not overlay.revealed)
        QTest.qWait(220)
        self.assertEqual(overlay.y(), 6 - overlay.height())
        self.assertEqual(window.scroll.geometry(), geometry)
        self.assertEqual(window.surface.target(), target)
        QTest.mouseMove(window.surface, QPoint(window.surface.width() // 2, 0))
        self.wait(lambda: overlay.revealed)
        overlay.pin_action.setChecked(True)
        overlay.hide_dashboard()
        self.assertTrue(overlay.revealed)
        self.assertFalse(overlay.hide_timer.isActive())
        overlay.pin_action.setChecked(False)
        window.clipboard_menu.popup(window.mapToGlobal(QPoint(100, 100)))
        self.app.processEvents()
        self.assertIn(window.clipboard_menu, overlay.open_menus)
        overlay.hide_dashboard()
        self.assertTrue(overlay.revealed)
        self.assertFalse(overlay.hide_timer.isActive())
        window.clipboard_menu.close()
        self.assertTrue(overlay.hide_timer.isActive())
        window.surface.setFocus()
        QTest.keyClick(window.surface, Qt.Key.Key_Escape)
        self.assertFalse(window.isFullScreen())
        self.assertFalse(overlay.isVisible())
        QTest.keyClick(window.surface, Qt.Key.Key_Escape)
        self.wait(lambda: any(e.get("key") == Qt.Key.Key_Escape for e in self.events))
        keys = [e for e in self.events if e["type"].startswith("key_")]
        self.assertEqual([e["key"] for e in keys], [Qt.Key.Key_Escape, Qt.Key.Key_Escape])
        for button in window.toolbar.findChildren(QToolButton):
            if button.defaultAction() is not None:
                self.assertTrue(button.accessibleName())
                self.assertTrue(button.toolTip())
                self.assertFalse(button.icon().isNull())
        self.assertEqual(window.clipboard_menu.actions(), list(window.clipboard_actions))


if __name__ == "__main__":
    unittest.main()
