"""Native cursor TLVs and Qt-independent, destination-aware RGB composition.

See docs/protocol-notes.md for the public compatibility and evidence scope.
RGBA is straight alpha. AND/XOR cannot in general be represented by RGBA.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import struct

from .protocol import ProtocolError, UnsupportedProtocolError

CURSOR_SHAPE_FLAG = 0x04
CURSOR_POSITION_FLAG = 0x08
DESKTOP_CURSOR_FLAGS = 0x01 | CURSOR_SHAPE_FLAG | CURSOR_POSITION_FLAG
MAX_CURSOR_PIXELS = 1024 * 1024
MAX_CURSOR_BYTES = 8 * 1024 * 1024
MAX_CURSOR_FRAMES = 256
_HEADER = 0x10000000
_DATA = 0x20000000


@dataclass(frozen=True)
class CursorShape:
    """Immutable cursor definition, or unresolved cache selection.

    All planes are top-down, tightly packed. AND/XOR masks contain one byte
    (0 or 255) per pixel; rgb contains color XOR operands, three bytes/pixel.
    rgba contains straight RGBA, four bytes/pixel. Empty planes do not apply.
    kind: 1 monochrome, 2 color XOR, 3 alpha, 0 selection/hidden.
    frames holds subsequent animation frames; this object is the first frame.
    A reference must go through CursorCache before painting. A resolved missing
    cache selection has zero dimensions and visible=False, matching the viewer.
    """

    cache_id: int
    width: int = 0
    height: int = 0
    hotspot: tuple[int, int] = (0, 0)
    kind: int = 0
    and_mask: bytes = b""
    xor_mask: bytes = b""
    rgb: bytes = b""
    rgba: bytes = b""
    delay_ms: int = 0
    frames: tuple[CursorShape, ...] = ()
    is_reference: bool = False

    def __post_init__(self):
        # Keep direct construction just as immutable and safe as decoding.
        object.__setattr__(self, "hotspot", tuple(self.hotspot))
        object.__setattr__(self, "frames", tuple(self.frames))
        for name in ("and_mask", "xor_mask", "rgb", "rgba"):
            object.__setattr__(self, name, bytes(getattr(self, name)))
        if not 0 <= self.cache_id <= 0xffffffff or not 0 <= self.delay_ms <= 0xffffffff:
            raise ValueError("Invalid cursor ID or frame delay")
        n = self.width * self.height
        if self.kind == 0:
            if (self.width or self.height or self.hotspot != (0, 0) or self.frames
                    or self.and_mask or self.xor_mask or self.rgb or self.rgba):
                raise ValueError("Invalid cursor selection")
            return
        if (self.kind not in (1, 2, 3) or self.is_reference
                or not (0 < self.width <= 4096 and 0 < self.height <= 4096)
                or n > MAX_CURSOR_PIXELS or len(self.hotspot) != 2
                or not (0 <= self.hotspot[0] < self.width and 0 <= self.hotspot[1] < self.height)):
            raise ValueError("Invalid cursor geometry or kind")
        sizes = {1: (n, n, 0, 0), 2: (n, 0, n * 3, 0), 3: (0, 0, 0, n * 4)}
        if tuple(map(len, (self.and_mask, self.xor_mask, self.rgb, self.rgba))) != sizes[self.kind]:
            raise ValueError("Invalid cursor plane lengths")
        if any(v not in (0, 255) for v in self.and_mask + self.xor_mask):
            raise ValueError("Invalid cursor mask value")
        if len(self.frames) >= MAX_CURSOR_FRAMES or any(
                not isinstance(f, CursorShape) or not f.visible or f.frames
                or f.cache_id != self.cache_id for f in self.frames):
            raise ValueError("Invalid cursor animation frames")

    @property
    def visible(self) -> bool:
        return self.kind != 0

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height

    def frame_at(self, elapsed_ms: int) -> CursorShape:
        """Select a looping animation frame. Zero delays use Windows' 10ms floor."""
        if elapsed_ms < 0:
            raise ValueError("Negative cursor animation time")
        if not self.frames:
            return self
        frames = (self,) + self.frames
        durations = tuple(max(10, f.delay_ms) for f in frames)
        elapsed_ms %= sum(durations)
        for frame, duration in zip(frames, durations):
            if elapsed_ms < duration:
                return frame
            elapsed_ms -= duration
        raise AssertionError("Unreachable cursor animation frame")


