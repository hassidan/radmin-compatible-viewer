"""Manager recovery orchestration. All windows/transports are in-memory fakes."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication, QDialog

from radmin_viewer.app import MainWindow
from radmin_viewer.power import PowerAction, PowerMethod, PowerOutcome, PowerResult
from radmin_viewer.storage import AddressBook, defaults


class Worker(QObject):
    finished = Signal()
    def __init__(self, parent):
        super().__init__(parent)
        self.cancel = threading.Event()
    def isRunning(self): return True


class Window(QDialog):
    authenticated = Signal()
    retry_requested = Signal(object, str, str)
    session_error = Signal(str)
    power_requested = Signal(object, str)
    terminal_requested = Signal(object)
    operation_finished = Signal()
    def __init__(self, entry, password, settings, mode="control", adapter_factory=None):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.entry, self.mode = dict(entry), mode
        self.closing = False
        self.failed_session = False
        self.worker = Worker(self)
        self.result_value = None
    def finish_power(self, outcome, dispatched):
        self.result_value = PowerResult(PowerAction.RESTART, PowerMethod.NATIVE, outcome, dispatched, "")
        self.worker.finished.emit()
        self.operation_finished.emit()
    def closeEvent(self, event):
        self.closing = True
        event.accept()


class Recovery(QDialog):
    ready = Signal(object, str, str)
    def __init__(self, entry, password, mode):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.entry, self.password, self.mode = dict(entry), password, mode
        self.closing = False
    def online(self):
        password, self.password = self.password, ""
        self.ready.emit(dict(self.entry), password, self.mode)
        self.close()
    def closeEvent(self, event):
        self.closing = True
        self.password = ""
        event.accept()


class RestartIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        data = defaults()
        self.entry = {"id": str(uuid.uuid4()), "name": "Lab", "host": "localhost", "port": 4899,
                      "username": "test", "group": "General"}
        data["connections"].append(self.entry)
        self.window = MainWindow(AddressBook(Path(self.directory.name) / "book.json"), data)
        self.patchers = [patch("radmin_viewer.app.PowerWindow", Window),
                         patch("radmin_viewer.app.SessionWindow", Window),
                         patch("radmin_viewer.app.RestartRecoveryWindow", Recovery)]
        for patcher in self.patchers:
            patcher.start()

    def wait(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.003)
        self.assertTrue(predicate())

    def tearDown(self):
        self.window.close()
        self.wait(lambda: not self.window.sessions)
        for patcher in reversed(self.patchers): patcher.stop()
        self.directory.cleanup()

    def test_restart_retires_old_session_and_reconnects_same_mode_once(self):
        w = self.window
        old = w.launch_session("view", self.entry, "fixture")
        power = w.launch_session("power:restart", self.entry, "fixture", reconnect_mode="view")
        power.finish_power(PowerOutcome.ACCEPTED, True)
        self.assertTrue(old.closing)
        self.assertTrue(power.closing)
        self.assertFalse(w._restart_intents)
        recovery = w._restart_recoveries[w.power_target(self.entry)]
        self.assertEqual(recovery.mode, "view")
        self.assertEqual(recovery.password, "fixture")
        with patch.object(w, "launch_session") as launch:
            recovery.online()
            launch.assert_called_once_with("view", self.entry, "fixture")
            recovery.ready.emit(self.entry, "stale-fixture", "view")
            launch.assert_called_once()
        self.assertEqual(recovery.password, "")
        self.assertFalse(w._restart_recoveries)

    def test_unknown_dispatch_monitors_but_rejected_unsent_cancelled_do_not(self):
        w = self.window
        for outcome, dispatched, watch in ((PowerOutcome.UNKNOWN, True, True),
                                            (PowerOutcome.REJECTED, True, False),
                                            (PowerOutcome.NOT_SENT, False, False),
                                            (PowerOutcome.CANCELLED, False, False)):
            with self.subTest(outcome=outcome):
                power = w.launch_session("power:restart", self.entry, "fixture", reconnect_mode="terminal")
                power.finish_power(outcome, dispatched)
                self.assertEqual(bool(w._restart_recoveries), watch)
                self.assertFalse(w._restart_intents)
                for session in list(w.sessions): session.close()
                self.wait(lambda: not w.sessions)

    def test_cancelled_wait_and_shutdown_of_manager_never_start_recovery(self):
        w = self.window
        power = w.launch_session("power:restart", self.entry, "fixture")
        power.worker.cancel.set()
        power.finish_power(PowerOutcome.UNKNOWN, True)
        self.assertFalse(w._restart_recoveries)
        self.assertFalse(w._restart_intents)
        power.close()
        self.wait(lambda: not w.sessions)
        power = w.launch_session("power:restart", self.entry, "fixture")
        w.exiting = True
        power.finish_power(PowerOutcome.ACCEPTED, True)
        self.assertFalse(w._restart_recoveries)

    def test_recovery_cancel_and_exit_clear_secret_without_reconnecting(self):
        w = self.window
        power = w.launch_session("power:restart", self.entry, "fixture")
        power.finish_power(PowerOutcome.ACCEPTED, True)
        recovery = next(iter(w._restart_recoveries.values()))
        with patch.object(w, "launch_session") as launch:
            recovery.close()
            recovery.ready.emit(self.entry, "stale-fixture", "control")
            launch.assert_not_called()
        self.assertEqual(recovery.password, "")
        self.wait(lambda: not w.sessions)
        self.assertFalse(w._restart_recoveries)

    def test_manual_reconnect_is_reused_and_extra_power_request_blocked(self):
        w = self.window
        power = w.launch_session("power:restart", self.entry, "fixture", reconnect_mode="control")
        power.finish_power(PowerOutcome.ACCEPTED, True)
        recovery = next(iter(w._restart_recoveries.values()))
        with patch.object(w, "notify") as notice, patch("radmin_viewer.app.confirm") as confirm:
            w.request_power(PowerAction.RESTART, self.entry)
            notice.assert_called_once()
            confirm.assert_not_called()
        manual = w.launch_session("control", self.entry, "manual-fixture")
        with patch.object(w, "launch_session") as launch:
            recovery.online()
            launch.assert_not_called()
        self.assertFalse(manual.closing)

    def test_terminal_menu_route_and_restart_confirmation_preserve_terminal_mode(self):
        w = self.window
        w.grid.setCurrentRow(0)
        self.assertTrue(w.terminal_action.isEnabled())
        self.assertIn(w.terminal_action, w.computer_menu.actions())
        with patch.object(w, "connect_session") as connect:
            w.terminal_action.trigger()
            connect.assert_called_once_with("terminal")
        with patch("radmin_viewer.app.confirm", return_value=True) as confirm, \
                patch.object(w, "connect_session") as connect:
            w.request_power(PowerAction.RESTART, self.entry, reconnect_mode="terminal")
            self.assertIn("reopen terminal", confirm.call_args.args[2])
            self.assertEqual(connect.call_args.kwargs["reconnect_mode"], "terminal")
