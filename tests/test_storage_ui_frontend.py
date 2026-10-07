import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from radmin_viewer.storage import AddressBook, StorageError, defaults, validate


def entry():
    return {"id": str(uuid.uuid4()), "name": "Lab", "host": "::1", "port": 4899,
            "username": "", "group": "General"}


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AddressBook(Path(self.temp.name) / "connections.json")
        self.data = defaults()
        self.data["connections"].append(entry())

    def test_roundtrip_and_missing(self):
        self.assertEqual(self.store.load(), defaults())
        self.store.save(self.data)
        self.assertEqual(self.store.load(), self.data)
        self.assertNotIn("password", self.store.path.read_text())
        if os.name == "posix":
            self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_atomic_failure_keeps_original(self):
        self.store.save(self.data)
        changed = copy.deepcopy(self.data)
        changed["connections"][0]["name"] = "Changed"
        with patch("radmin_viewer.storage.os.replace", side_effect=OSError("test failure")):
            with self.assertRaises(StorageError):
                self.store.save(changed)
        self.assertEqual(self.store.load(), self.data)
        self.assertEqual(list(self.store.path.parent.glob(".address-book-*")), [])

    def test_reject_credentials_and_bad_values(self):
        for field, value in (("password", "test-only"), ("port", True), ("port", 65536),
                             ("host", "https://host"), ("host", "bad host"),
                             ("group", "missing"), ("id", "bad"), ("username", "\0")):
            with self.subTest(field=field, value=value):
                data = copy.deepcopy(self.data)
                data["connections"][0][field] = value
                with self.assertRaises(StorageError):
                    validate(data)

    def test_corrupt_and_duplicate_json_preserved(self):
        for raw in ('{"version":1,"version":1}', 'not json', '[]'):
            self.store.path.write_text(raw)
            with self.assertRaises(StorageError):
                self.store.load()
            self.assertEqual(self.store.path.read_text(), raw)

    def test_unknown_version_and_duplicate_ids(self):
        data = copy.deepcopy(self.data)
        data["version"] = 2
        with self.assertRaises(StorageError):
            validate(data)
        self.data["connections"].append(copy.deepcopy(self.data["connections"][0]))
        with self.assertRaises(StorageError):
            validate(self.data)


if __name__ == "__main__":
    unittest.main()
