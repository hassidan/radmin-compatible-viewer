"""Protected, line-oriented command editing; deliberately not a VT/PTY emulator."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QTextCursor
from PySide6.QtWidgets import QApplication, QPlainTextEdit
from .ui_dialogs import RoundedMenu as QMenu


def utf16_length(text):
    return len(text.encode("utf-16-le")) // 2


class TerminalEditor(QPlainTextEdit):
    returnPressed = Signal()
    interrupt_requested = Signal()
    notice = Signal(str)
    INPUT_LIMIT = 8192

    def __init__(self, parent=None):
        super().__init__(parent)
        # Native mutation paths (undo, drops, IME, middle-click paste) cannot
        # change the document. All accepted edits go through _replace().
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setAcceptDrops(False)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, False)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse |
                                     Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setTabChangesFocus(True)
        self.setObjectName("TerminalOutput")
        self.setAccessibleName("Remote terminal, command input and output")
        self.setAccessibleDescription("Type a command and press Enter. Up and Down recall history. Control C copies a selection or interrupts the remote command.")
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.active = False
        self.prefix = ""
        self.boundary = 0
        self.history = []
        self.position = 0
        self.draft = ""

    def text(self):
        return self.toPlainText()[len(self.prefix):]

    def setText(self, text):
        cursor = self.textCursor()
        cursor.setPosition(self.boundary)
        cursor.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
        self._replace(text, cursor)

    def deselect(self):
        cursor = self.textCursor()
        cursor.clearSelection()
        self.setTextCursor(cursor)

    def set_remote_text(self, text):
        text = text.replace("\u2028", "\n").replace("\u2029", "\n")
        cursor = self.textCursor()
        old_boundary = self.boundary
        anchor, position = cursor.anchor(), cursor.position()
        pending = self.text()
        scroll = self.verticalScrollBar()
        value, bottom = scroll.value(), scroll.value() == scroll.maximum()
        self.prefix = text
        self.boundary = utf16_length(text)
        super().setPlainText(text + pending)
        def mapped(pos):
            return self.boundary + pos - old_boundary if pos >= old_boundary else min(pos, self.boundary)
        cursor = self.textCursor()
        cursor.setPosition(mapped(anchor))
        cursor.setPosition(mapped(position), QTextCursor.MoveMode.KeepAnchor)
        self.setTextCursor(cursor)
        scroll.setValue(scroll.maximum() if bottom else value)

    def _replace(self, text, cursor=None):
        if not self.active:
            return
        if any(ch in text for ch in "\r\n\x00\u2028\u2029"):
            self.notice.emit("Paste rejected: enter one command at a time; multiline input is not executed.")
            return
        cursor = cursor or self.textCursor()
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        if end < self.boundary or (not cursor.hasSelection() and start < self.boundary):
            start = end = self.document().characterCount() - 1
        else:
            start = max(start, self.boundary)
        if utf16_length(self.text()) - (end - start) + utf16_length(text) > self.INPUT_LIMIT:
            self.notice.emit("Command is too long (8192 UTF-16 units maximum).")
            return
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(text)
        self.setTextCursor(cursor)
        self.ensureCursorVisible()

    def remember(self, command):
        if command and (not self.history or self.history[-1] != command):
            self.history.append(command)
            del self.history[:-100]
        self.position = len(self.history)
        self.draft = ""
        # Only remote echo becomes transcript, avoiding duplicate local echo.
        self.setText("")

    def paste(self):
        self._replace(QApplication.clipboard().text())

    def insertFromMimeData(self, source):
        self._replace(source.text())

    def cut(self):
        cursor = self.textCursor()
        if self.active and cursor.hasSelection() and cursor.selectionStart() >= self.boundary:
            self.copy()
            self._replace("")

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.addAction("Copy", self.copy).setEnabled(self.textCursor().hasSelection())
        cursor = self.textCursor()
        menu.addAction("Cut", self.cut).setEnabled(self.active and cursor.hasSelection() and cursor.selectionStart() >= self.boundary)
        menu.addAction("Paste", self.paste).setEnabled(self.active)
        menu.addAction("Select all", self.selectAll)
        try:
            menu.exec(event.globalPos())
        finally:
            menu.deleteLater()

    def inputMethodEvent(self, event):
        # Preedit/replacement offsets must never reach the protected prefix.
        event.ignore()

    def dragEnterEvent(self, event):
        event.ignore()

    def dropEvent(self, event):
        event.ignore()

    def keyPressEvent(self, event):
        key = event.key()
        if event.matches(QKeySequence.StandardKey.Copy):
            if self.textCursor().hasSelection():
                self.copy()
            elif self.active:
                self.interrupt_requested.emit()
        elif event.matches(QKeySequence.StandardKey.Paste):
            self.paste()
        elif event.matches(QKeySequence.StandardKey.Cut):
            self.cut()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.active and not event.isAutoRepeat():
                self.returnPressed.emit()
        elif self.active and key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            if self.position == len(self.history):
                self.draft = self.text()
            self.position = max(0, min(len(self.history), self.position + (-1 if key == Qt.Key.Key_Up else 1)))
            self.setText(self.history[self.position] if self.position < len(self.history) else self.draft)
        elif self.active and key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            cursor = self.textCursor()
            if not cursor.hasSelection():
                if cursor.position() < self.boundary or (key == Qt.Key.Key_Backspace and cursor.position() == self.boundary):
                    return
                cursor.movePosition(QTextCursor.MoveOperation.PreviousCharacter if key == Qt.Key.Key_Backspace else QTextCursor.MoveOperation.NextCharacter, QTextCursor.MoveMode.KeepAnchor)
            self._replace("", cursor)
        elif self.active and key in (Qt.Key.Key_Home, Qt.Key.Key_End):
            cursor = self.textCursor()
            cursor.setPosition(self.boundary if key == Qt.Key.Key_Home else self.document().characterCount() - 1,
                               QTextCursor.MoveMode.KeepAnchor if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else QTextCursor.MoveMode.MoveAnchor)
            self.setTextCursor(cursor)
        elif (self.active and key == Qt.Key.Key_Left and
              not event.modifiers() and not self.textCursor().hasSelection() and
              self.textCursor().position() == self.boundary):
            pass
        elif self.active and event.text() and not (event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier)) and all(ord(ch) >= 32 for ch in event.text()):
            self._replace(event.text())
        else:
            super().keyPressEvent(event)
        event.accept()
