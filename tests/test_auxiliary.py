"""Offline auxiliary protocol tests. Shutdown is exercised ONLY on fake peers."""
import socket
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from radmin_viewer.auxiliary import (
    AuxiliaryCancelled, SendMessageClient, ShutdownAction, ShutdownClient,
    TelnetClient, TextChatClient, encode_message, encode_shutdown,
)
from radmin_viewer.channel import EncryptedChannel
from radmin_viewer.protocol import (
    ProtocolError, RadminError, RadminTimeout, TransportError, UnsupportedProtocolError,
)


class Peer:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.sent = []
        self.closed = False
        self._session = SimpleNamespace(timeout=8.0)
        self.timeouts = []

    def send(self, data):
        self.sent.append(data)
        self.timeouts.append(self._session.timeout)

    def receive(self):
        reply = next(self.replies)
        if callable(reply):
            return reply()
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def close(self):
        self.closed = True


class CodecTests(unittest.TestCase):
    def test_message_literal_fixture_and_surrogate_count(self):
        self.assertEqual(encode_message("A🙂", sender="U", computer="H"), bytes.fromhex(
            "29 00000003 0041d83dde42 00000001 0055 00000001 0048"))
        self.assertEqual(encode_message("x"), bytes.fromhex("290000000100780000000000000000"))

    def test_message_limits_and_invalid_inputs(self):
        encode_message("🙂" * 2048, sender="x" * 63, computer="y" * 63)
        for text in ("", "x\0y", "x" * 4097, "🙂" * 2049, "\ud800", "a\\b"):
            with self.subTest(text_length=len(text)), self.assertRaises(ValueError):
                encode_message(text)
        for kwargs in ({"sender": "x" * 64}, {"computer": "🙂" * 32}, {"sender": "\0"}):
            with self.assertRaises(ValueError):
                encode_message("hello", **kwargs)
        with self.assertRaises(TypeError):
            encode_message(b"hello")

    def test_shutdown_exact_observed_values_only(self):
        for action, hex_bytes in ((ShutdownAction.SHUT_DOWN, "1e00000005"),
                                  (ShutdownAction.RESTART, "1e00000006"),
                                  (ShutdownAction.POWER_OFF, "1e0000000c")):
            self.assertEqual(encode_shutdown(action), bytes.fromhex(hex_bytes))
        for invalid in (0, 1, 5, True, "restart", None):
            with self.assertRaises(ValueError):
                encode_shutdown(invalid)


