import unittest
from unittest.mock import Mock

from PySide6.QtCore import Qt
from radmin_viewer.adapter import DesktopAdapter, windows_key
from radmin_viewer.input import InputEncoder


class AdapterTests(unittest.TestCase):
    def test_logical_key_translation(self):
        self.assertEqual(windows_key(Qt.Key.Key_A), (65, False))
        self.assertEqual(windows_key(Qt.Key.Key_Left), (37, True))
        self.assertEqual(windows_key(Qt.Key.Key_F12), (123, False))
        self.assertEqual(windows_key(Qt.Key.Key_Asterisk, Qt.KeyboardModifier.KeypadModifier.value), (106, False))
        self.assertIsNone(windows_key(0x01000000 + 9999))

    def test_repeat_and_focus_release(self):
        adapter = DesktopAdapter.__new__(DesktopAdapter)
        adapter.can_control = True
        adapter.inputs = InputEncoder(1024, 768)
        adapter._held = set()
        adapter._closed = False
        adapter.stream = Mock()
        event = {"type": "key_press", "key": Qt.Key.Key_A, "modifiers": 0}
        adapter.handle_input(event)
        adapter.handle_input(dict(event, type="key_release", auto_repeat=True))
        self.assertEqual(adapter.stream.send_message.call_count, 1)
        adapter.handle_input(dict(event, auto_repeat=True))
        self.assertEqual(adapter.stream.send_message.call_count, 2)
        adapter.handle_input({"type": "release_all"})
        self.assertFalse(adapter._held)
        adapter.close()
        adapter.stream.close.assert_called_once()

    def test_view_does_not_send_input(self):
        adapter = DesktopAdapter.__new__(DesktopAdapter)
        adapter.can_control = False
        adapter.stream = Mock()
        adapter.handle_input({"type": "pointer_move", "x": 10, "y": 20})
        adapter.stream.send_message.assert_not_called()
