"""Rounded, model-backed combo popups and reusable synchronous dialogs.

Only accepting a popup commits its highlighted index. The ordinary QComboBox
model, roles, closed-control painting and signal blocking remain authoritative.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QListView, QPushButton, QToolButton, QVBoxLayout, QWidget, QMenu,
    QBoxLayout, QLayout, QScrollArea, QSizePolicy,
)
from .ui_icons import icon, application_icon


class RoundedMenu(QMenu):
    """Action menus share the combo popup's border and transparent corners."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet("QMenu { background: white; border: 1px solid #dbe1ec; border-radius: 10px; padding: 6px; }"
                           "QMenu::item { padding: 8px 24px 8px 10px; border-radius: 5px; }"
                           "QMenu::item:selected { background: #eaf1ff; color: #234eac; }")


def _selectable(index):
    flags = index.flags()
    return index.isValid() and bool(flags & Qt.ItemFlag.ItemIsEnabled) and bool(
        flags & Qt.ItemFlag.ItemIsSelectable)


class _ComboPopup(QWidget):
    def __init__(self, combo):
        super().__init__(combo, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.combo = combo
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setObjectName("ModernComboPopup")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.panel = QFrame(self)
        self.panel.setObjectName("ComboPopupPanel")
        outer.addWidget(self.panel)
        layout = QVBoxLayout(self.panel)
        layout.setContentsMargins(6, 6, 6, 6)
        self.view = QListView(self.panel)
        self.view.setObjectName("ComboPopupList")
        self.view.setFrameShape(QFrame.Shape.NoFrame)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.view)
        self.setStyleSheet("""
            QWidget#ModernComboPopup { background: transparent; }
            QFrame#ComboPopupPanel { background: white; border: 1px solid #dbe1ec;
                                    border-radius: 10px; }
            QListView#ComboPopupList { background: white; color: #202b40;
                border: none; outline: none; padding: 0; }
            QListView#ComboPopupList::item { min-height: 28px; padding: 3px 9px;
                                           border-radius: 5px; }
            QListView#ComboPopupList::item:selected { background: #e9f0ff; color: #244d9c; }
            QListView#ComboPopupList::item:disabled { color: #9aa3b2; }
        """)
        self.view.installEventFilter(self)
        self.view.clicked.connect(combo._commit_popup)

    def eventFilter(self, watched, event):
        if watched is self.view and event.type() == QEvent.Type.KeyPress:
            if self._key(event.key()):
                return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event):
        if self._key(event.key()):
            event.accept()
        else:
            super().keyPressEvent(event)

    def _key(self, key):
        if key in (Qt.Key.Key_Escape, Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            self.combo.hidePopup()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.combo._commit_popup(self.view.currentIndex())
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Home, Qt.Key.Key_End):
            model, root = self.view.model(), self.view.rootIndex()
            count = model.rowCount(root)
            current = self.view.currentIndex().row()
            if key == Qt.Key.Key_Home:
                rows = range(count)
            elif key == Qt.Key.Key_End:
                rows = range(count - 1, -1, -1)
            elif key == Qt.Key.Key_Down:
                rows = range(current + 1, count)
            else:
                rows = range(current - 1 if current >= 0 else count - 1, -1, -1)
            for row in rows:
                index = model.index(row, self.view.modelColumn(), root)
                if _selectable(index):
                    self.view.setCurrentIndex(index)
                    self.view.scrollTo(index)
                    break
        else:
            return False
        return True

    def mousePressEvent(self, event):
        # Qt routes an outside press to the active popup (including offscreen).
        if not self.rect().contains(event.position().toPoint()):
            self.combo.hidePopup()
            event.accept()
        else:
            super().mousePressEvent(event)

    def hideEvent(self, event):
        super().hideEvent(event)
        self.combo._popup_hidden()


