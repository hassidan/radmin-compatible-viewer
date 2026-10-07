# Installation

## Download and requirements

The **v0.1.0 — Initial preview** packages target **Linux x86_64** and **Windows
x86_64**. Visit [Releases](https://github.com/hassidan/radmin-compatible-viewer/releases)
after the draft is published. A private repository requires access permission;
this guide does not imply that the draft is already downloadable.

- Linux: **Debian 12 or Ubuntu 24.04+, glibc ≥ 2.36**, a graphical X11/Wayland
  desktop and the system libraries required by Qt. The `.deb` declares its system
  dependencies; portable users must provide them separately.
- Windows: **Windows 10/11 x64** with a graphical desktop. The installer is unsigned.
- A separately licensed Radmin Server reachable at a known hostname or IP address,
  and Radmin-security credentials with permissions for the desired connection mode.

Packaged builds include Python and runtime dependencies. Linux install, offscreen
smoke and uninstall checks passed in isolated Docker without network access.
The Windows build and frozen offscreen smoke checks passed on Windows Server 2022;
interactive installation, GUI use, upgrade and uninstall on Windows 10/11 remain
unverified. See [release verification](build-release.md#release-verification).

## Install a package

### Linux Debian package

From the directory containing the downloaded package:

```sh
sudo apt install ./radmin-compatible-viewer-0.1.0-linux-x86_64.deb
radmin-compatible-viewer
```

The package installs under `/opt` and provides a command and application-menu entry.
Installing dependencies may require network access even though the isolated package
verification used a pre-provisioned environment with networking disabled.

### Linux portable archive

```sh
tar -xzf radmin-compatible-viewer-0.1.0-linux-x86_64.tar.gz
```

Open the extracted directory. Run `./install.sh` for a per-user installation, or
launch `./radmin-compatible-viewer/radmin-compatible-viewer` directly from it.
Keep the entire application directory, including shared libraries, plugins and
notices. Do not move only the executable. The installer refuses to replace an
existing per-user installation; quit and move the old installation aside first.
No launcher or installer enables startup at login.

### Windows setup

After publication, download the Windows x86_64 setup `.exe` from Releases and run
it for a per-user installation. **The installer is unsigned**; Windows may show an
unknown-publisher or reputation warning. Verify the source and published checksum
before deciding whether to proceed. These instructions do not claim that the
interactive installer flow has been tested.

Installed files include replaceable shared libraries and notices; retain the
supplied layout. Checksums identify downloaded bytes but are not publisher signatures.

## Install from source

Use **Python 3.12 (recommended)**, pip and a graphical desktop; the source minimum
is Python 3.10. Pip installs the dependencies in `pyproject.toml`, subject to wheel
availability for the chosen Python/platform. Open a terminal in the source folder
and use a virtual environment to separate dependencies from the system Python.

### Linux

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m radmin_viewer
```

### Windows (PowerShell)

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install .
.venv\Scripts\python -m radmin_viewer
```

Installation also provides the `radmin-compatible-viewer` command inside the
environment. Activate the environment or use the full path to its executable.
For development, use `pip install -e .`; see [Contributing](../CONTRIBUTING.md).

On Linux, install your distribution's Qt XCB/Wayland runtime dependencies if Qt
reports a missing platform plugin or shared library. A working desktop session
is needed for interactive use; offscreen mode is intended for automated checks.

## First connection

1. Start the application and choose **+ → Add computer**.
2. Enter a label, hostname or unbracketed IP address, port and optional username.
   The default server port is **4899**; use the port configured by the administrator.
3. Right-click the computer and explicitly select **View Only**, **Full Control**,
   **File transfer** or **Terminal**.
4. Sign in. Saving the password in the OS credential vault is optional.

An unsaved quick connection is also available:

```sh
python -m radmin_viewer --host server.example --port 4899 --username operator --mode view
```

Use the environment's Python. `server.example` and `operator` are placeholders;
replace them with your own endpoint and account. There is no password argument.
Quick connections are not automatically added to the saved address book.

## Address book and credentials

By default, `connections.json` is stored in Qt's platform application configuration
location, using organization `IndependentViewer` and application name
`Radmin Compatible Viewer`. The exact parent directory depends on the OS and its
configuration. To select a known location:

```sh
python -m radmin_viewer --config ./connections.json
```

Only one instance owns an address book at a time. Starting the application with
the same config normally restores the existing manager; a quick-connect request
is forwarded without a password. Different config files have independent books.

Password saving requires a supported, accessible Secret Service/KWallet backend
on Linux or Windows Credential Manager on Windows. Unsupported or locked
vaults do not prevent manual sign-in. Address-book backups do not include saved
passwords.

## Optional Linux launcher

Using the Python executable from the installed environment:

```sh
python -m radmin_viewer --install-desktop-entry
```

This installs a per-user application-menu entry and project icon in the XDG data
directories. Keep that environment and installation at their current locations,
or rerun the command after moving them. **It does not enable startup at login.**

## Update or remove

Quit fully before updating, and back up the address book first. From updated source,
run the environment's `python -m pip install --upgrade .`. For a packaged build,
follow its release instructions and keep the previous version until validation.
Windows interactive upgrade/uninstall behavior is not yet verified. The Debian
package can be removed with `sudo apt remove radmin-compatible-viewer`. For the
portable per-user installation, remove its application directory and its launcher/
icon from the user's XDG data directories; user settings are retained separately.

For a source installation, `python -m pip uninstall radmin-compatible-viewer`
removes the installed application, not its saved data or OS-vault entries. Use
**Forget saved password** for each relevant account before removal if desired.
An installed Linux launcher/icon can be removed from the user's XDG applications
and icon directories by its `radmin-compatible-viewer` name. Retain or remove your
address-book backup separately.
