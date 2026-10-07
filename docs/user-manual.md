# User manual

## Computers, groups and availability

Use **+** to add a computer to the selected group; its arrow also offers **Add
group**. Search and the group selector filter the view. The grid/list buttons
switch between tiles and details. Click a group header to select and collapse or
expand it. Removing a populated group asks where to move its computers; the
application keeps at least one group.

Right-click a computer to choose a connection mode, edit it, manage credentials
or remove it. Tile double-click opens **Full Control**; detailed-list double-click
opens **View Only**. Choose an explicit menu action when the distinction matters.

**F5** or the refresh icon checks **all saved endpoints**, including entries hidden
by a filter. Click again to cancel. Unsaved quick-connect targets are not included.
This is not subnet scanning or discovery.

| Result | Meaning |
| --- | --- |
| Online | A supported Radmin greeting was received |
| Port open | TCP connected, but a supported greeting was not confirmed |
| Unreachable | The configured port refused, timed out or could not be reached |
| DNS error | The hostname could not be resolved |
| Not checked / Queued / Checking / Cancelled | No completed current result |

Availability does not verify credentials or permissions; an unreachable service
does not prove a powered-off computer. Connect time includes name resolution and
is not ICMP ping. Results are session-only snapshots; hover for details and refresh
when needed. Editing a host or port invalidates its previous result.

## Sign-in and saved passwords

The client supports **Radmin security**, not Windows/NTLM authentication. The
selected account must have permission for the requested mode.

Enable **Save password in OS credential vault** to save only after successful
authentication. Credentials are associated with host, port and username; entries
with the same identity share a saved credential. **Sign in with different
credentials…** opens a fresh login. **Forget saved password** removes the saved
credential for that identity. Unchecking Save removes that credential after a
successful sign-in, rather than on a failed or cancelled attempt.

Rejected credentials require a fresh submission. A missing or locked vault leaves
manual sign-in available. Passwords are not stored in address-book JSON or `.rpb`
files. They may remain temporarily in application memory.

## Desktop sessions

**View Only** displays the desktop without forwarding input. **Full Control**
enables mouse and keyboard input after the display is ready. Multiple independent
sessions may be open at once.

- Choose fit-to-window or actual pixels from the display controls. Actual pixels
  allow scrolling; fit preserves aspect ratio.
- **F11** toggles fullscreen; **Escape** exits it. Move to the top edge to reveal
  the dashboard. Pin it to keep it visible.
- **Show remote cursor** is enabled by default. With the overlay enabled in Full
  Control, the local pointer is hidden over the remote image.
- Open a separate terminal or file-transfer connection from the session controls
  without replacing the desktop session.
- **Disconnect**, or closing the session window, ends that session after cleanup.

F11 is reserved locally. Matching local and remote keyboard layouts is recommended;
international layouts and IME are not fully verified. There is no multi-monitor
selector or special secure-key injection facility.

### Clipboard

Clipboard actions are available in **Full Control** only:

- **Send clipboard** sends local text; paste it in the remote application.
- **Receive clipboard** copies remote text to the local clipboard.
- **Share clipboard automatically** sends the current local text when enabled,
  then watches local changes and polls remote text. Allow roughly a second for
  remote changes. Enabling this in another session transfers sharing ownership.

Automatic sharing starts off in each new session. Only Unicode text is shared,
up to **1 MiB of UTF-16 data**; files, images and rich formatting are excluded.
Choose automatic sharing deliberately because it can send unrelated copied text.
A missing reply is not interpreted as an empty clipboard. A timed-out receive
pauses polling until its reply arrives or the session is reconnected.

## File transfer

Open **File transfer** from the computer menu or a desktop session. It uses its
own authenticated connection. Browse to a remote absolute drive path and select
a single regular file to upload or download. Drive letters in the selector are
candidates to try, not an enumeration of actual remote drives.

Transfers have a **256 MiB per-file limit** and a **60-second operation deadline**.
Directories are not copied recursively. Uploads receive unique names and refuse
names already present in a preflight listing; atomic remote create-new behavior
has not been established. Unicode and spaces are preserved where valid for Windows
filenames; invalid characters are replaced and long names shortened.

Cancelling closes the dedicated transfer connection. A failed or cancelled upload
may leave a partial new remote file; inspect it before removing it manually. A
restart does not resume transfers.

