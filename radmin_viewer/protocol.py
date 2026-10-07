"""Radmin 3 Radmin-security authentication (not Windows/NTLM authentication).

The session stops after mutual SRP authentication, before encrypted channel
negotiation. See docs/protocol-notes.md for evidence and compatibility scope.
No credentials, proofs, or session keys are logged.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import socket
import struct
import time
from enum import Enum


class RadminError(Exception):
    """Base for expected connection/protocol errors."""


class TransportError(RadminError):
    """Network I/O failed or the peer closed the connection."""


class RadminTimeout(TransportError):
    """The connection/authentication deadline expired."""


class ProtocolError(RadminError):
    """Malformed or out-of-order peer message."""


class AuthenticationError(RadminError):
    """Authentication was rejected or the server proof was invalid."""


class UnsupportedProtocolError(ProtocolError):
    """Peer selected an unimplemented authentication protocol or group."""


class SessionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    AUTHENTICATING = "authenticating"
    AUTHENTICATED = "authenticated"
    FAILED = "failed"
    CLOSED = "closed"


PREAMBLE = bytes.fromhex("0100000005000002272702000000")
PREAMBLE_ACK = bytes.fromhex("0100000005000000272700000000")
MAX_AUTH_FRAME = 4096
# Public group observed from the authorized 3.5.2.1 server. Pinning avoids
# accepting attacker-selected weak groups. Other groups need explicit support.
MODULUS = bytes.fromhex(
    "9847fc7e0f891dfd5d02f19d587d8f77aec0b980d4304b0113b406f23e2cec58"
    "cafca04a53e36fb68e0c3bff92cf335786b0dbe60dfe4178ef2fcd2a4dd09947"
    "ffd8df96fd0f9e2981a32da95503342eca9f08062cbdd4ac2d7cdf810db4db96d"
    "b70102266261cd3f8bdd56a102fc6ceedbba5eae99e6127bdd952f7a0d18a7902"
    "1c881ae63ec4b3590387f548598f2cb8f90dea36fc4f80c5473fdb6b0c6bdb0fd"
    "baf4601f560dd149167ea125db8ad34fd0fd45350dec72cfb3b528ba2332d6091"
    "acea89dfd06c9c4d18f697245bd2ac9278b92bfe7dbafaa0c43b40a71f1930eb"
    "c4fd24c9e5a2e5a4ccf5d7f51544d70b2bca4af5b8d37b379fd7740a682f"
)


def _hash(data: bytes) -> bytes:
    return hashlib.sha1(data).digest()


def _integer(data: bytes) -> int:
    return int.from_bytes(data, "big")


def _minimal(number: int) -> bytes:
    return number.to_bytes(max(1, (number.bit_length() + 7) // 8), "big")


def _tlv(tag: int, value: bytes) -> bytes:
    # Tags are LE16; lengths are BE16. Thus tag 0x10 appears as 10 00.
    return struct.pack("<H", tag) + struct.pack(">H", len(value)) + value


def _parse_tlvs(data: bytes) -> dict[int, bytes]:
    fields = {}
    while data:
        if len(data) < 4:
            raise ProtocolError("Truncated TLV header")
        tag = struct.unpack_from("<H", data)[0]
        length = struct.unpack_from(">H", data, 2)[0]
        if length > len(data) - 4:
            raise ProtocolError("Truncated TLV value")
        if tag in fields:
            raise ProtocolError("Duplicate authentication TLV")
        fields[tag] = data[4:4 + length]
        data = data[4 + length:]
    return fields


def _auth_frame(step: int, fields: dict[int, bytes]) -> bytes:
    body = _tlv(0x10, struct.pack(">I", step))
    body += b"".join(_tlv(tag, value) for tag, value in fields.items())
    return struct.pack(">I", len(body)) + body


def _proofs(username: str, password: str, salt: bytes, a: int,
            public_a: bytes, public_b: bytes) -> tuple[bytes, bytes, bytes]:
    """Compute the Stanford SRP-6a/MGF1 variant used by Radmin."""
    n = _integer(MODULUS)
    b = _integer(public_b)
    if not 1 <= len(public_b) <= len(MODULUS) or not 0 < b < n:
        raise ProtocolError("Invalid SRP server public value")
    user = username.encode("utf-16-le")
    inner = _hash(user + b":" + password.encode("utf-16-le"))
    x = _integer(_hash(salt + inner))
    k = _integer(_hash(MODULUS + (5).to_bytes(len(MODULUS), "big")))
    u = _integer(_hash(public_a.rjust(len(MODULUS), b"\0")
                       + public_b.rjust(len(MODULUS), b"\0")))
    if not u:
        raise ProtocolError("Invalid SRP scrambling parameter")
    base = (b - k * pow(5, x, n)) % n
    if not base:
        raise ProtocolError("Invalid SRP shared-secret base")
    secret = _minimal(pow(base, a + u * x, n))
    key = _hash(secret + b"\0\0\0\0") + _hash(secret + b"\0\0\0\1")
    xor = bytes(x ^ y for x, y in zip(_hash(MODULUS), _hash(b"\x05")))
    m1 = _hash(xor + _hash(user) + salt + public_a + public_b + key)
    m2 = _hash(public_a + m1 + key)
    return m1, m2, key


class RadminSession:
    """Synchronous, single-owner session; connect returns after mutual proof.

    ``timeout`` bounds the whole connect/authentication exchange. Failed
    connections close their socket and enter FAILED. close() is idempotent.
    The password remains only in memory to permit an explicit reconnect.
    """

    def __init__(self, host: str, port: int, username: str, password: str,
                 timeout: float = 10.0):
        if not isinstance(host, str) or not host:
            raise ValueError("host must be a nonempty string")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if not isinstance(username, str) or not username or "\0" in username:
            raise ValueError("username must be nonempty and contain no NUL")
        if not isinstance(password, str) or "\0" in password:
            raise ValueError("password must be a string containing no NUL")
        try:
            user_bytes = username.encode("utf-16-be")
            password.encode("utf-16-le")
        except UnicodeEncodeError:
            raise ValueError("credentials must be valid Unicode") from None
        if len(user_bytes) > 512:
            raise ValueError("username exceeds the supported length")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        self.host, self.port = host, port
        self._username, self._password = username, password
        self.timeout = float(timeout)
        self._state = SessionState.DISCONNECTED
        self._socket = None
        self._session_key = None
        self._deadline = 0.0

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def authenticated(self) -> bool:
        return self._state is SessionState.AUTHENTICATED

    def _remaining(self) -> float:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise RadminTimeout("Connection/authentication deadline expired")
        return remaining

    def _send(self, data: bytes) -> None:
        self._socket.settimeout(self._remaining())
        self._socket.sendall(data)

    def _read_exact(self, length: int) -> bytes:
        data = bytearray()
        while len(data) < length:
            self._socket.settimeout(self._remaining())
            part = self._socket.recv(length - len(data))
            if not part:
                raise TransportError("Peer closed during authentication")
            data.extend(part)
        return bytes(data)

    def _receive(self, step: int, expected: set[int]) -> dict[int, bytes]:
        length = struct.unpack(">I", self._read_exact(4))[0]
        if not 8 <= length <= MAX_AUTH_FRAME:
            raise ProtocolError("Invalid authentication frame length")
        fields = _parse_tlvs(self._read_exact(length))
        if fields.pop(0x10, None) != struct.pack(">I", step):
            raise ProtocolError("Unexpected authentication step")
        if set(fields) != expected:
            raise ProtocolError("Unexpected authentication fields")
        return fields

    def connect(self) -> RadminSession:
        if self._socket is not None:
            raise RadminError("Session is already connected")
        self._state = SessionState.CONNECTING
        self._deadline = time.monotonic() + self.timeout
        try:
            self._socket = socket.create_connection((self.host, self.port), self._remaining())
            self._send(PREAMBLE)
            if self._read_exact(len(PREAMBLE_ACK)) != PREAMBLE_ACK:
                raise UnsupportedProtocolError("Unsupported server negotiation response")
            self._state = SessionState.AUTHENTICATING
            self._send(_auth_frame(1, {0x20: self._username.encode("utf-16-be")}))
            challenge = self._receive(2, {0x30, 0x40, 0x50})
            if challenge[0x30] != MODULUS or challenge[0x40] != b"\x05":
                raise UnsupportedProtocolError("Unsupported SRP group")
            salt = challenge[0x50]
            if len(salt) != 32:
                raise ProtocolError("Invalid SRP salt length")
            a = secrets.randbits(256) + 2048
            public_a = _minimal(pow(5, a, _integer(MODULUS)))
            self._send(_auth_frame(3, {0x60: public_a}))
            public_b = self._receive(4, {0x60})[0x60]
            m1, m2, key = _proofs(self._username, self._password, salt, a, public_a, public_b)
            self._send(_auth_frame(5, {0x70: m1}))
            received = self._receive(6, {0x70})[0x70]
            if not hmac.compare_digest(received, m2):
                raise AuthenticationError("Server authentication proof did not verify")
            self._session_key = key
            self._socket.settimeout(self.timeout)
            self._state = SessionState.AUTHENTICATED
            return self
        except (socket.timeout, TimeoutError):
            self._fail()
            raise RadminTimeout("Connection/authentication timed out") from None
        except OSError:
            self._fail()
            raise TransportError("Connection/authentication network failure") from None
        except BaseException:
            self._fail()
            raise

    def _fail(self) -> None:
        self.close()
        self._state = SessionState.FAILED

    def close(self) -> None:
        sock, self._socket = self._socket, None
        self._session_key = None
        self._state = SessionState.CLOSED
        if sock is not None:
            sock.close()

    def __enter__(self) -> RadminSession:
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()
