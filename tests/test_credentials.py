"""Fake vault only: these tests never discover or access a real OS keyring."""
import os
import types
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog, QLineEdit

from radmin_viewer.credentials import CredentialStore, CredentialStoreError, LoginDialog


class Keyring:
    priority = 1

    def __init__(self):
        self.values = {}
        self.calls = []
        self.failure = False

    def get_password(self, service, account):
        self.calls.append(("get", service, account))
        if self.failure:
            raise RuntimeError("sensitive-test-fixture")
        return self.values.get((service, account))

    def set_password(self, service, account, password):
        self.calls.append(("save", service, account))
        if self.failure:
            raise RuntimeError("sensitive-test-fixture")
        self.values[service, account] = password

    def delete_password(self, service, account):
        self.calls.append(("forget", service, account))
        if self.failure:
            raise RuntimeError("sensitive-test-fixture")
        del self.values[service, account]


Keyring.__module__ = "keyring.backends.SecretService"


def entry(**changes):
    return dict({"host": "EXAMPLE.test.", "port": 4899, "username": "Alice"}, **changes)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.backend = Keyring()
        self.discovery = []

        def discover():
            self.discovery.append(True)
            return self.backend

        modules = {
            "keyring": types.SimpleNamespace(get_keyring=discover),
            "keyring.backends.SecretService": types.SimpleNamespace(Keyring=Keyring),
        }
        self.importer = patch("radmin_viewer.credentials.importlib", types.SimpleNamespace(import_module=lambda name: modules[name]))
        self.importer.start()
        self.addCleanup(self.importer.stop)
        self.store = CredentialStore()

    def test_lazy_availability_does_not_read_secrets(self):
        self.assertEqual(self.discovery, [])
        self.assertTrue(self.store.available)
        self.assertTrue(self.store.availability[0])
        self.assertEqual(len(self.discovery), 1)
        self.assertEqual(self.backend.calls, [])

    def test_normalization_separation_and_hashed_identity(self):
        original = entry()
        self.store.save(original, "fixture-password")
        self.assertEqual(self.store.get(entry(host="example.test")), "fixture-password")
        for changed in (entry(host="other.test"), entry(port=4900), entry(username="alice")):
            self.assertIsNone(self.store.get(changed))
        self.store.save(entry(host="2001:0db8::1"), "ipv6-fixture")
        self.assertEqual(self.store.get(entry(host="2001:db8:0:0:0:0:0:1")), "ipv6-fixture")
        for service, account in self.backend.values:
            self.assertEqual(service, "radmin-compatible-viewer")
            self.assertRegex(account, r"^[0-9a-f]{64}$")
            self.assertNotIn("Alice", account)
        self.assertEqual(original, entry())
        self.store.forget(original)
        self.store.forget(original)
        self.assertIsNone(self.store.get(original))

    def test_errors_sanitized(self):
        self.backend.failure = True
        for operation in (lambda: self.store.get(entry()),
                          lambda: self.store.save(entry(), "fixture-password"),
                          lambda: self.store.forget(entry())):
            with self.assertRaises(CredentialStoreError) as caught:
                operation()
            self.assertNotIn("sensitive-test-fixture", str(caught.exception))
            self.assertNotIn("fixture-password", str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)

    def test_reject_plaintext_chainer_and_subclass_without_calls(self):
        for module, name in (("keyrings.alt.file", "PlaintextKeyring"),
                             ("keyring.backends.chainer", "ChainerBackend"),
                             ("custom", "CustomKeyring")):
            with self.subTest(module=module):
                self.backend = type(name, (Keyring,), {"__module__": module})()
                store = CredentialStore()
                self.assertFalse(store.available)
                for action in (lambda: store.get(entry()), lambda: store.save(entry(), "fixture"),
                               lambda: store.forget(entry())):
                    with self.assertRaises(CredentialStoreError):
                        action()
                self.assertEqual(self.backend.calls, [])

    def test_missing_dependency_and_bad_priority(self):
        with patch("radmin_viewer.credentials.importlib.import_module", side_effect=ImportError("secret")):
            self.assertFalse(CredentialStore().available)
        self.backend.priority = 0
        self.assertFalse(CredentialStore().available)

    def test_invalid_values_never_saved(self):
        for changed, password in ((entry(username="\U0001f600" * 129), "fixture"),
                                  (entry(username="bad\0user"), "fixture"),
                                  (entry(), "bad\0password"), (entry(), "\ud800"),
                                  (entry(port=True), "fixture")):
            with self.assertRaises(CredentialStoreError):
                self.store.save(changed, password)
        self.assertEqual(self.backend.values, {})