class StyledComboBox(QComboBox):
    """QComboBox with a frameless popup; ``popup_view`` shares ``model()``.

    ``view()`` retains Qt's standard internal view. It is never shown. The
    custom view is synchronized on opening, including root and model column.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.popup = _ComboPopup(self)
        self.popup_view = self.popup.view
        self._popup_open = False

    def showPopup(self):
        if not self.isEnabled() or self.count() == 0 or self._popup_open:
            return
        view = self.popup_view
        view.setModel(self.model())
        view.setRootIndex(self.rootModelIndex())
        view.setModelColumn(self.modelColumn())
        view.setCurrentIndex(self.model().index(
            self.currentIndex(), self.modelColumn(), self.rootModelIndex()))
        self.popup.ensurePolished()
        rows = min(self.count(), max(1, self.maxVisibleItems()))
        # Six-pixel padding plus the panel's one-pixel border on both sides.
        height = sum(max(28, view.sizeHintForRow(row)) for row in range(rows)) + 14
        width = max(self.width(), view.sizeHintForColumn(self.modelColumn()) + 36)
        anchor = self.mapToGlobal(QPoint(0, 0))
        screen = QApplication.screenAt(anchor) or self.screen()
        bounds = screen.availableGeometry()
        below = anchor.y() + self.height()
        bottom_space = max(0, bounds.bottom() + 1 - below)
        top_space = max(0, anchor.y() - bounds.top())
        use_below = height <= bottom_space or bottom_space >= top_space
        height = min(height, bottom_space if use_below else top_space, bounds.height())
        height = max(1, height)
        width = min(width, bounds.width())
        x = max(bounds.left(), min(anchor.x(), bounds.right() + 1 - width))
        y = below if use_below else anchor.y() - height
        y = max(bounds.top(), min(y, bounds.bottom() + 1 - height))
        self.popup.setGeometry(x, y, width, height)
        self._popup_open = True
        self.popup.show()
        view.setFocus(Qt.FocusReason.PopupFocusReason)
        view.scrollTo(view.currentIndex())

    def hidePopup(self):
        self.popup.hide()
        self._popup_hidden()

    def _popup_hidden(self):
        # Qt requires the base hidePopup to reset QComboBox's internal state
        # even when showPopup uses an entirely custom widget.
        super().hidePopup()
        if self._popup_open:
            self._popup_open = False
            if self.isVisible() and self.isEnabled():
                self.setFocus(Qt.FocusReason.PopupFocusReason)

    def _commit_popup(self, index):
        if not self._popup_open or not _selectable(index):
            return
        row = index.row()
        self.hidePopup()
        self.setCurrentIndex(row)
        # setCurrentIndex emits change signals itself, but not user activation.
        self.activated.emit(row)
        self.textActivated.emit(self.currentText())

    def hideEvent(self, event):
        self.hidePopup()
        super().hideEvent(event)


class ModernDialog(QDialog):
    """Frameless modal card with ``body_layout`` and ``button_area`` layouts."""
    def __init__(self, title, parent=None):
        super().__init__(parent, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setWindowTitle(title)
        self.setWindowIcon(application_icon())
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowModality(Qt.WindowModality.ApplicationModal if parent is None
                               else Qt.WindowModality.WindowModal)
        self.setObjectName("ModernDialog")
        self._fitting = False
        self._first_fit = True
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self._fit_content)
        outer = QVBoxLayout(self)
        outer.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        outer.setContentsMargins(0, 0, 0, 0)
        self.card = QFrame(self)
        self.card.setObjectName("DialogCard")
        outer.addWidget(self.card)
        layout = QVBoxLayout(self.card)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(18)
        header = QHBoxLayout()
        self.heading = QLabel(title)
        self.heading.setTextFormat(Qt.TextFormat.PlainText)
        self.heading.setWordWrap(True)
        self.heading.setObjectName("DialogHeading")
        header.addStretch()
        self.close_button = QToolButton()
        self.close_button.setIcon(icon("close"))
        self.close_button.setAccessibleName("Close")
        self.close_button.setFixedSize(30, 30)
        self.close_button.clicked.connect(self.reject)
        header.addWidget(self.close_button)
        layout.addLayout(header)
        # Heading and body share the scrolling region. Even an enormous title
        # must not push the close control or the action row off the screen.
        self.scroll_area = QScrollArea(self.card)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self.scroll_area.setWidgetResizable(False)
        self.content = QWidget()
        self.content.setStyleSheet("background: transparent;")
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(18)
        self.content_layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.content_layout.addWidget(self.heading)
        self.body_layout = QVBoxLayout()
        self.body_layout.setSpacing(12)
        self.content_layout.addLayout(self.body_layout)
        self.scroll_area.setWidget(self.content)
        layout.addWidget(self.scroll_area, 1)
        self.button_area = QHBoxLayout()
        self.button_area.addStretch()
        layout.addLayout(self.button_area)
        self.setStyleSheet("""
            QDialog#ModernDialog { background: transparent; }
            QFrame#DialogCard { background: white; border: 1px solid #dbe1ec;
                               border-radius: 14px; }
            QLabel { color: #26334b; background: transparent; border: none; }
            QLabel#DialogHeading { font-size: 18px; font-weight: 600; }
            QToolButton { color: #627087; background: transparent; border: none;
                          border-radius: 6px; font-size: 22px; }
            QToolButton:hover { background: #edf2fb; }
            QPushButton { background: white; color: #26334b; border: 1px solid #dbe1ec;
                          border-radius: 7px; padding: 8px 16px; }
            QPushButton:hover { background: #edf2fb; }
            QPushButton:focus { border: 2px solid #3865c7; }
            QPushButton[destructive="true"] { background: #c53642; color: white;
                                                border-color: #c53642; }
            QPushButton[destructive="true"]:hover { background: #aa2934; }
            QLineEdit { background: white; color: #26334b; border: 1px solid #dbe1ec;
                        border-radius: 7px; padding: 8px; }
        """)
        self.resize(440, 200)
        self.content.installEventFilter(self)
        self.card.installEventFilter(self)

    def _queue_fit(self):
        if not self._fitting and not self._fit_timer.isActive():
            self._fit_timer.start(0)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.LayoutRequest, QEvent.Type.FontChange,
                            QEvent.Type.StyleChange):
            self._queue_fit()
        return super().eventFilter(watched, event)

    def showEvent(self, event):
        super().showEvent(event)
        self._fit_content()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_fit_timer"):
            self._queue_fit()

    def _fit_content(self):
        """Measure at the real wrap width, then cap the viewport to the screen.

        LayoutRequest covers external body_layout insertions and QLabel.setText.
        Coalescing and changing geometry only when necessary lets Qt settle
        without a resize/layout-request feedback loop.
        """
        if self._fitting:
            return
        self._fitting = True
        try:
            self.ensurePolished()
            for label in self.content.findChildren(QLabel):
                if label.wordWrap():
                    policy = QSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)
                    policy.setHeightForWidth(True)
                    if label.sizePolicy() != policy:
                        label.setSizePolicy(policy)
            bounds = self.screen().availableGeometry()
            if self.minimumWidth() > bounds.width():
                self.setMinimumWidth(bounds.width())
            if self.minimumHeight() > bounds.height():
                self.setMinimumHeight(bounds.height())
            margins = self.card.layout().contentsMargins()
            horizontal = margins.left() + margins.right() + 2 * self.card.frameWidth()
            minimum = self.content_layout.minimumSize().width() + horizontal
            width = self.width()
            if self._first_fit:
                width = max(width, min(640, max(440, self.content_layout.sizeHint().width() + horizontal)))
                self._first_fit = False
            width = min(bounds.width(), self.maximumWidth(), max(width, min(640, minimum)))
            actions = [self.button_area.itemAt(i) for i in range(self.button_area.count())
                       if not self.button_area.itemAt(i).isEmpty()]
            action_width = (sum(item.sizeHint().width() for item in actions)
                            + max(0, len(actions) - 1) * self.button_area.spacing())
            direction = (QBoxLayout.Direction.TopToBottom if action_width > width - horizontal
                         else QBoxLayout.Direction.LeftToRight)
            if self.button_area.direction() != direction:
                self.button_area.setDirection(direction)
            # Reserve scrollbar width while measuring. This avoids alternating
            # between two wrap widths when content is near the screen limit.
            bar_width = self.scroll_area.verticalScrollBar().sizeHint().width()
            content_width = max(self.content_layout.minimumSize().width(), width - horizontal - bar_width)
            needed = max(self.content_layout.minimumSize().height(),
                         self.content_layout.totalHeightForWidth(content_width))
            self.content.resize(content_width, needed)
            self.content_layout.activate()
            chrome = (margins.top() + margins.bottom() + 2 * self.card.frameWidth()
                      + self.close_button.height() + self.button_area.sizeHint().height()
                      + 2 * self.card.layout().spacing())
            if content_width > width - horizontal - bar_width:
                chrome += self.scroll_area.horizontalScrollBar().sizeHint().height()
            height = min(bounds.height(), self.maximumHeight(), needed + chrome)
            self.resize(width, height)
            self.layout().activate()
            self.card.layout().activate()
            # Keep the entire frameless card on the available desktop after
            # growth, including dialogs initially placed near its lower edge.
            self.move(max(bounds.left(), min(self.x(), bounds.right() + 1 - self.width())),
                      max(bounds.top(), min(self.y(), bounds.bottom() + 1 - self.height())))
        finally:
            self._fitting = False

    def add_button(self, text, callback, *, default=False, destructive=False):
        button = QPushButton(text, self.card)
        button.setAutoDefault(False)
        button.setDefault(default)
        button.setProperty("destructive", destructive)
        button.clicked.connect(callback)
        self.button_area.addWidget(button)
        return button

    def add_message(self, message):
        label = QLabel(message)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body_layout.addWidget(label)
        return label


class ConfirmationDialog(ModernDialog):
    def __init__(self, title, message, parent=None, confirm_text="Remove", destructive=True):
        super().__init__(title, parent)
        self.message_label = self.add_message(message)
        self.cancel_button = self.add_button("Cancel", self.reject, default=True)
        self.confirm_button = self.add_button(confirm_text, self.accept, destructive=destructive)
        self.cancel_button.setFocus()


class TextInputDialog(ModernDialog):
    def __init__(self, title, label, initial="", parent=None):
        super().__init__(title, parent)
        self.label = self.add_message(label)
        self.line_edit = QLineEdit(initial)
        self.label.setBuddy(self.line_edit)
        self.body_layout.addWidget(self.line_edit)
        self.cancel_button = self.add_button("Cancel", self.reject)
        self.confirm_button = self.add_button("OK", self.accept, default=True)
        self.line_edit.selectAll()
        self.line_edit.setFocus()


def confirm(parent, title, message, confirm_text="Remove", destructive=True) -> bool:
    dialog = ConfirmationDialog(title, message, parent, confirm_text, destructive)
    try:
        return dialog.exec() == QDialog.DialogCode.Accepted
    finally:
        dialog.deleteLater()


def ask_text(parent, title, label, initial="") -> tuple[str, bool]:
    dialog = TextInputDialog(title, label, initial, parent)
    try:
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        return (dialog.line_edit.text(), True) if accepted else ("", False)
    finally:
        dialog.deleteLater()


def inform(parent, title, message) -> None:
    """Display a blocking message, suitable for synchronous validation."""
    dialog = ModernDialog(title, parent)
    dialog.add_message(message)
    button = dialog.add_button("OK", dialog.accept, default=True)
    button.setFocus()
    try:
        dialog.exec()
    finally:
        dialog.deleteLater()
