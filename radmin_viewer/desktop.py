"""Desktop negotiation, raw-DEFLATE messages and 24-bit image updates.

View-only is the default. Input helpers can submit messages only after explicit
control-mode negotiation. Unknown image encodings fail explicitly instead of
being rendered as pixels. See docs/protocol-notes.md for compatibility scope.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import struct
import select
import socket
import threading
import time
import zlib

from .channel import EncryptedChannel, MAX_RECORD
from .protocol import ProtocolError, TransportError, UnsupportedProtocolError

MAX_DESKTOP_MESSAGE = 64 * 1024 * 1024
MAX_PIXELS = 16 * 1024 * 1024


def encode_tlv(tag: int, value: bytes) -> bytes:
    if tag & ~0xf8000000 or not tag or not value or len(value) > 0x7ffffff:
        raise ValueError("Invalid desktop TLV")
    return struct.pack(">I", tag | len(value)) + value


def parse_tlvs(data: bytes) -> dict[int, bytes]:
    fields = {}
    pos = 0
    while pos < len(data):
        if len(data) - pos < 4:
            raise ProtocolError("Truncated desktop TLV header")
        header = struct.unpack_from(">I", data, pos)[0]
        tag, length = header & 0xf8000000, header & 0x7ffffff
        pos += 4
        if not tag or not length or length > len(data) - pos:
            raise ProtocolError("Invalid desktop TLV length or tag")
        if tag in fields:
            raise ProtocolError("Duplicate desktop TLV")
        fields[tag] = data[pos:pos + length]
        pos += length
    return fields


class DesktopCompression:
    """Independent persistent raw-DEFLATE state for each stream direction."""

    def __init__(self):
        self._send = zlib.compressobj(level=1, wbits=-15)
        self._receive = zlib.decompressobj(wbits=-15)
        self._failed = False

    def encode(self, data: bytes) -> bytes:
        if self._failed:
            raise ProtocolError("Desktop compression stream has failed")
        if not 1 <= len(data) <= MAX_DESKTOP_MESSAGE:
            raise ProtocolError("Invalid desktop message length")
        result = struct.pack(">I", len(data)) + self._send.compress(data)
        result += self._send.flush(zlib.Z_SYNC_FLUSH)
        if len(result) > MAX_RECORD - 24:
            self._failed = True
            raise ProtocolError("Compressed desktop message is too large")
        return result

    def decode(self, data: bytes) -> bytes:
        if self._failed:
            raise ProtocolError("Desktop compression stream has failed")
        try:
            if len(data) <= 4:
                raise ProtocolError("Truncated compressed desktop envelope")
            length = struct.unpack_from(">I", data)[0]
            if not 1 <= length <= MAX_DESKTOP_MESSAGE:
                raise ProtocolError("Desktop message exceeds size limit")
            result = self._receive.decompress(data[4:], length + 1)
            if (len(result) != length or self._receive.unconsumed_tail
                    or self._receive.unused_data or self._receive.eof):
                raise ProtocolError("Desktop DEFLATE length/stream mismatch")
            return result
        except (zlib.error, ProtocolError) as error:
            self._failed = True
            if isinstance(error, zlib.error):
                raise ProtocolError("Invalid desktop DEFLATE data") from None
            raise


@dataclass(frozen=True)
class DesktopFrame:
    width: int
    height: int
    rgb: bytes

    def __post_init__(self):
        if (self.width <= 0 or self.height <= 0 or self.width * self.height > MAX_PIXELS
                or len(self.rgb) != self.width * self.height * 3):
            raise ValueError("Invalid RGB frame geometry")

    def to_png(self) -> bytes:
        def chunk(kind, data):
            return (struct.pack(">I", len(data)) + kind + data
                    + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))
        stride = self.width * 3
        scanlines = b"".join(b"\0" + self.rgb[y:y + stride]
                             for y in range(0, len(self.rgb), stride))
        return (b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(scanlines)) + chunk(b"IEND", b""))


class DesktopDecoder:
    """Retain a top-down BGR framebuffer and apply bounded scanline updates.

    Cursor position/shape metadata is retained separately, never painted into
    screen pixels. Cursor-only updates return None. Shape bytes are opaque;
    decoding remote cursor bitmaps is not implemented.
    """

    def __init__(self):
        self._size = None
        self._format = None
        self._pixels = None
        self.cursor_position = None
        self.cursor_shape = None

    @staticmethod
    def _spans(commands: bytes, width: int, height: int):
        """Native region format: row skips or BE16 [left, right|last] spans.

        Server image 0x1468260/0x14682e0 emit row skips; 0x1468380
        emits coordinate pairs and marks the final span's right bound.
        """
        pos = y = previous_right = 0
        row_open = False
        while pos < len(commands):
            first = commands[pos]
            if first & 0x80:
                if row_open:
                    raise ProtocolError("Row skip inside an unfinished desktop row")
                y += (first & 0x7f) + 1
                if y > height:
                    raise ProtocolError("Desktop row skip exceeds framebuffer")
                pos += 1
                continue
            if len(commands) - pos < 4:
                raise ProtocolError("Truncated desktop span")
            left, encoded_right = struct.unpack_from(">HH", commands, pos)
            right = encoded_right & 0x7fff
            if not (y < height and previous_right <= left < right <= width):
                raise ProtocolError("Invalid or overlapping desktop span")
            yield y, left, right
            pos += 4
            previous_right = right
            row_open = not bool(encoded_right & 0x8000)
            if not row_open:
                y += 1
                previous_right = 0
        if row_open or y != height:
            raise ProtocolError("Incomplete desktop region")

    def decode(self, data: bytes) -> DesktopFrame | None:
        fields = parse_tlvs(data)
        if set(fields) - {0x10000000, 0x20000000, 0x30000000, 0x40000000,
                          0x50000000, 0x60000000, 0x70000000, 0x80000000, 0xa0000000}:
            raise UnsupportedProtocolError("Unsupported desktop update fields")
        pixel_format, size = self._format, self._size
        cursor_position, cursor_shape = self.cursor_position, self.cursor_shape
        if 0x10000000 in fields:
            fmt = fields[0x10000000]
            if len(fmt) != 16:
                raise ProtocolError("Invalid desktop pixel format length")
            pixel_format = struct.unpack(">4I", fmt)
        if 0x30000000 in fields:
            geometry = fields[0x30000000]
            if len(geometry) != 8:
                raise ProtocolError("Invalid desktop geometry length")
            width, height = struct.unpack(">II", geometry)
            if not width or not height or width * height > MAX_PIXELS:
                raise ProtocolError("Desktop geometry exceeds size limit")
            size = width, height
        if pixel_format is not None and pixel_format != (24, 0xff0000, 0xff00, 0xff):
            raise UnsupportedProtocolError("Unsupported desktop pixel format")
        if 0x20000000 in fields:
            if len(fields[0x20000000]) != 4:
                raise ProtocolError("Invalid desktop flags length")
            if struct.unpack(">I", fields[0x20000000])[0] & ~3:
                raise UnsupportedProtocolError("Unsupported desktop frame flags")
        if 0x70000000 in fields:
            if len(fields[0x70000000]) != 4:
                raise ProtocolError("Invalid cursor position length")
            cursor_position = struct.unpack(">hh", fields[0x70000000])
        if 0x80000000 in fields:
            parse_tlvs(fields[0x80000000])
            cursor_shape = fields[0x80000000]
        # Display-list tag A is metadata; its repeated inner record tags do not
        # affect framebuffer addressing and need not be interpreted here.
        full = fields.get(0x40000000)
        region, replacement = fields.get(0x50000000), fields.get(0x60000000)
        if (region is None) != (replacement is None):
            raise ProtocolError("Desktop delta requires both region and pixel data")
        changed_geometry = size != self._size or pixel_format != self._format
        pixels = None if changed_geometry else self._pixels
        has_image = full is not None or region is not None
        if has_image:
            if size is None or pixel_format is None:
                raise ProtocolError("Image arrived without geometry/pixel format")
            width, height = size
            stride = (width * 3 + 3) & ~3
            if full is not None:
                if region is not None:
                    raise ProtocolError("Ambiguous full image and delta")
                if len(full) != stride * height:
                    raise UnsupportedProtocolError("Unsupported desktop image encoding/extent")
                pixels = bytearray(full)
            else:
                if pixels is None:
                    raise ProtocolError("Desktop delta arrived without a matching base frame")
                # Validate the entire update before mutating retained state.
                required = sum((right - left) * 3 for _, left, right in self._spans(region, width, height))
                if required != len(replacement):
                    raise ProtocolError("Desktop delta pixel length mismatch")
                pixels = pixels.copy()
                offset = 0
                for y, left, right in self._spans(region, width, height):
                    length = (right - left) * 3
                    pixels[y * stride + left * 3:y * stride + right * 3] = replacement[offset:offset + length]
                    offset += length
        self._format, self._size, self._pixels = pixel_format, size, pixels
        self.cursor_position, self.cursor_shape = cursor_position, cursor_shape
        if not has_image:
            return None
        rgb = bytearray(width * height * 3)
        for y in range(height):
            row = pixels[y * stride:y * stride + width * 3]
            offset = y * width * 3
            rgb[offset:offset + width * 3:3] = row[2::3]
            rgb[offset + 1:offset + width * 3:3] = row[1::3]
            rgb[offset + 2:offset + width * 3:3] = row[0::3]
        return DesktopFrame(width, height, bytes(rgb))


class DesktopStream:
    """Persistent desktop stream; view-only by default, explicit control opt-in.

    next_frame(wait_timeout=None) waits through idle periods. A finite wait
    returns None when no complete new frame has started arriving, without
    poisoning cipher state. Once a record starts, the channel's I/O deadline
    still bounds completion. close() cancels by closing the owned channel.
    """

    def __init__(self, channel: EncryptedChannel, mode: str = "view", *, show_cursor: bool = False):
        if mode not in ("view", "control"):
            raise ValueError("Desktop mode must be 'view' or 'control'")
        self.channel = channel
        self.mode = mode
        self.compression = DesktopCompression()
        self.decoder = DesktopDecoder()
        self.show_cursor = show_cursor
        # Single-reader hook for non-image messages (e.g. requested clipboard).
        # Return the remaining TLVs, or b'' when the entire message was consumed.
        self.message_filter = None
        self._started = False
        self._closed = threading.Event()
        self._send_lock = threading.Lock()
        self._io_lock = threading.Lock()

    def start(self):
        if self._started:
            raise ProtocolError("Desktop stream has already started")
        try:
            self.channel.send(b"\x1a" + struct.pack(">I", 1 if self.mode == "control" else 6))
            reply = self.channel.receive()
            if reply == b"\x30":
                self.channel.send(b"\x31")
                reply = self.channel.receive()
                if reply != b"\x31":
                    raise ProtocolError("Remote desktop approval was not granted")
            elif reply != b"\x1a":
                raise ProtocolError("Desktop connection was not accepted")
            self.channel.send(b"\x32")
            reply = self.channel.receive()
            if not reply or reply[0] != 0x32:
                raise ProtocolError("Unexpected desktop negotiation reply")
            parse_tlvs(reply[1:])
            self.channel.send(b"\x28")
            if self.channel.receive() != b"\x28":
                raise ProtocolError("Desktop stream was not accepted")
            request = (encode_tlv(0x40000000, struct.pack(">I", 13 if self.show_cursor else 1))
                       + encode_tlv(0x30000000, struct.pack(">I", 24))
                       + encode_tlv(0x10000000, struct.pack(">4I", 24, 0xff0000, 0xff00, 0xff)))
            self._started = True
            self.send_message(request)
            return self
        except BaseException:
            self.close()
            raise

    def send_message(self, data: bytes):
        """Serialize a desktop TLV message through the shared outgoing streams.

        Input helpers must use this method, never independent DEFLATE/AES state.
        View-only streams reject the two recovered client input tags.
        Empty results from duplicate/released input events are harmless no-ops.
        """
        if not data:
            return
        fields = parse_tlvs(data)
        if self.mode != "control" and set(fields) & {0x60000000, 0x70000000}:
            raise ProtocolError("Input requires control mode")
        with self._send_lock:
            if not self._started or self._closed.is_set():
                raise ProtocolError("Desktop stream has not started or is closed")
            try:
                encoded = self.compression.encode(data)
                with self._io_lock:
                    self.channel.send(encoded)
            except BaseException:
                self.close()
                raise

    def next_frame(self, wait_timeout: float | None = None) -> DesktopFrame | None:
        if not self._started:
            raise ProtocolError("Desktop stream has not started")
        if wait_timeout is not None and (wait_timeout < 0 or not math.isfinite(wait_timeout)):
            raise ValueError("wait_timeout must be nonnegative and finite")
        deadline = None if wait_timeout is None else time.monotonic() + wait_timeout
        try:
            while not self._closed.is_set():
                sock = self.channel._socket
                if deadline is None:
                    interval = 0.25
                else:
                    interval = min(0.25, max(0, deadline - time.monotonic()))
                if not select.select([sock], [], [], interval)[0]:
                    if deadline is not None and time.monotonic() >= deadline:
                        return None
                    continue
                with self._io_lock:
                    if self._closed.is_set():
                        return None
                    message = self.channel.receive()
                decoded = self.compression.decode(message)
                if self.message_filter is not None:
                    decoded = self.message_filter(decoded)
                frame = self.decoder.decode(decoded) if decoded else None
                if frame is not None:
                    return frame
                if deadline is not None and time.monotonic() >= deadline:
                    return None
            return None
        except BaseException as error:
            if self._closed.is_set() and isinstance(error, (OSError, ValueError, TransportError)):
                return None
            self.close()
            if isinstance(error, (OSError, ValueError)):
                raise TransportError("Desktop readiness wait failed") from None
            raise

    def close(self):
        self._closed.set()
        self._started = False
        sock = getattr(self.channel, "_socket", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.channel.close()
