"""Offline protocol fixtures and filesystem invariants; never connects to a host."""
import hashlib
from pathlib import Path
import struct
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib

from radmin_viewer.filetransfer import (
    FileTransferClient, FileTransferCancelled, FileTransferRemoteError,
    _entries, _fields, _path,
)
from radmin_viewer.protocol import ProtocolError, RadminError, RadminTimeout


def record(tag, data):
    return (tag | len(data)).to_bytes(4, "big") + data


def unpack_records(data):
    result = []
    while data:
        word = int.from_bytes(data[:4], "big")
        size = word % (1 << 27)
        if not 0 < size <= len(data) - 4:
            raise AssertionError("invalid client field")
        result.append((word // (1 << 27) << 27, data[4:4 + size]))
        data = data[4 + size:]
    return result


def entry(name, size, *, attributes=32, kind=1):
    # Native metadata is size / raw FILETIME1 / raw FILETIME2 / attributes / kind.
    return record(0xb0000000,
        record(0xc0000000, struct.pack(">QQQIB", size, 1234, 5678, attributes, kind))
        + record(0x50000000, (name + "\x01").encode("utf-16-be")))


class Peer:
    """Independent in-memory peer with persistent DEFLATE state and disk model."""
    def __init__(self, files=None):
        self._session = SimpleNamespace(timeout=9.0)
        self.files = dict(files or {})
        self.closed = False
        self.sent = []
        self.operations = []
        self.timeout_values = []
        self.inflate = zlib.decompressobj(-15)
        self.deflate = zlib.compressobj(1, zlib.DEFLATED, -15)
        self.response_hook = None

    def close(self):
        self.closed = True

    def send(self, packet):
        self.timeout_values.append(self._session.timeout)
        self.sent.append(packet)
        if packet == bytes.fromhex("1a00000004"):
            self.reply = b"\x1a"
            return
        if packet == bytes.fromhex("351000000400000000"):
            self.reply = packet
            return
        plain = self.inflate.decompress(packet[4:])
        assert len(plain) == int.from_bytes(packet[:4], "big")
        f = dict(unpack_records(plain))
        operation = int.from_bytes(f[0x10000000], "big")
        path = f[0x50000000].decode("utf-16-be")
        assert path.endswith("\x01")
        path = path[:-1]
        self.operations.append((operation, path, f))
        response = b""
        compressed = operation != 6
        if operation == 0x21:
            if path.endswith("\\*"):
                parent = path[:-1]
                response = b"".join(entry(p[len(parent):], len(data)) for p, data in self.files.items()
                                    if p.startswith(parent) and "\\" not in p[len(parent):])
            elif path in self.files:
                response = entry(path.rsplit("\\", 1)[1], len(self.files[path]))
            else:
                response = bytes.fromhex("f000000400000002")
        elif operation == 5:
            count = int.from_bytes(f[0x30000000], "big")
            offset = int.from_bytes(f[0x20000000], "big")
            if path in self.files:
                data = self.files[path][offset:offset + count]
                response = record(0x40000000, data) if data else b""
            else:
                response = bytes.fromhex("f000000400000002")
        elif operation == 6:
            offset = int.from_bytes(f[0x20000000], "big")
            data = f.get(0x40000000, b"")
            old = self.files.get(path, b"")
            assert offset == len(old), "only sequential writes expected"
            self.files[path] = old + data
        else:
            raise AssertionError("unrecovered operation sent")
        if not response:
            response = bytes.fromhex("f800000400000000")
        if compressed:
            self.reply = len(response).to_bytes(4, "big") + self.deflate.compress(response)
            self.reply += self.deflate.flush(zlib.Z_SYNC_FLUSH)
        else:
            self.reply = response

    def receive(self):
        self.timeout_values.append(self._session.timeout)
        if self.response_hook:
            return self.response_hook(self.reply)
        return self.reply


class FileTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def client(self, peer=None, **options):
        peer = peer or Peer()
        client = FileTransferClient(peer, **options).start()
        self.addCleanup(client.close)
        return client, peer

    def test_mode_and_init_literal_bytes(self):
        client, peer = self.client()
        self.assertEqual(peer.sent, [bytes.fromhex("1a00000004"), bytes.fromhex("351000000400000000")])
        self.assertTrue(all(0 < x <= 2 for x in peer.timeout_values))
        self.assertEqual(peer._session.timeout, 9)
        with self.assertRaises(RadminError): client.start()

    def test_metadata_literal_fixture(self):
        # Retained shape from the live 5165-byte probe, name simplified to 'x'.
        fixture = bytes.fromhex(
            "b0000029c000001d000000000000142d01dd557f323d8837"
            "01dd557f323d883700000020015000000400780001")
        item, = _entries(fixture)
        self.assertEqual((item.name, item.size, item.attributes, item.entry_type), ("x", 5165, 32, 1))
        self.assertFalse(item.is_directory)

    def test_listing_repeated_entries_and_persistent_compression(self):
        client, peer = self.client(Peer({"C:\\Temp\\α.txt": b"abc", "C:\\Temp\\other": b"12345"}))
        for _ in range(3):
            items = client.list_directory("C:/Temp")
            self.assertEqual([(e.name, e.size) for e in items], [("α.txt", 3), ("other", 5)])
        self.assertTrue(all(op[1] == "C:\\Temp\\*" for op in peer.operations))

    def test_multichunk_upload_download_and_empty(self):
        client, peer = self.client(chunk_size=7)
        for index, data in enumerate((bytes(range(100)), b"")):
            local = self.root / f"source{index}"
            out = self.root / f"download{index}"
            local.write_bytes(data)
            path = f"C:\\Temp\\new{index}-λ.bin"
            result = client.upload(local, path)
            self.assertEqual(peer.files[path], data)
            self.assertEqual(result.sha256, hashlib.sha256(data).hexdigest())
            self.assertEqual(result.bytes_transferred, len(data))
            copied = client.download(path, out)
            self.assertEqual(out.read_bytes(), data)
            self.assertEqual(copied.sha256, result.sha256)
        offsets = [int.from_bytes(f[0x20000000], "big") for op, p, f in peer.operations if op == 6 and "new0" in p]
        self.assertEqual(offsets, list(range(0, 100, 7)))

    def test_existing_remote_case_insensitive_never_written(self):
        client, peer = self.client(Peer({"C:\\Temp\\Existing.bin": b"original"}))
        local = self.root / "source"
        local.write_bytes(b"replacement")
        with self.assertRaises(FileExistsError): client.upload(local, "C:\\Temp\\existing.BIN")
        self.assertFalse(any(op == 6 for op, _, _ in peer.operations))
        self.assertEqual(peer.files["C:\\Temp\\Existing.bin"], b"original")

    def test_existing_local_never_overwritten(self):
        client, peer = self.client(Peer({"C:\\Temp\\file": b"remote"}))
        local = self.root / "existing"
        local.write_bytes(b"local")
        with self.assertRaises(FileExistsError): client.download("C:\\Temp\\file", local)
        self.assertEqual(local.read_bytes(), b"local")
        self.assertFalse(any(op == 5 for op, _, _ in peer.operations))

    def test_cancel_before_start_no_messages(self):
        peer = Peer()
        client = FileTransferClient(peer)
        event = threading.Event(); event.set()
        with self.assertRaises(FileTransferCancelled): client.start(cancel=event)
        self.assertTrue(peer.closed)
        self.assertEqual(peer.sent, [])

    def test_cancel_during_download_removes_partial_only(self):
        client, peer = self.client(Peer({"C:\\Temp\\file": b"contents"}), chunk_size=2)
        event = threading.Event()
        def hook(reply):
            if peer.operations[-1][0] == 5: event.set()
            return reply
        peer.response_hook = hook
        local = self.root / "partial"
        with self.assertRaises(FileTransferCancelled): client.download("C:\\Temp\\file", local, cancel=event)
        self.assertFalse(local.exists())
        self.assertTrue(peer.closed)

    def test_cancel_upload_retains_only_new_partial_remote(self):
        client, peer = self.client(chunk_size=2)
        source = self.root / "source"; source.write_bytes(b"abcdef")
        event = threading.Event()
        def hook(reply):
            if peer.operations[-1][0] == 6: event.set()
            return reply
        peer.response_hook = hook
        with self.assertRaises(FileTransferCancelled): client.upload(source, "C:\\Temp\\new", cancel=event)
        self.assertEqual(peer.files, {"C:\\Temp\\new": b"ab"})
        self.assertTrue(peer.closed)

    def test_network_timeout_closes_and_restores_session_timeout(self):
        client, peer = self.client()
        def hook(reply): raise RadminTimeout("synthetic peer stall")
        peer.response_hook = hook
        with self.assertRaises(RadminTimeout): client.list_directory("C:\\Temp")
        self.assertTrue(peer.closed)
        self.assertEqual(peer._session.timeout, 9)

    def test_total_deadline(self):
        client, peer = self.client(operation_timeout=3)
        with patch("radmin_viewer.filetransfer.time.monotonic", side_effect=[0, 4]):
            with self.assertRaises(RadminTimeout): client.list_directory("C:\\Temp")
        self.assertTrue(peer.closed)
        self.assertEqual(len(peer.sent), 2)

    def test_remote_error_is_typed_and_stream_remains_usable(self):
        client, peer = self.client()
        with self.assertRaises(FileTransferRemoteError) as caught:
            client.download("C:\\Temp\\missing", self.root / "out")
        self.assertEqual(caught.exception.code, 2)
        self.assertFalse(peer.closed)
        self.assertEqual(client.list_directory("C:\\Temp"), ())

    def test_bounded_decompression_and_invalid_stream(self):
        for malformed in (b"abc", bytes.fromhex("7fffffff00"), struct.pack(">I", 5) + b"invalid"):
            client, peer = self.client()
            peer.response_hook = lambda reply, bad=malformed: bad
            with self.assertRaises(ProtocolError): client.list_directory("C:\\Temp")
            self.assertTrue(peer.closed)

    def test_bad_write_ack_fails_closed(self):
        client, peer = self.client()
        source = self.root / "source"; source.write_bytes(b"data")
        peer.response_hook = lambda reply: b"wrong" if peer.operations[-1][0] == 6 else reply
        with self.assertRaises(ProtocolError): client.upload(source, "C:\\Temp\\new")
        self.assertTrue(peer.closed)

    def test_size_limits_before_transfer(self):
        client, peer = self.client(Peer({"C:\\Temp\\large": b"abcd"}), max_file_size=3)
        source = self.root / "source"; source.write_bytes(b"abcd")
        with self.assertRaises(ValueError): client.upload(source, "C:\\Temp\\new")
        with self.assertRaises(ValueError): client.download("C:\\Temp\\large", self.root / "out")
        self.assertFalse(any(op in (5, 6) for op, _, _ in peer.operations))

    def test_path_boundaries(self):
        for path in ("relative", "C:relative", "C:\\a\\..\\b", "C:\\*", "C:\\a:stream",
                     "\\\\server\\share\\x", "C:\\NUL.txt", "C:\\file.", "C:\\bad\0name"):
            with self.subTest(path=path), self.assertRaises(ValueError): _path(path)
        self.assertEqual(_path("C:/Temp/α.bin"), "C:\\Temp\\α.bin")

    def test_malformed_directory_records(self):
        valid = entry("x", 3)
        for data in (valid[:-1], b"\x00", bytes.fromhex("b0000000"), valid + valid,
                     record(0xb0000000, record(0x50000000, b"\x00x\x00\x01"))):
            with self.subTest(data=data), self.assertRaises(ProtocolError): _entries(data)
        with self.assertRaises(ProtocolError): _fields(bytes.fromhex("f7ffffff00"))

    def test_short_read_fails_and_cleans_local_file(self):
        client, peer = self.client(Peer({"C:\\Temp\\file": b"abc"}))
        original_send = peer.send
        def send(payload):
            original_send(payload)
            if peer.operations and peer.operations[-1][0] == 0x21:
                peer.files["C:\\Temp\\file"] = b""
        peer.send = send
        local = self.root / "out"
        with self.assertRaises(ProtocolError): client.download("C:\\Temp\\file", local)
        self.assertFalse(local.exists())


if __name__ == "__main__":
    unittest.main()
