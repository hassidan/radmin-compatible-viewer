# Changelog

This file tracks user-visible changes. The initial preview's packages have been
built; its release is being prepared as a draft, not advertised as published.

## v0.1.0 — Initial preview

### Features

- Independent Qt client with grouped/searchable address book, saved-endpoint
  availability checks and optional OS-vault credential storage.
- Radmin-security authentication, desktop viewing/control, fullscreen, remote
  cursor and Unicode text clipboard sharing.
- Dedicated single-file transfer and line-oriented terminal windows.
- Confirmed remote power requests and bounded restart recovery without command
  replay or automatic power redispatch.
- Version-4 `.rpb` bookmark import/export, system tray, same-book instance activation
  and optional Linux application-menu launcher.
- Local native packaging entry point, packaged-runtime smoke checks and dependency
  license collection for Linux x86_64 and Windows x86_64.
- Public-facing installation, user, developer, security and licensing documentation.
  MIT licensing is limited to independently authored project work.

### Packages and verification

- Linux `.deb` and portable `.tar.gz` built. Installation, offscreen smoke and
  uninstall checks passed in isolated Docker with networking disabled.
- Unsigned Windows setup `.exe` built natively on Windows Server 2022. Frozen
  offscreen Qt, crypto and keyring-import checks passed; interactive installation,
  GUI use, upgrade and uninstall on Windows 10/11 remain unverified.
- Downloads will be available to repository readers through
  [Releases](https://github.com/hassidan/radmin-compatible-viewer/releases) after
  publication. No destructive power-transition tests were part of release verification.

### Known limits

No Windows/NTLM authentication, multi-monitor selector, chat GUI, VT terminal
emulation or general automatic reconnect. Native power transitions and Windows
desktop integration are not fully live-verified. See [README](README.md) and
[protocol notes](docs/protocol-notes.md) for the full compatibility boundaries.
