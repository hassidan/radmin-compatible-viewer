"""Radmin AES-CBC record layer. See docs/protocol-notes.md for evidence/scope."""
from __future__ import annotations

import hmac
import socket
import struct
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from Crypto.Hash import MD4

from .protocol import ProtocolError, RadminError, RadminTimeout, TransportError

MAX_RECORD = 16 * 1024 * 1024


def _checksum(data: bytes) -> bytes:
    """Fold MD4 of the little-endian 64-bit additive checksum to eight bytes."""
    total = sum(int.from_bytes(data[i:i + 8], "little") for i in range(0, len(data), 8))
    digest = MD4.new((total & ((1 << 64) - 1)).to_bytes(8, "little")).digest()
    return bytes((digest[i] + digest[i + 8]) & 255 for i in range(8))


def _pad(payload: bytes) -> bytes:
    if not payload or len(payload) > MAX_RECORD - 24:
        raise ProtocolError("Invalid channel payload length")
    size = ((len(payload) + 24) // 16) * 16
    body = payload + b"\xcc" * (size - 9 - len(payload))
    return body + _checksum(body) + bytes([size - len(payload)])


def _unpad(record: bytes) -> bytes:
    if not record or len(record) % 16 or len(record) > MAX_RECORD:
        raise ProtocolError("Invalid encrypted record length")
    padding = record[-1]
    if not 9 <= padding <= 24 or padding >= len(record):
        raise ProtocolError("Invalid encrypted record padding")
    if not hmac.compare_digest(_checksum(record[:-9]), record[-9:-1]):
        raise ProtocolError("Encrypted record checksum mismatch")
    return record[:-padding]


class EncryptedChannel:
    """Single-owner channel over an authenticated RadminSession.

    The session remains the socket owner. Closing either closes the connection.
    Every operation has a total deadline; a partial/invalid record poisons the
    stream and closes it rather than attempting an unsafe retry.
    """

    def __init__(self, session):
        if not session.authenticated or session._socket is None or session._session_key is None:
            raise RadminError("Channel requires an authenticated session")
        self._session = session
        self._socket = session._socket
        self._encryptor = self._decryptor = None
        self._started = False
        self._closed = False
        self._deadline = 0.0

    def _begin(self):
        if self._closed or self._session._socket is not self._socket:
            raise TransportError("Channel is closed")
        self._deadline = time.monotonic() + self._session.timeout

    def _timeout(self):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise RadminTimeout("Channel operation timed out")
        self._socket.settimeout(remaining)

    def _read_exact(self, length):
        result = bytearray()
        while len(result) < length:
            self._timeout()
            block = self._socket.recv(length - len(result))
            if not block:
                raise TransportError("Peer closed the channel")
            result.extend(block)
        return bytes(result)

    def _write_frame(self, payload):
        self._timeout()
        self._socket.sendall(struct.pack(">I", len(payload)) + payload)

    def _read_frame(self):
        length = struct.unpack(">I", self._read_exact(4))[0]
        if not 1 <= length <= MAX_RECORD:
            raise ProtocolError("Invalid channel frame length")
        return self._read_exact(length)

    def start(self):
        """Exchange the cleartext session token before encrypted records."""
        if self._started:
            raise RadminError("Channel has already started")
        self._begin()
        try:
            self._write_frame(b"\x2e")
            reply = self._read_frame()
            if len(reply) != 33 or reply[0] != 0x2e:
                raise ProtocolError("Unexpected channel-start reply")
            key = self._session._session_key[:32]
            self._encryptor = Cipher(algorithms.AES(key), modes.CBC(reply[17:33])).encryptor()
            self._decryptor = Cipher(algorithms.AES(key), modes.CBC(reply[1:17])).decryptor()
            self._started = True
            return self
        except BaseException as error:
            self._abort(error)

    def send(self, payload: bytes):
        if not self._started:
            raise RadminError("Channel has not started")
        padded = _pad(payload)
        self._begin()
        try:
            self._write_frame(self._encryptor.update(padded))
        except BaseException as error:
            self._abort(error)

    def receive(self) -> bytes:
        if not self._started:
            raise RadminError("Channel has not started")
        self._begin()
        try:
            frame = self._read_frame()
            if len(frame) % 16:
                raise ProtocolError("Ciphertext is not block aligned")
            return _unpad(self._decryptor.update(frame))
        except BaseException as error:
            self._abort(error)

    def _abort(self, error):
        self.close()
        if isinstance(error, (socket.timeout, TimeoutError)):
            raise RadminTimeout("Channel operation timed out") from None
        if isinstance(error, OSError):
            raise TransportError("Channel network failure") from None
        raise error

    def close(self):
        self._closed = True
        self._encryptor = self._decryptor = None
        self._session.close()
