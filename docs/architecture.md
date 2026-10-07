# Architecture

The application separates Qt presentation, worker-owned connections, protocol
codecs and local persistence. Application source is under `radmin_viewer/`; tests
are under `tests/`. The public compatibility boundary is documented in
[protocol notes](protocol-notes.md).

## Module map

| Modules | Responsibility |
| --- | --- |
| `app.py`, `__main__.py` | CLI, address-book manager, session orchestration and application shutdown |
| `application_identity.py`, `runtime_check.py` | Platform identity/launcher and packaged-runtime smoke checks |
| `storage.py`, `bookmarks.py`, `rpb.py` | Versioned JSON validation/atomic writes, non-destructive merge and binary phonebook codec |
| `credentials.py` | OS-vault adapter and login dialog; successful-authentication save policy |
| `scanner.py` | Asynchronous checks of explicit endpoints using Qt networking |
| `protocol.py`, `channel.py` | Radmin-security authentication and encrypted record transport |
| `desktop.py`, `cursor.py`, `input.py` | Desktop negotiation/compression/decoding, cursor composition and input encoding |
| `adapter.py`, `session_ui.py` | Qt image bridge, single-owner desktop worker, framebuffer and session window |
| `clipboard.py`, `clipboard_session.py`, `clipboard_ui.py` | Inner text codec, desktop-message handling and GUI-thread clipboard scheduling |
| `filetransfer.py`, `transfer_ui.py` | Dedicated transfer protocol client, operation worker and two-pane presentation |
| `auxiliary.py`, `terminal_ui.py`, `terminal_editor.py` | Auxiliary modes, terminal worker/decoding and protected line editor |
| `power.py`, `power_ui.py`, `restart_recovery.py` | One-shot power dispatch, confirmation/outcome UI and outage/return monitor |
| `system_tray.py`, `instance_activation.py` | Background-window lifecycle, quit coordination and local instance activation |
| `ui_icons.py`, `ui_dialogs.py`, `window_chrome.py`, `grouped_tiles.py`, `fullscreen_controls.py` | Shared presentation, dialogs and window-scoped controls |

## Connection ownership

```text
GUI thread: manager → session window → bounded input queue
                            ↑                  ↓
                      queued signals     owning worker
                                             ↓
                           RadminSession → EncryptedChannel
                                             ↓
                               desktop / transfer / auxiliary
```

Each authenticated connection and its channel belong to one worker. GUI code must
not read a session socket or share its cipher/compression state. Terminal, transfer
and power operations use dedicated connections instead of multiplexing through
the displayed desktop. Credentials are passed for the operation and references
are cleared when no longer needed; this is not a memory-zeroization guarantee.

Workers report credential-safe status/errors through queued Qt signals. UI code
requests cancellation through events or bounded queues; it does not forcibly
terminate threads. Teardown closes resources on their owning worker and the
manager waits asynchronously before completing exit. OS name resolution may
outlast a socket timeout, so cancellation is not instantaneous.

## Desktop adapter contract

`MainWindow` and `SessionWindow` accept `adapter_factory`. The built-in CLI supplies
`DesktopAdapter`. The factory runs on the connection worker after authentication:

```python
adapter_factory(authenticated_session, mode)  # mode: "view" or "control"

# Required adapter surface:
can_control: bool
def poll(timeout: float) -> QImage | None: ...
def handle_input(event: dict) -> None: ...
def close() -> None: ...
```

Return an owned, decoded image in remote framebuffer coordinates, or `None` for
idle polling. Idle waits are bounded; once a record arrives, its read is governed
by the session I/O timeout. Setup, input and cleanup also need bounds. No adapter
method may manipulate widgets. Cleanup releases held input where possible before
the worker closes the underlying session.

Frames are copied and delivered through queued signals with at most one delivery
outstanding. The worker retains the latest pending frame, preventing an unbounded
GUI backlog. Input uses a bounded queue; overflow requests disconnect rather than
silently dropping a key/button release. View mode does not forward control input.

Input dictionaries use **Qt enum values**, which the adapter translates; they are
not already Windows virtual-key values. Fit scaling maps local pointer positions
to clamped remote coordinates. Focus loss triggers release handling. Optional
`set_show_cursor()` and `take_clipboard_replies()` provide display and clipboard
integration without exposing the socket to the GUI.

## Clipboard and terminal boundaries

`clipboard.py` is an inner codec, not an OS clipboard or network API. Desktop
clipboard messages must be split after shared decompression and before image
decoding. The GUI owns OS clipboard access and automatic-sharing ownership. A
missing remote reply must remain distinct from an explicitly empty text value.

The terminal worker performs transport and bounded output queuing. The GUI decodes
output incrementally and protects received text while allowing line editing.
History belongs to the window; it is not replayed on reconnection. The existence
of auxiliary protocol helpers does not imply a chat or Send Message GUI.

## Persistence and instance lifecycle

`AddressBook` validates structure, size and permitted fields, rejecting duplicate
JSON keys and credential fields. Saves use a same-directory temporary file,
file-content fsync and atomic replacement. New POSIX files have private permissions;
Windows relies on OS ACLs. These mechanisms do not promise crash durability on
every filesystem. Invalid input is preserved and failed saves retain prior UI state.

The manager holds an address-book lock for its lifetime. Instance activation is
scoped to that config and forwards quick-connect metadata without passwords.
Closing the manager can hide it to a supported tray; explicit Quit initiates
worker cleanup, releases resources and then exits.

## Power and recovery state

Power dispatch is one-shot. Acceptance, rejection, unavailability, cancellation
before dispatch and unknown outcomes remain distinguishable. An unknown outcome
after possible dispatch must never cause automatic resend.

Restart monitoring begins only after an accepted or possibly dispatched request.
It observes `WAIT_OFFLINE → WAIT_ONLINE → READY`, with two consecutive recognized
greetings required for return. The monitor neither authenticates nor dispatches
power. The manager opens the replacement session and handles its normal failures.
This is an availability heuristic, not proof that a reboot occurred.

## Verification layers

Unit tests cover pure codecs and state logic; simulated peers exercise transport
and worker behavior; offscreen Qt tests exercise UI orchestration. A frozen smoke
check verifies selected imports/resources. None alone verifies a real server,
native tray, OS vault or completed power transition. Run and record those checks
separately when expanding a compatibility claim.
