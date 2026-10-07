import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QFrame

from radmin_viewer.grouped_tiles import GroupedComputerView
from radmin_viewer.ui_icons import apply_theme


def entries(prefix, count):
    return [{"id": f"{prefix}{i}", "name": f"Computer {i}",
             "host": f"host-{i}", "port": 4899, "username": "", "group": prefix}
            for i in range(count)]


class GroupedTilesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        apply_theme(cls.app)

    def setUp(self):
        self.view = GroupedComputerView()
        self.view.resize(540, 500)
        self.groups = [("Office", entries("a", 7)), ("Lab", entries("b", 3)), ("Empty", [])]
        self.view.set_groups(self.groups)
        self.view.show()
        self.settle()

    def tearDown(self):
        self.view.close()
        self.view.deleteLater()
        self.settle()

    def settle(self):
        for _ in range(8):
            self.app.processEvents()
        QTest.qWait(10)

    def click_item(self, entry_id):
        item = self.view.items_by_id[entry_id]
        tiles = item.listWidget()
        QTest.mouseClick(tiles.viewport(), Qt.MouseButton.LeftButton,
                         pos=tiles.visualItemRect(item).center())
        self.settle()

    def test_one_selection_across_groups_and_title_clears_stale_computer(self):
        spy = QSignalSpy(self.view.selection_changed)
        self.click_item("a0")
        self.assertEqual(spy.at(0), ["a0", "Office"])
        self.click_item("b1")
        self.assertEqual(spy.at(1), ["b1", "Lab"])
        self.assertEqual(self.view._sections["Office"].tiles.selectedItems(), [])
        self.assertIsNone(self.view._sections["Office"].tiles.currentItem())
        QTest.mouseClick(self.view._sections["Office"].title, Qt.MouseButton.LeftButton)
        self.assertEqual(spy.at(2), ["", "Office"])
        self.assertIsNone(self.view.currentItem())
        for section in self.view._sections.values():
            self.assertEqual(section.tiles.selectedItems(), [])
            self.assertIsNone(section.tiles.currentItem())
        self.assertTrue(self.view._sections["Office"].title.isChecked())
        QTest.mouseClick(self.view._sections["Office"].header, Qt.MouseButton.LeftButton)
        self.settle()
        self.click_item("a1")
        self.assertFalse(self.view._sections["Office"].title.isChecked())
        self.assertEqual(spy.at(3), ["a1", "Office"])

    def test_silent_sync_and_reentrant_signal_do_not_loop(self):
        spy = QSignalSpy(self.view.selection_changed)
        self.view.selection_changed.connect(
            lambda entry_id, group: self.view.set_selection(entry_id, group, emit=True))
        self.view.set_selection("a0")
        self.assertEqual(spy.count(), 0)
        self.view.set_selection("b0", emit=True)
        self.assertEqual(spy.count(), 1)
        self.view.set_selection("b0", emit=True)
        self.assertEqual(spy.count(), 1)
        self.view.set_selection(group="Empty", emit=True)
        self.assertEqual(spy.at(1), ["", "Empty"])
        self.view.set_selection(emit=True)
        self.assertEqual(spy.at(2), ["", ""])

    def test_child_deselection_clears_overall_selection(self):
        self.view.set_selection("a0")
        spy = QSignalSpy(self.view.selection_changed)
        self.view.items_by_id["a0"].listWidget().clearSelection()
        self.assertIsNone(self.view.currentItem())
        self.assertEqual(spy.count(), 1)
        self.assertEqual(spy.at(0), ["", ""])

    def test_long_header_does_not_force_a_wide_content_widget(self):
        name = "A very long group title " * 20
        self.view.set_groups([(name, entries("a", 3)), ("Empty", [])])
        self.view.resize(260, 400)
        self.settle()
        self.assertEqual(self.view.widget().width(), self.view.viewport().width())
        section = self.view._sections[name]
        self.assertIn(name, section.title.accessibleName())
        self.assertIn("3", section.title.text())
        self.assertFalse(section.header.icon().isNull())
        self.view.set_group_collapsed(name, True)
        self.assertFalse(section.header.icon().isNull())

    def test_independent_collapse_survives_rebuild_filter_clear_and_resize(self):
        spy = QSignalSpy(self.view.group_collapsed)
        selection = QSignalSpy(self.view.selection_changed)
        self.view.set_selection(group="Office")
        QTest.mouseClick(self.view._sections["Office"].header, Qt.MouseButton.LeftButton)
        self.assertEqual(spy.at(0), ["Office", True])
        self.assertEqual(selection.count(), 0)
        self.assertTrue(self.view._sections["Office"].title.isChecked())
        self.assertTrue(self.view._sections["Office"].tiles.isHidden())
        self.assertFalse(self.view._sections["Lab"].tiles.isHidden())
        self.view.resize(320, 350)
        self.view.set_groups(self.groups)
        self.settle()
        self.assertTrue(self.view._sections["Office"].title.isChecked())
        self.view.set_groups([self.groups[1]])
        self.view.set_group_collapsed("Lab", True)
        self.view.clear()
        self.assertEqual(self.view.collapsed_groups, {"Office", "Lab"})
        self.view.set_groups(self.groups)
        self.view.set_group_collapsed("Office", False, emit=True)
        self.settle()
        self.assertFalse(self.view._sections["Office"].tiles.isHidden())
        self.assertTrue(self.view._sections["Lab"].tiles.isHidden())
        self.assertEqual(spy.count(), 2)
        self.assertEqual(spy.at(1), ["Office", False])

    def test_exact_wrapped_rows_and_compact_empty_or_collapsed_sections(self):
        for width in (800, 330, 650, 200, 540):
            self.view.resize(width, 400)
            self.settle()
            for section in self.view._sections.values():
                tiles = section.tiles
                self.assertEqual(tiles.frameShape(), QFrame.Shape.NoFrame)
                self.assertEqual(tiles.verticalScrollBarPolicy(), Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                self.assertEqual(tiles.horizontalScrollBarPolicy(), Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                if tiles.count():
                    rects = [tiles.visualItemRect(tiles.item(i)) for i in range(tiles.count())]
                    rows = len({r.top() // tiles.gridSize().height() for r in rects})
                    self.assertEqual(tiles.height(), rows * tiles.gridSize().height())
                    self.assertTrue(all(r.left() >= 0 and r.right() < tiles.viewport().width()
                                        for r in rects))
                    self.assertEqual(tiles.verticalScrollBar().maximum(), 0)
                else:
                    self.assertTrue(tiles.isHidden())
                    self.assertEqual(section.height(), section.header.height())
            self.assertEqual(self.view.horizontalScrollBar().maximum(), 0)
        self.assertGreater(self.view.verticalScrollBar().maximum(), 0)
        self.view.set_group_collapsed("Office", True)
        self.settle()
        office = self.view._sections["Office"]
        self.assertEqual(office.height(), office.header.height())

    def test_keyboard_headers_and_computer_activation(self):
        section = self.view._sections["Office"]
        selected = QSignalSpy(self.view.selection_changed)
        activated = QSignalSpy(self.view.activated)
        section.title.setFocus()
        QTest.keyClick(section.title, Qt.Key.Key_Return)
        self.assertEqual(selected.at(0), ["", "Office"])
        QTest.keyClick(section.title, Qt.Key.Key_Left)
        self.assertIn("Office", self.view.collapsed_groups)
        self.assertTrue(section.title.isChecked())
        QTest.keyClick(section.title, Qt.Key.Key_Right)
        self.assertNotIn("Office", self.view.collapsed_groups)
        QTest.keyClick(section.header, Qt.Key.Key_Space)
        self.assertIn("Office", self.view.collapsed_groups)
        QTest.keyClick(section.header, Qt.Key.Key_Return)
        self.assertNotIn("Office", self.view.collapsed_groups)
        self.settle()
        self.view.set_selection("a0")
        section.tiles.setFocus()
        QTest.keyClick(section.tiles, Qt.Key.Key_Return)
        self.assertEqual(activated.count(), 1)
        self.assertEqual(activated.at(0), ["a0"])

    def test_double_click_activates_exactly_once(self):
        activated = QSignalSpy(self.view.activated)
        item = self.view.items_by_id["a0"]
        tiles = item.listWidget()
        point = tiles.visualItemRect(item).center()
        QTest.mouseClick(tiles.viewport(), Qt.MouseButton.LeftButton, pos=point)
        QTest.mouseDClick(tiles.viewport(), Qt.MouseButton.LeftButton, pos=point)
        self.assertEqual(activated.count(), 1)
        self.assertEqual(activated.at(0), ["a0"])

    def test_whole_header_clicks_toggle_without_a_separate_arrow_button(self):
        from PySide6.QtWidgets import QToolButton
        section = self.view._sections["Office"]
        self.assertEqual(section.header.findChildren(QToolButton), [])
        self.assertEqual(section.header.toolTip(), "")
        for point in (QPoint(section.header.width() - 12, section.header.height() // 2), QPoint(16, 16)):
            before = "Office" in self.view.collapsed_groups
            QTest.mouseClick(section.header, Qt.MouseButton.LeftButton, pos=point)
            self.settle()
            self.assertNotEqual("Office" in self.view.collapsed_groups, before)
            self.assertIsNone(self.view.currentItem())
            self.assertTrue(section.title.isChecked())

    def test_context_selects_target_and_reports_global_position(self):
        spy = QSignalSpy(self.view.context_requested)
        section = self.view._sections["Lab"]
        item = self.view.items_by_id["b0"]
        point = section.tiles.visualItemRect(item).center()
        section.tiles.customContextMenuRequested.emit(point)
        self.assertEqual(spy.at(0), ["b0", "Lab", section.tiles.viewport().mapToGlobal(point)])
        self.assertIs(self.view.currentItem(), item)
        point = QPoint(12, 12)
        section.title.customContextMenuRequested.emit(point)
        self.assertEqual(spy.at(1), ["", "Lab", section.title.mapToGlobal(point)])
        self.assertIsNone(self.view.currentItem())
        self.assertTrue(section.title.isChecked())

    def test_scan_item_updates_survive_geometry_collapse_and_rebuild(self):
        item = self.view.items_by_id["a0"]
        pixmap = QPixmap(72, 72)
        pixmap.fill(QColor("red"))
        item.setIcon(QIcon(pixmap))
        cache_key = item.icon().cacheKey()
        item.setToolTip("Online: freshly scanned")
        item.setData(Qt.ItemDataRole.AccessibleDescriptionRole, "Office computer online")
        item.setData(Qt.ItemDataRole.UserRole + 9, {"status": "online"})
        self.view.set_selection("a0")
        self.view.resize(320, 400)
        self.view.set_group_collapsed("Office", True)
        self.view.set_groups(self.groups)
        self.view.set_group_collapsed("Office", False)
        self.settle()
        self.assertIs(self.view.items_by_id["a0"], item)
        self.assertIs(self.view.currentItem(), item)
        self.assertEqual(item.icon().cacheKey(), cache_key)
        self.assertEqual(item.toolTip(), "Online: freshly scanned")
        self.assertEqual(item.data(Qt.ItemDataRole.AccessibleDescriptionRole), "Office computer online")
        self.assertEqual(item.data(Qt.ItemDataRole.UserRole + 9), {"status": "online"})

    def test_flattened_convenience_api_and_missing_selection(self):
        self.assertEqual(self.view.count(), 10)
        self.assertIs(self.view.item(7), self.view.items_by_id["b0"])
        self.view.setCurrentRow(8)
        self.assertIs(self.view.currentItem(), self.view.items_by_id["b1"])
        self.view.setCurrentItem(self.view.item(0))
        self.assertIs(self.view.currentItem(), self.view.items_by_id["a0"])
        self.view.setCurrentRow(-1)
        self.assertIsNone(self.view.currentItem())
        self.view.set_selection("a0")
        self.view.set_groups([self.groups[1]])
        self.assertIsNone(self.view.currentItem())
        self.assertFalse(self.view._sections["Lab"].title.isChecked())
        self.assertIsNone(self.view.item(999))


if __name__ == "__main__":
    unittest.main()
