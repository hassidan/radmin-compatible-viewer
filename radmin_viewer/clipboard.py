"""Pure codec for statically recovered Radmin clipboard format records.

This is an inner payload codec, not a clipboard or network API. No session
commands, encryption, OS clipboard access, or automatic synchronization occur.
See docs/protocol-notes.md for the public evidence scope and limitations.
"""

from dataclasses import dataclass
import struct


ANSI_TEXT = 0x10000000
UNICODE_TEXT = 0x20000000
RICH_TEXT = 0x30000000
MAX_PAYLOAD_BYTES = 16 * 1024 * 1024  # Local safety policy, not a server limit.
_HEADER = struct.Struct(">II")


class ClipboardFormatError(ValueError):
    """A clipboard record buffer is malformed or exceeds our local limit."""


@dataclass(frozen=True)
class ClipboardRecord:
    format_id: int
    data: bytes


def decode_records(payload: bytes) -> tuple[ClipboardRecord, ...]:
    """Decode BE32 length, BE32 format, data records, preserving unknown formats.

    Length counts data bytes only. Empty buffers represent no supplied formats.
    Zero-length records and partial headers/data are rejected, as are oversized
    buffers. Duplicate formats are preserved in order; native lookup returns
    the first match. Text contents are validated separately by unicode_text().
    """
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ClipboardFormatError("clipboard payload exceeds local size limit")
    records = []
    offset = 0
    while offset < len(payload):
        if len(payload) - offset < _HEADER.size:
            raise ClipboardFormatError("truncated clipboard record header")
        length, format_id = _HEADER.unpack_from(payload, offset)
        offset += _HEADER.size
        if not length or length > len(payload) - offset:
            raise ClipboardFormatError("invalid clipboard record length")
        records.append(ClipboardRecord(format_id, payload[offset:offset + length]))
        offset += length
    return tuple(records)


def encode_unicode_text(text: str) -> bytes:
    """Encode one NUL-terminated UTF-16BE clipboard record (no outer frame)."""
    if not isinstance(text, str):
        raise TypeError("text must be str")
    if "\x00" in text:
        raise ClipboardFormatError("embedded NUL in clipboard text")
    if len(text) > (MAX_PAYLOAD_BYTES - _HEADER.size - 2) // 2:
        raise ClipboardFormatError("clipboard text exceeds local size limit")
    try:
        data = text.encode("utf-16-be") + b"\x00\x00"
    except UnicodeEncodeError as exc:
        raise ClipboardFormatError("invalid Unicode clipboard text") from exc
    if len(data) + _HEADER.size > MAX_PAYLOAD_BYTES:
        raise ClipboardFormatError("clipboard text exceeds local size limit")
    return _HEADER.pack(len(data), UNICODE_TEXT) + data


def unicode_text(payload: bytes) -> str | None:
    """Return the first Unicode text record, or None if it is absent.

    ANSI/RTF bytes are deliberately not assigned an unverified code page.
    This decoder is stricter than the native setter, which forcibly terminates
    input: an even length, trailing NUL and valid UTF-16 are required here.
    """
    for record in decode_records(payload):
        if record.format_id != UNICODE_TEXT:
            continue
        data = record.data
        if len(data) < 2 or len(data) % 2 or not data.endswith(b"\x00\x00"):
            raise ClipboardFormatError("invalid Unicode clipboard terminator or length")
        try:
            text = data[:-2].decode("utf-16-be")
        except UnicodeDecodeError as exc:
            raise ClipboardFormatError("invalid UTF-16BE clipboard text") from exc
        if "\x00" in text:
            raise ClipboardFormatError("embedded NUL in clipboard text")
        return text
    return None
