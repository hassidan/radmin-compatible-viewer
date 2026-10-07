#!/usr/bin/env python3
"""Audit bundled ELF GLIBC requirements and runtime dependency resolution."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    root = args.bundle.resolve()
    libraries = [root / '_internal', root / '_internal/PySide6/Qt/lib', root / '_internal/PySide6',
                 root / '_internal/shiboken6']
    environment = dict(os.environ, LD_LIBRARY_PATH=':'.join(map(str, libraries)))
    records = []
    violations = []
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.is_symlink():
            continue
        with path.open('rb') as source:
            if source.read(4) != b'\x7fELF':
                continue
        symbols = subprocess.check_output(['readelf', '--version-info', str(path)], text=True)
        versions = [tuple(map(int, item.split('.'))) for item in re.findall(r'GLIBC_([0-9.]+)', symbols)]
        maximum = max(versions, default=(0, 0))
        dependencies = subprocess.run(['ldd', str(path)], env=environment, text=True, capture_output=True)
        missing = [line.strip() for line in dependencies.stdout.splitlines() if 'not found' in line]
        record = {'path': str(path.relative_to(root)), 'max_glibc': '.'.join(map(str, maximum)),
                  'missing_dependencies': missing}
        records.append(record)
        if maximum > (2, 36) or missing:
            violations.append(record)
    if not records:
        raise RuntimeError('No ELF files found')
    report = {'status': 'fail' if violations else 'ok', 'glibc_ceiling': '2.36',
              'elf_files': len(records), 'records': records, 'violations': violations}
    args.report.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key != 'records'}))
    if violations:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
