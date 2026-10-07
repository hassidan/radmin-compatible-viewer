"""Manager/login integration using a deterministic peer and an in-memory vault."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QLabel

from radmin_viewer.app import MainWindow
from radmin_viewer.credentials import CredentialStoreError, LoginDialog
from radmin_viewer.protocol import AuthenticationError
from radmin_viewer.scanner import ScanResult
from radmin_viewer.storage import AddressBook, defaults


class Vault:
    available = True
    availability = True, "Test vault"
    def __init__(self): self.values, self.saved = {}, []
    @staticmethod
    def key(e): return e["host"], e["port"], e["username"]
    def get(self, e): return self.values.get(self.key(e))
    def save(self, e, password):
        self.values[self.key(e)] = password
        self.saved.append(self.key(e))
    def forget(self, e): self.values.pop(self.key(e), None)


class Peer:
    def __init__(self, *args): self.authenticated = False
    def connect(self): self.authenticated = True
    def close(self): pass


class Adapter:
    can_control = True
    def poll(self, timeout):
        time.sleep(timeout)
        image = QImage(8, 8, QImage.Format.Format_RGB888)
        image.fill(Qt.GlobalColor.blue)
        return image
    def close(self): pass
    def handle_input(self, event): pass


class ModernManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.data = defaults()
        self.entry = {"id": str(uuid.uuid4()), "name": "Test machine", "host": "localhost",
                      "port": 4899, "username": "", "group": "General"}
        self.data["connections"].append(self.entry)
        self.vault = Vault()
        self.window = MainWindow(AddressBook(Path(self.directory.name) / "book.json"), self.data,
                                 lambda s, m: Adapter(), credential_store=self.vault)

    def wait(self, predicate):
        end = time.monotonic() + 3
        while not predicate() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(.003)
        self.assertTrue(predicate())

    def tearDown(self):
        self.window.close()
        self.wait(lambda: not self.window.sessions)
        self.directory.cleanup()

    def test_icon_selection_filter_and_live_status(self):
        self.assertIs(self.window.views.currentWidget(), self.window.grid)
        self.assertFalse(self.window.grid.item(0).icon().isNull())
        self.window.grid.setCurrentRow(0)
        self.assertEqual(self.window.selected(), self.entry)
        self.assertTrue(self.window.control_action.isEnabled())
        before = self.window.grid.item(0).icon().cacheKey()
        self.window.scan_result(ScanResult(self.entry["id"], "localhost", 4899, "Online"))
        self.assertNotEqual(self.window.grid.item(0).icon().cacheKey(), before)
        self.assertIn("Online", self.window.grid.item(0).toolTip())
        self.window.list_action.trigger()
        self.assertIs(self.window.views.currentWidget(), self.window.tree)
        self.window.search.setText("not a saved host")
        self.assertEqual(self.window.grid.count(), 0)
        self.assertFalse(self.window.control_action.isEnabled())
        self.window.search.clear()
        self.assertEqual(self.window.grid.count(), 1)

    def test_save_only_after_verified_login_then_auto_use(self):
        gate = threading.Event()
        class SlowPeer(Peer):
            def connect(self):
                gate.wait(2)
                super().connect()
        submitted = dict(self.entry, username="test-user")
        with patch("radmin_viewer.session_ui.RadminSession", SlowPeer):
            session = self.window.launch_session("control", submitted, "test-only-secret",
                                                 remember=True, original=self.entry)
            self.app.processEvents()
            self.assertEqual(self.vault.saved, [])
            gate.set()
            self.wait(lambda: bool(self.vault.saved))
            self.assertFalse(self.window._pending_credentials)
            saved_data = self.window.store.load()
            self.assertEqual(saved_data["connections"][0]["username"], "test-user")
            self.assertNotIn("test-only-secret", self.window.store.path.read_text())
            session.disconnect()
            self.wait(lambda: not self.window.sessions)
        with patch("radmin_viewer.session_ui.RadminSession", Peer), patch("radmin_viewer.app.CredentialsDialog") as dialog:
            self.window.connect_session("control", submitted)
            self.wait(lambda: next(iter(self.window.sessions)).active_session)
            dialog.assert_not_called()
            next(iter(self.window.sessions)).disconnect()
            self.wait(lambda: not self.window.sessions)

    def test_rejected_saved_password_prompts_once_after_cleanup(self):
        submitted = dict(self.entry, username="test-user")
        self.vault.save(submitted, "rejected-fixture")
        events = []
        class BadPeer(Peer):
            def connect(self): raise AuthenticationError()
            def close(self): events.append("closed")
        def dialog_factory(entry, parent, **kwargs):
            self.assertIn("closed", events)
            self.assertFalse(kwargs["prefill_saved"])
            dialog = LoginDialog(entry, parent, **kwargs)
            self.assertEqual(dialog.password.text(), "")
            events.append("prompt")
            dialog.exec = lambda: 0
            return dialog
        with patch("radmin_viewer.session_ui.RadminSession", BadPeer), patch("radmin_viewer.app.CredentialsDialog", side_effect=dialog_factory):
            self.window.connect_session("control", submitted)
            self.wait(lambda: "prompt" in events and not self.window.sessions)
            self.assertEqual(events, ["closed", "prompt"])
            self.assertEqual(self.vault.get(submitted), "rejected-fixture")
            self.assertFalse(self.window._pending_credentials)

    def test_cancel_before_worker_start_does_not_save(self):
        with patch("radmin_viewer.session_ui.RadminSession") as peer:
            session = self.window.launch_session("control", self.entry, "unused-fixture", remember=True)
            session.close()
            self.wait(lambda: not self.window.sessions)
            peer.assert_not_called()
            self.assertFalse(self.window._pending_credentials)
            self.assertEqual(self.vault.saved, [])

    def test_vault_failure_does_not_break_session_and_forget_action(self):
        with patch("radmin_viewer.session_ui.RadminSession", Peer), patch.object(self.vault, "save", side_effect=CredentialStoreError("Vault unavailable")):
            session = self.window.launch_session("control", self.entry, "private-fixture", remember=True)
            self.wait(lambda: session.active_session)
            self.assertTrue(any("Vault unavailable" in label.text()
                                for notice in self.window._notifications
                                for label in notice.findChildren(QLabel)))
            self.assertTrue(self.window.statusBar().isHidden())
            self.assertFalse(self.window._pending_credentials)
            session.disconnect()
            self.wait(lambda: not self.window.sessions)
        self.vault.save(self.entry, "forget-fixture")
        self.window.grid.setCurrentRow(0)
        self.window.forget_action.trigger()
        self.assertIsNone(self.vault.get(self.entry))
