<p align="center">
  <img src="radmin_viewer/assets/app-icon.svg" alt="Radmin Compatible Viewer icon" width="88">
</p>

# Radmin Compatible Viewer

An independent Python/Qt desktop client for connecting to Radmin Server, with an
address book, remote desktop, file transfer and a line-oriented remote terminal.

**Unofficial and not endorsed by or affiliated with the Radmin vendor.** You need
a separately licensed Radmin Server and an account with the appropriate permissions.
Original vendor binaries and artwork are not distributed with this project.

## Download

[Open Releases](https://github.com/hassidan/radmin-compatible-viewer/releases)
for **v0.1.0 — Initial preview**. Packages have been built; the release is being
prepared as a draft. Downloads become available to repository readers after
publication. While the repository is private, access requires permission.

| Platform | Prerequisites | Prepared packages |
| --- | --- | --- |
| Linux x86_64 | Debian 12 or Ubuntu 24.04+, glibc ≥ 2.36, graphical X11/Wayland desktop and required system libraries | `.deb` (about 45 MB), portable `.tar.gz` (about 63 MB) |
| Windows x86_64 | Windows 10/11 x64 and a graphical desktop | Unsigned setup `.exe` (about 39 MB) |

Packaged builds include Python and the application dependencies. Linux installation,
offscreen smoke and uninstall checks passed in isolated Docker with networking
disabled. The Windows package was built natively on Windows Server 2022 and passed
frozen offscreen Qt, crypto and keyring-import checks. **Interactive Windows GUI,
installation, upgrade and uninstall are not yet verified**, including on Windows
10/11. macOS is not included in this release.

See [installation](docs/INSTALL.md) and the
[release verification table](docs/build-release.md#release-verification).

## Run from source

**Python 3.12 is recommended** (source minimum: 3.10). From the source folder on
Linux:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m radmin_viewer
```

On Windows, use `py -3.12 -m venv .venv`, then
`.venv\Scripts\python -m pip install .` and
`.venv\Scripts\python -m radmin_viewer`.

Add a computer with **+**, enter its hostname and port (default **4899**), then
choose **View Only** or **Full Control** from its context menu. The login dialog
accepts Radmin-security credentials; Windows authentication is not supported.

See the [user manual](docs/user-manual.md) and
[troubleshooting](docs/troubleshooting.md) for connection modes and common problems.

## Capabilities

“Implemented” describes the current application, not certification across all
server versions or operating systems. Package smoke checks verify selected runtime
components; they do not establish live-server or native desktop compatibility.

| Feature | Current scope |
| --- | --- |
| Address book | Groups, search, icon/list views, validated JSON and atomic saves |
| Radmin `.rpb` files | Import/export of version-4 phonebooks; names, endpoints, usernames and folder hierarchy; no passwords or viewer preferences |
| Availability | Checks explicitly saved hosts and ports; distinguishes an open port from a recognized greeting; no subnet discovery |
| Authentication | Radmin-security authentication; optional OS-vault password storage after successful login |
| Desktop | View Only and Full Control, fit/actual-pixel display, fullscreen, remote cursor and mouse/keyboard input |
| Clipboard | Bidirectional Unicode text in Full Control; automatic sharing is opt-in per session |
| File transfer | Single-file upload/download and remote directory browsing; 256 MiB per-file limit; no recursive transfers |
| Terminal | In-pane line editing, history, interrupt and encoding selection; separate authenticated connection |
| Remote power | Restart, shut down, power off, sleep and hibernate requests with user confirmation; actual power transitions are not fully live-tested |
| Restart recovery | Bounded outage/return monitoring and reopening of the previous desktop/terminal mode; no command or transfer replay |
| Desktop integration | System tray, same-address-book instance activation and optional Linux application-menu launcher |

## Important limitations

- The terminal is a **line console, not a VT/PTY emulator**. Fullscreen terminal
  applications and ANSI screen control are unsupported.
- No chat or Send Message GUI, Windows/NTLM authentication, multi-monitor selector
  or special secure-key sequences. International keyboard layouts/IME are not
  fully verified.
- No general automatic reconnect outside the Restart workflow. An acknowledgement
  of a power request does not prove a completed power transition.
- `.rpb` compatibility is bounded to the implemented format; full original Windows
  Viewer GUI import/resave has not been verified.
- No protocol-standard compliance or universal Radmin compatibility claim. See
  [protocol notes and evidence scope](docs/protocol-notes.md).
- Installing a launcher does **not** enable automatic startup at login.

## Data and credentials

Connections and settings are stored in a local address book. Passwords are never
written to that JSON file or `.rpb` exports. Optional saving uses a supported OS
credential vault with no plaintext-file fallback. Hostnames, usernames and folder
names in address books are still potentially sensitive. Passwords can remain
temporarily in process memory; secure memory erasure is not claimed.

Closing the manager hides it to the tray where available; sessions remain active.
Choose **Quit** from the tray, **… → Quit application**, or **Ctrl+Q** to exit.
Without tray support, closing the manager exits normally.

## Development and licensing

- [Architecture](docs/architecture.md) · [Module comments guide](docs/module-comments.md)
- [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Changelog](CHANGELOG.md)
- [MIT license](LICENSE) · [Scope and ownership](NOTICE) · [Third-party notices](THIRD_PARTY_NOTICES.md)

The MIT license applies to independently authored project code. It does **not**
license Radmin software, its protocol, vendor trademarks or third-party libraries.
Those rights and licenses remain separate; Qt/PySide6 is not covered by this
project's MIT grant. See the [legal review checklist](docs/legal.md).
