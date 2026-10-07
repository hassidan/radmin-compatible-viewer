#!/usr/bin/env python3
"""Run local source checks. No hosted CI or GitHub Actions are required."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    environment = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    for command in (
        [sys.executable, "scripts/audit_publication.py"],
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        [sys.executable, "-m", "radmin_viewer", "--smoke-test"],
    ):
        subprocess.run(command, cwd=ROOT, env=environment, check=True)


if __name__ == "__main__":
    main()
