"""Single-worker Qt bridge to the recovered desktop and input protocols."""
from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from .channel import EncryptedChannel
from .desktop import DesktopStream
from .input import InputEncoder
from .cursor import CursorCache, composite_cursor
from .clipboard_session import ClipboardSession


def windows_key(key: int, modifiers: int = 0) -> tuple[int, bool] | None:
    """Translate Qt logical keys; native scan codes are OS-specific and unused."""
    key = int(key)
    keypad = bool(modifiers & Qt.KeyboardModifier.KeypadModifier.value)
    if keypad:
        operators = {Qt.Key.Key_Plus: 107, Qt.Key.Key_Minus: 109,
                     Qt.Key.Key_Asterisk: 106, Qt.Key.Key_Slash: 111,
                     Qt.Key.Key_Period: 110}
        if key in operators:
            return operators[key], key == Qt.Key.Key_Slash
    if 0x30 <= key <= 0x39:
        return (0x60 + key - 0x30 if keypad else key), False
    if 0x41 <= key <= 0x5a:
        return key, False
    if Qt.Key.Key_F1 <= key <= Qt.Key.Key_F24:
        return 0x70 + key - Qt.Key.Key_F1, False
    names = {
        "Backspace": (8, False), "Tab": (9, False), "Backtab": (9, False),
        "Return": (13, False), "Enter": (13, True), "Shift": (16, False),
        "Control": (17, False), "Alt": (18, False), "AltGr": (165, True),
        "Pause": (19, False), "CapsLock": (20, False), "Escape": (27, False),
        "Space": (32, False), "PageUp": (33, True), "PageDown": (34, True),
        "End": (35, True), "Home": (36, True), "Left": (37, True),
        "Up": (38, True), "Right": (39, True), "Down": (40, True),
        "Print": (44, True), "Insert": (45, True), "Delete": (46, True),
        "Meta": (91, True), "Menu": (93, True), "NumLock": (144, True),
        "ScrollLock": (145, False), "Semicolon": (186, False),
        "Colon": (186, False), "Equal": (187, False), "Plus": (187, False),
        "Comma": (188, False), "Less": (188, False), "Minus": (189, False),
        "Underscore": (189, False), "Period": (190, False), "Greater": (190, False),
        "Slash": (191, False), "Question": (191, False), "QuoteLeft": (192, False),
        "AsciiTilde": (192, False), "BracketLeft": (219, False), "BraceLeft": (219, False),
        "Backslash": (220, False), "Bar": (220, False), "BracketRight": (221, False),
        "BraceRight": (221, False), "Apostrophe": (222, False), "QuoteDbl": (222, False),
    }
    shifted = {ord(c): (ord(n), False) for c, n in zip("!@#$%^&*()", "1234567890")}
    if key in shifted:
        return shifted[key]
    for name, value in names.items():
        if key == getattr(Qt.Key, "Key_" + name):
            if keypad and name in ("Plus", "Minus", "Asterisk", "Slash", "Period"):
                return {"Plus": 107, "Minus": 109, "Asterisk": 106, "Slash": 111, "Period": 110}[name], name == "Slash"
            return value
    return None