def _tlvs(data: bytes, *, repeated: bool = False) -> list[tuple[int, bytes]]:
    fields = []
    seen = set()
    pos = 0
    while pos < len(data):
        if len(data) - pos < 4:
            raise ProtocolError("Truncated cursor TLV header")
        header = struct.unpack_from(">I", data, pos)[0]
        tag, length = header & 0xf8000000, header & 0x7ffffff
        pos += 4
        if not tag or not length or length > len(data) - pos:
            raise ProtocolError("Invalid cursor TLV length or tag")
        if tag in seen and not repeated:
            raise ProtocolError("Duplicate cursor TLV")
        seen.add(tag)
        fields.append((tag, data[pos:pos + length]))
        pos += length
        if repeated and len(fields) > MAX_CURSOR_FRAMES:
            raise ProtocolError("Too many cursor frames")
    return fields


def _mask(data: bytes, width: int, height: int) -> bytes:
    stride = ((width + 31) // 32) * 4
    return bytes(255 if data[y * stride + x // 8] & (0x80 >> (x & 7)) else 0
                 for y in range(height) for x in range(width))


def _decode_frame(data: bytes, cache_id: int) -> CursorShape:
    fields = dict(_tlvs(data))
    if set(fields) - {_HEADER, _DATA}:
        raise UnsupportedProtocolError("Unsupported cursor frame fields")
    if set(fields) != {_HEADER, _DATA} or len(fields[_HEADER]) != 24:
        raise ProtocolError("Cursor frame requires 24-byte header and bitmap")
    kind, hx, hy, width, height, delay = struct.unpack(">6I", fields[_HEADER])
    if kind not in (1, 2, 3):
        raise UnsupportedProtocolError("Unsupported cursor bitmap kind")
    if not (0 < width <= 4096 and 0 < height <= 4096 and width * height <= MAX_CURSOR_PIXELS
            and hx < width and hy < height):
        raise ProtocolError("Invalid cursor geometry or hotspot")
    mask_size = ((width + 31) // 32) * 4 * height
    color_stride = (width * 3 + 3) & ~3
    expected = {1: mask_size * 2, 2: mask_size + color_stride * height,
                3: width * height * 4}[kind]
    pixels = fields[_DATA]
    if len(pixels) != expected:
        raise ProtocolError("Cursor bitmap extent mismatch")
    and_mask = xor_mask = rgb = rgba = b""
    if kind in (1, 2):
        and_mask = _mask(pixels[:mask_size], width, height)
        if kind == 1:
            xor_mask = _mask(pixels[mask_size:], width, height)
        else:
            rgb = bytes(pixels[mask_size + y * color_stride + x * 3 + c]
                        for y in range(height) for x in range(width) for c in (2, 1, 0))
    else:
        rgba = bytes(pixels[i + c] for i in range(0, len(pixels), 4) for c in (2, 1, 0, 3))
    return CursorShape(cache_id, width, height, (hx, hy), kind, and_mask,
                       xor_mask, rgb, rgba, delay)


def decode_cursor(data: bytes) -> CursorShape:
    """Decode the value of desktop tag 80; never silently approximate a format.

    Definitions include bitmap data. ID-only messages return is_reference=True;
    use a per-session CursorCache.decode() to resolve them before UI emission.
    """
    if not data or len(data) > MAX_CURSOR_BYTES:
        raise ProtocolError("Invalid cursor message size")
    fields = dict(_tlvs(bytes(data)))
    if set(fields) - {_HEADER, _DATA}:
        raise UnsupportedProtocolError("Unsupported cursor fields")
    ident = fields.get(_HEADER, b"\0\0\0\0")
    if len(ident) != 4:
        raise ProtocolError("Invalid cursor cache ID length")
    cache_id = struct.unpack(">I", ident)[0]
    if _DATA not in fields:
        return CursorShape(cache_id, is_reference=True)
    records = _tlvs(fields[_DATA], repeated=True)
    if not records or any(tag != _HEADER for tag, _ in records):
        raise ProtocolError("Invalid cursor frame list")
    frames = []
    pixels = 0
    for _, value in records:
        frame = _decode_frame(value, cache_id)
        pixels += frame.width * frame.height
        if pixels > MAX_CURSOR_PIXELS:
            raise ProtocolError("Cursor animation exceeds pixel budget")
        frames.append(frame)
    return replace(frames[0], frames=tuple(frames[1:]))


class CursorCache:
    """Per-connection cursor definitions. Missing references hide the cursor.

    Discard/reset on reconnect. Resource limits fail explicitly rather than
    silently evicting definitions which the server can still reference.
    """

    def __init__(self):
        self._shapes: dict[int, CursorShape] = {}
        self._bytes = 0

    def clear(self):
        self._shapes.clear()
        self._bytes = 0

    @staticmethod
    def _size(shape: CursorShape) -> int:
        return sum(len(f.and_mask) + len(f.xor_mask) + len(f.rgb) + len(f.rgba)
                   for f in (shape,) + shape.frames)

    def decode(self, data: bytes) -> CursorShape:
        shape = decode_cursor(data)
        if shape.is_reference:
            return self._shapes.get(shape.cache_id, CursorShape(shape.cache_id))
        old = self._shapes.get(shape.cache_id)
        size = self._bytes + self._size(shape) - (self._size(old) if old else 0)
        if size > 32 * 1024 * 1024 or (old is None and len(self._shapes) >= 256):
            raise ProtocolError("Cursor cache exceeds resource budget")
        self._shapes[shape.cache_id] = shape
        self._bytes = size
        return shape


def composite_cursor(rgb: bytes, width: int, height: int, shape: CursorShape | None,
                     position: tuple[int, int] | None, *, elapsed_ms: int = 0) -> bytes:
    """Return a clipped RGB overlay at the remote hotspot, without mutation.

    Monochrome/color: (destination AND mask) XOR operand, including inversion.
    Alpha: floor((source * alpha + destination * (255-alpha)) / 255), matching
    the original viewer's software compositor, with straight-alpha RGBA.
    """
    if width <= 0 or height <= 0 or len(rgb) != width * height * 3:
        raise ValueError("Invalid RGB framebuffer extent")
    if shape is not None and shape.is_reference:
        raise ProtocolError("Cursor cache reference must be resolved before painting")
    if shape is None or not shape.visible or position is None:
        return bytes(rgb)
    shape = shape.frame_at(elapsed_ms)
    left, top = position[0] - shape.hotspot[0], position[1] - shape.hotspot[1]
    x0, y0 = max(0, left), max(0, top)
    x1, y1 = min(width, left + shape.width), min(height, top + shape.height)
    if x0 >= x1 or y0 >= y1:
        return bytes(rgb)
    out = bytearray(rgb)
    for y in range(y0, y1):
        for x in range(x0, x1):
            p = (y - top) * shape.width + x - left
            dest = (y * width + x) * 3
            for c in range(3):
                if shape.kind == 3:
                    a = shape.rgba[p * 4 + 3]
                    out[dest + c] = (shape.rgba[p * 4 + c] * a + out[dest + c] * (255 - a)) // 255
                else:
                    operand = shape.xor_mask[p] if shape.kind == 1 else shape.rgb[p * 3 + c]
                    out[dest + c] = (out[dest + c] & shape.and_mask[p]) ^ operand
    return bytes(out)
