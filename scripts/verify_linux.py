#!/usr/bin/env python3
"""Verify Linux installers in a clean Debian container with offline runtime checks."""
import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='viewer-verify-') as temporary:
        context = Path(temporary)
        for name in ('radmin-compatible-viewer-0.1.0-linux-x86_64.deb',
                     'radmin-compatible-viewer-0.1.0-linux-x86_64.tar.gz'):
            shutil.copy2(args.output / name, context / name)
        for name in ('verify.Dockerfile', 'verify.sh'):
            shutil.copy2(ROOT / 'packaging/linux' / name, context / name)
        subprocess.run(['docker', 'build', '--quiet', '--platform', 'linux/amd64', '-t',
                        'radmin-viewer-verification:0.1.0', '-f', str(context / 'verify.Dockerfile'),
                        str(context)], check=True)
    subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--platform', 'linux/amd64',
                    'radmin-viewer-verification:0.1.0'], check=True)


if __name__ == '__main__':
    main()