## Remote terminal

Choose **Terminal** in the manager or **Open terminal** from a desktop session.
The account needs the server's **Telnet permission**; the client opens a separate
authenticated Radmin connection.

- Type directly in the terminal pane and press **Enter** to submit one command.
- **Up/Down** browse this window's history and restore the draft. Normal editing
  keys modify the pending command while received output remains protected.
- **Ctrl+C** copies selected text; without a selection, it sends an interrupt and
  clears the draft. The Stop control also sends an interrupt.
- Paste inserts plain text. Multiline paste is rejected, rather than executing
  several commands automatically.
- Choose an encoding matching the remote shell: **CP437** (default), **UTF-8**,
  **CP862** or **CP1252**. Clear removes displayed output.

This is a **line-oriented console, not a VT/PTY emulator**. ANSI screen control,
fullscreen terminal programs, IME composition and drag/drop editing are unsupported.
Output and queues are bounded; new windows begin with empty command history.

## Power actions and restart recovery

Choose **Power** in the manager or a connected session. Each action requires
confirmation of its destination and effect:

| Action | Requirements and behavior |
| --- | --- |
| Restart / Shut down / Power off | Radmin Shutdown permission; may force-close programs and lose unsaved work |
| Sleep / Hibernate | Radmin Telnet permission, Windows shutdown privilege, PowerShell/Add-Type and available Windows power state; hibernation must already be enabled |

The application does not enable hibernation or alter power plans. No group/bulk
power actions are sent. Requests are not automatically resent after dispatch.
An acknowledged request is not proof of the final machine state; lost transport
after dispatch can mean an **unknown outcome**. Cancel stops setup/waiting, but
cannot recall an already dispatched action. Actual power-state transitions have
not been fully live-tested.

After an acknowledged or potentially dispatched Restart, **Restart recovery**:

1. Waits for that endpoint to become unreachable.
2. Waits for two consecutive supported Radmin greetings.
3. Reopens the previous View Only, Full Control or Terminal mode.

The monitor runs for up to **five minutes**. A restart from the manager uses the
last desktop/terminal mode for that endpoint, defaulting to Full Control. The
verified restart login is retained temporarily in memory for this recovery; this
does not enable persistent saving. A matching manual reconnection is reused.

Recovery never resends Restart, resumes transfers or replays terminal commands.
Cancel stops recovery without cancelling the remote restart. If no outage is
observed, including a reboot faster than the polling interval, recovery times out.
An outage and return do not prove a reboot, and the reopened session can still
fail authentication or negotiation. There is no general automatic reconnect.

## Import and export `.rpb` bookmarks

Choose **… → Import Radmin bookmarks…** and select a `.rpb` file. Review the preview
before applying it. Existing settings and computers remain; matching group names
are reused and exact duplicate computers are skipped. Same-name computers with
different endpoints remain separate.

**… → Export Radmin bookmarks…** exports all saved entries, regardless of the
current filter. Existing destinations require confirmation; replacement is atomic
after validation. Keep a backup before exchanging files with another application.

Supported fields are names, addresses, ports, usernames, empty folders and folder
hierarchy in Radmin 3.x **version-4 phonebooks**. Nested groups use slash-separated
paths, such as `Office/Floor 2`; `/` represents the root. Every parent group must
exist for export. Native text fields allow 99 UTF-16 code units. Unrepresentable
values, Windows-auth defaults and unsupported relay routing are reported rather
than silently converted. Passwords and display/chat/voice preferences are excluded.
Full original Windows Viewer GUI import/resave remains unverified.

## Tray, quitting and backups

Closing the **manager** hides it to a supported tray and leaves active sessions,
transfers and restart recovery running. Choose **Open viewer** to restore it.
Starting again with the same address book normally activates the existing instance.

Use tray **Quit**, **… → Quit application**, or **Ctrl+Q** to exit completely. Without
tray support, closing the manager exits; if tray support disappears while hidden,
the manager is restored. Individual session windows still disconnect when closed.
Cleanup is cooperative: a pending operation may need to finish or time out.

Quit before copying the address book as a backup. It contains settings, endpoints
and usernames, but no passwords. `.rpb` export is a bookmark exchange, not a full
settings backup. Do not post either file publicly without sanitizing it.
