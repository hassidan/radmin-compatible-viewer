import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import time
import unittest
import uuid

from PySide6.QtCore import QTimer
from PySide6.QtNetwork import QHostAddress, QTcpServer, QAbstractSocket
from PySide6.QtWidgets import QApplication, QToolBar

from radmin_viewer.scanner import ConnectionScanner, ScanResult
from radmin_viewer.protocol import PREAMBLE, PREAMBLE_ACK
from radmin_viewer.app import MainWindow
from radmin_viewer.storage import AddressBook, defaults


def entry(port, name="Test"):
    return {"id": str(uuid.uuid4()), "host": "127.0.0.1", "port": port,
            "name": name, "username": "not-sent", "group": "General"}


class ScannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.servers = []
        self.sockets = []
        self.scanners = []

    def tearDown(self):
        for scanner in self.scanners:
            scanner.cancel()
        for sock in self.sockets:
            sock.abort()
        for server in self.servers:
            server.close()
        self.app.processEvents()

    def wait(self, predicate, seconds=3):
        end = time.monotonic() + seconds
        while not predicate() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(.002)
        self.assertTrue(predicate())

    def server(self, reply=None):
        server = QTcpServer()
        self.assertTrue(server.listen(QHostAddress.SpecialAddress.LocalHost, 0))
        self.servers.append(server)
        requests = []
        def accept():
            while server.hasPendingConnections():
                sock = server.nextPendingConnection()
                self.sockets.append(sock)
                data = bytearray()
                answered = [False]
                def read(s=sock, data=data, answered=answered):
                    data.extend(bytes(s.readAll()))
                    if len(data) >= len(PREAMBLE) and not answered[0]:
                        answered[0] = True
                        requests.append(bytes(data))
                        if reply == "fragmented":
                            s.write(PREAMBLE_ACK[:4])
                            QTimer.singleShot(10, lambda s=s: s.write(PREAMBLE_ACK[4:])
                                              if s.state() == QAbstractSocket.SocketState.ConnectedState else None)
                        elif reply is not None:
                            s.write(reply)
                sock.readyRead.connect(read)
        server.newConnection.connect(accept)
        return server.serverPort(), requests

    def scanner(self, **kwargs):
        scan = ConnectionScanner(**kwargs)
        self.scanners.append(scan)
        return scan

    def test_fragmented_radmin_other_service_idle_and_refused(self):
        live, requests = self.server("fragmented")
        other, _ = self.server(b"HTTP/1.1 400 Bad Request\r\n")
        idle, _ = self.server()
        closed, _ = self.server()
        self.servers[-1].close()
        scan = self.scanner(timeout_ms=150, concurrency=2)
        results, finished = {}, []
        scan.result.connect(lambda r: results.__setitem__(r.entry_id, r))
        scan.finished.connect(finished.append)
        targets = [entry(port) for port in (live, other, idle, closed)]
        scan.start(targets)
        self.wait(lambda: bool(finished))
        self.assertEqual([results[e["id"]].status for e in targets],
                         ["Online", "Port open", "Port open", "Unreachable"])
        self.assertEqual(requests, [PREAMBLE])
        self.assertEqual(finished, [False])
        self.assertIsNotNone(results[targets[0]["id"]].latency_ms)
        self.assertTrue(all(results[e["id"]].checked_at for e in targets))
        self.assertFalse(scan._active)

    def test_concurrency_cancellation_restart_and_event_loop_responsiveness(self):
        port, requests = self.server()
        scan = self.scanner(timeout_ms=1000, concurrency=3)
        final = []
        results = {}
        scan.finished.connect(final.append)
        scan.result.connect(lambda r: results.__setitem__(r.entry_id, r.status))
        targets = [entry(port) for _ in range(20)]
        scan.start(targets)
        self.wait(lambda: len(requests) == 3)
        self.assertEqual(len(scan._active), 3)
        QTimer.singleShot(0, scan.cancel)
        self.wait(lambda: final == [True])
        self.assertEqual(set(results.values()), {"Cancelled"})
        self.assertFalse(scan._active)
        self.assertFalse(scan._pending)
        live, _ = self.server("fragmented")
        target = entry(live)
        scan.start([target])
        self.wait(lambda: len(final) == 2)
        self.assertEqual(final, [True, False])
        self.assertEqual(results[target["id"]], "Online")

    def test_dns_error_and_empty_scan(self):
        scan = self.scanner()
        done, results = [], []
        scan.result.connect(results.append)
        scan.finished.connect(done.append)
        scan.start([])
        self.wait(lambda: done == [False])
        idle, _ = self.server()
        scan.start([entry(idle)])
        self.wait(lambda: bool(scan._active))
        sock = next(iter(scan._active))
        # Exercise the Qt DNS failure classification without external DNS traffic.
        scan._active[sock]["connected"] = False
        scan._error(sock, QAbstractSocket.SocketError.HostNotFoundError)
        self.wait(lambda: len(done) == 2)
        self.assertEqual(results[-1].status, "DNS error")

    def test_manager_scan_hidden_entries_edit_invalidation_and_close(self):
        port, _ = self.server("fragmented")
        data = defaults()
        visible, hidden = entry(port, "Visible"), entry(port, "Hidden")
        data["connections"] = [visible, hidden]
        with tempfile.TemporaryDirectory() as root:
            window = MainWindow(AddressBook(Path(root) / "book.json"), data)
            try:
                window.search.setText("Visible")
                window.scan_action.trigger()
                self.wait(lambda: not window.scanner.running)
                self.assertEqual(len(window.scan_results), 2)
                self.assertEqual(window.tree.topLevelItem(0).child(0).text(4), "Online")
                window.search.clear()
                self.assertEqual(window._items_by_id[hidden["id"]].text(4), "Online")
                old = window.scan_results[visible["id"]]
                visible["port"] = 1
                window.refresh()
                window.scan_result(old)
                self.assertNotIn(visible["id"], window.scan_results)
                self.assertEqual(window._items_by_id[visible["id"]].text(4), "Not checked")
                # Availability remains ephemeral and never enters saved entries.
                self.assertTrue(window.commit(data))
                self.assertEqual(window.store.load(), data)
                window.start_scan()
                window.close()
                self.assertFalse(window.scanner.running)
            finally:
                window.close()

    def test_single_refresh_and_individual_animation_lifecycle(self):
        port, requests = self.server()
        data = defaults()
        a, b = entry(port, "A"), entry(port, "B")
        data["connections"] = [a, b]
        with tempfile.TemporaryDirectory() as root:
            window = MainWindow(AddressBook(Path(root) / "book.json"), data)
            try:
                bar = next(bar for bar in window.findChildren(QToolBar) if bar.windowTitle() == "Availability")
                self.assertEqual(bar.actions(), [window.scan_action])
                window.scan_action.trigger()
                self.wait(lambda: len(requests) == 2)
                self.assertTrue(window.scan_animation.isActive())
                button_key = window.scan_action.icon().cacheKey()
                tile_key = window._grid_by_id[a["id"]].icon().cacheKey()
                self.wait(lambda: window.scan_action.icon().cacheKey() != button_key)
                self.assertNotEqual(window._grid_by_id[a["id"]].icon().cacheKey(), tile_key)
                self.assertFalse(window._items_by_id[a["id"]].icon(4).isNull())
                self.sockets[0].write(PREAMBLE_ACK)
                self.wait(lambda: window.scan_results[a["id"]].status == "Online")
                finished_key = window._grid_by_id[a["id"]].icon().cacheKey()
                pending_key = window._grid_by_id[b["id"]].icon().cacheKey()
                self.wait(lambda: window._grid_by_id[b["id"]].icon().cacheKey() != pending_key)
                self.assertEqual(window._grid_by_id[a["id"]].icon().cacheKey(), finished_key)
                self.assertTrue(window._items_by_id[a["id"]].icon(4).isNull())
                # Searching mid-scan retains the animation for remaining entries.
                window.search.setText("B")
                self.assertFalse(window._items_by_id[b["id"]].icon(4).isNull())
                window.scan_action.trigger()  # Same button can stop the active scan.
                self.assertFalse(window.scan_animation.isActive())
                self.assertFalse(window.scanner.running)
                self.assertEqual(window.scan_results[b["id"]].status, "Cancelled")
                self.assertTrue(window._items_by_id[b["id"]].icon(4).isNull())
                window.search.clear()
                window.scanner.timeout_ms = 100
                window.scan_action.trigger()
                self.wait(lambda: not window.scanner.running)
                self.assertFalse(window.scan_animation.isActive())
                self.assertTrue(all(r.status == "Port open" for r in window.scan_results.values()))
                for item in window._items_by_id.values():
                    self.assertEqual(item.foreground(4).color().name(), "#22a06b")
                window.scan_action.trigger()
                window.close()
                self.assertFalse(window.scan_animation.isActive())
            finally:
                window.close()
