#!/usr/bin/env python3
"""Check publication inputs for private data and accidentally tracked artifacts.

Uses Git's tracked inventory when available, otherwise a generated-file-pruned
source inventory. Findings report locations/reasons, never matching secret text.
Optional RELEASE_FORBIDDEN_TEXT supplies newline-separated local secret literals;
that environment value is never written to a report or included in source.
"""
from pathlib import Path
import os
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
GENERATED = {".git", ".venv", "venv", "__pycache__", "dist", "build", ".pytest_cache"}
PRIVATE_DIRS = {"research", "evidence", "extracted", "vm"}
FORBIDDEN_SUFFIXES = {".rpb", ".pcap", ".pcapng", ".dmp", ".exe", ".dll", ".deb", ".rpm", ".dmg", ".pkg", ".key", ".p12"}
PATTERNS = (
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"), "GitHub token"),
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"), "private key"),
    (re.compile(r"/(?:home|Users)/[^/\s]+/"), "machine-specific home path"),
)


def inventory():
    result = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True)
    if result.returncode == 0 and result.stdout:
        return sorted(ROOT / name.decode("utf-8") for name in result.stdout.split(b"\0") if name)
    files = []
    for parent, directories, names in os.walk(ROOT):
        directories[:] = [d for d in directories if d not in GENERATED and not d.endswith(".egg-info")]
        files.extend(Path(parent) / name for name in names)
    return sorted(files)


def main():
    findings = []
    literals = [value for value in os.environ.get("RELEASE_FORBIDDEN_TEXT", "").splitlines() if value]
    files = inventory()
    if (ROOT / ".github/workflows").exists():
        findings.append((".github/workflows", "GitHub Actions are deliberately not part of this project"))
    for path in files:
        relative = path.relative_to(ROOT)
        if (any(part in PRIVATE_DIRS for part in relative.parts)
                or path.suffix.lower() in FORBIDDEN_SUFFIXES
                or path.name.startswith(".env")
                or (path.name.startswith("connections") and path.suffix == ".json")):
            findings.append((str(relative), "private/generated artifact in publication inventory"))
        if not path.is_file() or path.is_symlink():
            findings.append((str(relative), "missing file or unexpected symlink"))
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError:
            if path.suffix.lower() not in {".png", ".ico"}:
                findings.append((str(relative), "unexpected binary input"))
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for pattern, reason in PATTERNS:
                if pattern.search(line):
                    findings.append((f"{relative}:{number}", reason))
            if any(literal in line for literal in literals):
                findings.append((f"{relative}:{number}", "locally supplied forbidden literal"))
    for location, reason in findings:
        print(f"{location}: {reason}")
    print(f"Publication audit: {len(files)} files, {len(findings)} findings")
    return bool(findings)


if __name__ == "__main__":
    sys.exit(main())
