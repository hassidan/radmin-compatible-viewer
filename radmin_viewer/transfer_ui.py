"""File-transfer UI. A dedicated worker exclusively owns all protocol objects.

FileTransferWindow(entry, password, settings) starts authentication when created.
Closing/cancelling signals an Event, waits asynchronously for bounded operations,
and closes the client/channel/session on the owning thread. No desktop socket is
shared. There is no protocol drive enumeration: the drive selector offers letters
to try, not a claim that those drives exist. Transfers are single regular files.
"""
from __future__ import annotations

import ntpath
from pathlib import Path
import queue
import re
import string
import threading
import uuid

from PySide6.QtCore import Qt, QThread, Signal, QDir, QModelIndex, QTimer
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QLineEdit,
    QPushButton, QComboBox, QLabel, QTreeWidget, QTreeWidgetItem, QTreeView,
    QFileSystemModel, QFileDialog, QMessageBox, QProgressBar,
)

from .protocol import RadminSession, AuthenticationError, RadminTimeout, TransportError, ProtocolError
from .channel import EncryptedChannel
from .filetransfer import FileTransferClient, FileTransferCancelled, FileTransferRemoteError
from .ui_dialogs import StyledComboBox, confirm
from .ui_icons import icon, application_icon


def icon_button(name, label):
    button = QPushButton(icon(name), "")
    button.setToolTip(label)
    button.setAccessibleName(label)
    return button


def unique_upload_path(directory: str, filename: str) -> str:
    """Unpredictable Windows-safe leaf; preflight collision checks remain required.

    The wire protocol has no verified atomic create-new primitive. Unpredictable
    names plus the client's preflight are the available no-overwrite safeguards.
    """
    # Windows permits Hebrew, other Unicode letters and spaces. Replace only
    # forbidden filename characters/control codes (and invalid surrogate values),
    # not everything outside ASCII. The protocol already transports UTF-16 names.
    leaf = re.sub(r'[\\/:*?"<>|\x00-\x1f\ud800-\udfff]', "_", Path(filename).name).rstrip(". ") or "file"
    stem, extension = ntpath.splitext(leaf)
    suffix = "-" + uuid.uuid4().hex
    budget = 255 - len("upload-") - len(suffix)
    def prefix(value, units):
        # An astral character consumes two UTF-16 units. Drop an incomplete pair
        # at the boundary, never emit an invalid Windows/protocol filename.
        return value.encode("utf-16-le")[:units * 2].decode("utf-16-le", errors="ignore")
    extension = prefix(extension, budget - 1).rstrip(". ")
    stem_budget = budget - len(extension.encode("utf-16-le")) // 2
    stem = prefix(stem, stem_budget) or prefix("file", stem_budget)
    return ntpath.join(directory, f"upload-{stem}{suffix}{extension}")


def error_message(error: Exception) -> str:
    # Never display arbitrary exception strings, which could include credentials.
    if isinstance(error, FileTransferCancelled):
        return "Cancelled. The dedicated transfer connection has been closed."
    if isinstance(error, FileTransferRemoteError):
        return f"Server rejected the file operation (code 0x{error.code:08x}). Check path and permissions."
    if isinstance(error, AuthenticationError):
        return "Authentication rejected or the server proof is invalid."
    if isinstance(error, RadminTimeout):
        return "The connection or file operation timed out. Reopen File transfer to reconnect."
    if isinstance(error, FileExistsError):
        return "Destination already exists; nothing was overwritten. Choose a new filename."
    if isinstance(error, (ValueError, UnicodeError)):
        return "Invalid path or unsupported file. Use an absolute drive path and a regular file within the size limit."
    if isinstance(error, (TransportError, ProtocolError)):
        return "Transfer connection failed or the server returned an unsupported response."
    if isinstance(error, OSError):
        return "File I/O failed. Check file access, permissions, free space, and the connection."
    return "File transfer failed. Reopen File transfer to reconnect."


