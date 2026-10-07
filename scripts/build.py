#!/usr/bin/env python3
"""Offline native packaging CLI; Docker mode provisions an isolated Linux builder."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
NAME = 'radmin-compatible-viewer'
VERSION = '0.1.0'


def run(*args, **kwargs):
    subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def assets(destination):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtCore import Qt
    from PIL import Image
    app = QApplication.instance() or QApplication([])
    renderer = QSvgRenderer(str(ROOT / 'radmin_viewer/assets/app-icon.svg'))
    if not renderer.isValid():
        raise RuntimeError('Invalid application SVG')
    image = QImage(1024, 1024, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    png = destination / 'app-icon.png'
    if not image.save(str(png)):
        raise RuntimeError('Failed to render icon')
    with Image.open(png) as icon:
        icon.save(destination / 'app-icon.ico', sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])


def licenses(destination):
    """Retain license payloads, including dist-info/licenses and Qt module licenses."""
    directory = destination / 'licenses'
    directory.mkdir(parents=True, exist_ok=True)
    manifest = []
    qt_licenses = ROOT / 'packaging/licenses/qt'
    if not (qt_licenses / 'provenance.json').is_file():
        raise RuntimeError('Missing Qt license materials: run python scripts/vendor_qt_licenses.py before offline building')
    shutil.copytree(qt_licenses, directory / 'qt-upstream')
    manifest.append({'distribution': 'Qt/PySide upstream license supplements', 'version': '6.11.2',
                     'license_files': ['qt-upstream/provenance.json'],
                     'scope': 'matching upstream sources; provenance inventories retained texts'})
    python_license = Path(sysconfig.get_path('stdlib')) / 'LICENSE.txt'
    if not python_license.is_file():
        python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if python_license.is_file():
        shutil.copy2(python_license, directory / 'Python-LICENSE.txt')
        manifest.append({'distribution': 'Python', 'version': platform.python_version(),
                         'license_files': ['Python-LICENSE.txt'], 'scope': 'bundled runtime'})
    for dist in sorted(metadata.distributions(), key=lambda d: d.metadata['Name'].lower()):
        name = dist.metadata['Name']
        copied = []
        for item in dist.files or []:
            parts = Path(str(item)).parts
            if '..' in parts or Path(str(item)).is_absolute():
                continue
            if not any(p.lower().startswith(('license', 'licence', 'copying', 'copyright', 'notice')) for p in parts):
                continue
            source = Path(dist.locate_file(item))
            if source.suffix in ('.py', '.pyc', '.so', '.pyd'):
                continue
            if not source.is_file():
                continue
            target = directory / name / str(item)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(str(target.relative_to(directory)).replace('\\', '/'))
        manifest.append({'distribution': name, 'version': dist.version,
                         'license_files': copied,
                         'scope': 'installed build environment; not all distributions are shipped runtime code'})
    if sys.platform.startswith('linux'):
        # PyInstaller also collects system shared libraries; retain their Debian notices.
        for copyright_file in Path('/usr/share/doc').glob('*/copyright'):
            if copyright_file.is_file():
                target = directory / 'debian' / copyright_file.parent.name / 'copyright'
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(copyright_file, target)
        common = Path('/usr/share/common-licenses')
        if common.is_dir():
            shutil.copytree(common, directory / 'debian/common-licenses', symlinks=False)
        manifest.append({'distribution': 'Debian build environment native libraries',
                         'version': platform.libc_ver()[1],
                         'license_files': ['debian/'],
                         'scope': 'system package notices; superset of collected native libraries'})
    (directory / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')


def linux_installers(output, generated, bundle):
    stem = f'{NAME}-{VERSION}-linux-x86_64'
    portable = generated / 'portable'
    portable.mkdir()
    shutil.copytree(bundle, portable / NAME, symlinks=True)
    shutil.copy2(ROOT / 'packaging/linux/install.sh', portable / 'install.sh')
    (portable / 'install.sh').chmod(0o755)
    with tarfile.open(output / f'{stem}.tar.gz', 'w:gz') as archive:
        archive.add(portable, arcname=stem)
    deb = generated / 'deb'
    (deb / 'DEBIAN').mkdir(parents=True)
    shutil.copytree(bundle, deb / 'opt' / NAME, symlinks=True)
    (deb / 'usr/bin').mkdir(parents=True)
    (deb / 'usr/bin' / NAME).symlink_to('/opt/' + NAME + '/' + NAME)
    applications = deb / 'usr/share/applications'
    applications.mkdir(parents=True)
    (applications / f'{NAME}.desktop').write_text(
        '[Desktop Entry]\nType=Application\nName=Radmin Compatible Viewer\n'
        f'Exec=/opt/{NAME}/{NAME}\nIcon={NAME}\nTerminal=false\n'
        'StartupWMClass=Radmin Compatible Viewer\nCategories=Network;RemoteAccess;\n', encoding='utf-8')
    icons = deb / 'usr/share/icons/hicolor/scalable/apps'
    icons.mkdir(parents=True)
    shutil.copy2(ROOT / 'radmin_viewer/assets/app-icon.svg', icons / f'{NAME}.svg')
    size = sum(p.stat().st_size for p in deb.rglob('*') if p.is_file()) // 1024
    (deb / 'DEBIAN/control').write_text(
        f'Package: {NAME}\nVersion: {VERSION}\nArchitecture: amd64\n'
        'Maintainer: IndependentViewer\nSection: net\nPriority: optional\n'
        f'Installed-Size: {size}\n'
        'Depends: libc6 (>= 2.36), libstdc++6, libgl1, libegl1, libopengl0, '
        'libglib2.0-0, libdbus-1-3, libfontconfig1, libfreetype6, libx11-6, '
        'libx11-xcb1, libxcb1, libxcb-cursor0, libxcb-icccm4, libxcb-image0, '
        'libxcb-keysyms1, libxcb-render-util0, libxcb-shape0, libxcb-xfixes0, '
        'libxcb-randr0, libxcb-sync1, libxcb-xinerama0, libxcb-xkb1, '
        'libxkbcommon0, libxkbcommon-x11-0, libsm6, libice6, libxext6, libxrender1, '
        'libwayland-client0, libwayland-cursor0, libwayland-egl1, fonts-dejavu-core\n'
        'Recommends: gnome-keyring, dbus-user-session\n'
        'Description: Independent Radmin-compatible viewer\n'
        ' Remote desktop, terminal and file transfer frontend.\n', encoding='utf-8')
    run('dpkg-deb', '--root-owner-group', '--build', deb, output / f'{stem}.deb')


def container_build(output):
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='viewer-docker-') as temporary:
        context = Path(temporary)
        for name in ('radmin_viewer', 'packaging', 'scripts', 'docs'):
            shutil.copytree(ROOT / name, context / name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for name in ('requirements-build.txt', 'LICENSE', 'NOTICE', 'THIRD_PARTY_NOTICES.md', 'README.md'):
            shutil.copy2(ROOT / name, context / name)
        run('docker', 'build', '--platform', 'linux/amd64', '-t', 'radmin-viewer-builder:0.1.0',
            '-f', context / 'packaging/linux/Dockerfile', context)
    run('docker', 'run', '--rm', '--platform', 'linux/amd64',
        '--user', f'{os.getuid()}:{os.getgid()}', '-e', 'HOME=/tmp',
        '-v', f'{output}:/output', 'radmin-viewer-builder:0.1.0')


def main():
    host = {'linux': 'linux', 'win32': 'windows'}.get(sys.platform)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', choices=('linux', 'windows'), default=host)
    parser.add_argument('--format', choices=('bundle', 'all'), default='all')
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    parser.add_argument('--linux-container', action='store_true')
    parser.add_argument('--iscc', default='ISCC.exe')
    args = parser.parse_args()
    output = args.output.resolve()
    if output == ROOT or output / NAME == ROOT:
        parser.error('Build output must not replace the source directory')
    if host is None:
        parser.error('This release packages Linux and Windows only; use a native supported build host')
    if args.linux_container:
        if host != 'linux' or args.platform != 'linux':
            parser.error('Linux container release builds require a Linux host')
        container_build(output)
        return
    if args.platform != host:
        parser.error('Cross-compilation is unsupported; run on the requested native OS')
    if host in ('windows', 'linux') and platform.machine().lower() not in ('x86_64', 'amd64'):
        parser.error('Linux/Windows packages currently require x86_64')
    for filename in ('LICENSE', 'NOTICE', 'THIRD_PARTY_NOTICES.md'):
        if not (ROOT / filename).is_file():
            parser.error(f'Missing release input: {filename}')
    output.mkdir(parents=True, exist_ok=True)
    generated = output / 'build'
    if generated.exists():
        shutil.rmtree(generated)
    generated.mkdir()
    assets(generated)
    licenses(generated)
    environment = dict(os.environ, VIEWER_SOURCE=str(ROOT), VIEWER_GENERATED=str(generated))
    run(sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
        '--distpath', output, '--workpath', generated / 'pyinstaller',
        ROOT / 'packaging/viewer.spec', env=environment)
    bundle = output / NAME
    executable = bundle / (NAME + ('.exe' if host == 'windows' else ''))
    report = output / f'smoke-{host}.json'
    report.unlink(missing_ok=True)
    run(executable, '--smoke-test', '--smoke-report', report,
        env=dict(os.environ, QT_QPA_PLATFORM='offscreen'), timeout=90)
    smoke = json.loads(report.read_text(encoding='utf-8'))
    if smoke.get('status') != 'ok' or smoke.get('frozen') is not True or smoke.get('version') != VERSION:
        raise RuntimeError(f'Unexpected smoke report: {smoke}')
    if host == 'linux':
        run(sys.executable, ROOT / 'scripts/audit_linux.py', bundle,
            '--report', output / 'linux-elf-audit.json')
    if args.format == 'all':
        if host == 'linux':
            linux_installers(output, generated, bundle)
        elif host == 'windows':
            run(args.iscc, f'/DSourceDir={bundle}', f'/DOutputDir={output}',
                f'/DAppVersion={VERSION}', f'/DIconFile={generated / "app-icon.ico"}',
                f'/DLicenseFile={ROOT / "LICENSE"}', ROOT / 'packaging/windows/viewer.iss')
    artifacts = sorted(p for p in output.iterdir() if p.is_file() and
                       p.name.startswith(f'{NAME}-{VERSION}-') and
                       (p.suffix in ('.deb', '.exe') or p.name.endswith('.tar.gz')))
    lines = []
    for artifact in artifacts:
        with artifact.open('rb') as stream:
            lines.append(hashlib.file_digest(stream, 'sha256').hexdigest() + '  ' + artifact.name)
    (output / 'SHA256SUMS').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'Build and smoke check completed: {output}')


if __name__ == '__main__':
    main()
