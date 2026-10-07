"""Manager close/quit integration; uses fake tray registration and fake sessions."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
import subprocess
import sys
from unittest.mock import patch

from PySide6.QtCore import QEvent, QObject, Signal
from PySide6.QtWidgets import QApplication

from radmin_viewer.app import MainWindow, ApplicationQuitGuard
from radmin_viewer.storage import AddressBook, defaults


class FakeTray(QObject):
    activated = Signal(object)
    def __init__(self, parent):
        super().__init__(parent)
        self.visible = False
    def setIcon(self, value): self.icon = value
    def setToolTip(self, value): self.tooltip = value
    def setContextMenu(self, value): self.menu = value
    def show(self): self.visible = True
    def hide(self): self.visible = False


class TrayIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.window = MainWindow(AddressBook(Path(self.root.name) / "book.json"), defaults())
        self.window.show()
        self.supported = True
        self.tray = self.window.enable_tray(tray_factory=FakeTray, availability=lambda: self.supported)
        self.shutdowns = []
        self.window.shutdown_ready.connect(lambda: self.shutdowns.append(True))

    def wait(self, predicate):
        end = time.monotonic() + 3
        while not predicate() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(.005)
        self.assertTrue(predicate())

    def tearDown(self):
        self.window.request_quit()
        self.wait(lambda: self.window._shutdown_complete)
        self.root.cleanup()

    def test_close_hides_and_preserves_work_until_explicit_quit(self):
        cleanup = threading.Event()
        closing = threading.Event()
        class Peer:
            authenticated = False
            def __init__(self, *args): pass
            def connect(self): self.authenticated = True
            def close(self):
                closing.set()
                cleanup.wait(2)
        entry = {"id": str(uuid.uuid4()), "name": "Test", "host": "localhost", "port": 4899,
                 "username": "test", "group": "General"}
        try:
            with patch("radmin_viewer.session_ui.RadminSession", Peer):
                session = self.window.launch_session("view", entry, "fixture")
                self.wait(lambda: "Authenticated" in session.status_label.text())
                self.window.close()
                self.assertFalse(self.window.isVisible())
                self.assertFalse(self.window.exiting)
                self.assertTrue(self.tray.tray.visible)
                self.assertTrue(session.worker.isRunning())
                self.assertFalse(closing.is_set())
                self.assertEqual(self.shutdowns, [])
                self.tray.open_action.trigger()
                self.assertTrue(self.window.isVisible())
                self.window.close()
                self.tray.quit_action.trigger()
                self.wait(closing.is_set)
                self.assertTrue(self.tray.tray.visible)
                self.assertFalse(self.tray.quit_action.isEnabled())
                self.assertEqual(self.shutdowns, [])
                cleanup.set()
                self.wait(lambda: self.window._shutdown_complete)
                self.assertFalse(self.tray.tray.visible)
                self.assertFalse(self.window.sessions)
                self.assertEqual(self.shutdowns, [True])
        finally:
            cleanup.set()

    def test_no_tray_fallback_quits_instead_of_orphaning_process(self):
        self.supported = False
        self.window.close()
        self.assertTrue(self.window._shutdown_complete)
        self.assertEqual(self.shutdowns, [True])
        self.assertFalse(self.tray.tray.visible)

    def test_service_loss_restores_hidden_window_and_close_tooltip(self):
        self.window.close()
        self.assertFalse(self.window.isVisible())
        self.supported = False
        self.tray.check_availability()
        self.assertTrue(self.window.isVisible())
        self.assertEqual(self.window.chrome.close_button.toolTip(), "Close")
        self.assertFalse(self.window.exiting)

    def test_activation_restores_and_schedules_quick_connect(self):
        self.window.close()
        entry = {"id": str(uuid.uuid4()), "name": "Quick", "host": "localhost", "port": 4899,
                 "username": "test", "group": "General"}
        with patch.object(self.window, "connect_session") as connect:
            self.window.handle_activation({"quick_entry": entry, "mode": "terminal"})
            self.assertTrue(self.window.isVisible())
            self.wait(lambda: connect.called)
            connect.assert_called_once_with("terminal", entry)

    def test_application_quit_event_is_routed_through_cleanup(self):
        self.window.quit_guard = ApplicationQuitGuard(self.app, self.window)
        QApplication.sendEvent(self.app, QEvent(QEvent.Type.Quit))
        self.assertTrue(self.window._shutdown_complete)
        self.assertFalse(self.window.quit_guard.active)
        self.assertFalse(self.tray.tray.visible)
        self.assertEqual(self.shutdowns, [True])

    def test_menu_quit_works_with_no_selected_computer_and_is_idempotent(self):
        self.assertIsNone(self.window.selected())
        self.assertTrue(self.window.quit_action.isEnabled())
        self.window.quit_action.trigger()
        self.window.request_quit()
        self.assertTrue(self.window._shutdown_complete)
        self.assertEqual(self.shutdowns, [True])

    def test_entrypoint_stays_alive_hidden_then_exits_and_releases_lock(self):
        config = Path(self.root.name) / "process-book.json"
        script = '''
import sys
from unittest.mock import patch
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication
from radmin_viewer.app import MainWindow, main
from radmin_viewer.system_tray import SystemTrayController

class Tray(QObject):
    activated = Signal(object)
    def setIcon(self, value): pass
    def setToolTip(self, value): pass
    def setContextMenu(self, value): pass
    def show(self): pass
    def hide(self): pass

app = QApplication([])
state = {"hidden": False, "restored": False}
def exercise():
    window = next(w for w in app.topLevelWidgets() if isinstance(w, MainWindow))
    window.close()
    state["hidden"] = not window.isVisible() and not window.exiting
    def finish():
        window.handle_activation(None)
        state["restored"] = window.isVisible()
        window.request_quit()
    QTimer.singleShot(100, finish)
QTimer.singleShot(0, exercise)
with patch("radmin_viewer.app.SystemTrayController",
           lambda window: SystemTrayController(window, tray_factory=Tray, availability=lambda: True)):
    result = main(["--config", sys.argv[1]])
assert result == 0 and all(state.values()), (result, state)
print("TRAY_LIFECYCLE_OK")
'''
        process = subprocess.run([sys.executable, "-c", script, str(config)],
                                 cwd=Path(__file__).resolve().parents[1],
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("TRAY_LIFECYCLE_OK", process.stdout)
        self.assertFalse(Path(str(config) + ".lock").exists())
