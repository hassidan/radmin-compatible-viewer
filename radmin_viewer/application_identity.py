"""Application branding, native identity and optional per-user Linux launcher."""
import os
from pathlib import Path
import sys
import tempfile

from .ui_icons import application_icon

APP_NAME = "Radmin Compatible Viewer"
APP_ID = "radmin-compatible-viewer"
WINDOWS_APP_ID = "IndependentViewer.RadminCompatibleViewer"
_MARKER = "X-RadminCompatibleViewer-Managed=true"


def prepare_platform_identity():
    """Set Windows taskbar grouping before constructing Qt windows."""
    if sys.platform == "win32":
        try:
            import ctypes
            function = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
            function.argtypes = [ctypes.c_wchar_p]
            function.restype = ctypes.c_long
            function(WINDOWS_APP_ID)
        except (AttributeError, OSError):
            pass  # Window/tray icons still work when shell integration is absent.


def configure_application(app):
    # Keep applicationName/organization stable: QStandardPaths derives the
    # existing address-book directory from these names.
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName("IndependentViewer")
    app.setDesktopFileName(APP_ID)
    app.setWindowIcon(application_icon())


def _exec_argument(value):
    if any(ch in value for ch in "\n\r\0"):
        raise ValueError("Invalid launcher path")
    # Desktop Entry Exec is not a shell. Percent must not become a field code;
    # reserved characters inside quotes have a second string-unescape layer.
    value = value.replace("\\", "\\\\\\\\").replace('"', '\\\\"')
    value = value.replace("`", "\\\\`").replace("$", "\\\\$").replace("%", "%%")
    return '"' + value + '"'


def _atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install_linux_launcher(*, data_home=None, executable=None, working_directory=None):
    """Install only our per-user desktop entry/icon; never alter login autostart."""
    if not sys.platform.startswith("linux"):
        raise OSError("Desktop-entry installation is available on Linux only")
    root = Path(data_home or os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share").expanduser()
    if not root.is_absolute():
        raise ValueError("Desktop data directory must be absolute")
    frozen = bool(getattr(sys, "frozen", False))
    executable = str(Path(executable or sys.executable).absolute())
    exec_argument = _exec_argument(executable)
    default_directory = Path(executable).parent if frozen else Path(__file__).parent.parent
    working_directory = str(Path(working_directory or default_directory).resolve())
    for path in (str(root), working_directory):
        if any(ch in path for ch in "\n\r\0"):
            raise ValueError("Invalid launcher path")
    destination = root / "applications" / (APP_ID + ".desktop")
    if destination.exists() and _MARKER not in destination.read_text(encoding="utf-8").splitlines():
        raise OSError("An unmanaged desktop entry already exists; it was not replaced")
    icon_path = root / "icons/hicolor/scalable/apps" / (APP_ID + ".svg")
    _atomic_write(icon_path, (Path(__file__).with_name("assets") / "app-icon.svg").read_bytes())
    entry = ("[Desktop Entry]\nType=Application\nVersion=1.0\n"
             f"Name={APP_NAME}\nComment=Remote desktop, terminal and file transfer\n"
             f"Exec={exec_argument}{'' if frozen else ' -m radmin_viewer'}\n"
             f"Path={working_directory.replace(chr(92), chr(92) * 2)}\n"
             f"Icon={str(icon_path).replace(chr(92), chr(92) * 2)}\n"
             f"StartupWMClass={APP_NAME}\nTerminal=false\nStartupNotify=true\n"
             "Categories=Network;RemoteAccess;\n" + _MARKER + "\n")
    _atomic_write(destination, entry.encode("utf-8"))
    return destination
