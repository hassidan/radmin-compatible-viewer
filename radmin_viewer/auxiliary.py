"""Dedicated Telnet, Send Message and shutdown sessions; see docs/protocol-notes.md.

Construct clients with a started EncryptedChannel on a fresh authenticated
session. Operations are synchronous and single-owner. No credentials are stored
here. Closing a client closes its channel and session.
"""
from __future__ import annotations

from contextlib import contextmanager
from enum import IntEnum
import math
import struct
import threading
import time

from .channel import EncryptedChannel
from .protocol import ProtocolError, RadminError, RadminTimeout, UnsupportedProtocolError


class AuxiliaryCancelled(RadminError):
    """Operation cancelled and dedicated connection closed; remote effects may remain."""


class ShutdownAction(IntEnum):
    SHUT_DOWN = 5
    RESTART = 6
    POWER_OFF = 12


def encode_shutdown(action: ShutdownAction) -> bytes:
    """Encode only the three recovered actions. This function performs no I/O."""
    if not isinstance(action, ShutdownAction):
        raise ValueError("action must be a ShutdownAction")
    return b"\x1e" + struct.pack(">I", action)


def _text(value: str, limit: int, *, nonempty: bool = False) -> bytes:
    if not isinstance(value, str):
        raise TypeError("text fields must be str")
    if "\0" in value or (nonempty and not value) or len(value) > limit:
        raise ValueError("Invalid text field length or embedded NUL")
    try:
        encoded = value.encode("utf-16-be")
    except UnicodeEncodeError:
        raise ValueError("Text must be valid Unicode") from None
    if len(encoded) // 2 > limit:
        raise ValueError("Text exceeds UTF-16 code-unit limit")
    return struct.pack(">I", len(encoded) // 2) + encoded


def encode_message(text: str, *, sender: str = "", computer: str = "") -> bytes:
    """Encode a plain pop-up message (4096 UTF-16 units; identity fields 63).

    Backslash is reserved by the native rich-text language; its escaping and
    color commands are not yet implemented. Names are display labels, not auth.
    Oversize input is rejected rather than silently truncated by the native UI.
    """
    body = _text(text, 4096, nonempty=True)
    if "\\" in text:
        raise ValueError("Backslash/rich-text escapes are not supported")
    return b"\x29" + body + _text(sender, 63) + _text(computer, 63)


class _AuxiliaryClient:
    mode: int

    def __init__(self, channel: EncryptedChannel, *, operation_timeout: float = 10.0,
                 io_timeout: float = 2.0):
        if any(not math.isfinite(t) or t <= 0 for t in (operation_timeout, io_timeout)):
            raise ValueError("Timeouts must be positive and finite")
        self.channel = channel
        self.operation_timeout = float(operation_timeout)
        self.io_timeout = float(io_timeout)
        self._started = False
        self._closed = False
        self._lock = threading.Lock()
        self._deadline = 0.0
        self._cancel = None

    def close(self) -> None:
        self._closed = True
        self.channel.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _check(self):
        if self._cancel is not None and self._cancel.is_set():
            raise AuxiliaryCancelled("Auxiliary operation cancelled")
        if time.monotonic() >= self._deadline:
            raise RadminTimeout("Auxiliary operation deadline exceeded")

    @contextmanager
    def _operation(self, cancel, *, starting=False):
        if not self._lock.acquire(blocking=False):
            raise RadminError("Auxiliary client already has an active operation")
        try:
            if self._closed or (self._started if starting else not self._started):
                raise RadminError("Auxiliary client is closed or in the wrong state")
            self._cancel = cancel
            self._deadline = time.monotonic() + self.operation_timeout
            try:
                self._check()
                yield
                self._check()
            except BaseException:
                self.close()
                raise
            finally:
                self._cancel = None
        finally:
            self._lock.release()

    def _io(self, method, *args):
        self._check()
        session = self.channel._session
        previous = session.timeout
        session.timeout = min(previous, self.io_timeout, self._deadline - time.monotonic())
        try:
            result = method(*args)
        except BaseException:
            if self._cancel is not None and self._cancel.is_set():
                raise AuxiliaryCancelled("Auxiliary operation cancelled") from None
            raise
        finally:
            session.timeout = previous
        self._check()
        return result

    def _exchange(self, payload):
        self._io(self.channel.send, payload)
        return self._io(self.channel.receive)

    def _select(self):
        if self._exchange(b"\x1a" + struct.pack(">I", self.mode)) != b"\x1a":
            raise ProtocolError(f"Auxiliary mode {self.mode} was not immediately accepted")

    def start(self, *, cancel=None):
        """Select this mode. A rejection or deferred acceptance closes the channel."""
        with self._operation(cancel, starting=True):
            self._select()
            self._started = True
        return self


class TelnetClient(_AuxiliaryClient):
    """Radmin command shell, not TCP Telnet/IAC. Byte encoding is caller-selected.

    exchange(b'') polls output; exchange(data) writes input and returns currently
    available output. An empty result means no output yet, not EOF. Every result
    must be consumed by the caller, including the result from a command write.
    """
    mode = 2
    MAX_INPUT = 65536
    MAX_OUTPUT = 1024 * 1024

    def start(self, *, cancel=None):
        with self._operation(cancel, starting=True):
            self._select()
            if self._exchange(b"\x15") != b"\x11":
                raise ProtocolError("Remote command shell is unavailable")
            if self._exchange(b"\x12") != b"\x12":
                raise ProtocolError("Unexpected command-shell start reply")
            self._started = True
        return self

    def _exchange_input(self, data):
        reply = self._exchange(b"\x14" + data)
        if not reply or reply[0] != 0x14 or len(reply) - 1 > self.MAX_OUTPUT:
            raise ProtocolError("Invalid or oversized command-shell output")
        return reply[1:]

    def exchange(self, data: bytes = b"", *, cancel=None) -> bytes:
        """Send raw shell input/poll once. No terminator or encoding is added."""
        if not isinstance(data, bytes):
            raise TypeError("shell input must be bytes")
        if len(data) > self.MAX_INPUT:
            raise ValueError("Shell input exceeds 65536 bytes")
        with self._operation(cancel):
            return self._exchange_input(data)

    def send_line(self, line: str, *, encoding: str = "ascii", cancel=None) -> bytes:
        """Write one command line with CRLF; return immediate output, not completion."""
        if not isinstance(line, str):
            raise TypeError("line must be str")
        if any(ch in line for ch in "\r\n\0"):
            raise ValueError("Use a single command line without NUL")
        return self.exchange(line.encode(encoding) + b"\r\n", cancel=cancel)

    def read_until(self, marker: bytes, *, initial: bytes = b"", max_bytes: int = 1024 * 1024,
                   poll_interval: float = 0.2, cancel=None) -> bytes:
        """Poll until a literal byte marker appears, preserving all returned bytes.

        Marker may cross record boundaries. This is not an exit-status API: a
        command echo can itself contain a marker. The total deadline is the
        client's operation_timeout; exceeding it closes the connection.
        """
        if not isinstance(marker, bytes) or not marker or not isinstance(initial, bytes):
            raise ValueError("marker must be nonempty bytes and initial must be bytes")
        if type(max_bytes) is not int or not 1 <= max_bytes <= 16 * 1024 * 1024:
            raise ValueError("max_bytes must be 1..16777216")
        if len(initial) > max_bytes or len(marker) > max_bytes:
            raise ValueError("Initial data or marker exceeds output limit")
        if not math.isfinite(poll_interval) or not 0 < poll_interval <= 2:
            raise ValueError("poll_interval must be positive and at most 2 seconds")
        with self._operation(cancel):
            output = bytearray(initial)
            while marker not in output:
                block = self._exchange_input(b"")
                if len(output) + len(block) > max_bytes:
                    raise ProtocolError("Command-shell accumulated output exceeds limit")
                output.extend(block)
                if marker in output:
                    break
                delay = min(poll_interval, max(0, self._deadline - time.monotonic()))
                if cancel is None:
                    time.sleep(delay)
                else:
                    cancel.wait(delay)
                self._check()
            return bytes(output)


class SendMessageClient(_AuxiliaryClient):
    mode = 9

    def send_message(self, text: str, *, sender: str = "", computer: str = "", cancel=None) -> None:
        """Send one pop-up and close. ACK means server receipt, not that a user read it.

        Never automatically retry: a lost ACK can leave delivery indeterminate.
        """
        payload = encode_message(text, sender=sender, computer=computer)
        with self._operation(cancel):
            if self._exchange(payload) != b"\x29":
                raise ProtocolError("Unexpected Send Message acknowledgement")
        self.close()


class ShutdownClient(_AuxiliaryClient):
    """Statically recovered, NOT live-tested. Selecting mode alone takes no action."""
    mode = 11

    def execute(self, action: ShutdownAction, *, cancel=None) -> bytes:
        """Send the selected disruptive action once, close, and return raw reply.

        Native code checks transport success only. Reply does not prove power
        state; EOF/timeout leaves the outcome indeterminate. No automatic retry.
        """
        payload = encode_shutdown(action)
        with self._operation(cancel):
            reply = self._exchange(payload)
        self.close()
        return reply


class TextChatClient(_AuxiliaryClient):
    """Explicit unsupported boundary: plugin handshake and schemas are unresolved."""
    mode = 7

    def start(self, *, cancel=None):
        raise UnsupportedProtocolError(
            "Text Chat plugin session handshake and channel messages are not recovered")
