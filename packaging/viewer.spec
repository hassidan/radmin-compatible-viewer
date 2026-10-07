import os
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, copy_metadata

root = Path(os.environ['VIEWER_SOURCE'])
generated = Path(os.environ['VIEWER_GENERATED'])
name = 'radmin-compatible-viewer'
datas = [(str(root / 'radmin_viewer/assets'), 'radmin_viewer/assets'),
         (str(generated / 'licenses'), 'licenses'),
         (str(generated / 'app-icon.png'), 'radmin_viewer/assets')]
for filename in ('LICENSE', 'NOTICE', 'THIRD_PARTY_NOTICES.md', 'README.md'):
    datas.append((str(root / filename), '.'))
datas.append((str(root / 'docs'), 'docs'))
hidden = ['PySide6.QtSvg', 'PySide6.QtNetwork']
hidden += collect_submodules('keyring.backends')
for distribution in ('keyring', 'cryptography', 'pycryptodome', 'PySide6', 'PySide6_Essentials', 'shiboken6'):
    datas += copy_metadata(distribution)
a = Analysis([str(root / 'packaging/entrypoint.py')], pathex=[str(root)],
             hookspath=[str(root / 'packaging/hooks')],
             binaries=[], datas=datas, hiddenimports=hidden,
             excludes=['PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
                       'PySide6.QtWebEngineQuick', 'PySide6.QtQml', 'PySide6.QtQuick',
                       'PySide6.QtMultimedia', 'PySide6.QtPdf', 'tkinter'],
             noarchive=False)
# The application uses its own widget styling, not the optional GTK theme bridge.
# Keep XCB/Wayland/offscreen platform backends, but avoid a GTK runtime dependency.
if sys.platform.startswith('linux'):
    a.binaries = [item for item in a.binaries if not item[0].endswith('/libqgtk3.so')]
pyz = PYZ(a.pure)
icon = generated / 'app-icon.ico'
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=name, debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False,
          console=sys.platform != 'win32',
          icon=str(icon) if sys.platform == 'win32' else None)
bundle = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=name)
