"""Power-menu routing and login integration. No live/network power actions."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication, QDialog, QToolButton

from radmin_viewer.app import MainWindow
from radmin_viewer.power import PowerAction
from radmin_viewer.storage import AddressBook, defaults


class FakeWorker(QObject):
    finished = Signal()


class FakePowerWindow(QDialog):
    authenticated = Signal()
    retry_requested = Signal(object, str, str)
    session_error = Signal(str)
    operation_finished = Signal()

    def __init__(self, entry, password, settings, action):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.worker = FakeWorker(self)
        self.entry = entry
        self.action = action
        self.closing = False


class PowerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.data = defaults()
        self.entry = {"id": str(uuid.uuid4()), "name": "Lab", "host": "localhost", "port": 4899,
                      "username": "test", "group": "General"}
        self.data["connections"].append(self.entry)
        self.window = MainWindow(AddressBook(Path(self.root.name) / "book.json"), self.data)

    def wait(self, predicate):
        end = time.monotonic() + 3
        while not predicate() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(.003)
        self.assertTrue(predicate())

    def tearDown(self):
        self.window.close()
        self.wait(lambda: not self.window.sessions)
        self.root.cleanup()

    def test_five_actions_single_computer_only_and_cancel_sends_nothing(self):
        w = self.window
        actions = [a for a in w.power_menu.actions() if not a.isSeparator()]
        self.assertEqual([a.data() for a in actions], [a.value for a in PowerAction])
        self.assertFalse(w.power_action.isEnabled())
        w.grid.setCurrentRow(0)
        self.assertTrue(w.power_action.isEnabled())
        with patch("radmin_viewer.app.confirm", return_value=False), patch.object(w, "connect_session") as connect:
            actions[0].trigger()
            connect.assert_not_called()
        w.grid.set_selection(group="General", emit=True)
        self.assertFalse(w.power_action.isEnabled())
        self.assertFalse(w.power_menu.isEnabled())

    def test_confirmation_has_exact_target_action_and_permission(self):
        w = self.window
        w.grid.setCurrentRow(0)
        for action in PowerAction:
            with patch("radmin_viewer.app.confirm", return_value=True) as confirm, \
                    patch.object(w, "connect_session") as connect:
                w.request_power(action)
            self.assertIn("localhost:4899", confirm.call_args.args[2])
            self.assertIn("Lab", confirm.call_args.args[2])
            required = "Telnet" if action in (PowerAction.SLEEP, PowerAction.HIBERNATE) else "Shutdown"
            self.assertIn(required, confirm.call_args.args[2])
            self.assertEqual(connect.call_args.args[0], "power:" + action.value)
            self.assertEqual(connect.call_args.args[1], self.entry)

    def test_one_open_power_request_per_endpoint_and_cleanup(self):
        w = self.window
        with patch("radmin_viewer.app.PowerWindow", FakePowerWindow), patch.object(w, "notify") as notice:
            first = w.launch_session("power:restart", self.entry, "fixture")
            self.assertEqual(first.action, PowerAction.RESTART)
            duplicate = w.launch_session("power:shutdown", dict(self.entry, username="other"), "fixture")
            self.assertIsNone(duplicate)
            self.assertEqual(len(w.sessions), 1)
            notice.assert_called_once()
            first.close()
            self.wait(lambda: not w.sessions)
            self.assertFalse(w._power_targets)

    def test_auth_retry_requires_new_confirmation_and_manual_login(self):
        w = self.window
        with patch("radmin_viewer.app.PowerWindow", FakePowerWindow):
            window = w.launch_session("power:restart", self.entry, "fixture", remember=True, original=self.entry)
            with patch.object(w, "request_power") as request:
                window.retry_requested.emit(dict(self.entry), "power:restart", "Authentication failed")
                window.close()
                self.wait(lambda: request.called and not w.sessions)
                request.assert_called_once_with(PowerAction.RESTART, self.entry,
                                               force_prompt=True, error_message="Authentication failed",
                                               reconnect_mode="control")
            self.assertFalse(w._pending_credentials)
            self.assertFalse(w._power_targets)

    def test_session_and_fullscreen_power_menu_route_same_target(self):
        class AuthenticatedPeer:
            authenticated = False
            def __init__(self, *args): pass
            def connect(self): self.authenticated = True
            def close(self): pass

        w = self.window
        with patch("radmin_viewer.session_ui.RadminSession", AuthenticatedPeer), \
                patch.object(w, "request_power") as request:
            session = w.launch_session("view", self.entry, "fixture")
            try:
                self.wait(lambda: session.power_action.isEnabled())
                session.toggle_fullscreen()
                sleep = next(a for a in session.power_menu.actions() if a.data() == "sleep")
                sleep.trigger()
                request.assert_called_once_with("sleep", self.entry, reconnect_mode="view")
                self.assertTrue(any(button.menu() is session.power_menu
                                    for button in session.fullscreen_controls.findChildren(QToolButton)))
            finally:
                session.disconnect()
                self.wait(lambda: not w.sessions)

    def test_cancel_restart_before_start_clears_recovery_and_save_intent(self):
        w = self.window
        with patch("radmin_viewer.power_ui.RadminSession") as peer:
            window = w.launch_session("power:restart", self.entry, "fixture", remember=True)
            self.assertIn(window, w._restart_intents)
            window.cancel_button.click()
            self.assertFalse(w._restart_intents)
            self.assertFalse(w._pending_credentials)
            self.assertFalse(w._restart_recoveries)
            self.app.processEvents()
            peer.assert_not_called()
            window.close()
            self.wait(lambda: not w.sessions)

    def test_quick_file_transfer_uses_active_desktop_target_in_fullscreen(self):
        class AuthenticatedPeer:
            authenticated = False
            def __init__(self, *args): pass
            def connect(self): self.authenticated = True
            def close(self): pass

        w = self.window
        origin = dict(self.entry)
        with patch("radmin_viewer.session_ui.RadminSession", AuthenticatedPeer), \
                patch.object(w, "connect_session") as connect:
            session = w.launch_session("view", origin, "fixture")
            try:
                self.assertFalse(session.transfer_action.isEnabled())
                self.wait(lambda: session.transfer_action.isEnabled())
                # A different manager selection must not redirect the shortcut.
                w.data["connections"][0]["host"] = "other.example"
                w.refresh()
                session.toggle_fullscreen()
                self.assertTrue(any(button.defaultAction() is session.transfer_action
                                    for button in session.fullscreen_controls.findChildren(QToolButton)))
                session.transfer_action.trigger()
                connect.assert_called_once_with("transfer", origin)
                self.assertTrue(session.worker.isRunning())
                self.assertFalse(session.closing)
            finally:
                session.disconnect()
                self.wait(lambda: not w.sessions)
