"""Keep desktop GUI plugins without optional PDF/QML/virtual-keyboard engines."""
from PyInstaller.utils.hooks.qt import add_qt6_dependencies

hiddenimports, binaries, datas = add_qt6_dependencies(__file__)

# Filter before PyInstaller's recursive native dependency scan, so optional
# plugins cannot pull in unused QtPdf, QtQuick or QtVirtualKeyboard libraries.
unused = ('qpdf', 'qtvirtualkeyboard', 'qgtk3', 'qeglfs', 'qlinuxfb', 'qvkkhrdisplay')
binaries = [(source, destination) for source, destination in binaries
            if not any(name in source.replace('\\', '/').rsplit('/', 1)[-1].lower()
                       for name in unused)
            and '/egldeviceintegrations' not in destination.replace('\\', '/')]
