"""UI/lifecycle tests use explicit test doubles; no network or real credentials."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from PySide6.QtCore import Qt, QEvent
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest

from radmin_viewer.app import MainWindow, ConnectionDialog, CredentialsDialog
from radmin_viewer.session_ui import SessionWindow
from radmin_viewer.storage import AddressBook, defaults


class TestSession:
    closed = False

    def __init__(self, *args):
        self.authenticated = False

    def connect(self):
        time.sleep(0.03)
        self.authenticated = True

    def close(self):
        type(self).closed = True


class UITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.entry = {"id": str(uuid.uuid4()), "name": "Lab", "host": "localhost", "port": 4899,
                      "username": "", "group": "General"}

    def wait_until(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertTrue(predicate())

    def dispose(self, window):
        window.close()
        self.wait_until(lambda: not window.worker.isRunning())
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_manager_search_selection_and_dialogs(self):
        with tempfile.TemporaryDirectory() as root:
            data = defaults()
            data["connections"] = [self.entry]
            window = MainWindow(AddressBook(Path(root) / "book.json"), data)
            window.show()
            item = window.tree.topLevelItem(0).child(0)
            window.tree.setCurrentItem(item)
            self.assertTrue(window.view_action.isEnabled())
            window.search.setText("no-match")
            self.assertEqual(window.tree.topLevelItemCount(), 0)
            self.assertFalse(window.view_action.isEnabled())
            window.search.setText("LOCALHOST")
            self.assertEqual(window.tree.topLevelItem(0).childCount(), 1)
            dialog = ConnectionDialog(data["groups"], self.entry, window)
            self.assertEqual(dialog.value(), self.entry)
            credentials = CredentialsDialog(self.entry, window)
            self.assertEqual(credentials.password.text(), "")
            window.close()

    def test_auth_only_status_and_shutdown(self):
        TestSession.closed = False
        with patch("radmin_viewer.session_ui.RadminSession", TestSession):
            window = SessionWindow(self.entry, "", defaults()["settings"])
            window.show()
            self.wait_until(lambda: "Authenticated" in window.status_label.text())
            self.assertIn("not integrated", window.status_label.text())
            self.assertFalse(window.surface.control)
            self.assertTrue(window.surface.image.isNull())
            self.dispose(window)
            self.assertTrue(TestSession.closed)

    def test_adapter_frame_input_mapping_and_scaling(self):
        events = []
        class Adapter:
            can_control = True
            def poll(self, timeout):
                time.sleep(timeout)
                image = QImage(320, 200, QImage.Format.Format_RGB32)
                image.fill(Qt.GlobalColor.blue)
                return image
            def handle_input(self, event):
                events.append(event)
            def close(self):
                pass
        with patch("radmin_viewer.session_ui.RadminSession", TestSession):
            window = SessionWindow(self.entry, "", defaults()["settings"], "control", lambda s, m: Adapter())
            window.show()
            self.wait_until(lambda: not window.surface.image.isNull())
            self.assertTrue(window.surface.control)
            rect = window.surface.target()
            QTest.mouseClick(window.surface, Qt.MouseButton.LeftButton, pos=rect.center())
            self.wait_until(lambda: len(events) >= 2)
            self.assertAlmostEqual(events[0]["x"], 160, delta=2)
            self.assertAlmostEqual(events[0]["y"], 100, delta=2)
            window.set_scaling(False)
            self.assertEqual(window.surface.size().width(), 320)
            window.toggle_fullscreen()
            self.assertTrue(window.isFullScreen())
            window.toggle_fullscreen()
            self.dispose(window)

    def test_close_during_authentication_is_deferred(self):
        with patch("radmin_viewer.session_ui.RadminSession", TestSession):
            window = SessionWindow(self.entry, "", defaults()["settings"])
            window.show()
            self.dispose(window)

    def test_failure_is_sanitized_and_session_closed(self):
        class BrokenSession(TestSession):
            def connect(self):
                raise RuntimeError("sensitive-exception-marker")
        with patch("radmin_viewer.session_ui.RadminSession", BrokenSession):
            window = SessionWindow(self.entry, "", defaults()["settings"])
            window.show()
            self.wait_until(lambda: window.failed_session)
            self.assertNotIn("sensitive-exception-marker", window.status_label.text())
            self.assertIn("Session failed", window.status_label.text())
            self.dispose(window)

    def test_view_mode_never_dispatches_input(self):
        calls = []
        class Adapter:
            can_control = True
            def poll(self, timeout):
                time.sleep(timeout)
                return None
            def handle_input(self, event):
                calls.append(event)
            def close(self):
                pass
        with patch("radmin_viewer.session_ui.RadminSession", TestSession):
            window = SessionWindow(self.entry, "", defaults()["settings"], "view", lambda s, m: Adapter())
            window.show()
            self.wait_until(lambda: "Starting desktop" in window.status_label.text())
            window.worker.enqueue_input({"type": "key_press", "key": 65})
            QTest.qWait(100)
            self.assertFalse(window.surface.control)
            self.assertEqual(calls, [])
            self.dispose(window)


if __name__ == "__main__":
    unittest.main()
