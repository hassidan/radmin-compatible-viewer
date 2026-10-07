import copy
import json
import os
from pathlib import Path
import socket as native_socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from PySide6.QtCore import QCoreApplication, QLockFile
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from radmin_viewer import instance_activation as activation


def entry():
    return {"id": str(uuid.uuid4()), "name": "Lab", "host": "::1", "port": 4899,
            "username": "", "group": "General"}


def message(quick_entry=None, mode="view"):
    return {"version": activation.PROTOCOL_VERSION, "quick_entry": quick_entry, "mode": mode}


def wire(value):
    return json.dumps(value, ensure_ascii=False).encode("utf-8") + b"\n"


# Run the real server in another process: blocking client waits must not starve
# the server's event loop. The child holds the same lock used by the application.
CHILD_SERVER = r'''
import sys
from PySide6.QtCore import QCoreApplication, QLockFile, QTimer
from PySide6.QtNetwork import QLocalServer
from radmin_viewer.instance_activation import ActivationServer, server_name
app = QCoreApplication([])
lock = QLockFile(sys.argv[1] + '.lock')
assert lock.tryLock(0)
kind = sys.argv[2]
if kind == 'real':
    server = ActivationServer(sys.argv[1])
    assert server.start()
    server.activate_requested.connect(lambda payload: print('accepted', flush=True))
else:
    server = QLocalServer()
    server.setSocketOptions(QLocalServer.UserAccessOption)
    assert server.listen(server_name(sys.argv[1]))
    clients = []
    def accept():
        while server.hasPendingConnections():
            sock = server.nextPendingConnection()
            clients.append(sock)
            if kind == 'bad-ack':
                sock.write(b'{"version":2,"accepted":true}\n')
                sock.disconnectFromServer()
    server.newConnection.connect(accept)
print('ready', flush=True)
QTimer.singleShot(10000, app.quit)
app.exec()
if kind == 'real':
    server.stop()
else:
    server.close()
lock.unlock()
'''


class ActivationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "connections.json"
        self.lock = QLockFile(str(self.config) + ".lock")
        self.assertTrue(self.lock.tryLock(0))
        self.addCleanup(self.lock.unlock)
        self.server = activation.ActivationServer(self.config)
        self.received = []
        self.server.activate_requested.connect(self.received.append)
        self.addCleanup(self.server.stop)
        self.assertTrue(self.server.start())

    def pump(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.002)
        self.assertTrue(predicate(), "Qt condition timed out")

    def connect(self):
        sock = QLocalSocket()
        self.addCleanup(sock.abort)
        sock.connectToServer(self.server.server_name)
        self.pump(lambda: sock.state() == QLocalSocket.ConnectedState)
        return sock

    def exchange(self, raw):
        sock = self.connect()
        sock.write(raw)
        self.pump(lambda: sock.state() == QLocalSocket.UnconnectedState)
        return bytes(sock.readAll())

    def test_show_and_all_quick_modes_ack(self):
        self.assertEqual(self.exchange(wire(message())), activation.ACK)
        self.assertEqual(self.received, [None])
        for mode in sorted(activation.ALLOWED_MODES):
            quick = entry()
            quick["name"] = "Lab 🖥"
            quick["group"] = "Unsaved"
            self.assertEqual(self.exchange(wire(message(quick, mode))), activation.ACK)
            self.assertEqual(self.received[-1], {"quick_entry": quick, "mode": mode})
        self.assertEqual(len(self.received), 5)
        self.assertFalse(self.server._clients)

    def test_fragmented_request_emits_only_after_complete(self):
        sock = self.connect()
        raw = wire(message(entry(), "control"))
        for part in (raw[:3], raw[3:-1]):
            sock.write(part)
            sock.flush()
            self.app.processEvents()
            self.assertEqual(self.received, [])
        sock.write(raw[-1:])
        self.pump(lambda: sock.state() == QLocalSocket.UnconnectedState)
        self.assertEqual(bytes(sock.readAll()), activation.ACK)
        self.assertEqual(len(self.received), 1)

    def test_reject_schema_version_modes_and_credentials(self):
        cases = [[], None, {}, {"version": 1},
                 dict(message(), password="secret"), dict(message(), version=True),
                 dict(message(), version=2), dict(message(), version=1.0)]
        for mode in ("power", "power:restart", "shutdown", "VIEW", None, [], 0):
            cases.append(message(mode=mode))
        for field, value in (("password", "secret"), ("token", "secret"),
                             ("port", True), ("port", 65536), ("id", "bad"),
                             ("host", "https://host"), ("host", "bad host"),
                             ("username", "\0"), ("username", "😀" * 129),
                             ("group", []), ("name", "")):
            quick = entry()
            quick[field] = value
            cases.append(message(quick))
        missing = entry()
        del missing["host"]
        cases.extend([message(missing), message([]), message("host")])
        for value in cases:
            with self.subTest(value=value):
                self.assertEqual(self.exchange(wire(value)), b"")
        self.assertEqual(self.received, [])

    def test_malformed_duplicate_trailing_and_oversized(self):
        cases = [b'no json\n', b'\xff\n', b'[]\n',
                 b'{"version":1,"version":1,"quick_entry":null,"mode":"view"}\n',
                 b'{"version":1,"quick_entry":null,"mode":"view","mode":"control"}\n',
                 wire(message()) + wire(message()), wire(message()) + b'junk',
                 b'[' * 2000 + b']' * 2000 + b'\n',
                 b'x' * (activation.MAX_MESSAGE_BYTES + 1),
                 b' ' * activation.MAX_MESSAGE_BYTES + wire(message())]
        quick = wire(message(entry())).replace(b'"name": "Lab"', b'"name": "a", "name": "b"')
        cases.append(quick)
        for raw in cases:
            with self.subTest(prefix=raw[:60]):
                self.assertEqual(self.exchange(raw), b"")
        self.assertEqual(self.received, [])

    def test_exact_message_limit(self):
        raw = wire(message())
        self.assertEqual(self.exchange(b' ' * (activation.MAX_MESSAGE_BYTES - len(raw)) + raw),
                         activation.ACK)

    def test_incomplete_timeout_and_disconnect_cleanup(self):
        with patch.object(activation, "CLIENT_TIMEOUT_MS", 50):
            sock = self.connect()
            sock.write(b'{"version":1')
            self.pump(lambda: sock.state() == QLocalSocket.UnconnectedState)
        self.assertEqual(bytes(sock.readAll()), b"")
        self.assertEqual(self.received, [])
        self.assertFalse(self.server._clients)
        sock = self.connect()
        sock.write(b'{')
        self.pump(lambda: bool(self.server._clients))
        sock.abort()
        self.pump(lambda: not self.server._clients)

    def test_max_clients_stop_and_restart(self):
        with patch.object(activation, "MAX_CLIENTS", 2):
            first, second = self.connect(), self.connect()
            self.pump(lambda: len(self.server._clients) == 2)
            third = self.connect()
            self.pump(lambda: third.state() == QLocalSocket.UnconnectedState)
            self.assertEqual(len(self.server._clients), 2)
        self.server.stop()
        self.pump(lambda: all(s.state() == QLocalSocket.UnconnectedState for s in (first, second)))
        self.assertFalse(self.server._clients)
        self.server.stop()
        self.assertTrue(self.server.start())
        self.assertTrue(self.server.start())
        self.assertEqual(self.exchange(wire(message())), activation.ACK)

    def test_competitor_cannot_remove_live_server(self):
        competitor = activation.ActivationServer(self.config)
        self.assertFalse(competitor.start())
        competitor.stop()
        self.assertEqual(self.exchange(wire(message())), activation.ACK)

    def test_canonical_config_and_user_names(self):
        same = self.config.parent / "unused" / ".." / self.config.name
        self.assertEqual(activation.server_name(same), self.server.server_name)
        self.assertNotEqual(activation.server_name(self.config.with_name("other.json")),
                            self.server.server_name)
        with patch.object(activation, "_user_identity", return_value="other-user"):
            self.assertNotEqual(activation.server_name(self.config), self.server.server_name)
        self.assertNotIn("connections.json", self.server.server_name)
        if os.name != "nt":
            self.assertLessEqual(len(os.fsencode(self.server.server_name)), 100)
            alias = self.config.parent / "alias"
            alias.symlink_to(self.config.parent, target_is_directory=True)
            self.assertEqual(activation.server_name(alias / self.config.name), self.server.server_name)

    @unittest.skipIf(os.name == "nt", "Unix socket filesystem")
    def test_socket_permissions_and_stale_recovery(self):
        self.assertEqual(Path(self.server.server_name).stat().st_mode & 0o077, 0)
        self.server.stop()
        stale = native_socket.socket(native_socket.AF_UNIX)
        stale.bind(self.server.server_name)
        stale.close()
        self.assertTrue(self.server.start())
        self.assertEqual(self.exchange(wire(message())), activation.ACK)

    @unittest.skipIf(os.name == "nt", "Unix socket filesystem")
    def test_no_regular_file_or_symlink_deletion(self):
        self.server.stop()
        endpoint = Path(self.server.server_name)
        endpoint.write_text("keep")
        try:
            self.assertFalse(self.server.start())
            self.assertEqual(endpoint.read_text(), "keep")
        finally:
            endpoint.unlink()
        target = self.config.parent / "keep"
        target.write_text("keep")
        endpoint.symlink_to(target)
        try:
            self.assertFalse(self.server.start())
            self.assertTrue(endpoint.is_symlink())
            self.assertEqual(target.read_text(), "keep")
        finally:
            endpoint.unlink()

    @unittest.skipIf(os.name == "nt", "Unix runtime directory")
    def test_fallback_short_and_private_directory_checks(self):
        with patch.object(activation.QStandardPaths, "writableLocation", return_value="/missing/" + "x" * 150):
            name = activation.server_name(self.config)
        self.assertTrue(name.startswith("/tmp/rv-"))
        self.assertLess(len(os.fsencode(name)), 108)
        unsafe = self.config.parent / "unsafe"
        unsafe.mkdir(mode=0o755)
        unsafe.chmod(0o755)
        with self.assertRaises(OSError):
            activation._private_directory(unsafe, create=True)
        link = self.config.parent / "link"
        link.symlink_to(self.config.parent, target_is_directory=True)
        with self.assertRaises(OSError):
            activation._private_directory(link, create=True)

    def child(self, kind):
        config = self.config.with_name(kind + ".json")
        proc = subprocess.Popen([sys.executable, "-u", "-c", CHILD_SERVER, str(config), kind],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def cleanup():
            proc.terminate()
            try:
                proc.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
            # Child termination intentionally models a crash. Tests own this
            # unique config and remove only its generated socket after exit.
            QLocalServer.removeServer(activation.server_name(config))
        self.addCleanup(cleanup)
        self.assertEqual(proc.stdout.readline().strip(), "ready")
        return proc, config

    def test_blocking_client_real_server_and_lock_retained(self):
        proc, config = self.child("real")
        competing_lock = QLockFile(str(config) + ".lock")
        self.assertFalse(competing_lock.tryLock(0))
        self.assertTrue(activation.request_activation(config))
        self.assertEqual(proc.stdout.readline().strip(), "accepted")
        quick = entry()
        original = copy.deepcopy(quick)
        for mode in sorted(activation.ALLOWED_MODES):
            self.assertTrue(activation.request_activation(config, quick, mode))
            self.assertEqual(proc.stdout.readline().strip(), "accepted")
        self.assertEqual(quick, original)
        self.assertFalse(competing_lock.tryLock(0))

    def test_client_missing_invalid_rejected_and_deadline(self):
        self.assertFalse(activation.request_activation(self.config.with_name("missing.json"), timeout_ms=100))
        for timeout in (0, -1, True, 1.5, 2**32):
            self.assertFalse(activation.request_activation(self.config, timeout_ms=timeout))
        for mode in ("power", "power:restart", None, []):
            self.assertFalse(activation.request_activation(self.config, mode=mode))
        self.assertFalse(activation.request_activation(self.config, dict(entry(), password="secret")))
        _, bad = self.child("bad-ack")
        self.assertFalse(activation.request_activation(bad))
        _, silent = self.child("silent")
        before = time.monotonic()
        self.assertFalse(activation.request_activation(silent, timeout_ms=100))
        self.assertLess(time.monotonic() - before, 0.8)


if __name__ == "__main__":
    unittest.main()
