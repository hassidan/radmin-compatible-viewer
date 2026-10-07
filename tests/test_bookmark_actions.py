import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import copy
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from PySide6.QtCore import QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionToolButton

from radmin_viewer.app import MainWindow
from radmin_viewer.bookmarks import merge_bookmarks
from radmin_viewer.rpb import dumps_rpb, read_rpb
from radmin_viewer.storage import AddressBook, defaults
from radmin_viewer.ui_icons import apply_theme


def record(name="Machine", group="Lab"):
    return {"id": str(uuid.uuid4()), "name": name, "host": "example.test", "port": 4899,
            "username": "user", "group": group}


class BookmarkActionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        apply_theme(cls.app)

    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.directory = Path(self.root.name)
        self.window = MainWindow(AddressBook(self.directory / "book.json"), defaults())

    def tearDown(self):
        self.window.close()
        self.app.processEvents()
        self.root.cleanup()

    def test_merge_preserves_settings_and_local_edits_and_skips_duplicates(self):
        current = defaults()
        current["groups"] = ["Lab"]
        entry = record()
        current["connections"] = [entry]
        current["settings"]["timeout"] = 27
        imported = {"groups": ["Lab", "Empty"], "connections": [dict(entry), dict(entry, name="Other")]}
        # Parsed artifacts have unique IDs; this case collides only with local data.
        imported["connections"][0]["id"] = str(uuid.uuid4())
        before = copy.deepcopy(current)
        merged, summary = merge_bookmarks(current, imported)
        self.assertEqual(current, before)
        self.assertEqual(merged["settings"], current["settings"])
        self.assertEqual((summary.computers, summary.groups, summary.duplicates), (1, 1, 1))
        self.assertEqual(merged["connections"][0], entry)
        self.assertNotEqual(merged["connections"][1]["id"], entry["id"])
        again, result = merge_bookmarks(merged, imported)
        self.assertEqual(again, merged)
        self.assertEqual(result.computers, 0)
        self.assertEqual(result.duplicates, 2)

    def test_import_confirmation_cancel_merge_and_repeat(self):
        source = self.directory / "native.rpb"
        source.write_bytes(dumps_rpb({"groups": ["研究"], "connections": [record("Computer 🙂", "研究")]}))
        before = source.read_bytes()
        with patch("radmin_viewer.app.QFileDialog.getOpenFileName", return_value=(str(source), "")):
            with patch("radmin_viewer.app.confirm", return_value=False):
                self.window.import_bookmarks()
            self.assertEqual(self.window.data, defaults())
            with patch("radmin_viewer.app.confirm", return_value=True):
                self.window.import_bookmarks()
            self.assertEqual(len(self.window.data["connections"]), 1)
            self.assertEqual(self.window.data["connections"][0]["name"], "Computer 🙂")
            self.assertIn("研究", self.window.grid._sections)
            with patch.object(self.window, "notify") as notice:
                self.window.import_bookmarks()
                self.assertIn("No new bookmarks", notice.call_args.args[0])
            self.assertEqual(len(self.window.data["connections"]), 1)
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(self.window.store.load(), self.window.data)

    def test_invalid_import_does_not_change_address_book(self):
        source = self.directory / "bad.rpb"
        source.write_bytes(b"not a Radmin phonebook")
        with patch("radmin_viewer.app.QFileDialog.getOpenFileName", return_value=(str(source), "")), \
                patch("radmin_viewer.app.inform") as message:
            self.window.import_bookmarks()
        message.assert_called_once()
        self.assertEqual(self.window.data, defaults())
        self.assertFalse(self.window.store.path.exists())

    def test_export_binary_roundtrip_and_cancel_overwrite(self):
        self.window.data["connections"].append(record("Exported", "General"))
        self.window.refresh()
        stem = self.directory / "exported"
        destination = stem.with_suffix(".rpb")
        with patch("radmin_viewer.app.QFileDialog.getSaveFileName", return_value=(str(stem), "")), \
                patch.object(self.window, "notify"):
            self.window.export_bookmarks()
        parsed = read_rpb(destination)
        self.assertEqual(parsed["connections"][0]["name"], "Exported")
        self.assertEqual(parsed["connections"][0]["port"], 4899)
        before = destination.read_bytes()
        self.window.data["connections"][0]["name"] = "Changed"
        with patch("radmin_viewer.app.QFileDialog.getSaveFileName", return_value=(str(stem), "")), \
                patch("radmin_viewer.app.confirm", return_value=False):
            self.window.export_bookmarks()
        self.assertEqual(destination.read_bytes(), before)

    def test_bookmark_actions_available_without_a_selected_computer(self):
        self.assertIsNone(self.window.selected())
        for menu in (self.window.computer_menu, self.window.group_menu):
            self.assertIn(self.window.import_action, menu.actions())
            self.assertIn(self.window.export_action, menu.actions())
        self.assertTrue(self.window.import_action.isEnabled())
        self.assertTrue(self.window.export_action.isEnabled())

    def test_split_arrow_hover_is_darker_than_main_button_hover(self):
        self.window.show()
        self.app.processEvents()
        button = self.window.add_button
        option = QStyleOptionToolButton()
        button.initStyleOption(option)
        arrow = button.style().subControlRect(QStyle.ComplexControl.CC_ToolButton, option,
                                             QStyle.SubControl.SC_ToolButtonMenu, button)
        QTest.mouseMove(button, QPoint(8, button.height() // 2))
        self.app.processEvents()
        normal_hover = button.grab().toImage().pixelColor(4, button.height() // 2)
        QTest.mouseMove(button, arrow.center())
        self.app.processEvents()
        menu_hover = button.grab().toImage().pixelColor(arrow.center().x(), arrow.top() + 6)
        self.assertLess(menu_hover.red() + menu_hover.green() + menu_hover.blue(),
                        normal_hover.red() + normal_hover.green() + normal_hover.blue())
