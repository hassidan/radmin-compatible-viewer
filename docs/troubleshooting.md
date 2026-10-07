# Troubleshooting

## The application does not start

Run it from a terminal with the same Python environment used for installation:

```sh
python -m radmin_viewer --version
python -m radmin_viewer --help
python -m radmin_viewer
```

“No module named …” usually means a different environment is active or installation
did not finish. Reinstall from the source folder using that environment's
`python -m pip install .`.

For a Qt platform-plugin/shared-library error on Linux, check the named dependency
against your distribution's Qt XCB/Wayland packages and verify that a graphical
session is available. Offscreen test success does not verify desktop integration.
For a packaged build, extract the whole onedir bundle and keep its libraries and
plugins beside the executable in the supplied layout.

## A second window does not appear, or the book is locked

The same address book is single-owner. A second launch normally restores the
existing manager; check the tray's **Open viewer** action. Quit the existing
instance completely before moving or repairing its book. Do not remove a lock
while an instance is running. If activation fails, confirm that the previous
process has exited and that the config directory is writable.

## The address book cannot be opened or saved

Invalid JSON or unsupported data is reported and preserved. Back up the original
before repairing it. Check directory permissions and free space. To isolate a
problem, launch with a different `--config ./temporary-connections.json` path;
this creates a separate book rather than repairing the existing one. A failed
save does not silently accept the unsaved change.

## Online, but cannot connect

**Online** only establishes a supported greeting. **Port open** establishes even
less: a TCP service answered. Verify the server's configured port, authentication
mode and per-mode permissions. Windows/NTLM authentication is unsupported. Use
**Sign in with different credentials…** if an old saved login is selected.

**Unreachable** may mean a stopped service, firewall, routing problem or timeout;
it does not establish the machine's power state. DNS errors require checking the
hostname/resolver. Refresh checks saved endpoints only, not the network as a whole.

## Password saving is unavailable

Unlock or configure a supported OS credential vault: Secret Service/KWallet on
Linux or Windows Credential Manager on Windows. Arbitrary file and chained keyring
backends are not accepted. Manual sign-in remains available. A new port or username
has a different credential identity. Backing up JSON does not back up vault data.

## Desktop input, clipboard or terminal behaves unexpectedly

- Input and clipboard require **Full Control**. Wait for the first desktop frame.
- Check matching keyboard layouts; F11 is local and secure-key sequences/IME are
  not fully supported.
- Automatic clipboard sharing must be explicitly enabled and can belong to only
  one session. Reconnect if a receive remains pending; images/files are unsupported.
- Select the remote shell's encoding for garbled terminal text. The line console
  does not support fullscreen terminal applications or ANSI screen control.
- Ctrl+C copies a terminal selection; clear the selection to send an interrupt.

## File transfer fails

Check the account's file-transfer permission, remote absolute drive path, filename,
local permissions and available space. The drive selector does not prove a drive
exists. Use a single regular file under 256 MiB and account for the 60-second
operation deadline. Reopen File transfer after cancellation or transport failure.
Inspect for a partial new remote upload before retrying; no automatic cleanup or
resume is performed.

## Bookmark import/export fails

The codec supports version-4 `.rpb` phonebooks, not every vendor format or setting.
Check field length, parent groups and unsupported Windows-auth/relay settings.
Do not truncate the original file or remove unknown fields blindly. Reproduce with
a synthetic file instead of attaching a private address book to a public issue.

## A power result is unknown, or restart recovery times out

An unknown result may follow a successfully dispatched action. Check the remote
machine's state before deciding whether to send another request. An acknowledgement
is not confirmation of shutdown, sleep or restart. Sleep/hibernate also depend on
Windows privileges, available power states and PowerShell support.

Recovery requires an observed outage followed by two supported greetings within
five minutes. It can miss a very fast reboot. Verify the endpoint and reconnect
manually if appropriate; the viewer does not resend the restart command.

## Reporting a problem

Include app version, OS/architecture, Python/Qt versions or package identifier,
steps, expected/actual behavior and sanitized error text. Say whether the failure
occurred with a simulated peer or a real server and which mode was used. Never
attach credentials, private endpoints, unsanitized phonebooks or remote-screen
captures. Follow [SECURITY.md](../SECURITY.md) for suspected vulnerabilities.