class DesktopAdapter:
    """One instance per authenticated session, exclusively called by its worker."""

    def __init__(self, session, mode):
        self.can_control = mode == "control"
        self.inputs = None
        self._held = set()
        self._closed = False
        self.show_cursor = True
        self._cursor_cache = CursorCache()
        self._cursor_payload = None
        self._cursor_shape = None
        self._cursor_position = None
        self._cursor_started = time.monotonic()
        self._painted_animation = None
        self._frame = None
        self._dirty = False
        self._clipboard_request = None
        self._clipboard_replies = []
        self.stream = DesktopStream(EncryptedChannel(session).start(), mode=mode, show_cursor=True).start()
        self.clipboard = ClipboardSession(self.stream)
        self.stream.message_filter = self._filter_message

    def _filter_message(self, message):
        remaining, update = self.clipboard.consume_message(message)
        if update is not None and self._clipboard_request is not None:
            self._clipboard_replies.append((self._clipboard_request, update.text))
            self._clipboard_request = None
        return remaining

    def take_clipboard_replies(self):
        replies, self._clipboard_replies = self._clipboard_replies, []
        return replies

    def poll(self, timeout):
        frame = self.stream.next_frame(wait_timeout=timeout)
        if frame is not None:
            self._frame = frame
            self._dirty = True
            if self.inputs is None:
                self.inputs = InputEncoder(frame.width, frame.height)
            else:
                self.inputs.resize(frame.width, frame.height)
        decoder = self.stream.decoder
        if decoder.cursor_shape != self._cursor_payload:
            self._cursor_payload = decoder.cursor_shape
            self._cursor_shape = self._cursor_cache.decode(decoder.cursor_shape) if decoder.cursor_shape else None
            self._cursor_started = time.monotonic()
            self._dirty = True
        if decoder.cursor_position != self._cursor_position:
            self._cursor_position = decoder.cursor_position
            self._dirty = True
        elapsed = int((time.monotonic() - self._cursor_started) * 1000)
        animated = self._cursor_shape.frame_at(elapsed) if self.show_cursor and self._cursor_shape else None
        if animated is not self._painted_animation:
            self._painted_animation = animated
            self._dirty = True
        if not self._dirty or self._frame is None:
            return None
        frame = self._frame
        pixels = composite_cursor(frame.rgb, frame.width, frame.height,
                                  self._cursor_shape, self._cursor_position, elapsed_ms=elapsed) if self.show_cursor else frame.rgb
        self._dirty = False
        return QImage(pixels, frame.width, frame.height, frame.width * 3,
                      QImage.Format.Format_RGB888).copy()

    def set_show_cursor(self, enabled):
        self.show_cursor = bool(enabled)
        self._dirty = True

    def handle_input(self, event):
        if not self.can_control or self._closed:
            return
        kind = event["type"]
        if kind == "clipboard_send":
            self.clipboard.send_text(event["text"])
            return
        if kind == "clipboard_receive":
            if self._clipboard_request is None:
                self._clipboard_request = event["request_id"]
                self.clipboard.request_receive()
            return
        if self.inputs is None:
            return
        data = b""
        if kind == "release_all":
            data = self.inputs.release_all()
            self._held.clear()
        elif kind == "pointer_move":
            data = self.inputs.move(event["x"], event["y"])
        elif kind in ("pointer_press", "pointer_release"):
            names = {Qt.MouseButton.LeftButton.value: "left", Qt.MouseButton.RightButton.value: "right",
                     Qt.MouseButton.MiddleButton.value: "middle", Qt.MouseButton.BackButton.value: "x1",
                     Qt.MouseButton.ForwardButton.value: "x2"}
            name = names.get(event["button"])
            if name:
                data = self.inputs.button(name, kind == "pointer_press", event["x"], event["y"])
        elif kind == "wheel":
            data = self.inputs.wheel(max(-32768, min(32767, event["dy"])), event["x"], event["y"])
        elif kind in ("key_press", "key_release"):
            # Qt's auto-repeat key releases must not release the remote held key.
            if kind == "key_release" and event.get("auto_repeat"):
                return
            translated = windows_key(event["key"], event.get("modifiers", 0))
            if translated is None:
                return
            vk, extended = translated
            pressed = kind == "key_press"
            repeat = pressed and translated in self._held
            data = self.inputs.key(vk, pressed, extended=extended, repeat=repeat,
                                   alt=bool(event.get("modifiers", 0) & Qt.KeyboardModifier.AltModifier.value))
            if pressed:
                self._held.add(translated)
            else:
                self._held.discard(translated)
        self.stream.send_message(data)

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self.can_control and self.inputs:
                self.stream.send_message(self.inputs.release_all())
        finally:
            if hasattr(self, "clipboard"):
                self.clipboard.close()
            self.stream.close()
