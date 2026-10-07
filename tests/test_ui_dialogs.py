"""Real widget tests; no desktop interaction or native dialog automation."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt, QTimer
from PySide6.QtGui import QStandardItemModel, QStandardItem
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QVBoxLayout, QWidget

from radmin_viewer.ui_dialogs import (
    ConfirmationDialog, ModernDialog, StyledComboBox, TextInputDialog,
    ask_text, confirm, inform,
)
from radmin_viewer.ui_icons import apply_theme


class DialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.old_stylesheet = self.app.styleSheet()
        self.old_palette = self.app.palette()
        apply_theme(self.app)
        self.window = QWidget()
        layout = QVBoxLayout(self.window)
        self.combo = StyledComboBox()
        layout.addWidget(self.combo)
        self.combo.addItem("First", {"id": 1})
        self.combo.addItem("Second", {"id": 2})
        self.combo.addItems(["Third", "Fourth"])
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        for window in QApplication.topLevelWidgets():
            window.close()
            window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.setStyleSheet(self.old_stylesheet)
        self.app.setPalette(self.old_palette)

    def open_popup(self):
        self.combo.showPopup()
        self.app.processEvents()
        return self.combo.popup_view

    def test_popup_chrome_and_shared_model(self):
        view = self.open_popup()
        popup = self.combo.popup
        self.assertEqual(popup.windowType(), Qt.WindowType.Popup)
        self.assertTrue(popup.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.assertTrue(popup.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
        self.assertIs(view.model(), self.combo.model())
        self.assertFalse(self.combo.view().isVisible())
        self.assertEqual(self.combo.styleSheet(), "")
        self.combo.addItem("Live", 99)
        self.assertEqual(view.model().rowCount(), 5)
        self.assertEqual(popup.grab().toImage().pixelColor(0, 0).alpha(), 0)

    def test_navigation_commit_once_and_userdata(self):
        changes = QSignalSpy(self.combo.currentIndexChanged)
        activated = QSignalSpy(self.combo.activated)
        view = self.open_popup()
        QTest.keyClick(view, Qt.Key.Key_Down)
        self.assertEqual(self.combo.currentIndex(), 0)
        QTest.keyClick(view, Qt.Key.Key_Return)
        self.assertEqual(self.combo.currentData(), {"id": 2})
        self.assertEqual(self.combo.currentText(), "Second")
        self.assertEqual(changes.count(), 1)
        self.assertEqual(activated.count(), 1)
        self.assertFalse(self.combo.popup.isVisible())
        self.assertTrue(self.combo.hasFocus())
        view = self.open_popup()
        QTest.keyClick(view, Qt.Key.Key_Return)
        self.assertEqual(changes.count(), 1)
        self.assertEqual(activated.count(), 2)

    def test_click_and_signal_blocking(self):
        view = self.open_popup()
        changes = QSignalSpy(self.combo.currentIndexChanged)
        index = self.combo.model().index(2, 0)
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                         pos=view.visualRect(index).center())
        self.assertEqual(self.combo.currentIndex(), 2)
        self.assertEqual(changes.count(), 1)
        self.combo.blockSignals(True)
        view = self.open_popup()
        QTest.keyClick(view, Qt.Key.Key_Home)
        QTest.keyClick(view, Qt.Key.Key_Enter)
        self.combo.blockSignals(False)
        self.assertEqual(self.combo.currentIndex(), 0)
        self.assertEqual(changes.count(), 1)

    def test_native_control_opening_paths_and_live_roles(self):
        QTest.mouseClick(self.combo, Qt.MouseButton.LeftButton,
                         pos=QPoint(self.combo.width() - 12, self.combo.height() // 2))
        self.app.processEvents()
        self.assertTrue(self.combo.popup.isVisible())
        role = Qt.ItemDataRole.UserRole + 7
        self.combo.setItemData(1, "extra role", role)
        self.assertEqual(self.combo.popup_view.model().index(1, 0).data(role), "extra role")
        QTest.keyClick(self.combo.popup_view, Qt.Key.Key_Escape)
        QTest.keyClick(self.combo, Qt.Key.Key_Down, Qt.KeyboardModifier.AltModifier)
        self.app.processEvents()
        self.assertTrue(self.combo.popup.isVisible())
        self.combo.clear()
        QTest.keyClick(self.combo.popup_view, Qt.Key.Key_Return)
        self.assertEqual(self.combo.currentIndex(), -1)
        QTest.keyClick(self.combo.popup_view, Qt.Key.Key_Escape)
        self.assertFalse(self.combo.popup.isVisible())

    def test_disabled_choices_empty_and_disabled_combo(self):
        self.combo.model().item(1).setEnabled(False)
        self.combo.model().item(3).setSelectable(False)
        view = self.open_popup()
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                         pos=view.visualRect(self.combo.model().index(1, 0)).center())
        self.assertTrue(self.combo.popup.isVisible())
        QTest.keyClick(view, Qt.Key.Key_Home)
        QTest.keyClick(view, Qt.Key.Key_Down)
        self.assertEqual(view.currentIndex().row(), 2)
        QTest.keyClick(view, Qt.Key.Key_End)
        self.assertEqual(view.currentIndex().row(), 2)
        QTest.keyClick(view, Qt.Key.Key_Up)
        self.assertEqual(view.currentIndex().row(), 0)
        self.combo.hidePopup()
        self.combo.clear()
        self.combo.showPopup()
        self.assertFalse(self.combo.popup.isVisible())
        self.combo.addItem("Disabled")
        self.combo.setEnabled(False)
        self.combo.showPopup()
        self.assertFalse(self.combo.popup.isVisible())

    def test_escape_and_outside_cancel_then_reopen(self):
        changes = QSignalSpy(self.combo.currentIndexChanged)
        for outside in (False, True):
            view = self.open_popup()
            QTest.keyClick(view, Qt.Key.Key_End)
            if outside:
                QTest.mouseClick(self.combo.popup, Qt.MouseButton.LeftButton, pos=QPoint(-8, -8))
            else:
                QTest.keyClick(view, Qt.Key.Key_Escape)
            self.assertFalse(self.combo.popup.isVisible())
            self.assertEqual(self.combo.currentIndex(), 0)
        self.assertEqual(changes.count(), 0)
        self.open_popup()
        self.window.hide()
        self.assertFalse(self.combo.popup.isVisible())

    def test_model_replacement_and_screen_placement(self):
        model = QStandardItemModel(self.combo)
        for text in ("One", "Two"):
            item = QStandardItem(text)
            item.setData(text.upper(), Qt.ItemDataRole.UserRole)
            model.appendRow(item)
        self.combo.setModel(model)
        bounds = self.window.screen().availableGeometry()
        self.window.move(bounds.right() - self.window.width(), bounds.bottom() - self.window.height())
        view = self.open_popup()
        self.assertTrue(bounds.contains(self.combo.popup.geometry()))
        self.assertLess(self.combo.popup.y(), self.combo.mapToGlobal(QPoint()).y())
        QTest.keyClick(view, Qt.Key.Key_End)
        QTest.keyClick(view, Qt.Key.Key_Return)
        self.assertEqual(self.combo.currentData(), "TWO")

    def test_confirmation_plain_text_safe_default_escape_and_remove(self):
        dialog = ConfirmationDialog("<b>Delete</b>", "<b>Really?</b>", self.window)
        self.assertIsInstance(dialog, ModernDialog)
        self.assertTrue(dialog.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.assertTrue(dialog.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
        self.assertEqual(dialog.windowModality(), Qt.WindowModality.WindowModal)
        self.assertEqual(dialog.heading.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(dialog.message_label.textFormat(), Qt.TextFormat.PlainText)
        destination = StyledComboBox()
        destination.addItems(["A", "B"])
        dialog.body_layout.addWidget(destination)
        dialog.show()
        self.app.processEvents()
        QTest.keyClick(dialog, Qt.Key.Key_Return)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.assertFalse(dialog.isVisible())
        dialog.show()
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        dialog.show()
        QTest.mouseClick(dialog.confirm_button, Qt.MouseButton.LeftButton)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        dialog.show()
        QTest.mouseClick(dialog.close_button, Qt.MouseButton.LeftButton)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)

    def test_text_state_and_blocking_helpers(self):
        dialog = TextInputDialog("Rename", "Name", "Original", self.window)
        dialog.show()
        self.app.processEvents()
        self.assertEqual(dialog.line_edit.selectedText(), "Original")
        QTest.keyClicks(dialog.line_edit, "Replacement")
        QTest.keyClick(dialog.line_edit, Qt.Key.Key_Return)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.line_edit.text(), "Replacement")

        def accept_text():
            active = QApplication.activeModalWidget()
            active.line_edit.setText("new name")
            active.accept()

        QTimer.singleShot(0, accept_text)
        self.assertEqual(ask_text(self.window, "Name", "Label", "old"), ("new name", True))
        QTimer.singleShot(0, lambda: QApplication.activeModalWidget().reject())
        self.assertEqual(ask_text(self.window, "Name", "Label", "old"), ("", False))
        QTimer.singleShot(0, lambda: QApplication.activeModalWidget().confirm_button.click())
        self.assertTrue(confirm(self.window, "Remove", "Really?"))
        QTimer.singleShot(0, lambda: QApplication.activeModalWidget().reject())
        self.assertFalse(confirm(self.window, "Remove", "Really?"))
        QTimer.singleShot(0, lambda: QApplication.activeModalWidget().accept())
        self.assertIsNone(inform(self.window, "Information", "Done"))

    def settle_dialog(self, dialog):
        for _ in range(12):
            self.app.processEvents()
        geometry = dialog.geometry()
        content_size = dialog.content.size()
        for _ in range(12):
            self.app.processEvents()
        self.assertEqual(dialog.geometry(), geometry)
        self.assertEqual(dialog.content.size(), content_size)
        self.assertFalse(dialog._fit_timer.isActive(), "layout must settle without polling")

    def assert_dialog_fits(self, dialog, destination=None):
        self.settle_dialog(dialog)
        self.assertTrue(dialog.screen().availableGeometry().contains(dialog.geometry()))
        for label in dialog.content.findChildren(QLabel):
            self.assertGreaterEqual(label.height(), label.heightForWidth(label.width()),
                                    (label.text()[:40], label.size()))
        viewport = dialog.scroll_area.viewport()
        viewport_bottom = viewport.mapTo(dialog, viewport.rect().bottomLeft()).y()
        for button in (dialog.cancel_button, dialog.confirm_button, dialog.close_button):
            rect = button.rect().translated(button.mapTo(dialog, QPoint()))
            self.assertTrue(dialog.rect().contains(rect))
            if button is not dialog.close_button:
                self.assertGreater(rect.top(), viewport_bottom)
        self.assertLess(dialog.heading.geometry().bottom(),
                        dialog.message_label.geometry().top())
        cancel_rect = dialog.cancel_button.rect().translated(dialog.cancel_button.mapTo(dialog, QPoint()))
        confirm_rect = dialog.confirm_button.rect().translated(dialog.confirm_button.mapTo(dialog, QPoint()))
        self.assertFalse(cancel_rect.intersects(confirm_rect))
        if destination is not None:
            self.assertGreaterEqual(destination.height(), destination.minimumHeight())
            self.assertLess(dialog.message_label.geometry().bottom(), destination.geometry().top())
        if dialog.content.height() > viewport.height():
            bar = dialog.scroll_area.verticalScrollBar()
            self.assertGreater(bar.maximum(), 0)
            bar.setValue(bar.maximum())
            self.app.processEvents()
            if destination is not None:
                self.assertLessEqual(destination.mapTo(viewport, destination.rect().bottomLeft()).y(),
                                     viewport.height())

    def test_wrapped_remove_group_with_inserted_destination(self):
        dialog = ConfirmationDialog("Remove group", "Move the computers to another group before "
                                    "removing this group.\nThe computers will not be deleted.", self.window)
        dialog.setFixedWidth(440)
        destination = StyledComboBox()
        destination.addItems(["All computers", "Office"])
        destination.setMinimumHeight(58)
        dialog.body_layout.addWidget(destination)
        dialog.show()
        self.assert_dialog_fits(dialog, destination)
        self.assertEqual(dialog.scroll_area.verticalScrollBar().maximum(), 0)
        self.assertTrue(dialog.cancel_button.isDefault())
        self.assertFalse(dialog.confirm_button.isDefault())

    def test_large_font_multiline_and_unbroken_unicode_at_narrow_width(self):
        for message in ("One line of a much longer explanation.\n" * 45,
                        "Å界🙂Жé" * 500):
            with self.subTest(message=message[:20]):
                dialog = ConfirmationDialog("Remove group", message, self.window)
                dialog.setFixedWidth(360)
                font = dialog.message_label.font()
                font.setPointSize(28)
                dialog.message_label.setFont(font)
                destination = StyledComboBox()
                destination.addItem("Destination")
                destination.setMinimumSize(280, 72)
                dialog.body_layout.addWidget(destination)
                dialog.show()
                self.assert_dialog_fits(dialog, destination)
                self.assertGreater(dialog.scroll_area.verticalScrollBar().maximum(), 0)
                dialog.close()

    def test_live_insertion_text_changes_and_resize(self):
        dialog = ConfirmationDialog("Remove group", "Short message", self.window)
        dialog.show()
        self.settle_dialog(dialog)
        initial_height = dialog.height()
        destination = StyledComboBox()
        destination.addItem("Destination")
        destination.setMinimumSize(460, 85)
        dialog.body_layout.addWidget(destination)
        extra = QLabel("An extra explanation that was inserted after showing the dialog. " * 3)
        extra.setWordWrap(True)
        dialog.body_layout.addWidget(extra)
        dialog.message_label.setText("A longer explanation of the destination.\n" * 4)
        self.assert_dialog_fits(dialog)
        self.assertGreater(dialog.height(), initial_height)
        self.assertGreaterEqual(destination.width(), 460)
        self.assertLess(dialog.message_label.geometry().bottom(), destination.geometry().top())
        self.assertLess(destination.geometry().bottom(), extra.geometry().top())
        dialog.resize(500, 160)
        self.assert_dialog_fits(dialog)
        dialog.message_label.setText("Dynamic text\n" * 100)
        self.assert_dialog_fits(dialog)
        long_scroll = dialog.scroll_area.verticalScrollBar().maximum()
        self.assertGreater(long_scroll, 0)
        dialog.message_label.setText("Short again")
        self.assert_dialog_fits(dialog)
        self.assertLess(dialog.scroll_area.verticalScrollBar().maximum(), long_scroll)
        if dialog.height() < dialog.screen().availableGeometry().height():
            self.assertEqual(dialog.scroll_area.verticalScrollBar().maximum(), 0)

    def test_huge_heading_scrolls_without_hiding_close_or_actions(self):
        dialog = ConfirmationDialog("Ж界Å" * 1500, "Keep the destination reachable.", self.window)
        dialog.heading.setStyleSheet("font-size: 38px;")
        dialog.show()
        self.assert_dialog_fits(dialog)
        self.assertLessEqual(dialog.width(), 640)
        self.assertGreater(dialog.scroll_area.verticalScrollBar().maximum(), 0)
        QTest.keyClick(dialog, Qt.Key.Key_Return)
        self.assertFalse(dialog.isVisible())
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)

    def test_large_action_fonts_remain_reachable_at_narrow_width(self):
        dialog = ConfirmationDialog("Remove group", "Choose a destination.\n" * 25, self.window)
        dialog.setFixedWidth(360)
        for button in (dialog.cancel_button, dialog.confirm_button):
            font = button.font()
            font.setPointSize(28)
            button.setFont(font)
        dialog.show()
        self.assert_dialog_fits(dialog)
        for button in (dialog.cancel_button, dialog.confirm_button):
            self.assertGreaterEqual(button.width(), button.sizeHint().width())
            self.assertGreaterEqual(button.height(), button.sizeHint().height())
        QTest.mouseClick(dialog.cancel_button, Qt.MouseButton.LeftButton)
        self.assertFalse(dialog.isVisible())


if __name__ == "__main__":
    unittest.main()
