"""Real adapter composition/routing and GUI controls, with deterministic peers."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import struct
import time
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from radmin_viewer.adapter import DesktopAdapter
from radmin_viewer.clipboard import encode_unicode_text
from radmin_viewer.desktop import DesktopDecoder, DesktopFrame, encode_tlv, parse_tlvs
from radmin_viewer.session_ui import SessionWindow


class Stream:
    def __init__(self, channel, mode="view", show_cursor=False):
        self.mode = mode
        self.show_cursor = show_cursor
        self.decoder = DesktopDecoder()
        self.message_filter = None
        self.frame = DesktopFrame(4, 2, bytes((10, 20, 30)) * 8)
        self.messages = []
        self.sent = []
        self.closed = False

    def start(self):
        return self

    def send_message(self, message):
        self.sent.append(message)

    def next_frame(self, wait_timeout):
        for message in self.messages:
            remaining = self.message_filter(message)
            if remaining:
                self.decoder.decode(remaining)
        self.messages.clear()
        frame, self.frame = self.frame, None
        return frame

    def close(self):
        self.closed = True


def white_cursor():
    header = encode_tlv(0x10000000, struct.pack(">6I", 3, 0, 0, 1, 1, 0))
    pixels = encode_tlv(0x20000000, b"\xff" * 4)
    return (encode_tlv(0x10000000, struct.pack(">I", 1))
            + encode_tlv(0x20000000, encode_tlv(0x10000000, header + pixels)))


class AdapterFeatures(unittest.TestCase):
    def make_adapter(self):
        with patch("radmin_viewer.adapter.EncryptedChannel", Mock()), patch("radmin_viewer.adapter.DesktopStream", Stream):
            return DesktopAdapter(Mock(), "control")

    def test_cursor_only_movement_toggle_and_clean_frame(self):
        adapter = self.make_adapter()
        self.assertTrue(adapter.stream.show_cursor)
        adapter.poll(0)
        adapter.stream.decoder.cursor_shape = white_cursor()
        adapter.stream.decoder.cursor_position = (1, 0)
        image = adapter.poll(0)
        self.assertEqual(image.pixelColor(1, 0).getRgb()[:3], (255, 255, 255))
        adapter.stream.decoder.cursor_position = (2, 1)
        image = adapter.poll(0)
        self.assertEqual(image.pixelColor(1, 0).getRgb()[:3], (10, 20, 30))
        self.assertEqual(image.pixelColor(2, 1).getRgb()[:3], (255, 255, 255))
        adapter.set_show_cursor(False)
        image = adapter.poll(0)
        self.assertEqual(image.pixelColor(2, 1).getRgb()[:3], (10, 20, 30))
        adapter.set_show_cursor(True)
        self.assertEqual(adapter.poll(0).pixelColor(2, 1).getRgb()[:3], (255, 255, 255))
        self.assertIsNone(adapter.poll(0))
        adapter.close()

    def test_clipboard_only_reply_without_frame(self):
        adapter = self.make_adapter()
        adapter.poll(0)
        adapter.handle_input({"type": "clipboard_send", "text": "hello 世界"})
        self.assertIn(0x60000000, parse_tlvs(adapter.stream.sent[-1]))
        adapter.handle_input({"type": "clipboard_receive", "request_id": 42})
        adapter.stream.messages.append(encode_tlv(0x90000000, encode_unicode_text("received 😀")))
        self.assertIsNone(adapter.poll(0))
        self.assertEqual(adapter.take_clipboard_replies(), [(42, "received 😀")])
        self.assertEqual(adapter.take_clipboard_replies(), [])
        # Unsolicited replies are consumed but never overwrite the OS clipboard.
        adapter.stream.messages.append(encode_tlv(0x90000000, encode_unicode_text("unsolicited")))
        adapter.poll(0)
        self.assertEqual(adapter.take_clipboard_replies(), [])
        adapter.close()


class SessionFeatures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def wait(self, predicate):
        end = time.monotonic() + 3
        while not predicate() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(.005)
        self.assertTrue(predicate())

    def test_buttons_signal_delivery_and_view_only_toggle(self):
        from PySide6.QtGui import QImage
        class Peer:
            authenticated = True
            def __init__(self, *args): pass
            def connect(self): pass
            def close(self): pass
        events = []
        class Adapter:
            can_control = True
            replies = []
            def poll(self, timeout):
                time.sleep(timeout)
                image = QImage(4, 2, QImage.Format.Format_RGB888)
                image.fill(Qt.GlobalColor.blue)
                return image
            def set_show_cursor(self, enabled): events.append(("cursor", enabled))
            def handle_input(self, event):
                events.append(event)
                if event["type"] == "clipboard_receive":
                    self.replies.append((event["request_id"], "from VM"))
            def take_clipboard_replies(self):
                result, self.replies = self.replies, []
                return result
            def close(self): pass
        entry = {"name": "Test", "host": "localhost", "port": 4899, "username": ""}
        settings = {"timeout": 1, "scaling": "fit", "fullscreen": False}
        with patch("radmin_viewer.session_ui.RadminSession", Peer):
            window = SessionWindow(entry, "", settings, "control", lambda s, m: Adapter())
            try:
                self.wait(lambda: window.send_clipboard_action.isEnabled())
                self.app.clipboard().setText("local test")
                window.send_clipboard_action.trigger()
                self.wait(lambda: {"type": "clipboard_send", "text": "local test"} in events)
                window.receive_clipboard_action.trigger()
                self.wait(lambda: self.app.clipboard().text() == "from VM")
                window.cursor_action.setChecked(False)
                self.wait(lambda: ("cursor", False) in events)
                self.assertEqual(window.surface.cursor().shape(), Qt.CursorShape.ArrowCursor)
            finally:
                window.close()
                self.wait(lambda: not window.worker.isRunning())
                self.app.processEvents()
            events.clear()
            window = SessionWindow(entry, "", settings, "view", lambda s, m: Adapter())
            try:
                self.wait(lambda: not window.surface.image.isNull())
                self.assertFalse(window.send_clipboard_action.isEnabled())
                window.cursor_action.setChecked(False)
                self.wait(lambda: ("cursor", False) in events)
            finally:
                window.close()
                self.wait(lambda: not window.worker.isRunning())
                self.app.processEvents()
