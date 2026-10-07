"""Credential-free, same-user activation of the config-lock owner.

The application MUST acquire its existing QLockFile for the canonical config
path before calling start(), and retain that lock until after stop(). This
module neither acquires nor replaces that lock. Run the server in a Qt event
loop; a QCoreApplication suffices. The blocking client requires the server's
event loop to run in another thread/process (normally the first launch).

Wire format: one UTF-8 JSON object followed by LF, at most MAX_MESSAGE_BYTES
including LF. Exact fields: version, quick_entry (null or a storage connection),
mode. Acceptance emits None for show-only, otherwise {quick_entry, mode}.
Handlers should restore the manager and schedule quick-connect, not block on a
dialog. ACK means accepted for dispatch, not connected to the remote computer.
Qt UserAccessOption supplies Unix permissions / Windows named-pipe ACLs; this
is a same-user trust boundary, not protection against other same-user programs.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import time

from PySide6.QtCore import QObject, QStandardPaths, QTimer, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from .storage import defaults, validate

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 8192
CLIENT_TIMEOUT_MS = 1500
MAX_CLIENTS = 32
ALLOWED_MODES = frozenset({"view", "control", "transfer", "terminal"})
ACK = b'{"version":1,"accepted":true}\n'


def _user_identity():
    if os.name != "nt":
        return str(os.geteuid())
    # Obtain the token SID, not environment-controlled USERNAME/USERDOMAIN.
    import ctypes
    from ctypes import wintypes
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                       ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                          ctypes.c_void_p, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,
                                            ctypes.POINTER(wintypes.LPWSTR)]
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise OSError("Cannot identify activation user")
    try:
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise OSError("Cannot read activation user")
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise OSError("Cannot read activation SID")
        try:
            return text.value
        finally:
            kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        kernel.CloseHandle(token)


def _private_directory(path, create=False):
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077):
        raise OSError("Unsafe activation runtime directory")


def server_name(config_path):
    """Return a short, deterministic endpoint; never contains config/user text."""
    identity = _user_identity()
    canonical = os.path.normcase(str(Path(config_path).expanduser().resolve()))
    digest = hashlib.sha256((identity + "\0" + canonical).encode("utf-8")).hexdigest()[:32]
    leaf = "rv-" + digest
    if os.name == "nt":
        return leaf
    runtime = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.RuntimeLocation)
    if runtime:
        root = Path(runtime)
        try:
            _private_directory(root)
            candidate = root / leaf
            if len(os.fsencode(candidate)) <= 100:
                return str(candidate)
        except OSError:
            pass
    # Fixed short fallback, independent of TMPDIR. A private owned directory
    # prevents other users replacing sockets; never follow a precreated symlink.
    user_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return str(Path("/tmp") / ("rv-" + user_hash) / leaf)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate field")
        result[key] = value
    return result


def _validated(message):
    if type(message) is not dict or set(message) != {"version", "quick_entry", "mode"}:
        raise ValueError("Invalid activation fields")
    if type(message["version"]) is not int or message["version"] != PROTOCOL_VERSION:
        raise ValueError("Unsupported activation version")
    if type(message["mode"]) is not str or message["mode"] not in ALLOWED_MODES:
        raise ValueError("Invalid activation mode")
    entry = message["quick_entry"]
    if entry is None:
        return None
    if type(entry) is not dict:
        raise ValueError("Invalid quick entry")
    data = defaults()
    data["groups"] = [entry.get("group")]
    data["connections"] = [entry]
    validate(data)
    return {"quick_entry": entry, "mode": message["mode"]}


class ActivationServer(QObject):
    activate_requested = Signal(object)

    def __init__(self, config_path, parent=None):
        super().__init__(parent)
        self.server_name = server_name(config_path)
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.setMaxPendingConnections(MAX_CLIENTS)
        self._server.newConnection.connect(self._accept)
        self._clients = {}

    def start(self) -> bool:
        """Listen ONLY after the caller acquires and retains the config QLockFile.

        Under that guarantee, remove only an owned stale socket in our private
        namespace. A live listener, symlink, regular file, or ambiguous probe
        error is never removed. Repeated start on this object is idempotent.
        """
        if self._server.isListening():
            return True
        try:
            if os.name != "nt":
                path = Path(self.server_name)
                _private_directory(path.parent, create=True)
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    info = None
                if info is not None:
                    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
                        return False
                    probe = QLocalSocket()
                    try:
                        probe.connectToServer(self.server_name)
                        if probe.waitForConnected(100):
                            return False
                        if probe.error() != QLocalSocket.LocalSocketError.ConnectionRefusedError:
                            return False
                    finally:
                        probe.abort()
                    current = path.lstat()
                    if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                        return False
                    if not QLocalServer.removeServer(self.server_name):
                        return False
            else:
                # Windows can allow multiple listeners for a pipe name. Refuse
                # an existing listener even if a caller violates the lock rule.
                probe = QLocalSocket()
                try:
                    probe.connectToServer(self.server_name)
                    if probe.waitForConnected(100):
                        return False
                    if probe.error() not in (
                            QLocalSocket.LocalSocketError.ServerNotFoundError,
                            QLocalSocket.LocalSocketError.ConnectionRefusedError):
                        return False
                finally:
                    probe.abort()
            # Named pipes have no stale filesystem entry to remove on Windows.
            return self._server.listen(self.server_name)
        except OSError:
            return False

    def stop(self):
        """Close clients and listener before releasing the caller's config lock."""
        self._server.close()
        for socket in list(self._clients):
            self._drop(socket)

    def _drop(self, socket):
        state = self._clients.pop(socket, None)
        if state is not None:
            state["timer"].stop()
            state["timer"].timeout.disconnect()
            socket.readyRead.disconnect()
            socket.disconnected.disconnect()
            socket.abort()
            socket.deleteLater()

    def _accept(self):
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if len(self._clients) >= MAX_CLIENTS:
                socket.abort()
                socket.deleteLater()
                continue
            socket.setReadBufferSize(MAX_MESSAGE_BYTES + 1)
            timer = QTimer(socket)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda s=socket: self._drop(s))
            self._clients[socket] = {"timer": timer, "buffer": bytearray(), "accepted": False}
            socket.readyRead.connect(lambda s=socket: self._read(s))
            socket.disconnected.connect(lambda s=socket: self._drop(s))
            timer.start(CLIENT_TIMEOUT_MS)
            self._read(socket)

    def _read(self, socket):
        state = self._clients.get(socket)
        if state is None or state["accepted"]:
            return
        buffer = state["buffer"]
        buffer.extend(bytes(socket.read(MAX_MESSAGE_BYTES + 1 - len(buffer))))
        if len(buffer) > MAX_MESSAGE_BYTES:
            self._drop(socket)
            return
        if b"\n" not in buffer:
            return
        try:
            if buffer.index(b"\n") != len(buffer) - 1:
                raise ValueError("Trailing activation data")
            message = json.loads(buffer[:-1].decode("utf-8"), object_pairs_hook=_unique_object)
            payload = _validated(message)
        except (ValueError, TypeError, RecursionError, UnicodeError):
            self._drop(socket)
            return
        state["accepted"] = True  # Also guards signal-handler reentrancy.
        self.activate_requested.emit(payload)
        if socket in self._clients:
            socket.write(ACK)
            socket.disconnectFromServer()  # Qt drains the ACK before closing.


