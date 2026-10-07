# Third-party notices

The project's [MIT license](LICENSE) covers independently authored project work.
Dependencies, bundled runtimes and build tools keep their own licenses. This
summary is not a substitute for their complete license texts or a binary-specific
inventory.

## Direct dependencies and packaging tool

The runtime versions below were inspected through installed Python distribution
metadata during documentation preparation. They are **observations, not pinned
requirements or a claim about a released bundle**. Supported requirement ranges
are maintained in `pyproject.toml`.

| Component | Inspected version | License reported by metadata / verification status |
| --- | --- | --- |
| PySide6, PySide6_Essentials, PySide6_Addons, shiboken6 | 6.11.2 | `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only` in the License field |
| cryptography | 50.0.2 | `Apache-2.0 OR BSD-3-Clause`; metadata lists `LICENSE`, `LICENSE.APACHE`, `LICENSE.BSD` |
| PyCryptodome | 3.24.0 | `BSD, Public Domain`; metadata lists `LICENSE.rst`, `AUTHORS.rst`; applicability varies by component |
| keyring | 25.7.0 | `MIT`; metadata lists `LICENSE` |
| PyInstaller (build tool) | Not installed in the inspected runtime environment | Upstream GPL-2.0-or-later with a bootloader distribution exception; runtime hooks and some runtime modules use Apache-2.0; verify the selected version's COPYING text |

Upstream references: [Qt licensing](https://www.qt.io/licensing/),
[Qt for Python licenses](https://doc.qt.io/qtforpython-6/licenses.html),
[cryptography](https://github.com/pyca/cryptography),
[PyCryptodome license](https://www.pycryptodome.org/src/license),
[keyring](https://github.com/jaraco/keyring), and
[PyInstaller license](https://pyinstaller.org/en/stable/license.html).

## Qt and PySide6 distribution

Qt libraries, plugins and their embedded third-party components need review in
addition to the Python package metadata. Do not assume that every module in a Qt
wheel has identical terms, or that the presence of an LGPL option in PySide6
metadata makes all collected components LGPL. The inspected PySide6 metadata did
not declare `License-File` entries; this does not remove the obligation to retain
the actual license texts shipped in the wheels and libraries.

The packaging policy is an **onedir distribution with shared libraries left
replaceable**. Keep the full directory intact for normal use. For LGPL components,
review the actual bundle for notices, license copies, corresponding-source
availability and any applicable installation/relinking requirements. Do not impose
terms that prohibit library modification or reverse engineering for debugging
those modifications. A replaceable-library layout helps meet these requirements;
it is not a complete compliance determination by itself.

## Release inventory

The local builder is responsible for collecting bundled dependency license texts
and metadata. Maintainers must inspect its output against the files actually
distributed, including Python, Qt plugins, transitive Python dependencies and
native libraries such as OpenSSL where included. Metadata collection alone may
miss notices embedded in a wheel or native library; supplement it as necessary.

Before publishing a binary, record resolved versions, retain copyright notices
and full license/exception texts, provide required source or source-access
materials, and verify that the resulting package contains them. Build-only tools
should be distinguished from code actually included in the executable; the
PyInstaller bootloader exception permits combined executables without imposing
PyInstaller's GPL terms on the independent application. Review the selected
version's exception and separately licensed runtime files; it does not waive the
licenses of the application's other dependencies.
See [build and release](docs/build-release.md) and [legal review](docs/legal.md).

Radmin Server and original vendor binaries are **not bundled dependencies**.
