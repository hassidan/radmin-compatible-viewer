import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import QApplication

from radmin_viewer.application_identity import APP_NAME, APP_ID, configure_application, install_linux_launcher
from radmin_viewer.ui_icons import application_icon


class ApplicationIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_brand_icon_and_identity_preserve_address_book_location(self):
        app = self.app
        old_name, old_org = app.applicationName(), app.organizationName()
        old_display, old_desktop, old_icon = app.applicationDisplayName(), app.desktopFileName(), app.windowIcon()
        try:
            app.setApplicationName(APP_NAME)
            app.setOrganizationName("IndependentViewer")
            before = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)
            configure_application(app)
            self.assertEqual(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation), before)
            self.assertEqual(app.desktopFileName(), APP_ID)
            self.assertEqual(app.applicationDisplayName(), APP_NAME)
            self.assertFalse(app.windowIcon().isNull())
            for size in (16, 32, 64, 128, 256):
                image = application_icon().pixmap(size, size).toImage()
                self.assertEqual(image.width(), size)
                self.assertGreater(image.pixelColor(size // 2, size // 2).alpha(), 0)
                self.assertEqual(image.pixelColor(0, 0).alpha(), 0)
        finally:
            app.setApplicationName(old_name)
            app.setOrganizationName(old_org)
            app.setApplicationDisplayName(old_display)
            app.setDesktopFileName(old_desktop)
            app.setWindowIcon(old_icon)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux desktop entries")
    def test_launcher_icon_install_update_and_protect_unmanaged_entry(self):
        with tempfile.TemporaryDirectory() as root:
            data = Path(root) / "data with spaces"
            executable = Path(root) / "viewer %f" / "python"
            working = Path(root) / "source files"
            path = install_linux_launcher(data_home=data, executable=executable, working_directory=working)
            text = path.read_text()
            self.assertIn(f'Exec="{str(executable).replace("%", "%%")}" -m radmin_viewer', text)
            self.assertIn(f"Path={working}", text)
            icon_path = data / "icons/hicolor/scalable/apps" / (APP_ID + ".svg")
            self.assertTrue(icon_path.is_file())
            self.assertIn(str(icon_path), text)
            self.assertNotIn("autostart", text)
            self.assertEqual(install_linux_launcher(data_home=data), path)
            path.write_text("[Desktop Entry]\nName=User-owned entry\n")
            with self.assertRaises(OSError):
                install_linux_launcher(data_home=data)
            self.assertEqual(path.read_text(), "[Desktop Entry]\nName=User-owned entry\n")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux desktop entries")
    def test_launcher_rejects_line_break_injection(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                install_linux_launcher(data_home=Path(root), executable="/tmp/python\nExec=bad")
            self.assertFalse((Path(root) / "applications").exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux desktop entries")
    def test_frozen_launcher_uses_the_app_binary_without_python_arguments(self):
        with tempfile.TemporaryDirectory() as root:
            executable = Path(root) / "portable app" / APP_ID
            with patch.object(sys, "frozen", True, create=True):
                path = install_linux_launcher(data_home=Path(root) / "data", executable=executable)
            entry = path.read_text()
            self.assertIn(f'Exec="{executable}"\n', entry)
            self.assertNotIn(" -m radmin_viewer", entry)
            self.assertIn(f"Path={executable.parent}\n", entry)
