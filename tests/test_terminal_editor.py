"""Direct-pane editing and native mutation-path regression tests."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest

from PySide6.QtCore import Qt, QMimeData, QPoint, QPointF, QTimer
from PySide6.QtGui import QTextCursor, QInputMethodEvent, QKeyEvent, QDropEvent, QContextMenuEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from radmin_viewer.terminal_editor import TerminalEditor, utf16_length


class TerminalEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.editor = TerminalEditor()
        self.editor.active = True
        self.editor.set_remote_text("remote 😀> ")
        self.editor.show()

    def tearDown(self):
        self.editor.close()

    def select(self, start, end):
        cursor = self.editor.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        self.editor.setTextCursor(cursor)

    def test_prefix_keyboard_protection_and_click_then_type(self):
        e = self.editor
        prefix = e.prefix
        self.select(0, e.boundary)
        QTest.keyClick(e, Qt.Key.Key_Backspace)
        self.assertEqual(e.toPlainText(), prefix)
        self.select(1, 1)
        QTest.keyClicks(e, "echo")
        self.assertEqual(e.toPlainText(), prefix + "echo")
        QTest.keyClick(e, Qt.Key.Key_Home)
        QTest.keyClick(e, Qt.Key.Key_Left)
        QTest.keyClick(e, Qt.Key.Key_Backspace)
        self.assertEqual(e.textCursor().position(), e.boundary)
        QTest.keyClick(e, Qt.Key.Key_Delete)
        self.assertEqual(e.text(), "cho")
        e.selectAll()
        QTest.keyClick(e, Qt.Key.Key_Delete)
        self.assertEqual(e.toPlainText(), prefix)
        QTest.keyClick(e, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        e.undo()
        self.assertEqual(e.toPlainText(), prefix)

    def test_unicode_cursor_and_async_selection(self):
        e = self.editor
        e.setText("a😀b")
        self.select(e.boundary + 1, e.boundary + 3)
        e.set_remote_text("remote 😀> output 😀\nnext> ")
        self.assertEqual(e.textCursor().selectedText(), "😀")
        self.assertEqual(e.textCursor().position(), e.boundary + 3)
        self.assertEqual(e.text(), "a😀b")
        QTest.keyClick(e, Qt.Key.Key_Right)
        QTest.keyClick(e, Qt.Key.Key_Backspace)
        self.assertEqual(e.text(), "ab")
        e.set_remote_text("short> ")
        self.assertEqual(e.textCursor().position(), e.boundary + 1)
        self.assertEqual(e.boundary, utf16_length(e.prefix))

    def test_clipboard_context_cut_paste_and_multiline(self):
        e = self.editor
        notices = []
        e.notice.connect(notices.append)
        self.select(0, e.boundary)
        e.cut()
        self.assertEqual(e.toPlainText(), e.prefix)
        mime = QMimeData()
        mime.setText("<b>literal</b>")
        mime.setHtml("<b>formatted</b>")
        self.app.clipboard().setMimeData(mime)
        # Exercise the real menu and its action through its nested event loop.
        def paste_action():
            menu = self.app.activePopupWidget()
            next(a for a in menu.actions() if a.text() == "Paste").trigger()
            menu.close()
        QTimer.singleShot(0, paste_action)
        e.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Reason.Keyboard, QPoint(), QPoint()))
        self.assertEqual(e.text(), "<b>literal</b>")
        e.selectAll()
        e.cut()
        self.assertEqual(e.text(), "<b>literal</b>")
        self.select(e.boundary, e.document().characterCount() - 1)
        e.cut()
        self.assertEqual(e.text(), "")
        self.app.clipboard().setText("echo one\necho two")
        e.paste()
        self.assertEqual(e.text(), "")
        self.assertIn("Paste rejected", notices[-1])

    def test_ime_drop_inactive_and_copy_interrupt(self):
        e = self.editor
        interrupts, submitted = [], []
        e.interrupt_requested.connect(lambda: interrupts.append(True))
        e.returnPressed.connect(lambda: submitted.append(True))
        self.select(0, e.boundary)
        QTest.keyClick(e, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.app.clipboard().text(), e.prefix)
        self.assertEqual(interrupts, [])
        e.deselect()
        QTest.keyClick(e, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(interrupts, [True])
        ime = QInputMethodEvent()
        ime.setCommitString("bad", -100, 100)
        e.inputMethodEvent(ime)
        mime = QMimeData()
        mime.setText("bad")
        e.dropEvent(QDropEvent(QPointF(), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
        self.assertEqual(e.toPlainText(), e.prefix)
        e.active = False
        QTest.keyClicks(e, "bad")
        QTest.keyClick(e, Qt.Key.Key_Return)
        QTest.keyClick(e, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(submitted, [])
        self.assertEqual(interrupts, [True])
        self.assertEqual(e.toPlainText(), e.prefix)
        self.assertTrue(e.isEnabled())

    def test_enter_repeat_history_and_limits(self):
        e = self.editor
        submitted = []
        e.returnPressed.connect(lambda: (submitted.append(e.text()), e.remember(e.text())))
        e.setText("echo one")
        QTest.keyClick(e, Qt.Key.Key_Return)
        e.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier, "\r", True))
        self.assertEqual(submitted, ["echo one"])
        self.assertEqual(e.toPlainText(), e.prefix)
        e.setText("draft")
        QTest.keyClick(e, Qt.Key.Key_Up)
        self.assertEqual(e.text(), "echo one")
        QTest.keyClick(e, Qt.Key.Key_Down)
        self.assertEqual(e.text(), "draft")
        e.setText("x" * 8193)
        self.assertEqual(e.text(), "draft")
        for i in range(110):
            e.remember(str(i))
        self.assertEqual(len(e.history), 100)
