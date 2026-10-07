# Native release build contract

Python 3.12, 64-bit. From the repository root:

```
python -m pip install -r requirements-build.txt
python scripts/vendor_qt_licenses.py
python scripts/build.py --platform windows --format all --output dist
```

`--platform` is `linux` or `windows`, and must match the host.
`--format bundle` only freezes and smoke-tests; `--format all` also creates the
native installer(s). The build CLI never downloads dependencies. Install them
first; an offline builder can use `pip install --no-index --find-links WHEELHOUSE
-r requirements-build.txt`. No source-package installation is necessary.
The license provisioning command requires network only once; retain and transfer
`packaging/licenses/qt` with the source for offline builders. Exact Qt 6.11.2
upstream archive URLs, SHA-256 hashes and retained files are recorded there.
Build commands accept `--output PATH`; generated work stays under OUTPUT/build.
`--iscc PATH` selects Inno Setup 6's ISCC.exe. Windows uses a windowed executable;
smoke verification waits for process completion and reads `--smoke-report PATH`.
The smoke command is `EXE --smoke-test --smoke-report PATH`, with
`QT_QPA_PLATFORM=offscreen`. A nonzero exit or missing/invalid report fails the build.

Linux official artifacts must be produced with:

```
python scripts/build.py --linux-container --output dist
python scripts/verify_linux.py --output dist
```

This copies only application/build inputs into a Debian 12 Python 3.12 image;
only the output directory is mounted writable. Docker needs network access to
pull the base and install build dependencies. Base glibc is 2.36, targeting
Debian 12 / Ubuntu 24.04+ x86_64. A compatible graphical X11/Wayland session,
OpenGL driver, and Secret Service/D-Bus service are runtime requirements.
The .deb declares shared-system-library dependencies. Portable users must install
the same dependencies listed in `linux/Dockerfile`; a headless container smoke
test does not validate graphical drivers or a real credential vault.
Extract the portable tar and run `./install.sh` for per-user installation, or
launch its bundled executable directly. The .deb installs under `/opt` and
provides `/usr/bin/radmin-compatible-viewer` and a desktop entry.

Windows: build natively with Python 3.12 x64 and Inno Setup 6 installed. Pass
`--iscc "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"` when it is not on PATH.
The installer is per-user with no elevation. Only x86_64 Windows is currently
packaged. Install and launch testing in a Windows desktop remains necessary.

Qt remains replaceable shared libraries in an onedir bundle; never use onefile.
Only imported Qt modules and their PyInstaller-hook-selected plugins are bundled,
not a collect-all of PySide6. `_internal/licenses/manifest.json` inventories
installed distributions and copied license files.
Root LICENSE, NOTICE and THIRD_PARTY_NOTICES.md must exist before freezing.
The output `SHA256SUMS` covers final archives/installers. Linux builds also audit
all bundled ELF files for unresolved shared libraries and GLIBC requirements
above 2.36 (`linux-elf-audit.json`). The separate Linux verifier installs the .deb
in clean Debian 12, disables network for runtime testing, checks version/smoke,
removes the package, and tests portable installation under a path with spaces.
Build validation does not claim live Radmin protocol, graphical desktop/vault,
upgrade or Windows uninstall verification.
No GitHub workflows are used.
