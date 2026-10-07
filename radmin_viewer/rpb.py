"""Radmin 3.x version-4 binary phonebooks (see docs/protocol-notes.md).

Only bookmark identity, Unicode text, port and folder hierarchy are transferred.
Viewer display/chat preferences are not transferred; passwords are never read
into the model or exported. Windows-authentication defaults and relay routing
are rejected rather than converted into a different connection.

Groups are slash-separated folder paths; '/' denotes the phonebook root.
Every parent path must be present. Native folder names containing '/' cannot
be represented unambiguously and are rejected. Local UUIDs are import identities,
not Radmin's numeric record identifiers; merge/conflict handling belongs to UI.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import struct
import tempfile
import uuid

from .storage import StorageError, defaults, validate


class RpbError(StorageError):
    """Invalid, unsupported or unrepresentable phonebook."""


HEADER = struct.Struct("<6IB")
RECORD_SIZE = 6138
PAGE_SLOTS = 128
PAGE_SIZE = PAGE_SLOTS * (RECORD_SIZE + 1)
MAX_BYTES = 70 * 1024 * 1024
MAX_SLOTS = 11000
ROOT_GROUP = "/"
IMPORT_LIMITATIONS = (
    "Viewer display, window, chat and voice preferences are not transferred.",
    "Passwords and OS-keyring credentials are never included.",
    "Local UUIDs are regenerated on import; numeric Radmin IDs are not preserved.",
)
_NAMESPACE = uuid.UUID("d180625d-a98b-4650-a218-b22ff301c4d4")


def _u32(raw, offset):
    return struct.unpack_from("<I", raw, offset)[0]


def _string(raw, offset, label):
    field = raw[offset:offset + 200]
    end = next((i for i in range(0, 200, 2) if field[i:i + 2] == b"\0\0"), None)
    if end is None:
        raise RpbError(f"Unterminated RPB {label}")
    try:
        return field[:end].decode("utf-16le")
    except UnicodeError:
        raise RpbError(f"Invalid Unicode in RPB {label}") from None


def _put_string(raw, offset, value, label):
    try:
        encoded = value.encode("utf-16le")
    except (UnicodeError, AttributeError):
        raise RpbError(f"Invalid RPB {label}") from None
    if len(encoded) > 198 or "\0" in value:
        raise RpbError(f"RPB {label} exceeds 99 UTF-16 code units or contains NUL")
    raw[offset:offset + len(encoded)] = encoded


def _validated(data):
    if not isinstance(data, dict):
        raise RpbError("Expected an address book object")
    if set(data) == {"groups", "connections"}:
        full = defaults()
        full.update(data)
    else:
        full = data
    try:
        validate(full)
    except (StorageError, TypeError) as error:
        raise RpbError(str(error)) from None
    return full


def loads_rpb(raw: bytes) -> dict:
    """Parse data only. Return {'groups': [...], 'connections': [...]}.

    All active slots are validated before returning anything. Deleted slots are
    skipped. No registry import, subprocess, network, pickle or eval is used.
    """
    if not isinstance(raw, bytes) or not HEADER.size <= len(raw) <= MAX_BYTES:
        raise RpbError("Invalid RPB size or input type")
    version, revision, tick, size, slots, hint, flag = HEADER.unpack_from(raw)
    if (version, size, slots, flag) != (4, RECORD_SIZE, PAGE_SLOTS, 1):
        raise RpbError("Unsupported RPB header (expected Radmin version 4)")
    records = []
    offset = HEADER.size
    slot = 0
    ids = set()
    while offset < len(raw):
        if len(raw) - offset < PAGE_SLOTS:
            raise RpbError("Truncated RPB allocation table")
        table = raw[offset:offset + PAGE_SLOTS]
        offset += PAGE_SLOTS
        count = min(PAGE_SLOTS, (len(raw) - offset) // RECORD_SIZE)
        if count == 0 or any(table[count:]) or any(mark not in (0, 1) for mark in table):
            raise RpbError("Invalid RPB allocation table")
        if slot + count > MAX_SLOTS:
            raise RpbError("Too many RPB record slots")
        for index in range(count):
            record = raw[offset:offset + RECORD_SIZE]
            offset += RECORD_SIZE
            slot += 1
            if not table[index]:
                continue
            ident, relay, parent = struct.unpack_from("<III", record, 6112)
            folder, flagged = record[6124:6126]
            if not ident or ident in ids:
                raise RpbError("Missing or duplicate RPB record identifier")
            ids.add(ident)
            if folder not in (0, 1) or flagged not in (0, 1):
                raise RpbError("Unsupported RPB record flags")
            if relay:
                raise RpbError("RPB relay/through connections are unsupported")
            if any(record[5308:5908]):
                raise RpbError("RPB Windows-authentication defaults are unsupported")
            if any(record[6108:6112]) or any(record[6130:]):
                raise RpbError("Unsupported RPB extension fields")
            name = _string(record, 5104, "name")
            host = _string(record, 4904, "host")
            username = _string(record, 5908, "username")
            port = _u32(record, 5304)
            if folder and (host or username or port):
                raise RpbError("Unsupported connection data on RPB folder")
            records.append((ident, parent, folder, name, host, port, username))
    folders = {r[0]: r for r in records if r[2]}
    paths = {}
    for ident in folders:
        chain = []
        visiting = set()
        current = ident
        while current and current not in paths:
            if current in visiting:
                raise RpbError("Cyclic RPB folder hierarchy")
            if current not in folders:
                raise RpbError("Missing RPB parent folder")
            visiting.add(current)
            node = folders[current]
            if not node[3].strip() or "/" in node[3]:
                raise RpbError("Empty or ambiguous RPB folder name")
            chain.append(node)
            current = node[1]
        prefix = paths.get(current, "")
        for node in reversed(chain):
            prefix = prefix + "/" + node[3] if prefix else node[3]
            if len(prefix) > 100:
                raise RpbError("RPB folder path exceeds storage's 100-character limit")
            paths[node[0]] = prefix
    groups = [paths[r[0]] for r in records if r[2]]
    if len(groups) != len(set(groups)):
        raise RpbError("Duplicate RPB folder paths cannot be represented")
    connections = []
    source_id = hashlib.sha256(raw).hexdigest()
    for ident, parent, folder, name, host, port, username in records:
        if folder:
            continue
        if parent and parent not in paths:
            raise RpbError("Missing RPB parent folder")
        group = paths[parent] if parent else ROOT_GROUP
        if group not in groups:
            groups.append(group)
        connections.append(dict(id=str(uuid.uuid5(_NAMESPACE, f"{source_id}:{ident}")),
                                name=name, host=host, port=port, username=username, group=group))
    result = {"groups": groups or [ROOT_GROUP], "connections": connections}
    _validated(result)
    return result


def _record(ident, parent, slot, name, connection=None):
    record = bytearray(RECORD_SIZE)
    _put_string(record, 5104, name, "name")
    struct.pack_into("<III", record, 6112, ident, 0, parent)
    struct.pack_into("<I", record, 6126, slot)
    record[6124] = int(connection is None)
    if connection is not None:
        # Original default-options table at Viewer VA 0x014f27e0; never copy opaque
        # settings from imported records, which can contain user information.
        for offset, value in ((0, 100), (12, 1), (24, 1), (28, 1), (140, 1),
                              (148, 5), (2328, 2), (2332, 2), (2336, 3)):
            struct.pack_into("<I", record, offset, value)
        _put_string(record, 152, "User", "chat nickname")
        _put_string(record, 1240, "User", "voice nickname")
        _put_string(record, 4904, connection["host"], "host")
        _put_string(record, 5908, connection["username"], "username")
        struct.pack_into("<I", record, 5304, connection["port"])
    return bytes(record)


def dumps_rpb(data: dict) -> bytes:
    """Encode an imported dict or a complete storage.validate address book.

    Reject overlong text rather than truncate. Settings in a full storage book
    are validated but not exported. Password/unknown connection keys fail.
    """
    data = _validated(data)
    groups = data["groups"]
    if (ROOT_GROUP in groups and len(groups) > 1
            and not any(c["group"] == ROOT_GROUP for c in data["connections"])):
        raise RpbError("An empty root group alongside folders has no RPB record representation")
    folder_ids = {group: index + 1 for index, group in enumerate(groups) if group != ROOT_GROUP}
    records = []
    for group, ident in folder_ids.items():
        components = group.split("/")
        if any(not component.strip() for component in components):
            raise RpbError("Invalid RPB folder path")
        parent = "/".join(components[:-1])
        if parent and parent not in folder_ids:
            raise RpbError("RPB folder parent must be present in groups")
        records.append(_record(ident, folder_ids.get(parent, 0), len(records), components[-1]))
    next_id = len(groups) + 1
    for entry in data["connections"]:
        records.append(_record(next_id, folder_ids.get(entry["group"], 0), len(records),
                               entry["name"], entry))
        next_id += 1
    parts = [HEADER.pack(4, 1, 0, RECORD_SIZE, PAGE_SLOTS, 0, 1)]
    for start in range(0, len(records), PAGE_SLOTS):
        page = records[start:start + PAGE_SLOTS]
        parts.append(bytes([1]) * len(page) + bytes(PAGE_SLOTS - len(page)))
        parts.extend(page)
    return b"".join(parts)


def read_rpb(path) -> dict:
    """Read a bounded RPB file without modifying it."""
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
    except OSError as error:
        raise RpbError(f"Cannot read RPB: {error}") from None
    return loads_rpb(raw)


def write_rpb(path, data: dict) -> None:
    """Validate, then atomically replace path using a private temporary file."""
    raw = dumps_rpb(data)
    path = Path(path)
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".rpb-", dir=path.parent)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as error:
        raise RpbError(f"Cannot write RPB: {error}") from None
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
