"""Offscreen tray behavior with no native system-tray registration."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, Signal
from PySide6.QtWidgets import QApplication, QMainWindow, QSystemTrayIcon
from shiboken6 import isValid

from radmin_viewer.system_tray import SystemTrayController


class FakeTray(QObject):
    activated = Signal(object)

    def __init__(self, parent):
        super().__init__(parent)
        self.visible = False
        self.show_calls = 0
        self.hide_calls = 0

    def setIcon(self, icon):
        self.icon = icon

    def setToolTip(self, text):
        self.tooltip = text

    def setContextMenu(self, menu):
        self.menu = menu

    def show(self):
        self.visible = True
        self.show_calls += 1

    def hide(self):
        self.visible = False
        self.hide_calls += 1


class Manager(QMainWindow):
    def __init__(self):
        super().__init__()
        self.exiting = False
        self.close_calls = 0

    def closeEvent(self, event):
        self.close_calls += 1
        super().closeEvent(event)


class SystemTrayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = Manager()
        self.window.show()
        self.supported = True
        self.controller = SystemTrayController(
            self.window, tray_factory=FakeTray, availability=lambda: self.supported)
        self.requests = []
        self.controller.quit_requested.connect(lambda: self.requests.append("quit"))

    def tearDown(self):
        if isValid(self.controller):
            self.controller.stop()
        if isValid(self.window):
            self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_menu_and_parent_ownership(self):
        c = self.controller
        self.assertIs(c.parent(), self.window)
        self.assertIs(c.tray.parent(), c)
        self.assertIs(c.menu.parent(), self.window)
        self.assertIs(c.tray.menu, c.menu)
        self.assertEqual([a.text() for a in c.menu.actions()],
                         ["Open viewer", "Hide viewer", "", "Quit"])
        self.assertTrue(c.menu.actions()[2].isSeparator())
        self.assertFalse(c.tray.icon.isNull())
        self.assertTrue(c.tray.visible)
        self.assertEqual(c._timer.interval(), 2000)
        self.assertTrue(c._timer.isActive())

    def test_hide_never_closes_or_requests_quit(self):
        self.controller.hide_action.trigger()
        self.assertFalse(self.window.isVisible())
        self.assertFalse(self.window.exiting)
        self.assertEqual(self.window.close_calls, 0)
        self.assertEqual(self.requests, [])
        self.assertTrue(self.controller.tray.visible)
        self.controller.open_action.trigger()
        self.assertTrue(self.window.isVisible())

    def test_restore_preserves_maximized_and_clears_only_minimized(self):
        for state in (Qt.WindowState.WindowNoState,
                      Qt.WindowState.WindowMaximized,
                      Qt.WindowState.WindowMinimized,
                      Qt.WindowState.WindowMaximized | Qt.WindowState.WindowMinimized):
            with self.subTest(state=state):
                self.window.setWindowState(state)
                self.controller.hide_to_tray()
                with patch.object(self.window, "raise_") as raised, \
                        patch.object(self.window, "activateWindow") as activated:
                    self.controller.restore()
                    raised.assert_called_once_with()
                    activated.assert_called_once_with()
                self.controller.restore()
                self.assertTrue(self.window.isVisible())
                self.assertFalse(self.window.isMinimized())
                self.assertEqual(self.window.isMaximized(),
                                 bool(state & Qt.WindowState.WindowMaximized))

    def test_only_left_click_and_double_click_restore(self):
        for reason in QSystemTrayIcon.ActivationReason:
            with self.subTest(reason=reason):
                self.controller.hide_to_tray()
                self.controller.tray.activated.emit(reason)
                self.assertEqual(self.window.isVisible(), reason in (
                    QSystemTrayIcon.ActivationReason.Trigger,
                    QSystemTrayIcon.ActivationReason.DoubleClick))
        self.assertEqual(self.requests, [])

    def test_quit_is_latched_and_icon_survives_graceful_shutdown(self):
        c = self.controller
        c.quit_action.trigger()
        c.quit_action.trigger()
        c._request_quit()
        self.assertEqual(self.requests, ["quit"])
        self.assertEqual(self.window.close_calls, 0)
        self.assertTrue(c.tray.visible)
        c.begin_shutdown()
        c.begin_shutdown()
        self.window.hide()
        c.restore()
        self.assertFalse(c.hide_to_tray())
        c.tray.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)
        self.assertFalse(self.window.isVisible())
        self.assertFalse(any(a.isEnabled() for a in (
            c.open_action, c.hide_action, c.quit_action)))
        self.assertIn("Shutting down", c.tray.tooltip)
        self.supported = False
        c._timer.timeout.emit()
        self.assertFalse(self.window.isVisible())
        self.assertTrue(c.tray.visible)
        self.assertEqual(c.tray.hide_calls, 0)
        c.stop()
        c.stop()
        self.assertEqual(c.tray.hide_calls, 1)
        self.assertEqual(self.requests, ["quit"])

    def test_availability_loss_restores_and_return_enables_hide(self):
        c = self.controller
        c.hide_to_tray()
        self.supported = False
        c._timer.timeout.emit()
        self.assertTrue(self.window.isVisible())
        self.assertFalse(c.available)
        self.assertFalse(c.can_hide)
        self.assertFalse(c.hide_action.isEnabled())
        self.supported = True
        c._timer.timeout.emit()
        self.assertTrue(c.can_hide)
        self.assertTrue(c.hide_action.isEnabled())
        self.assertEqual(c.tray.show_calls, 2)
        self.assertTrue(c.hide_to_tray())
        self.assertEqual(self.requests, [])

    def test_hide_rechecks_availability_between_polls(self):
        self.supported = False
        self.assertFalse(self.controller.hide_to_tray())
        self.assertTrue(self.window.isVisible())
        self.assertEqual(self.window.close_calls, 0)

    def test_external_shutdown_does_not_emit_quit(self):
        c = self.controller
        c.begin_shutdown()
        c.quit_action.trigger()
        c._request_quit()
        self.assertEqual(self.requests, [])
        self.assertTrue(c.tray.visible)

    def test_parent_destruction_cleans_active_monitor_without_stop(self):
        c = self.controller
        tray, menu, timer = c.tray, c.menu, c._timer
        self.assertTrue(timer.isActive())
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        for owned in (c, tray, menu, timer):
            self.assertFalse(isValid(owned))

    def test_initially_unsupported_desktop_can_gain_tray(self):
        self.controller.stop()
        self.supported = False
        c = SystemTrayController(self.window, tray_factory=FakeTray,
                                 availability=lambda: self.supported)
        self.controller = c
        self.assertFalse(c.tray.visible)
        self.assertFalse(c.hide_to_tray())
        self.supported = True
        c._timer.timeout.emit()
        self.assertTrue(c.tray.visible)
        self.assertTrue(c.hide_to_tray())

    def test_stop_disables_late_callbacks_and_parent_deletion_cleans_children(self):
        c = self.controller
        c.stop()
        self.assertFalse(c._timer.isActive())
        self.assertIsNone(c.tray.menu)
        self.window.hide()
        c._timer.timeout.emit()
        c.tray.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)
        c.restore()
        self.assertFalse(c.hide_to_tray())
        self.assertFalse(self.window.isVisible())
        self.assertFalse(c.tray.visible)
        self.assertEqual(self.requests, [])
        menu, tray, timer = c.menu, c.tray, c._timer
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        for owned in (c, menu, tray, timer):
            self.assertFalse(isValid(owned))


if __name__ == "__main__":
    unittest.main()