class TransferWorker(QThread):
    """GUI submits one operation at a time; all synchronous I/O stays in run().

    Signals: authenticated(), authentication_failed(message), ready(),
    listed(path, tuple[DirectoryEntry]), completed(kind, TransferResult),
    failed(message, fatal), status(message), inherited finished().
    submit(kind, *args) accepts list(path), upload(local, remote),
    download(remote, local). stop() cancels the whole dedicated session.
    """
    ready = Signal()
    listed = Signal(str, object)
    completed = Signal(str, object)
    failed = Signal(str, bool)
    status = Signal(str)
    authenticated = Signal()
    authentication_failed = Signal(str)

    def __init__(self, entry, password, timeout, parent=None):
        super().__init__(parent)
        self.entry = dict(entry)
        self.password = password
        self.timeout = timeout
        self.cancel = threading.Event()
        self.commands = queue.Queue(maxsize=1)

    def submit(self, kind, *args):
        if kind not in ("list", "upload", "download") or self.cancel.is_set():
            return False
        try:
            self.commands.put_nowait((kind, args))
            return True
        except queue.Full:
            return False

    def stop(self):
        self.cancel.set()

    def run(self):
        session = channel = client = None
        try:
            if self.cancel.is_set():
                return
            self.status.emit("Authenticating a dedicated file-transfer connection…")
            session = RadminSession(self.entry["host"], self.entry["port"],
                                    self.entry["username"], self.password, self.timeout)
            self.password = ""
            session.connect()
            if self.cancel.is_set():
                return
            if not session.authenticated:
                raise AuthenticationError()
            self.authenticated.emit()
            self.status.emit("Authenticated · Starting encrypted channel…")
            channel = EncryptedChannel(session)
            channel.start()
            if self.cancel.is_set():
                return
            client = FileTransferClient(channel, operation_timeout=60.0, io_timeout=2.0)
            self.status.emit("Starting file-transfer service…")
            client.start(cancel=self.cancel)
            if self.cancel.is_set():
                return
            self.ready.emit()
            while not self.cancel.is_set():
                try:
                    kind, args = self.commands.get(timeout=0.1)
                except queue.Empty:
                    continue
                if self.cancel.is_set():
                    break
                try:
                    if kind == "list":
                        entries = client.list_directory(args[0], cancel=self.cancel)
                        self.listed.emit(args[0], entries)
                    else:
                        method = client.upload if kind == "upload" else client.download
                        result = method(*args, cancel=self.cancel)
                        self.completed.emit(kind, result)
                except (FileTransferRemoteError, FileExistsError, ValueError) as error:
                    # These errors do not poison the file-transfer stream.
                    self.failed.emit(error_message(error), False)
        except Exception as error:
            if self.cancel.is_set():
                error = FileTransferCancelled()
            if isinstance(error, AuthenticationError):
                self.authentication_failed.emit(error_message(error))
            else:
                self.failed.emit(error_message(error), True)
        finally:
            self.password = ""
            for resource in (client, channel, session):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        pass


