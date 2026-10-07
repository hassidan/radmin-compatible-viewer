"""Unicode preservation at the UI-to-file-transfer boundary."""
import ntpath
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from unittest.mock import Mock, patch

from radmin_viewer.transfer_ui import FileTransferWindow, unique_upload_path
from radmin_viewer.filetransfer import _path, _path_field, _decode_name, _fields


class TransferFilenameTests(unittest.TestCase):
    def test_hebrew_spaces_and_other_unicode_survive_wire_encoding(self):
        for name in ("Example דוגמה - quarterly report.csv", "דו״ח שָׁלוֹם.xlsx",
                     "résumé 中文 😀.txt"):
            with self.subTest(name=name):
                remote = unique_upload_path("C:\\Users\\Demo\\Downloads", name)
                stem, extension = ntpath.splitext(name)
                self.assertTrue(ntpath.basename(remote).startswith("upload-" + stem + "-"))
                self.assertTrue(remote.endswith(extension))
                self.assertEqual(_path(remote, file=True), remote)
                # A path uses the same Unicode name payload format on the wire.
                payload = _fields(_path_field(remote))[0][1]
                self.assertEqual(payload[:-2].decode("utf-16-be"), remote)
                leaf_payload = _fields(_path_field(ntpath.basename(remote)))[0][1]
                self.assertEqual(_decode_name(leaf_payload), ntpath.basename(remote))

    def test_invalid_characters_cannot_become_paths_or_streams(self):
        name = 'שלום\\..\\evil:*?"<>|\x00\x01\x1f.txt'
        remote = unique_upload_path("C:\\Safe", name)
        leaf = ntpath.basename(remote)
        self.assertEqual(ntpath.dirname(remote), "C:\\Safe")
        self.assertIn("שלום", leaf)
        self.assertFalse(any(ch in leaf for ch in '\\/:*?"<>|\x00\x01\x1f'))
        self.assertEqual(_path(remote, file=True), remote)
        for name in ("CON.txt", "..", "trailing. ", "bad\udcff.txt"):
            self.assertEqual(_path(unique_upload_path("C:\\Safe", name), file=True).split("\\")[0], "C:")

    def test_long_names_keep_extension_and_valid_utf16_with_unique_suffix(self):
        for name in ("ש" * 300 + ".csv", "😀" * 200 + ".txt", "name." + "ק" * 300):
            remote = unique_upload_path("C:\\Safe", name)
            leaf = ntpath.basename(remote)
            self.assertLessEqual(len(leaf.encode("utf-16-le")) // 2, 255)
            self.assertEqual(leaf.encode("utf-16-le").decode("utf-16-le"), leaf)
            if name.endswith((".csv", ".txt")):
                self.assertEqual(ntpath.splitext(leaf)[1], ntpath.splitext(name)[1])
        self.assertNotEqual(unique_upload_path("C:\\Safe", "שלום.csv"),
                            unique_upload_path("C:\\Safe", "שלום.csv"))

    def test_upload_confirmation_and_worker_receive_same_hebrew_path(self):
        # Exercise upload routing without starting a Qt worker or touching remote files.
        window = Mock()
        window.remote_directory = "C:\\Users\\Demo\\Downloads"
        source = "/tmp/Example דוגמה - report.csv"
        window.local_model.filePath.return_value = source
        window.upload_button.isEnabled.return_value = True
        with patch("radmin_viewer.transfer_ui.confirm", return_value=True) as confirmation:
            FileTransferWindow.upload(window)
        kind, actual_source, remote = window.submit.call_args.args
        self.assertEqual((kind, actual_source), ("upload", source))
        self.assertIn("Example דוגמה - report-", remote)
        self.assertIn(remote, confirmation.call_args.args[2])
        window.submit.reset_mock()
        with patch("radmin_viewer.transfer_ui.confirm", return_value=False):
            FileTransferWindow.upload(window)
        window.submit.assert_not_called()