class ClientTests(unittest.TestCase):
    def shell(self, *replies, **kwargs):
        peer = Peer([b"\x1a", b"\x11", b"\x12", *replies])
        client = TelnetClient(peer, **kwargs).start()
        return client, peer

    def test_shell_handshake_write_and_poll(self):
        client, peer = self.shell(b"\x14first", b"\x14", b"\x14second")
        self.assertEqual(client.send_line("echo OK"), b"first")
        self.assertEqual(client.exchange(), b"")
        self.assertEqual(client.exchange(), b"second")
        self.assertEqual(peer.sent, [bytes.fromhex("1a00000002"), b"\x15", b"\x12",
                                     b"\x14echo OK\r\n", b"\x14", b"\x14"])
        self.assertEqual(peer._session.timeout, 8)
        self.assertTrue(all(0 < t <= 2 for t in peer.timeouts))

    def test_marker_across_packets_and_initial_output(self):
        client, peer = self.shell(b"\x14", b"\x14ND\r", b"\x14\ntrailing")
        self.assertEqual(client.read_until(b"END\r\n", initial=b"E", poll_interval=.001),
                         b"END\r\ntrailing")
        self.assertFalse(peer.closed)

    def test_initial_marker_requires_no_poll(self):
        client, peer = self.shell()
        self.assertEqual(client.read_until(b"OK", initial=b"OK!"), b"OK!")
        self.assertEqual(len(peer.sent), 3)

    def test_unexpected_mode_and_start_replies(self):
        for replies in ([b"\x30"], [b"\x2f\0\0\0\5"], [b"\x1a", b"\x11x"],
                        [b"\x1a", b"\x11", b"\x13"]):
            peer = Peer(replies)
            with self.assertRaises(ProtocolError):
                TelnetClient(peer).start()
            self.assertTrue(peer.closed)

    def test_bad_output_and_output_limits_close(self):
        for reply in (b"", b"\x13", b"\x14" + b"x" * (TelnetClient.MAX_OUTPUT + 1)):
            client, peer = self.shell(reply)
            with self.assertRaises(ProtocolError):
                client.exchange()
            self.assertTrue(peer.closed)
        client, peer = self.shell(b"\x14abc", b"\x14def")
        with self.assertRaises(ProtocolError):
            client.read_until(b"END", max_bytes=5, poll_interval=.001)
        self.assertTrue(peer.closed)

    def test_invalid_local_input_does_not_send(self):
        client, peer = self.shell()
        for line in ("a\rb", "a\nb", "\0"):
            with self.assertRaises(ValueError):
                client.send_line(line)
        with self.assertRaises(UnicodeEncodeError):
            client.send_line("é")
        with self.assertRaises(TypeError):
            client.exchange("not bytes")
        with self.assertRaises(ValueError):
            client.exchange(b"x" * 65537)
        self.assertEqual(len(peer.sent), 3)
        self.assertFalse(peer.closed)

    def test_total_deadline_even_with_fast_empty_replies(self):
        client, peer = self.shell(*([b"\x14"] * 100))
        client.operation_timeout = .02
        with self.assertRaises(RadminTimeout):
            client.read_until(b"never", poll_interval=.005)
        self.assertTrue(peer.closed)
        self.assertEqual(peer._session.timeout, 8)

    def test_pre_cancel_sends_nothing(self):
        peer = Peer([])
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(AuxiliaryCancelled):
            TelnetClient(peer).start(cancel=cancel)
        self.assertEqual(peer.sent, [])
        self.assertTrue(peer.closed)

    def test_cancel_during_io_has_typed_error_and_restores_timeout(self):
        cancel = threading.Event()
        def interrupt():
            cancel.set()
            raise TransportError("closed")
        client, peer = self.shell(interrupt)
        with self.assertRaises(AuxiliaryCancelled):
            client.exchange(cancel=cancel)
        self.assertTrue(peer.closed)
        self.assertEqual(peer._session.timeout, 8)

    def test_concurrent_operation_rejected_without_closing_owner(self):
        client, peer = self.shell()
        client._lock.acquire()
        try:
            with self.assertRaises(RadminError):
                client.exchange()
        finally:
            client._lock.release()
        self.assertFalse(peer.closed)

    def test_bad_state_and_context_teardown(self):
        peer = Peer([b"\x1a", b"\x11", b"\x12"])
        with TelnetClient(peer) as client:
            with self.assertRaises(RadminError):
                client.exchange()
            client.start()
            with self.assertRaises(RadminError):
                client.start()
        self.assertTrue(peer.closed)
        with self.assertRaises(RadminError):
            client.exchange()

    def test_send_message_success_and_no_retry_after_ambiguous_failure(self):
        for reply in (b"\x29", b"\x2f", RadminTimeout("timeout")):
            peer = Peer([b"\x1a", reply])
            client = SendMessageClient(peer).start()
            if reply == b"\x29":
                self.assertIsNone(client.send_message("hello"))
            else:
                with self.assertRaises(RadminError):
                    client.send_message("hello")
            self.assertTrue(peer.closed)
            with self.assertRaises(RadminError):
                client.send_message("hello")
            self.assertEqual(len(peer.sent), 2)
            self.assertEqual(peer.sent[0], bytes.fromhex("1a00000009"))

    def test_shutdown_fake_transport_only(self):
        peer = Peer([b"\x1a", b"\x1e"])
        client = ShutdownClient(peer).start()
        self.assertEqual(peer.sent, [bytes.fromhex("1a0000000b")])
        self.assertEqual(client.execute(ShutdownAction.RESTART), b"\x1e")
        self.assertEqual(peer.sent[1], bytes.fromhex("1e00000006"))
        self.assertTrue(peer.closed)

    def test_chat_fails_before_network(self):
        peer = Peer([])
        with self.assertRaises(UnsupportedProtocolError):
            TextChatClient(peer).start()
        self.assertEqual(peer.sent, [])

    def test_timeout_configuration(self):
        for t in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                TelnetClient(Peer([]), io_timeout=t)

    def test_real_record_wait_is_bounded_and_cancelled(self):
        # Real local socket + existing channel deadline. Peer withholds a frame.
        left, right = socket.socketpair()
        session = SimpleNamespace(authenticated=True, _socket=left,
                                  _session_key=b"x" * 40, timeout=8.0)
        def close():
            left.close()
            session._socket = None
        session.close = close
        channel = EncryptedChannel(session)
        channel._started = True
        client = TelnetClient(channel, io_timeout=.08)
        client._started = True
        cancel = threading.Event()
        timer = threading.Timer(.02, cancel.set)
        timer.start()
        started = time.monotonic()
        try:
            with patch.object(channel, "send"), self.assertRaises(AuxiliaryCancelled):
                client.exchange(cancel=cancel)
            self.assertLess(time.monotonic() - started, .8)
            self.assertIsNone(session._socket)
            self.assertEqual(session.timeout, 8)
        finally:
            timer.join()
            left.close()
            right.close()


if __name__ == "__main__":
    unittest.main()
