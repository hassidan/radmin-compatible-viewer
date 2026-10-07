# Local builds and release preparation

All builds and checks are run locally. **No GitHub Actions or `.github/workflows`
are used.** Build inputs are maintained in `scripts/build.py`,
`requirements-build.txt` and `packaging/`; see the
[build contract](../packaging/README.md).

## v0.1.0 — Initial preview

This release targets **Linux x86_64 and Windows x86_64 only**. Linux `.deb` (about
45 MB), portable `.tar.gz` (about 63 MB) and an unsigned Windows setup `.exe`
(about 39 MB) have been built. They are being prepared for a draft release;
publication has not been confirmed. Use the
[Releases page](https://github.com/hassidan/radmin-compatible-viewer/releases)
for downloads after publication. Access to the private repository requires permission.

### Release verification

| Check | Linux x86_64 | Windows x86_64 |
| --- | --- | --- |
| Target | Debian 12 / Ubuntu 24.04+, glibc ≥ 2.36 | Windows 10/11 x64 |
| Native package build | Passed; `.deb` and portable archive produced | Passed on Windows Server 2022; setup `.exe` produced |
| Frozen runtime smoke | Passed with offscreen Qt | Passed with offscreen Qt, crypto and keyring-backend import |
| Installation / smoke / uninstall | Passed in isolated Docker with networking disabled | Interactive install, upgrade and uninstall not yet verified |
| Interactive desktop | Container checks do not validate a real display, tray or vault | Actual desktop GUI not yet verified, including Windows 10/11 |
| Destructive power transitions | Not tested as part of release verification | Not tested as part of release verification |

The Windows installer is **unsigned**. A successful keyring import is not a
credential-vault save/read/delete check. Neither offscreen smoke result establishes
live Radmin interoperability or a completed remote power transition.

## Source and native build environments

For ordinary development, see [Contributing](../CONTRIBUTING.md). Native packaging
uses **64-bit Python 3.12** and the pinned build requirements, in a dedicated virtual
environment on the target OS:

```sh
python -m pip install -r requirements-build.txt
python scripts/build.py --help
```

The native CLI uses the provisioned dependencies. An offline environment can install
from an already prepared wheelhouse with `pip install --no-index --find-links
WHEELHOUSE -r requirements-build.txt`. Container provisioning needs network access
unless its image/dependency inputs are already available. Build from the clean
publication tree, never a directory containing private research artifacts.

Use a **fresh output directory for each release build**. The builder replaces its
`OUTPUT/build` working directory, and old installer/archive files in a reused output
directory can otherwise be mistaken for current products.

## Platform commands

### Linux

The release baseline is built with Docker on Linux:

```sh
python scripts/build.py --linux-container --output dist
```

This targets x86_64 using a Debian 12 / glibc 2.36 baseline. The contract targets
Debian 12 and Ubuntu 24.04+; native graphical drivers, X11/Wayland support and
credential-vault services still need validation on the deployment system. The
container copies selected application/build inputs and mounts the output directory.

For a native development bundle on a compatible Linux host:

```sh
python scripts/build.py --platform linux --format bundle --output dist
```

`--format all` additionally creates a portable `.tar.gz` and `.deb`, requiring
`dpkg-deb`. A build made on a newer host does not establish the release baseline.
The portable package includes a per-user `install.sh`; the Debian package installs
under `/opt` and supplies a command and desktop entry. Neither configures login
autostart.

### Windows

Build natively with Python 3.12 x64 and Inno Setup 6:

```powershell
python scripts/build.py --platform windows --format all --output dist --iscc "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
```

The installer is per-user. Use `--format bundle` when validating only the frozen
directory without Inno Setup. Windows packaging targets x86_64. The current native
build and smoke check passed on Windows Server 2022; installation and actual desktop
use on Windows 10/11 still need verification.

## Build output and smoke checks

`--format bundle` freezes an onedir application and runs its smoke check.
`--format all` also invokes the platform's installer/archive tools. Cross-compilation
is rejected. The release platform choices are `linux` and `windows`. Consult
`scripts/build.py --help` for the current option interface.

The builder invokes the actual frozen executable with:

```text
EXE --smoke-test --smoke-report REPORT.json
```

`EXE` and `REPORT.json` are placeholders for the executable and a writable report
path. Smoke mode uses offscreen Qt and checks selected crypto imports, the platform
keyring backend import and the application icon without loading user configuration
or connecting to a server. A missing/invalid report or failed command fails the
build. It does not prove that the real vault can save/read credentials, that a tray
works, or that a live server accepts a session.

Output categories include the frozen directory, a
platform-named smoke JSON report, generated build inputs, and, with `all`, native
installers/archives. `SHA256SUMS` covers final archive/installer files; it does not
constitute a signature or checksum inventory of the entire onedir tree. Do not
publish `build/` working files as part of a release.

## Licenses and publication contents

Keep Qt as replaceable shared libraries in the **onedir** layout; do not convert
the package to onefile. The builder collects license payloads and an installed
distribution manifest and includes `LICENSE`, `NOTICE` and
`THIRD_PARTY_NOTICES.md`. The manifest describes the build environment, not an
exact runtime software bill of materials. Reconcile it with the frozen files and
add missing native-library notices/source-access materials before distribution.

Review [third-party notices](../THIRD_PARTY_NOTICES.md) and the
[legal checklist](legal.md), especially the selected Qt modules, PyInstaller
exception and bundled native dependencies. The project's MIT license cannot
replace these terms.

## Release checklist

- [ ] Record the source revision/version, target OS/architecture, toolchain and
  resolved dependencies. Confirm version agreement across application and packaging.
- [ ] Run the test suite locally and retain actual pass/fail/skip results.
- [ ] Build into a fresh output directory and inspect the frozen smoke report.
- [ ] On each advertised OS, test install, launch, icon, tray hide/restore/quit,
  second-instance activation, config persistence and uninstall/update behavior.
- [ ] Exercise a disposable OS-vault credential save/read/delete on that platform.
- [ ] Record live interoperability coverage separately, with server version/mode
  and untested cases stated. Do not run disruptive power actions as an implicit
  build check; explicitly arrange any power-transition validation.
- [ ] Verify license texts, ownership/provenance, shared-library replacement and
  required source availability against the exact distributed contents.
- [ ] Inspect archives/installers for private data, vendor executables/artwork,
  research captures, unintended build files and environment-specific paths.
- [ ] Compute/verify checksums for the final files after any changes. State that
  the current Windows installer is unsigned and identify unverified behavior.
- [ ] Write release notes listing only files actually produced and checked, their
  target platforms, known limitations and verification scope. Upload only after
  those files and checks are confirmed.
