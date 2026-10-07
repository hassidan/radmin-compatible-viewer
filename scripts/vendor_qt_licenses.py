#!/usr/bin/env python3
"""Explicit online provisioning of license texts omitted from Qt binary wheels.

Run once before offline native builds. Downloads exact upstream release archives,
retains license files and attribution records only, and records archive hashes.
"""
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
VERSION = '6.11.2'
EXCLUDED_PARTS = {'example', 'examples', 'test', 'tests', 'doc', 'docs',
                  'documentation', 'demo', 'demos', 'tool', 'tools'}
LEGAL_NAME = re.compile(r'^(?:licen[cs]e|copying|copyright|notice)(?:$|[._-].+)', re.I)
NON_TEXT_SUFFIXES = {'.png', '.jpg', '.jpeg', '.gif', '.ico', '.svg', '.pdf',
                     '.py', '.cpp', '.c', '.h', '.hpp', '.json', '.ui', '.qrc'}


def runtime_path(path):
    return not (set(part.lower() for part in path.parts[:-1]) & EXCLUDED_PARTS)


def license_candidate(path):
    if len(path.parts) == 2 and path.parts[0] == 'LICENSES' and path.suffix == '.txt':
        return True
    return (runtime_path(path) and bool(LEGAL_NAME.fullmatch(path.name))
            and path.suffix.lower() not in NON_TEXT_SUFFIXES)


def selected_members(archive):
    members = {m.name: m for m in archive.getmembers() if m.isfile()}
    selected = set()
    for name, member in members.items():
        path = PurePosixPath(name)
        relative = PurePosixPath(*path.parts[1:])
        if license_candidate(relative):
            selected.add(name)
        if relative.name == 'qt_attribution.json' and runtime_path(relative):
            selected.add(name)
            data = json.load(archive.extractfile(member), strict=False)
            for entry in data if isinstance(data, list) else [data]:
                references = entry.get('LicenseFile', '')
                if isinstance(references, str):
                    references = references.split()
                for license_file in references:
                    target = posixpath.normpath(str(path.parent / license_file))
                    if target not in members or PurePosixPath(target).parts[0] != path.parts[0]:
                        raise ValueError(f'Unresolved LicenseFile: {name}: {license_file}')
                    # An explicit attribution may designate a source header as its
                    # license text. Preserve those bytes without broad code copying.
                    selected.add(target)
    return members, selected


def managed_inventory(destination):
    if not destination.exists():
        return {}, []
    records = json.loads((destination / 'provenance.json').read_text(encoding='utf-8'))
    expected = {'provenance.json'}
    for record in records:
        for name in record['files']:
            path = PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts or path.parts[0] != record['repository']:
                raise ValueError(f'Unsafe provenance entry: {name}')
            if name in expected:
                raise ValueError(f'Duplicate provenance entry: {name}')
            expected.add(name)
    paths = list(destination.rglob('*'))
    if any(path.is_symlink() for path in paths):
        raise ValueError('Managed inventory contains symlinks; refusing replacement')
    actual = {path.relative_to(destination).as_posix() for path in paths if path.is_file()}
    if actual != expected:
        raise ValueError(f'Managed inventory mismatch: unexpected={sorted(actual - expected)}, '
                         f'missing={sorted(expected - actual)}')
    snapshot = {name: hashlib.sha256((destination / name).read_bytes()).hexdigest()
                for name in sorted(actual)}
    for record in records:
        for name, digest in record.get('file_sha256', {}).items():
            if snapshot.get(name) != digest:
                raise ValueError(f'Managed license hash mismatch: {name}')
    return snapshot, records


def main():
    destination = ROOT / 'packaging/licenses/qt'
    old_snapshot, old_records = managed_inventory(destination)
    print(f'Existing inventory verified: {len(old_snapshot)} files, exactly provenance.files + provenance.json', flush=True)
    with tempfile.TemporaryDirectory(prefix='qt-licenses-', dir=destination.parent) as temporary:
        staged = Path(temporary) / 'new'
        staged.mkdir()
        regenerate(staged, old_records)
        new_snapshot, records = managed_inventory(staged)
        if managed_inventory(destination)[0] != old_snapshot:
            raise RuntimeError('Managed inventory changed during download; refusing replacement')
        backup = Path(temporary) / 'old'
        if destination.exists():
            destination.rename(backup)
        try:
            staged.rename(destination)
        except BaseException:
            if backup.exists():
                backup.rename(destination)
            raise
        if backup.exists():
            if managed_inventory(backup)[0] != old_snapshot:
                raise RuntimeError('Backup inventory changed; refusing deletion')
            shutil.rmtree(backup)
    print(f'Regenerated {len(new_snapshot) - 1} retained files + provenance.json: '
          + ', '.join(f"{r['repository']}={len(r['files'])}" for r in records))


def regenerate(destination, old_records):
    records = []
    for repository in ('qtbase', 'qtsvg', 'qtimageformats', 'qtwayland', 'pyside-setup'):
        organization = 'pyside' if repository == 'pyside-setup' else 'qt'
        url = f'https://codeload.github.com/{organization}/{repository}/tar.gz/refs/tags/v{VERSION}'
        print(f'Downloading {url}', flush=True)
        with urllib.request.urlopen(url, timeout=180) as response:
            payload = response.read()
        archive_digest = hashlib.sha256(payload).hexdigest()
        previous = next((r for r in old_records if r['repository'] == repository), None)
        if previous and (previous['source_url'] != url or previous['archive_sha256'] != archive_digest):
            raise ValueError(f'Upstream source identity changed for {repository}')
        with tarfile.open(fileobj=io.BytesIO(payload), mode='r:gz') as archive:
            members, selected = selected_members(archive)
            retained = []
            hashes = {}
            for name in sorted(selected):
                relative = PurePosixPath(*PurePosixPath(name).parts[1:])
                if '..' in relative.parts or relative.is_absolute():
                    raise ValueError(f'Unsafe archive path: {name}')
                target = destination / repository / str(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                data = archive.extractfile(members[name]).read()
                target.write_bytes(data)
                output_name = target.relative_to(destination).as_posix()
                retained.append(output_name)
                hashes[output_name] = hashlib.sha256(data).hexdigest()
        records.append({'repository': repository, 'version': VERSION, 'source_url': url,
                        'archive_sha256': archive_digest, 'files': retained, 'file_sha256': hashes})
    (destination / 'provenance.json').write_text(json.dumps(records, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
