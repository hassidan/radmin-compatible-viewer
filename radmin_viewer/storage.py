"""Validated, credential-free address book with atomic replacement."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import uuid


class StorageError(ValueError):
    pass


def defaults():
    return {"version": 1, "groups": ["General"], "connections": [],
            "settings": {"timeout": 10, "scaling": "fit", "fullscreen": False}}


def text(value, label, maximum=256, empty=False):
    if (not isinstance(value, str) or len(value) > maximum
            or (not empty and not value.strip())
            or any(ord(c) < 32 for c in value)):
        raise StorageError(f"Invalid {label}")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise StorageError(f"Invalid {label}") from None
    return value


def validate(data):
    if not isinstance(data, dict) or set(data) != {"version", "groups", "connections", "settings"}:
        raise StorageError("Invalid address book fields")
    if type(data["version"]) is not int or data["version"] != 1:
        raise StorageError("Unsupported address book version")
    groups = data["groups"]
    if not isinstance(groups, list) or not 1 <= len(groups) <= 1000:
        raise StorageError("Address book must contain 1–1000 groups")
    for group in groups:
        text(group, "group", 100)
    if len(set(groups)) != len(groups):
        raise StorageError("Duplicate groups")
    connections = data["connections"]
    if not isinstance(connections, list) or len(connections) > 10000:
        raise StorageError("Invalid connection list")
    ids = set()
    for entry in connections:
        if not isinstance(entry, dict) or set(entry) != {"id", "name", "host", "port", "username", "group"}:
            raise StorageError("Invalid connection fields (passwords are never stored in the address book)")
        try:
            if str(uuid.UUID(entry["id"])) != entry["id"]:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise StorageError("Invalid connection ID") from None
        if entry["id"] in ids:
            raise StorageError("Duplicate connection ID")
        ids.add(entry["id"])
        text(entry["name"], "connection name")
        host = text(entry["host"], "host", 253)
        if any(c.isspace() for c in host) or any(c in host for c in "/\\@?#[]"):
            raise StorageError("Host must be a hostname or unbracketed IP address, not a URL")
        text(entry["username"], "username", 256, empty=True)
        if len(entry["username"].encode("utf-16-be")) > 512:
            raise StorageError("Username is too long")
        if type(entry["port"]) is not int or not 1 <= entry["port"] <= 65535:
            raise StorageError("Port must be between 1 and 65535")
        if entry["group"] not in groups:
            raise StorageError("Unknown connection group")
    settings = data["settings"]
    if not isinstance(settings, dict) or set(settings) != {"timeout", "scaling", "fullscreen"}:
        raise StorageError("Invalid settings")
    if type(settings["timeout"]) is not int or not 1 <= settings["timeout"] <= 120:
        raise StorageError("Timeout must be between 1 and 120 seconds")
    if settings["scaling"] not in ("fit", "actual") or type(settings["fullscreen"]) is not bool:
        raise StorageError("Invalid display settings")
    return data


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise StorageError("Duplicate JSON field")
        result[key] = value
    return result


class AddressBook:
    """Call under the application's lifetime config lock; no silent recovery."""

    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise StorageError("Address book exceeds 4 MiB")
            return validate(json.loads(raw, object_pairs_hook=_unique_object))
        except FileNotFoundError:
            return defaults()
        except (OSError, UnicodeError, ValueError, TypeError) as error:
            raise StorageError(f"Cannot load address book: {error}") from None

    def save(self, data):
        validate(data)
        raw = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        if len(raw) > 4 * 1024 * 1024:
            raise StorageError("Address book exceeds 4 MiB")
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".address-book-", dir=self.path.parent)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as error:
            raise StorageError(f"Cannot save address book: {error}") from None
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
