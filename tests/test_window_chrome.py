"""Real offscreen widgets exercising the window chrome's input paths."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

from radmin_viewer.window_chrome import FramelessMainWindow


class CleanupWindow(FramelessMainWindow):
    def __init__(self):
        super().__init__()
        self.closed = 0
        self.allow_close = True

    def closeEvent(self, event):
        self.closed += 1
        event.accept() if self.allow_close else event.ignore()


class WindowChromeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.window = CleanupWindow()
        self.window.setWindowTitle("Manager")
        self.menu = self.window.menuBar()
        self.menu.setAccessibleName("Manager menus")
        self.file_menu = self.menu.addMenu("&File")
        self.action = self.file_menu.addAction("&Connect")
        self.action.setShortcut("Ctrl+K")
        self.central = QPushButton("Inner control")
        self.window.setCentralWidget(self.central)
        self.window.resize(600, 400)
        self.window.move(100, 100)
        self.window.install_chrome()
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.allow_close = True
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def mouse(self, widget, kind, global_point, held=True):
        button = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton
        event = QMouseEvent(kind, QPointF(widget.mapFromGlobal(global_point)),
                            QPointF(global_point), button,
                            Qt.MouseButton.LeftButton if held else Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(widget, event)

    def test_install_preserves_menu_and_accessor(self):
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertIs(self.window.menuBar(), self.menu)
        self.assertIs(self.window.menuWidget(), self.window.chrome)
        self.assertIs(self.window.install_chrome(), self.window.chrome)
        self.assertEqual(self.menu.actions(), [self.file_menu.menuAction()])
        self.assertIs(self.file_menu.actions()[0], self.action)
        self.assertEqual(self.menu.accessibleName(), "Manager menus")
        self.assertFalse(self.menu.isNativeMenuBar())
        self.assertTrue(self.window.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.window.menuBar().addMenu("&Help")
        self.assertIs(self.window.menuWidget(), self.window.chrome)
        called = []
        self.action.triggered.connect(lambda: called.append(True))
        self.window.activateWindow()
        self.central.setFocus()
        self.app.processEvents()
        QTest.keyClick(self.central, Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(called, [True])
        self.window.setWindowTitle("Updated")
        self.assertEqual(self.window.chrome.drag_area.title.text(), "Updated")

    def test_control_buttons_and_deferred_cleanup(self):
        chrome = self.window.chrome
        QTest.mouseClick(chrome.minimize_button, Qt.MouseButton.LeftButton)
        self.assertTrue(self.window.isMinimized())
        self.window.showNormal()
        QTest.mouseClick(chrome.maximize_button, Qt.MouseButton.LeftButton)
        self.assertTrue(self.window.isMaximized())
        self.assertEqual(chrome.maximize_button.accessibleName(), "Restore")
        QTest.mouseClick(chrome.maximize_button, Qt.MouseButton.LeftButton)
        self.assertFalse(self.window.isMaximized())
        self.assertEqual(chrome.maximize_button.accessibleName(), "Maximize")
        self.window.allow_close = False
        QTest.mouseClick(chrome.close_button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.window.closed, 0)
        self.app.processEvents()
        self.assertEqual(self.window.closed, 1)
        self.assertTrue(self.window.isVisible())
        self.window.allow_close = True
        QTest.mouseClick(chrome.close_button, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertEqual(self.window.closed, 2)
        self.assertFalse(self.window.isVisible())

    def test_native_move_and_resize_are_attempted(self):
        handle = self.window.windowHandle()
        with patch.object(handle, "startSystemMove", return_value=True) as move:
            QTest.mousePress(self.window.chrome.drag_area, Qt.MouseButton.LeftButton)
            move.assert_called_once_with()
            self.assertIsNone(self.window.chrome.drag_area._press)
            QTest.mouseRelease(self.window.chrome.drag_area, Qt.MouseButton.LeftButton)
        grip = self.window._resize_grips[-1]
        with patch.object(handle, "startSystemResize", return_value=True) as resize:
            QTest.mousePress(grip, Qt.MouseButton.LeftButton)
            resize.assert_called_once_with(Qt.Edge.BottomEdge | Qt.Edge.RightEdge)
            self.assertIsNone(grip._press)
            QTest.mouseRelease(grip, Qt.MouseButton.LeftButton)

    def test_fallback_resize_all_eight_grips_bounds_and_anchor(self):
        self.window.setMinimumSize(320, 180)
        self.window.setMaximumSize(700, 500)
        with patch.object(self.window.windowHandle(), "startSystemResize", return_value=False):
            for grip in self.window._resize_grips:
                for grow in (False, True):
                    with self.subTest(edges=grip.edges, grow=grow):
                        self.window.setGeometry(100, 100, 600, 400)
                        original = self.window.geometry()
                        start = grip.mapToGlobal(grip.rect().center())
                        left = bool(grip.edges & Qt.Edge.LeftEdge)
                        top = bool(grip.edges & Qt.Edge.TopEdge)
                        sign = 1 if grow else -1
                        delta = QPoint((-1 if left else 1) * sign * 2000,
                                       (-1 if top else 1) * sign * 2000)
                        self.mouse(grip, QEvent.Type.MouseButtonPress, start)
                        self.mouse(grip, QEvent.Type.MouseMove, start + delta)
                        self.mouse(grip, QEvent.Type.MouseButtonRelease, start + delta, False)
                        result = self.window.geometry()
                        horizontal = grip.edges & (Qt.Edge.LeftEdge | Qt.Edge.RightEdge)
                        vertical = grip.edges & (Qt.Edge.TopEdge | Qt.Edge.BottomEdge)
                        self.assertEqual(result.width(), (700 if grow else 320) if horizontal else 600)
                        self.assertEqual(result.height(), (500 if grow else 180) if vertical else 400)
                        self.assertEqual(result.right() if left else result.left(),
                                         original.right() if left else original.left())
                        self.assertEqual(result.bottom() if top else result.top(),
                                         original.bottom() if top else original.top())

    def test_grips_disabled_in_maximized_and_fullscreen(self):
        self.assertEqual(len(self.window._resize_grips), 8)
        for change in (self.window.showMaximized, self.window.showFullScreen):
            change()
            self.app.processEvents()
            self.assertTrue(all(not g.isVisible() and not g.isEnabled()
                                for g in self.window._resize_grips))
            self.window.showNormal()
            self.app.processEvents()
            self.assertTrue(all(g.isVisible() and g.isEnabled() for g in self.window._resize_grips))
        center = self.central.mapTo(self.window, self.central.rect().center())
        self.assertIs(self.window.childAt(center), self.central)

    def test_drag_fallback_is_bounded_and_stops_on_release(self):
        drag = self.window.chrome.drag_area
        with patch.object(self.window.windowHandle(), "startSystemMove", return_value=False):
            start = drag.mapToGlobal(drag.rect().center())
            original = self.window.pos()
            self.mouse(drag, QEvent.Type.MouseButtonPress, start)
            self.mouse(drag, QEvent.Type.MouseMove, start + QPoint(20, 20))
            self.assertEqual(self.window.pos(), original + QPoint(20, 20))
            self.mouse(drag, QEvent.Type.MouseMove, QPoint(-10000, -10000))
            self.assertEqual(self.window.y(), self.window.screen().availableGeometry().top())
            self.assertGreaterEqual(self.window.geometry().right(), 79)
            self.mouse(drag, QEvent.Type.MouseButtonRelease, start, False)
            last = self.window.pos()
            self.mouse(drag, QEvent.Type.MouseMove, start + QPoint(40, 40))
            self.assertEqual(self.window.pos(), last)

    def test_double_click_and_restore_drag_attempt_native_move(self):
        drag = self.window.chrome.drag_area
        QTest.mouseDClick(drag, Qt.MouseButton.LeftButton)
        self.assertTrue(self.window.isMaximized())
        with patch.object(self.window.windowHandle(), "startSystemMove", return_value=True) as move:
            start = drag.mapToGlobal(drag.rect().center())
            self.mouse(drag, QEvent.Type.MouseButtonPress, start)
            move.assert_not_called()
            self.mouse(drag, QEvent.Type.MouseMove, start + QPoint(40, 30))
            self.assertFalse(self.window.isMaximized())
            move.assert_called_once_with()
            self.mouse(drag, QEvent.Type.MouseButtonRelease, start, False)
        self.window.showFullScreen()
        QTest.mouseDClick(drag, Qt.MouseButton.LeftButton)
        self.assertTrue(self.window.isFullScreen())

    def test_buttons_and_menus_do_not_start_drag(self):
        with patch.object(self.window.windowHandle(), "startSystemMove") as move:
            for widget in (self.window.chrome.minimize_button,
                           self.window.chrome.maximize_button,
                           self.window.chrome.close_button, self.central):
                start = widget.mapToGlobal(widget.rect().center())
                self.mouse(widget, QEvent.Type.MouseButtonPress, start)
                self.mouse(widget, QEvent.Type.MouseMove, start + QPoint(100, 100))
                self.mouse(widget, QEvent.Type.MouseButtonRelease, start + QPoint(100, 100), False)
            QTest.mouseClick(self.menu, Qt.MouseButton.LeftButton,
                             pos=self.menu.actionGeometry(self.file_menu.menuAction()).center())
            self.file_menu.hide()
            move.assert_not_called()

    def test_unavailable_native_api_and_fixed_size_fallback(self):
        self.window.setFixedSize(600, 400)
        grip = self.window._resize_grips[-1]
        with patch.object(self.window.windowHandle(), "startSystemResize",
                          side_effect=NotImplementedError):
            start = grip.mapToGlobal(grip.rect().center())
            original = self.window.geometry()
            self.mouse(grip, QEvent.Type.MouseButtonPress, start)
            self.mouse(grip, QEvent.Type.MouseMove, start + QPoint(200, 200))
            self.mouse(grip, QEvent.Type.MouseButtonRelease, start, False)
            self.assertEqual(self.window.geometry(), original)

    def test_install_on_visible_window_and_timer_lifetime(self):
        other = CleanupWindow()
        other.resize(600, 400)
        menu = other.menuBar()
        menu.addMenu("File")
        other.show()
        other.install_chrome()
        self.app.processEvents()
        self.assertTrue(other.isVisible())
        self.assertIs(other.menuBar(), menu)
        # Destruction before the queued close must cancel the owned timer.
        QTest.mouseClick(other.chrome.close_button, Qt.MouseButton.LeftButton)
        other.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.assertEqual(other.closed, 0)


if __name__ == "__main__":
    unittest.main()