class FileTransferWindow(QMainWindow):
    """Dedicated transfer session, with local filesystem and remote path browsers."""
    retry_requested = Signal(object, str, str)
    authenticated = Signal()

    def __init__(self, entry, password, settings):
        super().__init__()
        self.entry = dict(entry)
        self.retry_pending = False
        self.retry_emitted = False
        self.retry_message = ""
        self.worker_started = False
        self.worker_finished = False
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(f"{entry['name']} — File transfer")
        self.setWindowIcon(application_icon())
        self.resize(1100, 700)
        self.connected = False
        self.busy = True
        self.closing = False
        self.stopping = False
        self.last_error = False
        self.remote_directory = None  # Only a successfully listed directory is a destination.
        self.active_kind = None
        self.active_remote = None
        self.local_directory = str(Path.home())
        central = QWidget()
        layout = QVBoxLayout(central)
        splitter = QSplitter()
        layout.addWidget(splitter, 1)

        local = QWidget()
        left = QVBoxLayout(local)
        left.addWidget(QLabel("Local folder"))
        local_row = QHBoxLayout()
        self.local_path = QLineEdit(self.local_directory)
        self.local_path.addAction(icon("folder"), QLineEdit.ActionPosition.LeadingPosition)
        self.local_path.setAccessibleName("Local folder path")
        local_row.addWidget(self.local_path, 1)
        browse = icon_button("folder", "Choose local folder")
        browse.clicked.connect(self.choose_local_folder)
        local_row.addWidget(browse)
        left.addLayout(local_row)
        self.local_model = QFileSystemModel(self)
        self.local_model.setReadOnly(True)
        self.local_model.setFilter(QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot)
        self.local_model.setRootPath(self.local_directory)
        self.local_tree = QTreeView()
        self.local_tree.setModel(self.local_model)
        self.local_tree.setRootIndex(self.local_model.index(self.local_directory))
        self.local_tree.setColumnWidth(0, 260)
        self.local_tree.setSortingEnabled(True)
        self.local_tree.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.local_tree.doubleClicked.connect(self.local_open)
        self.local_tree.selectionModel().selectionChanged.connect(self.update_actions)
        self.local_path.returnPressed.connect(lambda: self.set_local_folder(self.local_path.text()))
        left.addWidget(self.local_tree)
        local_up = icon_button("folder", "Local parent folder")
        local_up.clicked.connect(lambda: self.set_local_folder(str(Path(self.local_directory).parent)))
        left.addWidget(local_up)
        splitter.addWidget(local)

        remote = QWidget()
        right = QVBoxLayout(remote)
        right.addWidget(QLabel("Remote folder"))
        remote_row = QHBoxLayout()
        self.drive = StyledComboBox()
        self.drive.setToolTip("Drive letters are candidates to try, not an enumerated drive list.")
        self.drive.setAccessibleName("Remote drive candidate")
        self.drive.addItems([f"{letter}:\\" for letter in string.ascii_uppercase])
        self.drive.setCurrentText("C:\\")
        self.drive.activated.connect(lambda index: self.browse_remote(self.drive.itemText(index)))
        remote_row.addWidget(self.drive)
        self.remote_path = QLineEdit("C:\\")
        self.remote_path.addAction(icon("folder"), QLineEdit.ActionPosition.LeadingPosition)
        self.remote_path.setAccessibleName("Remote folder path")
        self.remote_path.returnPressed.connect(lambda: self.browse_remote(self.remote_path.text()))
        remote_row.addWidget(self.remote_path, 1)
        self.go = icon_button("folder", "Open remote folder")
        self.go.clicked.connect(lambda: self.browse_remote(self.remote_path.text()))
        remote_row.addWidget(self.go)
        right.addLayout(remote_row)
        self.remote_tree = QTreeWidget()
        self.remote_tree.setHeaderLabels(["Name", "Bytes", "Type"])
        self.remote_tree.setRootIsDecorated(False)
        self.remote_tree.setColumnWidth(0, 250)
        self.remote_tree.setSortingEnabled(True)
        self.remote_tree.itemDoubleClicked.connect(self.remote_open)
        self.remote_tree.itemSelectionChanged.connect(self.update_actions)
        right.addWidget(self.remote_tree)
        remote_buttons = QHBoxLayout()
        self.remote_up = icon_button("folder", "Remote parent folder")
        self.remote_up.clicked.connect(self.parent_remote)
        self.refresh = icon_button("retry", "Refresh remote")
        self.refresh.clicked.connect(lambda: self.browse_remote(self.remote_directory or self.remote_path.text()))
        remote_buttons.addWidget(self.remote_up)
        remote_buttons.addWidget(self.refresh)
        right.addLayout(remote_buttons)
        splitter.addWidget(remote)

        buttons = QHBoxLayout()
        self.upload_button = icon_button("upload", "Upload selected local file")
        self.upload_button.clicked.connect(self.upload)
        self.download_button = icon_button("download", "Download selected remote file")
        self.download_button.clicked.connect(self.download)
        self.cancel_button = icon_button("disconnect", "Cancel transfer and disconnect")
        self.cancel_button.clicked.connect(self.disconnect)
        self.retry_button = icon_button("retry", "Retry login")
        self.retry_button.clicked.connect(self.request_retry)
        self.retry_button.hide()
        for button in (self.upload_button, self.download_button, self.cancel_button, self.retry_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)
        self.status_label = QLabel("Starting file-transfer session…")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status_label)
        note = icon_button("more", "Transfer limits and safeguards")
        note.setToolTip("Single regular files, up to 256 MiB; 60-second operation deadline. "
                        "Uploads use unique names and refuse existing names, but the protocol cannot "
                        "guarantee atomic create-new against concurrent remote writers. Failed uploads "
                        "may leave a partial new remote file. Cancel closes this transfer session.")
        note.clicked.connect(lambda: QMessageBox.information(self, "Transfer limits", note.toolTip()))
        buttons.addWidget(note)
        self.setCentralWidget(central)
        self.worker = TransferWorker(entry, password, settings["timeout"], self)
        self.worker.status.connect(self.on_status)
        self.worker.authenticated.connect(self.on_authenticated)
        self.worker.authentication_failed.connect(self.on_authentication_failed)
        self.worker.ready.connect(self.on_ready)
        self.worker.listed.connect(self.on_listed)
        self.worker.completed.connect(self.on_completed)
        self.worker.failed.connect(self.on_failed)
        self.worker.finished.connect(self.on_finished)
        self.update_actions()
        self.start_timer = QTimer(self)
        self.start_timer.setSingleShot(True)
        self.start_timer.timeout.connect(self.start_worker)
        self.start_timer.start(0)

    def start_worker(self):
        if not self.stopping and not self.closing and not self.worker_started:
            self.worker_started = True
            self.worker.start()

    def on_status(self, message):
        if not self.stopping:
            self.status_label.setText(message)

    def on_authenticated(self):
        if not self.stopping:
            self.authenticated.emit()

    def on_authentication_failed(self, message):
        if self.stopping:
            return
        self.on_failed(message, True)
        self.request_retry()

    def request_retry(self):
        if self.closing or self.retry_pending or self.retry_emitted:
            return
        self.retry_pending = True
        self.disconnect()

    def finish_close(self):
        if self.retry_pending and not self.retry_emitted:
            self.retry_emitted = True
            self.retry_requested.emit(dict(self.entry), "transfer", self.retry_message)
        self.close()

    def update_actions(self, *args):
        available = self.connected and not self.busy and not self.stopping
        for widget in (self.go, self.refresh, self.remote_path, self.drive, self.remote_tree):
            widget.setEnabled(available)
        self.remote_up.setEnabled(available and self.remote_directory is not None)
        index = self.local_tree.currentIndex()
        info = self.local_model.fileInfo(index)
        self.upload_button.setEnabled(available and self.remote_directory is not None
                                      and index.isValid() and info.isFile() and not info.isSymLink())
        selected = self.remote_tree.currentItem()
        entry = selected.data(0, Qt.ItemDataRole.UserRole) if selected else None
        self.download_button.setEnabled(available and entry is not None and not entry.is_directory
                                        and not entry.attributes & 0x400 and entry.entry_type == 1)
        self.progress.setRange(0, 0 if self.busy else 1)
        if not self.busy:
            self.progress.setValue(0)

    def set_local_folder(self, path):
        path = str(Path(path).expanduser().absolute())
        index = self.local_model.index(path)
        if not index.isValid() or not self.local_model.isDir(index):
            self.status_label.setText("Local folder does not exist or cannot be accessed.")
            return
        self.local_directory = path
        self.local_path.setText(path)
        self.local_tree.setRootIndex(index)
        self.local_tree.clearSelection()
        self.local_tree.setCurrentIndex(QModelIndex())
        self.update_actions()

    def choose_local_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Local folder", self.local_directory)
        if path:
            self.set_local_folder(path)

    def local_open(self, index):
        if self.local_model.isDir(index):
            self.set_local_folder(self.local_model.filePath(index))

    def submit(self, kind, *args):
        if not self.connected or self.busy or self.stopping:
            return False
        if not self.worker.submit(kind, *args):
            return False
        self.active_kind = kind
        self.active_remote = args[1] if kind == "upload" else args[0]
        self.last_error = False
        self.busy = True
        self.status_label.setText(f"{ {'list': 'Listing', 'upload': 'Uploading to', 'download': 'Downloading'}[kind]} {self.active_remote}…")
        self.update_actions()
        return True

    def browse_remote(self, path):
        # Protocol client performs authoritative drive/path validation off-thread.
        path = path.replace("/", "\\")
        self.submit("list", path)

    def parent_remote(self):
        if self.remote_directory:
            self.browse_remote(ntpath.dirname(self.remote_directory.rstrip("\\")) + "\\")

    def remote_open(self, item, column):
        entry = item.data(0, Qt.ItemDataRole.UserRole)
        if entry.is_directory and self.remote_directory:
            self.browse_remote(ntpath.join(self.remote_directory, entry.name))

    def on_ready(self):
        if self.stopping:
            return
        self.connected = True
        self.busy = False
        self.browse_remote(self.remote_path.text())

    def on_listed(self, path, entries):
        if self.stopping:
            return
        self.remote_directory = ntpath.normpath(path)
        self.remote_path.setText(self.remote_directory)
        drive = ntpath.splitdrive(path)[0].upper() + "\\"
        self.drive.setCurrentText(drive)
        self.remote_tree.clear()
        for entry in entries:
            item = QTreeWidgetItem([entry.name, "" if entry.is_directory else str(entry.size),
                                   "Folder" if entry.is_directory else "File"])
            if entry.is_directory:
                item.setIcon(0, icon("folder"))
            item.setData(0, Qt.ItemDataRole.UserRole, entry)
            self.remote_tree.addTopLevelItem(item)
        self.busy = False
        self.active_kind = None
        self.status_label.setText(f"{len(entries)} entries in {self.remote_directory}")
        self.update_actions()

    def upload(self):
        if not self.upload_button.isEnabled():
            return
        source = self.local_model.filePath(self.local_tree.currentIndex())
        destination = unique_upload_path(self.remote_directory, Path(source).name)
        if confirm(self, "Upload with a unique filename",
                   f"Source:\n{source}\n\nDestination:\n{destination}\n\n"
                   "A unique filename is used to avoid replacing existing files. "
                   "Atomic create-new against concurrent remote writers is not guaranteed.",
                   "Upload", destructive=False):
            self.submit("upload", source, destination)

    def download(self):
        if not self.download_button.isEnabled():
            return
        entry = self.remote_tree.currentItem().data(0, Qt.ItemDataRole.UserRole)
        remote = ntpath.join(self.remote_directory, entry.name)
        # Never turn a remote-controlled name into an unchecked local path.
        leaf = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", entry.name).strip(". ") or "download"
        destination, _ = QFileDialog.getSaveFileName(
            self, "Download to a NEW local file", str(Path(self.local_directory) / leaf),
            options=QFileDialog.Option.DontConfirmOverwrite)
        if destination:
            self.submit("download", remote, destination)  # Client uses exclusive 'xb'.

    def on_completed(self, kind, result):
        if self.stopping:
            return
        self.busy = False
        self.active_kind = None
        self.status_label.setText(f"{kind.capitalize()} complete: {result.bytes_transferred} bytes\n"
                                  f"{result.remote_path} ↔ {result.local_path}\nSHA-256: {result.sha256}\n"
                                  "Use Refresh remote to update the remote listing.")
        self.update_actions()

    def on_failed(self, message, fatal):
        if self.stopping:
            return
        self.last_error = True
        if self.active_kind == "upload":
            message += f"\nA partial new remote file may remain: {self.active_remote}"
        self.status_label.setText(message)
        self.busy = False
        if fatal:
            self.retry_message = message
            self.connected = False
            self.retry_button.show()
            self.retry_button.setEnabled(False)
            self.worker.stop()
        elif self.active_kind == "list" and self.remote_directory:
            self.remote_path.setText(self.remote_directory)
        self.active_kind = None
        self.update_actions()

    def disconnect(self):
        if self.stopping:
            return
        self.stopping = True
        self.closing = True
        self.start_timer.stop()
        self.busy = True
        self.connected = False
        self.cancel_button.setEnabled(False)
        message = "Cancelling / disconnecting… Waiting for the current bounded network operation to finish."
        if self.active_kind == "upload":
            message += f"\nA partial new remote file may remain: {self.active_remote}"
        self.status_label.setText(message)
        self.worker.stop()
        self.retry_button.setEnabled(False)
        self.update_actions()
        if not self.worker_started:
            self.worker.password = ""
            self.worker_finished = True
        if self.worker_finished:
            self.finish_close()

    def on_finished(self):
        self.worker_finished = True
        self.connected = False
        self.busy = False
        self.cancel_button.setEnabled(not self.closing)
        self.retry_button.setEnabled(not self.closing)
        if not self.last_error:
            message = "Disconnected. Reopen File transfer to reconnect."
            if self.active_kind == "upload":
                message += f"\nA partial new remote file may remain: {self.active_remote}"
            self.status_label.setText(message)
        self.update_actions()
        if self.closing:
            self.finish_close()

    def closeEvent(self, event):
        if self.worker_started and not self.worker_finished:
            self.disconnect()
            event.ignore()
        else:
            self.closing = True
            self.stopping = True
            self.start_timer.stop()
            self.worker.stop()
            self.worker.password = ""
            event.accept()
