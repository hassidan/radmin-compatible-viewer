"""Explicit text clipboard operations over a control-mode DesktopStream.

Call consume_message AFTER shared decompression and BEFORE DesktopDecoder.decode.
The UI owns OS clipboard access, worker scheduling and receive deadlines. This
helper neither reads the channel nor creates cipher/compression state. Evidence
scope and live-verification limits are summarized in docs/protocol-notes.md.
"""
from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Protocol

from .clipboard import (ClipboardFormatError, ClipboardRecord, decode_records,
                        encode_unicode_text, unicode_text)
from .desktop import encode_tlv, parse_tlvs
from .protocol import ProtocolError

SEND_CLIPBOARD_TAG = 0x60000000
INPUT_TAG = 0x70000000
RECEIVE_CLIPBOARD_TAG = 0x90000000
GET_CLIPBOARD_EVENT = b"\x13"


class ClipboardTransport(Protocol):
    mode: str

    def send_message(self, data: bytes) -> None: ...


@dataclass(frozen=True)
class ClipboardUpdate:
    """Received formats; text=None is unsupported text, text='' is empty text.

    records/payload are retained for callers needing lossless format inspection;
    do not log them. They are not an acknowledgement of a preceding Send action.
    """

    payload: bytes
    records: tuple[ClipboardRecord, ...]
    text: str | None


def encode_send_text(text: str) -> bytes:
    """One uncompressed client desktop TLV, including the inner record header."""
    return encode_tlv(SEND_CLIPBOARD_TAG, encode_unicode_text(text))


def encode_receive_request() -> bytes:
    """Native one-byte Get event, packed under the client input tag."""
    return encode_tlv(INPUT_TAG, GET_CLIPBOARD_EVENT)


def split_clipboard_message(data: bytes) -> tuple[bytes, ClipboardUpdate | None]:
    """Extract server tag 90 from ONE decrypted, decompressed desktop message.

    Remaining fields retain order and exact bytes, including unknown fields so
    DesktopDecoder can apply its usual checks. Clipboard-only messages return
    b'' as the remainder. No clipboard field returns None, never empty text.
    Invalid outer framing or inner records raise ProtocolError before delivery.
    """
    fields = parse_tlvs(data)
    payload = fields.pop(RECEIVE_CLIPBOARD_TAG, None)
    if payload is None:
        return data, None
    try:
        records = decode_records(payload)
        text = unicode_text(payload)
    except ClipboardFormatError as error:
        raise ProtocolError(str(error)) from error
    remaining = b"".join(encode_tlv(tag, value) for tag, value in fields.items())
    return remaining, ClipboardUpdate(payload, records, text)


class ClipboardSession:
    """One helper per desktop connection, with at most one pending Receive.

    send_text returns after transport submission, NOT remote acknowledgement.
    request_receive arms delivery of the next clipboard field. The network
    owner must feed every desktop message to consume_message, even if it has no
    image. Unsolicited clipboard fields are removed but not delivered to the UI.

    A request has no wire ID. After cancellation a late reply can be mistaken
    for a later request; callers requiring strict correlation must reconnect.
    close only closes this helper; the desktop/session owner closes the socket.
    """

    def __init__(self, stream: ClipboardTransport):
        self._stream = stream
        self._lock = threading.RLock()
        self._pending = False
        self._closed = False

    @property
    def receive_pending(self) -> bool:
        with self._lock:
            return self._pending

    def _require_control(self) -> None:
        if self._closed:
            raise ProtocolError("Clipboard session is closed")
        if self._stream.mode != "control":
            raise ProtocolError("Clipboard transfer requires control mode")

    def send_text(self, text: str) -> None:
        """Send clipboard action; text is captured by the UI on the UI thread."""
        with self._lock:
            self._require_control()
            self._stream.send_message(encode_send_text(text))

    def request_receive(self) -> None:
        """Receive clipboard action; completion arrives through consume_message."""
        with self._lock:
            self._require_control()
            if self._pending:
                raise ProtocolError("A clipboard receive is already pending")
            self._pending = True
            try:
                self._stream.send_message(encode_receive_request())
            except BaseException:
                self._pending = False
                raise

    def consume_message(self, data: bytes) -> tuple[bytes, ClipboardUpdate | None]:
        """Network-reader hook. Return a requested update for UI-thread delivery."""
        remaining, update = split_clipboard_message(data)
        with self._lock:
            if self._closed or self._stream.mode != "control" or not self._pending:
                return remaining, None
            if update is not None:
                self._pending = False
            return remaining, update

    def cancel_receive(self) -> bool:
        """Locally cancel on UI deadline/focus teardown; sends no wire command.

        Returns whether a receive was pending. Timeout means no supported reply,
        not proof the remote clipboard is empty. Late replies are discarded
        while no new receive is pending.
        """
        with self._lock:
            pending, self._pending = self._pending, False
            return pending

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._pending = False