class DialogTests(unittest.TestCase):
    setUp = StoreTests.setUp
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def dialog(self, **kwargs):
        dialog = LoginDialog(entry(), store=self.store, **kwargs)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_accept_only_collects_save_intent(self):
        dialog = self.dialog()
        dialog.password.setText("fixture-password")
        dialog.remember.setChecked(True)
        dialog.show_password.setChecked(True)
        self.assertEqual(dialog.password.echoMode(), QLineEdit.EchoMode.Normal)
        dialog.accept()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertTrue(dialog.remember.isChecked())
        self.assertEqual(self.backend.values, {})
        self.assertFalse(any(call[0] != "get" for call in self.backend.calls))

    def test_prefill_and_selected_username(self):
        self.store.save(entry(), "alice-fixture")
        self.store.save(entry(username="Bob"), "bob-fixture")
        dialog = self.dialog()
        self.assertTrue(dialog.has_saved)
        self.assertTrue(dialog.remember.isChecked())
        self.assertEqual(dialog.password.text(), "alice-fixture")
        dialog.username.setText("Bob")
        self.assertEqual(dialog.password.text(), "")
        dialog.username.editingFinished.emit()
        self.assertEqual(dialog.password.text(), "bob-fixture")
        dialog.remember.setChecked(False)
        dialog.accept()
        self.assertEqual(self.store.get(entry(username="Bob")), "bob-fixture")

    def test_retry_is_blank_and_does_not_read_saved_secret(self):
        self.store.save(entry(), "rejected-fixture")
        self.backend.calls.clear()
        dialog = self.dialog(prefill_saved=False, error_message="sensitive-test-fixture")
        dialog.username.editingFinished.emit()
        self.assertEqual(dialog.password.text(), "")
        self.assertFalse(dialog.has_saved)
        self.assertFalse(dialog.remember.isChecked())
        self.assertNotIn("sensitive-test-fixture", dialog.error.text())
        self.assertEqual(self.backend.calls, [])

    def test_unavailable_and_locked_vault_allow_manual_login(self):
        for unavailable in (True, False):
            self.store = CredentialStore()
            self.backend.priority = 0 if unavailable else 1
            self.backend.failure = not unavailable
            dialog = self.dialog()
            self.assertFalse(dialog.remember.isEnabled())
            self.assertTrue(dialog.remember_help.text())
            dialog.password.setText("manual-fixture")
            dialog.accept()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)

    def test_validation_and_cancel_have_no_store_side_effects(self):
        dialog = self.dialog(prefill_saved=False)
        for username, password in (("\U0001f600" * 129, "fixture"), ("bad\0", "fixture"),
                                   ("Alice", "bad\0")):
            dialog.username.setText(username)
            dialog.password.setText(password)
            dialog.accept()
            self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        # Qt normalizes isolated surrogates on setText; exercise the boundary
        # directly rather than assuming QString preserves malformed Unicode.
        with patch.object(dialog.password, "text", return_value="\ud800"):
            dialog.accept()
            self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        dialog.reject()
        self.assertEqual(self.backend.calls, [])


if __name__ == "__main__":
    unittest.main()
