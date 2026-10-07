"""Non-network smoke check for frozen applications and native release packages."""
import importlib
import json
import platform
import sys

import PySide6
from Crypto.Hash import MD4
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import __version__
from .ui_icons import application_icon


def run(app, report_path=None):
    """Verify packaged resources/native modules without loading user configuration."""
    backend = {"linux": "SecretService", "win32": "Windows", "darwin": "macOS"}.get(sys.platform)
    if backend:
        importlib.import_module("keyring.backends." + backend)
    assert len(MD4.new(b"release-smoke-test").digest()) == 16
    cipher = Cipher(algorithms.AES(bytes(32)), modes.CBC(bytes(16))).encryptor()
    assert len(cipher.update(bytes(16)) + cipher.finalize()) == 16
    image = application_icon().pixmap(64, 64)
    assert not image.isNull(), "Packaged application icon is missing"
    app.processEvents()
    report = {"status": "ok", "version": __version__, "platform": platform.system(),
              "architecture": platform.machine(), "pyside": PySide6.__version__,
              "frozen": bool(getattr(sys, "frozen", False)), "qt_platform": app.platformName()}
    text = json.dumps(report, sort_keys=True) + "\n"
    if report_path is not None:
        report_path.write_text(text, encoding="utf-8")
    if sys.stdout is not None:
        print(text, end="")
    return 0
