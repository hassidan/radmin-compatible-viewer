"""Synchronous Radmin mode-4 file transfer over a dedicated EncryptedChannel.

See docs/protocol-notes.md. No credentials, UI, or automatic remote cleanup.
Uploads refuse names present in a preflight listing; the recovered protocol has
no proven atomic create-new operation, so callers must use unique remote names.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import math
import ntpath
import os
from pathlib import Path
import stat
import struct
import threading
import time
import zlib

from .channel import EncryptedChannel, MAX_RECORD
from .protocol import ProtocolError, RadminError, RadminTimeout

MAX_MESSAGE = 16 * 1024 * 1024
MAX_ENTRIES = 100_000
OPERATION = 0x10000000
OFFSET = 0x20000000
COUNT = 0x30000000
DATA = 0x40000000
PATH = 0x50000000
ENTRY = 0xb0000000
METADATA = 0xc0000000
ERROR = 0xf0000000
SUCCESS = 0xf8000000


class FileTransferCancelled(RadminError):
    """Operation cancelled; the dedicated channel has been closed."""


class FileTransferRemoteError(RadminError):
    def __init__(self, code: int):
        self.code = code
        super().__init__(f"Radmin file operation failed (remote code 0x{code:08x})")


@dataclass(frozen=True)
class DirectoryEntry:
    name: str
    size: int
    timestamp1_filetime: int
    timestamp2_filetime: int
    attributes: int
    entry_type: int

    @property
    def is_directory(self) -> bool:
        return bool(self.attributes & 0x10)


@dataclass(frozen=True)
class TransferResult:
    remote_path: str
    local_path: str
    bytes_transferred: int
    sha256: str


def _field(tag: int, value: bytes) -> bytes:
    if not tag or tag & ~0xf8000000 or not 0 < len(value) < 0x08000000:
        raise ValueError("Invalid file-transfer field")
    return struct.pack(">I", tag | len(value)) + value


def _fields(data: bytes) -> list[tuple[int, bytes]]:
    if len(data) > MAX_MESSAGE:
        raise ProtocolError("File-transfer message exceeds size limit")
    result = []
    pos = 0
    while pos < len(data):
        if len(data) - pos < 4:
            raise ProtocolError("Truncated file-transfer field")
        header = struct.unpack_from(">I", data, pos)[0]
        tag, length = header & 0xf8000000, header & 0x07ffffff
        pos += 4
        if not tag or not length or length > len(data) - pos:
            raise ProtocolError("Invalid file-transfer field length or tag")
        result.append((tag, data[pos:pos + length]))
        pos += length
        if len(result) > MAX_ENTRIES:
            raise ProtocolError("Too many file-transfer fields")
    return result


def _unique(fields: list[tuple[int, bytes]]) -> dict[int, bytes]:
    result = {}
    for tag, value in fields:
        if tag in result:
            raise ProtocolError("Duplicate file-transfer field")
        result[tag] = value
    return result


def _check_error(fields: list[tuple[int, bytes]]) -> None:
    errors = [value for tag, value in fields if tag == ERROR]
    if errors:
        if len(errors) != 1 or len(errors[0]) != 4:
            raise ProtocolError("Malformed remote error")
        raise FileTransferRemoteError(struct.unpack(">I", errors[0])[0])


def _path(path: str, *, file: bool = False) -> str:
    if not isinstance(path, str):
        raise TypeError("remote path must be str")
    path = path.replace("/", "\\")
    drive, tail = ntpath.splitdrive(path)
    if (len(drive) != 2 or drive[1] != ":" or drive[0] not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
            or not tail.startswith("\\") or any(ch in path for ch in '\x00\x01*?"<>|')
            or ":" in tail or any(part in (".", "..") for part in tail.split("\\"))
            or any(part and (part.endswith(".") or part.endswith(" ")) for part in tail.split("\\"))):
        raise ValueError("Use an absolute drive path without wildcards, traversal or stream syntax")
    if file and (tail == "\\" or tail.endswith("\\")):
        raise ValueError("Expected a file path")
    normalized = ntpath.normpath(path)
    # Avoid Windows DOS devices even when a filename extension is supplied.
    devices = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    devices.update(f"{prefix}{i}" for prefix in ("COM", "LPT") for i in range(1, 10))
    if any(part.split(".", 1)[0].upper() in devices for part in tail.split("\\")):
        raise ValueError("Windows device paths are not supported")
    encoded = normalized.encode("utf-16-be")
    if len(encoded) > 65530:
        raise ValueError("Remote path exceeds local limit")
    return normalized


def _path_field(path: str) -> bytes:
    return _field(PATH, path.encode("utf-16-be") + b"\x00\x01")


def _decode_name(data: bytes) -> str:
    if len(data) < 2 or len(data) % 2 or data[-2:] != b"\x00\x01":
        raise ProtocolError("Malformed remote filename")
    try:
        name = data[:-2].decode("utf-16-be")
    except UnicodeDecodeError:
        raise ProtocolError("Invalid UTF-16BE filename") from None
    if not name or any(c in name for c in "\x00\x01\\/"):
        raise ProtocolError("Invalid remote entry name")
    return name


def _entries(data: bytes) -> tuple[DirectoryEntry, ...]:
    fields = _fields(data)
    _check_error(fields)
    entries = []
    names = set()
    for tag, value in fields:
        if tag == SUCCESS and value == b"\x00" * 4:
            continue
        if tag != ENTRY:
            raise ProtocolError("Unexpected directory response field")
        record = _unique(_fields(value))
        if PATH not in record or len(record.get(METADATA, b"")) != 29:
            raise ProtocolError("Incomplete directory entry")
        name = _decode_name(record[PATH])
        if name.casefold() in names:
            raise ProtocolError("Duplicate remote filename")
        names.add(name.casefold())
        if name in (".", ".."):
            continue
        entries.append(DirectoryEntry(name, *struct.unpack(">QQQIB", record[METADATA])))
    return tuple(entries)


class FileTransferClient:
    """Single-owner client; supply a started, otherwise unused encrypted channel.

    Call start() once, then list_directory/download/upload. cancel may be a
    threading.Event. Checks occur between records, with network waits bounded
    by io_timeout (default 2 seconds) and total operation_timeout (default 60).
    Cancellation, timeout or malformed protocol closes the channel. A remote
    error leaves the synchronized channel usable. Uploads may leave a partial
    NEW remote file on failure; no remote deletion is performed automatically.
    """

    def __init__(self, channel: EncryptedChannel, *, chunk_size: int = 65536,
                 max_file_size: int = 256 * 1024 * 1024,
                 operation_timeout: float = 60.0, io_timeout: float = 2.0):
        if type(chunk_size) is not int or not 1 <= chunk_size <= 1024 * 1024:
            raise ValueError("chunk_size must be 1..1048576")
        if type(max_file_size) is not int or not 0 <= max_file_size <= 2**63 - 1:
            raise ValueError("Invalid file size limit")
        if any(not math.isfinite(t) or t <= 0 for t in (operation_timeout, io_timeout)):
            raise ValueError("Timeouts must be positive and finite")
        self.channel = channel
        self.chunk_size = chunk_size
        self.max_file_size = max_file_size
        self.operation_timeout = operation_timeout
        self.io_timeout = io_timeout
        self._compress = zlib.compressobj(1, zlib.DEFLATED, -15)
        self._decompress = zlib.decompressobj(-15)
        self._started = False
        self._closed = False
        self._lock = threading.Lock()
        self._deadline = 0.0
        self._cancel = None

    def close(self) -> None:
        self._closed = True
        self.channel.close()

    def _check(self):
        if self._cancel is not None and self._cancel.is_set():
            raise FileTransferCancelled("File transfer cancelled")
        if time.monotonic() >= self._deadline:
            raise RadminTimeout("File-transfer operation deadline exceeded")

    @contextmanager
    def _operation(self, cancel, *, starting=False):
        if not self._lock.acquire(blocking=False):
            raise RadminError("FileTransferClient is single-owner; another operation is active")
        try:
            if self._closed or (not starting and not self._started):
                raise RadminError("File-transfer client is not started or is closed")
            self._deadline = time.monotonic() + self.operation_timeout
            self._cancel = cancel
            try:
                self._check()
                yield
                self._check()
            except (ProtocolError, RadminTimeout, FileTransferCancelled):
                self.close()
                raise
            finally:
                self._cancel = None
        finally:
            self._lock.release()

    def _io(self, method, *args):
        self._check()
        session = self.channel._session
        previous = session.timeout
        session.timeout = min(previous, self.io_timeout, self._deadline - time.monotonic())
        try:
            result = method(*args)
        except BaseException:
            self.close()
            if self._cancel is not None and self._cancel.is_set():
                raise FileTransferCancelled("File transfer cancelled") from None
            raise
        finally:
            session.timeout = previous
        self._check()
        return result

    def _exchange(self, payload: bytes) -> bytes:
        self._io(self.channel.send, payload)
        return self._io(self.channel.receive)

    def start(self, *, cancel=None) -> FileTransferClient:
        with self._operation(cancel, starting=True):
            if self._started:
                raise RadminError("File-transfer client has already started")
            reply = self._exchange(b"\x1a" + struct.pack(">I", 4))
            if reply != b"\x1a":
                self.close()
                raise ProtocolError("File-transfer mode 4 was not immediately accepted")
            init = b"\x35" + _field(OPERATION, b"\x00" * 4)
            if self._exchange(init) != init:
                raise ProtocolError("Unsupported file-transfer initialization reply")
            self._started = True
        return self

    def _request(self, operation: int, *fields: bytes, compressed_reply=True):
        self._check()
        data = _field(OPERATION, struct.pack(">I", operation)) + b"".join(fields)
        if len(data) > MAX_MESSAGE:
            raise ValueError("File-transfer request exceeds local limit")
        encoded = struct.pack(">I", len(data)) + self._compress.compress(data)
        encoded += self._compress.flush(zlib.Z_SYNC_FLUSH)
        if len(encoded) > MAX_RECORD - 24:
            raise ProtocolError("Compressed request exceeds channel limit")
        reply = self._exchange(encoded)
        if not compressed_reply:
            _check_error(_fields(reply))
            return reply
        if len(reply) <= 4:
            raise ProtocolError("Truncated compressed file reply")
        size = struct.unpack_from(">I", reply)[0]
        if not 0 < size <= MAX_MESSAGE:
            raise ProtocolError("File reply exceeds decompression limit")
        try:
            result = self._decompress.decompress(reply[4:], size + 1)
        except zlib.error:
            raise ProtocolError("Invalid file-transfer DEFLATE stream") from None
        if (len(result) != size or self._decompress.unconsumed_tail
                or self._decompress.unused_data or self._decompress.eof):
            raise ProtocolError("File-transfer DEFLATE length/stream mismatch")
        _check_error(_fields(result))
        return result

    def _list(self, path):
        return _entries(self._request(0x21, _path_field(ntpath.join(path, "*"))))

    def list_directory(self, remote_path: str, *, cancel=None) -> tuple[DirectoryEntry, ...]:
        path = _path(remote_path)
        with self._operation(cancel):
            return self._list(path)

    def _stat(self, path):
        entries = _entries(self._request(0x21, _path_field(path)))
        if len(entries) != 1 or entries[0].name.casefold() != ntpath.basename(path).casefold():
            raise ProtocolError("Expected one matching file in remote stat reply")
        return entries[0]

    def _read(self, path, offset, count):
        reply = _unique(_fields(self._request(
            5, _field(COUNT, struct.pack(">I", count)),
            _field(OFFSET, struct.pack(">Q", offset)), _path_field(path))))
        if set(reply) - {DATA, SUCCESS}:
            raise ProtocolError("Unexpected file read response")
        data = reply.get(DATA, b"")
        if len(data) > count:
            raise ProtocolError("Remote read exceeded requested byte count")
        return data

    def _write(self, path, offset, data):
        parts = [_field(OFFSET, struct.pack(">Q", offset)), _path_field(path)]
        if data:
            parts.append(_field(DATA, data))
        reply = self._request(6, *parts, compressed_reply=False)
        if _unique(_fields(reply)) != {SUCCESS: b"\x00" * 4}:
            raise ProtocolError("Unexpected file write acknowledgement")

    def download(self, remote_path: str, local_path: str | os.PathLike, *, cancel=None) -> TransferResult:
        """Download a regular file to a new local path (exclusive create).

        A failure removes only the partial local file created by this call.
        No remote file is changed. Returns byte count and content SHA-256.
        """
        remote = _path(remote_path, file=True)
        local = Path(local_path)
        with self._operation(cancel):
            entry = self._stat(remote)
            if entry.entry_type != 1 or entry.attributes & (0x10 | 0x400):
                raise ValueError("Only regular, non-reparse remote files can be downloaded")
            if entry.size > self.max_file_size:
                raise ValueError("Remote file exceeds max_file_size")
            self._check()
            with local.open("xb") as output:
                identity = os.fstat(output.fileno())
                try:
                    digest = hashlib.sha256()
                    offset = 0
                    while offset < entry.size:
                        self._check()
                        data = self._read(remote, offset, min(self.chunk_size, entry.size - offset))
                        if not data:
                            raise ProtocolError("Remote file ended before advertised size")
                        output.write(data)
                        digest.update(data)
                        offset += len(data)
                    if self._stat(remote) != entry:
                        raise ProtocolError("Remote file metadata changed during download")
                    output.flush()
                    self._check()
                except BaseException:
                    # Do not unlink a replacement created by another local process.
                    try:
                        current = local.lstat()
                        if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                            local.unlink()
                    except FileNotFoundError:
                        pass
                    raise
            return TransferResult(remote, str(local), offset, digest.hexdigest())

    def upload(self, local_path: str | os.PathLike, remote_path: str, *, cancel=None) -> TransferResult:
        """Upload a regular local file using a caller-chosen NEW remote filename.

        Existing names (case-insensitive) are rejected before any write. This is
        a preflight guard, not atomic remote exclusivity; use unpredictable names
        in a directory without concurrent writers. Failure leaves any new partial
        remote file in place and reports the error. No overwrite/resume API.
        """
        remote = _path(remote_path, file=True)
        local = Path(local_path)
        with self._operation(cancel), local.open("rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Upload source must be a regular file")
            if info.st_size > self.max_file_size:
                raise ValueError("Local file exceeds max_file_size")
            parent, name = ntpath.split(remote)
            if any(e.name.casefold() == name.casefold() for e in self._list(parent)):
                raise FileExistsError("Remote destination already exists")
            digest = hashlib.sha256()
            offset = 0
            while offset < info.st_size:
                self._check()
                data = source.read(min(self.chunk_size, info.st_size - offset))
                if not data:
                    raise OSError("Local upload source was truncated")
                self._write(remote, offset, data)
                digest.update(data)
                offset += len(data)
            if not info.st_size:
                self._write(remote, 0, b"")
            after = os.fstat(source.fileno())
            if (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
                raise OSError("Local upload source changed during transfer")
            entry = self._stat(remote)
            if entry.is_directory or entry.size != offset:
                raise ProtocolError("Uploaded file size did not match remote listing")
            return TransferResult(remote, str(local), offset, digest.hexdigest())