def request_activation(config_path, quick_entry=None, mode="view", timeout_ms=1000) -> bool:
    """Send one request and wait for ACK within a single total time budget.

    Requires an existing QCoreApplication/QApplication. Never removes endpoints
    or touches the config lock. False includes invalid input, missing listener,
    rejection, or timeout; a lost ACK can leave dispatch outcome uncertain.
    """
    if type(timeout_ms) is not int or not 0 < timeout_ms <= 2147483647:
        return False
    deadline = time.monotonic() + timeout_ms / 1000

    def remaining():
        return max(0, int((deadline - time.monotonic()) * 1000))

    try:
        message = {"version": PROTOCOL_VERSION, "quick_entry": quick_entry, "mode": mode}
        _validated(message)
        raw = (json.dumps(message, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")) + "\n").encode("utf-8")
        if len(raw) > MAX_MESSAGE_BYTES:
            return False
        name = server_name(config_path)
        if os.name != "nt":
            _private_directory(Path(name).parent)
    except (OSError, ValueError, TypeError, RecursionError, UnicodeError):
        return False
    socket = QLocalSocket()
    socket.setReadBufferSize(len(ACK) + 1)
    try:
        socket.connectToServer(name)
        if not remaining() or not socket.waitForConnected(remaining()):
            return False
        if socket.write(raw) != len(raw):
            return False
        while socket.bytesToWrite():
            if not remaining() or not socket.waitForBytesWritten(remaining()):
                return False
        reply = bytearray()
        while remaining():
            reply.extend(bytes(socket.read(len(ACK) + 1 - len(reply))))
            if b"\n" in reply or len(reply) > len(ACK):
                return bytes(reply) == ACK
            if not socket.waitForReadyRead(remaining()):
                return False
        return False
    finally:
        socket.abort()
