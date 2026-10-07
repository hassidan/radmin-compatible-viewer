"""Non-destructive merge policy for imported Radmin phonebooks."""
import copy
from dataclasses import dataclass
import uuid

from .storage import StorageError, defaults, validate


@dataclass(frozen=True)
class ImportSummary:
    computers: int
    groups: int
    duplicates: int


def merge_bookmarks(current, imported):
    """Keep existing entries/settings; reuse group names, skip exact duplicates.

    Same-name computers with different addresses/ports remain separate bookmarks.
    Imported UUIDs are renewed on collision, never used to overwrite local edits.
    """
    validate(current)
    if not isinstance(imported, dict) or set(imported) != {"groups", "connections"}:
        raise StorageError("Invalid imported bookmark fields")
    candidate = defaults()
    candidate.update(imported)
    validate(candidate)
    merged = copy.deepcopy(current)
    groups_before = len(merged["groups"])
    known_groups = set(merged["groups"])
    for name in imported["groups"]:
        if name not in known_groups:
            merged["groups"].append(name)
            known_groups.add(name)
    def identity(entry):
        return tuple(entry[k] for k in ("group", "name", "host", "port", "username"))
    known = {identity(entry) for entry in merged["connections"]}
    ids = {entry["id"] for entry in merged["connections"]}
    added = skipped = 0
    for entry in imported["connections"]:
        key = identity(entry)
        if key in known:
            skipped += 1
            continue
        entry = dict(entry)
        while entry["id"] in ids:
            entry["id"] = str(uuid.uuid4())
        ids.add(entry["id"])
        known.add(key)
        merged["connections"].append(entry)
        added += 1
    validate(merged)
    return merged, ImportSummary(added, len(merged["groups"]) - groups_before, skipped)
