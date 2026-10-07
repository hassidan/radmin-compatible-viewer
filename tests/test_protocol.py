"""Offline authentication tests. All credentials here are synthetic fixtures."""
import hashlib
import socket
import struct
import unittest
from unittest.mock import patch

from radmin_viewer import protocol as p


USER = "fixture-\u00e9"
PASSWORD = "synthetic test password"
SALT = bytes(range(32))


def digest(data):
    return hashlib.sha1(data).digest()


def integer_bytes(value):
    return value.to_bytes((value.bit_length() + 7) // 8, "big")


def packet(step, fields):
    # Independent encoder: the first TLV has tag 0x0010 and a four-byte value.
    body = b"\x10\x00\x00\x04" + step.to_bytes(4, "big")
    for tag, value in fields:
        body += bytes([tag, 0]) + len(value).to_bytes(2, "big") + value
    return len(body).to_bytes(4, "big") + body


class SRPPeer:
    """Independent server-side SRP algebra with deliberately fragmented reads.

    This uses the server equation (A*v**u)**b, never the client implementation
    or its shared-secret/proof functions. The entire TCP stream is modeled,
    including a following frame that must remain unread after authentication.
    """
    def __init__(self, mode="ok"):
        self.mode = mode
        self.buffer = bytearray()
        self.closed = False
        self.count = 0
        self.n = int.from_bytes(p.MODULUS, "big")
        self.b = 0xBADC0FFEE123456789ABCDEF
        user = USER.encode("utf-16-le")
        x = int.from_bytes(digest(SALT + digest(user + b":" + PASSWORD.encode("utf-16-le"))), "big")
        self.v = pow(5, x, self.n)
        k = int.from_bytes(digest(p.MODULUS + (5).to_bytes(256, "big")), "big")
        self.B = integer_bytes((k * self.v + pow(5, self.b, self.n)) % self.n)
        self.timeouts = []

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def close(self):
        self.closed = True

    def recv(self, size):
        if self.mode == "timeout":
            raise socket.timeout()
        size = min(size, 3)  # Splits length prefixes, TLV headers and values.
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def sendall(self, data):
        self.count += 1
        if self.count == 1:
            assert data == bytes.fromhex("0100000005000002272702000000")
            self.buffer += p.PREAMBLE_ACK if self.mode != "preamble" else bytes(14)
        elif self.count == 2:
            assert data == packet(1, [(0x20, USER.encode("utf-16-be"))])
            fields = [(0x30, p.MODULUS), (0x40, b"\5"), (0x50, SALT)]
            if self.mode == "group":
                fields[1] = (0x40, b"\2")
            if self.mode == "salt":
                fields[2] = (0x50, b"short")
            response = packet(2, fields)
            if self.mode == "duplicate":
                response = packet(2, fields + [(0x50, SALT)])
            elif self.mode == "step":
                response = packet(4, fields)
            elif self.mode == "oversize":
                response = (p.MAX_AUTH_FRAME + 1).to_bytes(4, "big")
            elif self.mode == "truncated":
                response = response[:-1]
            self.buffer += response
        elif self.count == 3:
            assert data[4:16] == bytes.fromhex("10000004000000036000") + len(data[16:]).to_bytes(2, "big")
            self.A = data[16:]
            a_number = int.from_bytes(self.A, "big")
            assert 0 < a_number < self.n
            u = int.from_bytes(digest(self.A.rjust(256, b"\0") + self.B.rjust(256, b"\0")), "big")
            secret = integer_bytes(pow(a_number * pow(self.v, u, self.n) % self.n, self.b, self.n))
            self.key = b"".join(digest(secret + i.to_bytes(4, "big")) for i in range(2))
            xor = bytes(a ^ b for a, b in zip(digest(p.MODULUS), digest(b"\5")))
            self.m1 = digest(xor + digest(USER.encode("utf-16-le")) + SALT + self.A + self.B + self.key)
            self.m2 = digest(self.A + self.m1 + self.key)
            self.buffer += packet(4, [(0x60, b"\0" if self.mode == "zero_b" else self.B)])
        elif self.count == 4:
            assert data == packet(5, [(0x70, self.m1)])
            response = self.m2 if self.mode != "bad_proof" else bytes(20)
            self.buffer += packet(6, [(0x70, response)])
            self.buffer += b"following frame must stay unread"
        else:
            raise AssertionError("Unexpected protocol traffic")


class ProtocolTests(unittest.TestCase):
    def session(self, **kwargs):
        return p.RadminSession("test.invalid", 4899, USER, PASSWORD, **kwargs)

    def test_mutual_authentication_fragmentation_and_close(self):
        peer = SRPPeer()
        with patch.object(p.socket, "create_connection", return_value=peer):
            session = self.session()
            self.assertEqual(session.state, "disconnected")
            self.assertIs(session.connect(), session)
            self.assertTrue(session.authenticated)
            self.assertEqual(session._session_key, peer.key)
            self.assertEqual(len(session._session_key), 40)
            self.assertEqual(peer.buffer, b"following frame must stay unread")
            with self.assertRaises(p.RadminError):
                session.connect()
            self.assertTrue(session.authenticated)
            session.close()
            session.close()
            self.assertEqual(session.state, "closed")
            self.assertFalse(session.authenticated)
            self.assertIsNone(session._session_key)
            self.assertTrue(peer.closed)

    def test_fail_closed(self):
        cases = {
            "preamble": p.UnsupportedProtocolError,
            "group": p.UnsupportedProtocolError,
            "salt": p.ProtocolError,
            "duplicate": p.ProtocolError,
            "step": p.ProtocolError,
            "oversize": p.ProtocolError,
            "truncated": p.TransportError,
            "zero_b": p.ProtocolError,
            "bad_proof": p.AuthenticationError,
            "timeout": p.RadminTimeout,
        }
        for mode, error in cases.items():
            with self.subTest(mode=mode):
                peer = SRPPeer(mode)
                with patch.object(p.socket, "create_connection", return_value=peer):
                    session = self.session()
                    with self.assertRaises(error) as exc:
                        session.connect()
                    self.assertNotIn(PASSWORD, str(exc.exception))
                    self.assertNotIn(USER, str(exc.exception))
                    self.assertEqual(session.state, "failed")
                    self.assertFalse(session.authenticated)
                    self.assertIsNone(session._session_key)
                    self.assertIsNone(session._socket)
                    self.assertTrue(peer.closed)

    def test_public_value_bounds(self):
        for public_b in (b"", b"\0", p.MODULUS, bytes(257)):
            with self.subTest(length=len(public_b)):
                with self.assertRaises(p.ProtocolError):
                    p._proofs(USER, PASSWORD, SALT, 123, b"\1", public_b)

    def test_short_public_a_uses_minimal_proof_encoding(self):
        # Exercise the distinction between fixed-width u hashing and minimal
        # public-value encoding in M1/M2. This case occurs in real random keys.
        n = int.from_bytes(p.MODULUS, "big")
        a = next(a for a in range(2048, 10000) if pow(5, a, n).bit_length() <= 2040)
        peer = SRPPeer()
        with patch.object(p.socket, "create_connection", return_value=peer):
            with patch.object(p.secrets, "randbits", return_value=a - 2048):
                with self.session() as session:
                    self.assertTrue(session.authenticated)
                    self.assertLess(len(peer.A), 256)

    def test_tlv_bounds(self):
        for body in (b"\x10", b"\x10\0\0", b"\x10\0\0\x04\0"):
            with self.assertRaises(p.ProtocolError):
                p._parse_tlvs(body)
        self.assertEqual(p._parse_tlvs(b"\x20\0\0\2AB"), {0x20: b"AB"})

    def test_context_and_reconnect(self):
        first, second = SRPPeer(), SRPPeer()
        with patch.object(p.socket, "create_connection", side_effect=[first, second]):
            session = self.session()
            with session as connected:
                self.assertTrue(connected.authenticated)
            self.assertTrue(first.closed)
            with session:
                self.assertTrue(session.authenticated)
            self.assertTrue(second.closed)

    def test_network_failure(self):
        with patch.object(p.socket, "create_connection", side_effect=ConnectionRefusedError()):
            session = self.session()
            with self.assertRaises(p.TransportError):
                session.connect()
            self.assertEqual(session.state, "failed")

    def test_deadline_is_total_not_per_read(self):
        peer = SRPPeer()
        with patch.object(p.socket, "create_connection", return_value=peer):
            with patch.object(p.time, "monotonic", side_effect=[0, 0, 0, 0, 11]):
                session = self.session(timeout=10)
                with self.assertRaises(p.RadminTimeout):
                    session.connect()
                self.assertTrue(peer.closed)

    def test_arguments(self):
        for timeout in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                self.session(timeout=timeout)
        for port in (0, 65536, True, "4899"):
            with self.assertRaises(ValueError):
                p.RadminSession("test.invalid", port, USER, PASSWORD)
        for user in ("", "bad\0name", "\ud800", "x" * 257):
            with self.assertRaises(ValueError):
                p.RadminSession("test.invalid", 4899, user, PASSWORD)


if __name__ == "__main__":
    unittest.main()
