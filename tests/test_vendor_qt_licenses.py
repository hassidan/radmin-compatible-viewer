"""Regression checks for curated legal material selection and managed replacement."""
import io
import json
from pathlib import Path, PurePosixPath
import tarfile
import tempfile
import unittest

from scripts import vendor_qt_licenses as vendor


class LicenseSelectionTests(unittest.TestCase):
    def test_legal_names_and_spdx(self):
        for name in ('COPYING', 'LICENSE.txt', 'copyright', 'NOTICE-THIRD-PARTY',
                     'LICENSES/LGPL-3.0-only.txt', 'LICENSES/Qt-GPL-exception-1.0.txt',
                     'src/thirdparty/library/COPYRIGHT'):
            self.assertTrue(vendor.license_candidate(PurePosixPath(name)), name)
        for name in ('licensewizard.py', 'licensewizard.cpp', 'licensewizard.png',
                     'licenseRule.json', 'LICENSE.png', 'examples/LICENSE',
                     'tests/subdir/COPYING', 'doc/NOTICE', 'src/tools/demo/LICENSE'):
            self.assertFalse(vendor.license_candidate(PurePosixPath(name)), name)

    def test_runtime_attribution_preserves_only_explicit_header(self):
        payload = io.BytesIO()
        files = {
            'root/src/thirdparty/lib/qt_attribution.json': b'{"LicenseFile":"header.h"}',
            'root/src/thirdparty/lib/header.h': b'/* upstream license */\r\n',
            'root/src/thirdparty/lib/unrelated.h': b'code',
            'root/examples/qt_attribution.json': b'{"LicenseFile":"example.cpp"}',
            'root/examples/example.cpp': b'example',
            'root/LICENSES/LGPL-3.0-only.txt': b'LGPL text',
        }
        with tarfile.open(fileobj=payload, mode='w') as archive:
            for name, data in files.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        payload.seek(0)
        with tarfile.open(fileobj=payload) as archive:
            members, selected = vendor.selected_members(archive)
            self.assertEqual(selected, {'root/src/thirdparty/lib/qt_attribution.json',
                                        'root/src/thirdparty/lib/header.h',
                                        'root/LICENSES/LGPL-3.0-only.txt'})
            self.assertEqual(archive.extractfile(members['root/src/thirdparty/lib/header.h']).read(),
                             files['root/src/thirdparty/lib/header.h'])

    def test_inventory_refuses_unrelated_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'provenance.json').write_text('[]')
            self.assertEqual(len(vendor.managed_inventory(root)[0]), 1)
            (root / 'unrelated.txt').write_text('preserve me')
            with self.assertRaisesRegex(ValueError, 'inventory mismatch'):
                vendor.managed_inventory(root)
            self.assertTrue((root / 'unrelated.txt').is_file())

    def test_real_inventory_and_required_texts(self):
        root = vendor.ROOT / 'packaging/licenses/qt'
        snapshot, records = vendor.managed_inventory(root)
        self.assertEqual(len(snapshot), 1 + sum(len(r['files']) for r in records))
        for record in records:
            self.assertEqual(set(record['files']), set(record['file_sha256']))
            self.assertIn(record['repository'] + '/LICENSES/LGPL-3.0-only.txt', snapshot)
            self.assertIn(record['repository'] + '/LICENSES/GPL-3.0-only.txt', snapshot)
        self.assertIn('qtbase/LICENSES/Qt-GPL-exception-1.0.txt', snapshot)
        for name in snapshot:
            self.assertNotIn('licensewizard', name.lower())
            self.assertNotIn(PurePosixPath(name).suffix.lower(), ('.png', '.py', '.cpp'))
            self.assertTrue(vendor.runtime_path(PurePosixPath(name)), name)


if __name__ == '__main__':
    unittest.main()
