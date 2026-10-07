"""Optional OS-vault credentials and a login dialog with save *intent* only.

After authentication succeeds, the caller's store_result should save the
submitted entry/password if remember.isChecked(), otherwise forget the submitted
entry. If the username changed, also forget the original entry when remember was
unchecked. Never call store_result on cancellation or failed authentication.
Catch CredentialStoreError without preventing a connection. Retry rejected saved
credentials with prefill_saved=False and require a fresh explicit submission.
The caller owns clearing dialog/worker password references after use.
"""
from __future__ import annotations

import hashlib
import importlib
import ipaddress
import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
    QVBoxLayout,
)

from .storage import StorageError, text
from .ui_icons import application_icon


class CredentialStoreError(RuntimeError):
    """Safe, fixed user-facing message; never includes backend exception text."""


def _username(value):
    text(value, "username", 256, empty=True)
    if len(value.encode("utf-16-be")) > 512:
        raise StorageError("Username is too long")
    return value


def _password(value):
    if not isinstance(value, str) or "\0" in value:
        raise StorageError("Invalid password")
    try:
        value.encode("utf-16-be")
    except UnicodeError:
        raise StorageError("Invalid password") from None
    return value


def _account(entry):
    host = text(entry["host"], "host", 253)
    if any(c.isspace() for c in host) or any(c in host for c in "/\\@?#[]"):
        raise StorageError("Invalid host")
    try:
        host = ipaddress.ip_address(host).compressed
    except ValueError:
        host = host.rstrip(".").encode("idna").decode("ascii").lower()
    if not host:
        raise StorageError("Invalid host")
    port = entry["port"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise StorageError("Invalid port")
    identity = [host, port, _username(entry["username"])]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode("ascii")).hexdigest()


# Exact classes only: no arbitrary subclasses, file stores, or chained backends.
_OS_BACKENDS = (
    ("keyring.backends.SecretService", "Keyring"),
    ("keyring.backends.kwallet", "DBusKeyring"),
    ("keyring.backends.kwallet", "DBusKeyringKWallet4"),
    ("keyring.backends.kwallet", "DBusKeyringKWallet5"),
    ("keyring.backends.Windows", "WinVaultKeyring"),
    ("keyring.backends.macOS", "Keyring"),
)
_UNAVAILABLE = "Password saving is unavailable. Select an OS Secret Service, KWallet, Windows Credential Manager, or macOS Keychain backend. You can still connect."


class CredentialStore:
    service = "radmin-compatible-viewer"

    def __init__(self):
        self._backend = None
        self._checked = False

    def _resolve(self):
        if not self._checked:
            self._checked = True
            try:
                keyring = importlib.import_module("keyring")
                backend = keyring.get_keyring()
                for module, name in _OS_BACKENDS:
                    if type(backend).__module__ != module or type(backend).__name__ != name:
                        continue
                    cls = getattr(importlib.import_module(module), name)
                    if type(backend) is cls and backend.priority > 0:
                        self._backend = backend
                        break
            except Exception:
                self._backend = None
        return self._backend

    @property
    def available(self) -> bool:
        """Checks backend support, without retrieving/unlocking any credential."""
        return self._resolve() is not None

    @property
    def availability(self) -> tuple[bool, str]:
        return (True, "Passwords are stored in your OS credential vault.") if self.available else (False, _UNAVAILABLE)

    def _call(self, operation, entry, password=None):
        backend = self._resolve()
        if backend is None:
            raise CredentialStoreError(_UNAVAILABLE)
        try:
            account = _account(entry)
            if operation == "save":
                backend.set_password(self.service, account, _password(password))
            elif operation == "forget":
                if backend.get_password(self.service, account) is not None:
                    backend.delete_password(self.service, account)
            else:
                result = backend.get_password(self.service, account)
                return None if result is None else _password(result)
        except Exception:
            raise CredentialStoreError("The OS credential vault could not complete the request. You can still connect without saving a password.") from None

    def get(self, entry) -> str | None:
        return self._call("get", entry)

    def save(self, entry, password):
        self._call("save", entry, password)

    def forget(self, entry):
        self._call("forget", entry)


class LoginDialog(QDialog):
    def __init__(self, entry, parent=None, *, store=None, error_message="", prefill_saved=True):
        super().__init__(parent)
        self._entry = dict(entry)
        self.store = store if store is not None else CredentialStore()
        self._prefill_saved = prefill_saved
        self.has_saved = False
        self.setWindowTitle("Sign in to remote computer")
        self.setWindowIcon(application_icon())
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)
        heading = QLabel("Sign in")
        heading.setStyleSheet("font-size: 22px; font-weight: 600;")
        layout.addWidget(heading)
        endpoint = QLabel(f"{entry['host']}:{entry['port']}")
        endpoint.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(endpoint)
        self.error = QLabel()
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #c34b46;")
        # Never render arbitrary server/backend errors, which may contain secrets.
        self.error.setText("Connection failed. Check your login details and server, then try again." if error_message else "")
        layout.addWidget(self.error)
        form = QFormLayout()
        self.username = QLineEdit(entry.get("username", ""))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("&Username", self.username)
        form.addRow("&Password", self.password)
        layout.addLayout(form)
        self.show_password = QCheckBox("Show password")
        self.show_password.toggled.connect(lambda checked: self.password.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password))
        layout.addWidget(self.show_password)
        self.remember = QCheckBox("Save password in OS credential vault")
        layout.addWidget(self.remember)
        self.remember_help = QLabel()
        self.remember_help.setWordWrap(True)
        self.remember_help.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.remember_help)
        available, message = self.store.availability
        self.remember.setEnabled(available)
        self.remember_help.setText(message + (" Saved only after successful sign-in. Uncheck to forget after sign-in." if available else ""))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Sign in")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.username.textChanged.connect(self._username_changed)
        self.username.editingFinished.connect(self._load_saved)
        self._loaded_username = None
        self._load_saved()
        self.password.setFocus()

    def _username_changed(self):
        self.password.clear()
        self.has_saved = False
        self.remember.setChecked(False)
        self._loaded_username = None

    def _load_saved(self):
        username = self.username.text()
        if not username or not self._prefill_saved or not self.remember.isEnabled() or self._loaded_username == username:
            return
        try:
            _username(username)
        except StorageError:
            return
        self._loaded_username = username
        try:
            saved = self.store.get(dict(self._entry, username=username))
        except CredentialStoreError:
            self.remember.setChecked(False)
            self.remember.setEnabled(False)
            self.remember_help.setText("The OS credential vault is unavailable or locked. Enter a password to connect without saving.")
            return
        self.has_saved = saved is not None
        self.remember.setChecked(self.has_saved)
        # Do not overwrite a password already manually entered for this user.
        if saved is not None and not self.password.text():
            self.password.setText(saved)

    def accept(self):
        try:
            _username(self.username.text())
            _password(self.password.text())
        except StorageError:
            self.error.setText("Enter a valid username (at most 512 UTF-16 bytes) and password without NUL characters or invalid Unicode.")
            return
        super().accept()
