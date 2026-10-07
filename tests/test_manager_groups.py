"""Manager group actions, view synchronization and removal without data loss."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import copy
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QDialog

from radmin_viewer.app import MainWindow
from radmin_viewer.storage import AddressBook, defaults
from radmin_viewer.ui_dialogs import StyledComboBox, ConfirmationDialog


class ManagerGroupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        data = defaults()
        data["groups"] = ["General", "Office", "Empty"]
        data["connections"] = [
            {"id": str(uuid.uuid4()), "name": name, "host": "localhost", "port": 4899,
             "username": "", "group": group}
            for name, group in (("Local", "General"), ("Workstation", "Office"))]
        self.window = MainWindow(AddressBook(Path(self.root.name) / "book.json"), data)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.app.processEvents()
        self.root.cleanup()

    def test_plus_menu_and_no_menu_or_footer(self):
        w = self.window
        self.assertEqual(w.menuBar().actions(), [])
        self.assertTrue(w.menuBar().isHidden())
        self.assertTrue(w.statusBar().isHidden())
        self.assertFalse(hasattr(w, "scan_label"))
        self.assertEqual([a.text() for a in w.add_menu.actions()], ["Add computer", "Add group"])
        self.assertIsInstance(w.group_filter, StyledComboBox)
        self.assertEqual(set(w.grid._sections), {"General", "Office", "Empty"})

    def test_group_selection_and_collapse_survive_filter_and_scan_updates(self):
        w = self.window
        w.grid.setCurrentRow(1)
        self.assertIsNotNone(w.selected())
        w.grid.set_selection(group="Office", emit=True)
        self.assertIsNone(w.selected())
        self.assertEqual(w.selected_group_header(), "Office")
        self.assertEqual(w.edit_action.text(), "Rename group")
        self.assertTrue(w.edit_action.isEnabled())
        self.assertFalse(w.control_action.isEnabled())
        w.grid.set_group_collapsed("Office", True, emit=True)
        self.assertFalse(w._group_items["Office"].isExpanded())
        self.assertTrue(w._group_items["General"].isExpanded())
        w._group_items["General"].setExpanded(False)
        self.assertIn("General", w.grid.collapsed_groups)
        w.group_filter.setCurrentIndex(w.group_filter.findData("Office"))
        self.assertEqual(set(w.grid._sections), {"Office"})
        w.group_filter.setCurrentIndex(0)
        self.assertFalse(w._group_items["Office"].isExpanded())
        self.assertFalse(w._group_items["General"].isExpanded())
        w.scan_progress(1, 2)
        w.scan_finished(False)
        self.assertTrue(w.statusBar().isHidden())

    def test_rename_selected_header_changes_members_and_preserves_collapse(self):
        w = self.window
        w.grid.set_selection(group="Office", emit=True)
        w.grid.set_group_collapsed("Office", True, emit=True)
        with patch("radmin_viewer.app.ask_text", return_value=("Studio", True)):
            w.edit_action.trigger()
        self.assertNotIn("Office", w.data["groups"])
        self.assertIn("Studio", w.data["groups"])
        self.assertEqual(w.data["connections"][1]["group"], "Studio")
        self.assertEqual(w.selected_group_header(), "Studio")
        self.assertIn("Studio", w.grid.collapsed_groups)
        self.assertFalse(w._group_items["Studio"].isExpanded())
        self.assertEqual(w.store.load(), w.data)

    def test_add_group_from_plus_is_visible_when_another_group_is_filtered(self):
        w = self.window
        w.group_filter.setCurrentIndex(w.group_filter.findData("Office"))
        with patch("radmin_viewer.app.ask_text", return_value=("New site", True)):
            w.add_group_action.trigger()
        self.assertIn("New site", w.grid._sections)
        self.assertEqual(w.selected_group_header(), "New site")
        self.assertTrue(w.edit_action.isEnabled())
        self.assertTrue(w.remove_action.isEnabled())

    def test_remove_populated_group_moves_members_only_after_confirmation(self):
        w = self.window
        w.grid.set_selection(group="Office", emit=True)
        before = copy.deepcopy(w.data)
        with patch.object(ConfirmationDialog, "exec", return_value=QDialog.DialogCode.Rejected):
            w.remove_action.trigger()
        self.assertEqual(w.data, before)

        def accept(dialog):
            self.assertIn("will be moved", dialog.message_label.text())
            combo = dialog.findChild(StyledComboBox)
            self.assertIsNotNone(combo)
            combo.setCurrentText("General")
            return QDialog.DialogCode.Accepted
        with patch.object(ConfirmationDialog, "exec", accept):
            w.remove_action.trigger()
        self.assertNotIn("Office", w.data["groups"])
        self.assertEqual(len(w.data["connections"]), 2)
        self.assertEqual(w.data["connections"][1]["group"], "General")
        self.assertEqual(w.store.load(), w.data)

    def test_computer_delete_uses_modern_confirmation_and_correct_selection(self):
        w = self.window
        target = w.data["connections"][1]
        w.grid.set_selection(entry_id=target["id"], emit=True)
        with patch("radmin_viewer.app.confirm", return_value=False) as confirm:
            w.remove_action.trigger()
            self.assertIn("Workstation", confirm.call_args.args[2])
        self.assertEqual(len(w.data["connections"]), 2)
        with patch("radmin_viewer.app.confirm", return_value=True):
            w.remove_action.trigger()
        self.assertEqual(len(w.data["connections"]), 1)
        self.assertNotIn(target["id"], w._grid_by_id)
        self.assertTrue(w.grid._sections["Office"].title.isEnabled())
