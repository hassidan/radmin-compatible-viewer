"""Qt-independent Radmin control input. See docs/protocol-notes.md for scope.

Each nonempty return value is ONE uncompressed desktop TLV. Send it through
the existing DesktopStream compression and channel, in order. One encoder per
control connection; callers must send release_all() before focus loss/teardown.
State describes encoded events, not acknowledged remote delivery.
"""
from __future__ import annotations

import struct

from .desktop import encode_tlv

INPUT_TAG = 0x70000000
_BUTTONS = {"left": (8, 7), "right": (6, 5), "middle": (10, 9),
            "x1": (17, 16), "x2": (17, 16)}


def _integer(value: int, low: int, high: int, name: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in [{low}, {high}]")
    return value


def _boolean(value: bool, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} must be bool")
    return value


def _field(events: bytes) -> bytes:
    return encode_tlv(INPUT_TAG, events) if events else b""


class InputEncoder:
    """Encode semantic events for mode 1 (never enable in view-only mode).

    Coordinates are remote image pixels, after the UI removes scaling and
    letterboxing. Out-of-image positions are clamped for captured drags.
    Keys are Windows virtual-key codes, not Qt keys, Unicode, or X11 keycodes.
    scancode is an optional Windows set-1 scan byte; extended is the E0 flag.
    Native menu shortcuts establish that a zero scan byte is supported.
    """

    def __init__(self, width: int, height: int):
        self._keys: dict[tuple[int, bool], tuple[int, bool]] = {}
        self._buttons: dict[str, None] = {}
        self._position = (0, 0)
        self.resize(width, height)

    def resize(self, width: int, height: int) -> None:
        width = _integer(width, 1, 32768, "width")
        height = _integer(height, 1, 32768, "height")
        self.width, self.height = width, height

    def _point(self, x: int, y: int) -> bytes:
        _integer(x, -2147483648, 2147483647, "x")
        _integer(y, -2147483648, 2147483647, "y")
        # Viewer 0x147c7f2: version family 0x02000000 uses pixel coordinates.
        # The normalized 0..65535 branch belongs to older servers.
        nx = max(0, min(self.width - 1, x))
        ny = max(0, min(self.height - 1, y))
        return struct.pack("<HH", nx, ny)

    def move(self, x: int, y: int) -> bytes:
        point = self._point(x, y)
        self._position = struct.unpack("<HH", point)
        return _field(b"\x0b" + point)

    def button(self, button: str, pressed: bool, x: int, y: int) -> bytes:
        if button not in _BUTTONS:
            raise ValueError("Unknown mouse button")
        _boolean(pressed, "pressed")
        point = self._point(x, y)
        if pressed == (button in self._buttons):
            return b""
        self._position = struct.unpack("<HH", point)
        code = _BUTTONS[button][0 if pressed else 1]
        extra = struct.pack("<H", 1 if button == "x1" else 2) if button in ("x1", "x2") else b""
        if pressed:
            self._buttons[button] = None
        else:
            del self._buttons[button]
        # Native emits a move before every button/wheel event.
        return _field(b"\x0b" + point + bytes([code]) + extra + point)

    def wheel(self, delta: int, x: int, y: int) -> bytes:
        _integer(delta, -32768, 32767, "delta")
        point = self._point(x, y)
        if delta == 0:
            return b""
        self._position = struct.unpack("<HH", point)
        return _field(b"\x0b" + point + b"\x0f" + struct.pack("<h", delta) + point)

    def key(self, vk: int, pressed: bool, *, scancode: int = 0,
            extended: bool = False, system: bool = False,
            alt: bool = False, repeat: bool = False) -> bytes:
        _integer(vk, 1, 255, "vk")
        _integer(scancode, 0, 255, "scancode")
        for name, value in (("pressed", pressed), ("extended", extended),
                            ("system", system), ("alt", alt), ("repeat", repeat)):
            _boolean(value, name)
        identity = vk, extended
        held = identity in self._keys
        if not pressed and not held:
            return b""
        if pressed and held and not repeat:
            return b""
        if repeat and (not pressed or not held):
            raise ValueError("Repeat requires a held key-down")
        if held:
            scancode, system = self._keys[identity]
        flags = 1 | (scancode << 16) | (int(extended) << 24) | (int(alt) << 29)
        if held:
            flags |= 1 << 30
        if not pressed:
            flags |= 1 << 31
        code = (0 if pressed else 2) + int(system)
        result = _field(struct.pack("<BBI", code, vk, flags))
        if pressed:
            self._keys[identity] = scancode, system
        else:
            del self._keys[identity]
        return result

    def release_all(self) -> bytes:
        """Native focus-loss releases; idempotent. Send before closing transport.

        Uses native key type 2 with only the extended flag (Viewer 0x147e340).
        Button releases retain the last pointer position: the native routine's
        zero coordinates can jump the modern server pointer to (0, 0).
        """
        events = bytearray()
        for vk, extended in sorted(self._keys):
            events.extend(struct.pack("<BBI", 2, vk, int(extended) << 24))
        for name in ("right", "left", "middle", "x1", "x2"):
            if name in self._buttons:
                events.append(_BUTTONS[name][1])
                if name in ("x1", "x2"):
                    events.extend(struct.pack("<H", 1 if name == "x1" else 2))
                events.extend(struct.pack("<HH", *self._position))
        self._keys.clear()
        self._buttons.clear()
        return _field(bytes(events))
