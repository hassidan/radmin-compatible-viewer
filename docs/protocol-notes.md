# Protocol notes and compatibility scope

This is a public summary of the independently implemented interoperability surface,
not an official specification or a grant of rights in a vendor protocol. It is
intentionally usable without private research files, captures or vendor binaries.

## How to read the evidence

The implementation was informed by bounded static/native analysis, synthetic
protocol peers, pure-codec and offscreen GUI tests, and selected historical live
checks. Those forms of evidence answer different questions:

| Authority | What it can support | What it cannot establish alone |
| --- | --- | --- |
| Static/native analysis | Candidate formats, constants and control flow for the examined version | Execution of a path, all versions, or a complete protocol standard |
| Synthetic peers and codec tests | Tested parsing, bounds, ordering and failure behavior | Acceptance by an original server |
| Offscreen Qt tests | Widget/worker contracts and tested lifecycle scenarios | Native tray, compositor, keychain or installed-package behavior |
| A recorded live check | The observed operation on that server/environment | Untested modes, platforms, versions or power transitions |

Historical live observations below summarize the prior development checkpoint.
They have not been rerun by preparing this documentation, and private captures
are not published as fixtures. New release claims need their own recorded checks.

## Implemented surfaces

- **Authentication:** Radmin 3 Radmin-security mutual authentication, followed by
  separate encrypted-channel negotiation. The supported authentication group was
  observed on a 3.5.2.1 server; this does not imply support for every Radmin 3 server.
  Windows/NTLM authentication is unsupported.
- **Record and desktop transport:** encrypted records, desktop negotiation,
  bounded decompression and implemented image update encodings. Unknown image
  encodings fail explicitly rather than being interpreted as pixels.
- **Control and cursor:** logical keyboard/mouse encoding and cursor shape,
  hotspot, position and composition. Historical live work covered desktop/control
  and selected cursor behavior; animated cursors were covered offline. Secure-key
  sequences and broad international-layout/IME compatibility are not established.
- **Clipboard:** Unicode text exchange over Full Control, using the desktop's
  existing compression/channel state. Historical live checks included a Unicode
  roundtrip and repeated polling. Inner rich-text/ANSI record identifiers do not
  imply a user-facing rich clipboard feature.
- **File transfer:** a separate service for directory listing and single-file
  upload/download. Local file-size and deadline policies bound resource use.
  Atomic remote create-new, recursive copy and resume are not established.
- **Terminal:** a dedicated authenticated command-shell service with line editing
  in the UI. Historical transport checks included a harmless echo; the newer
  terminal GUI checkpoint relied on deterministic peers rather than a successful
  end-to-end live GUI check. It is not VT/PTY emulation.
- **Power:** native shutdown-mode requests for restart/shutdown/power-off;
  sleep/hibernate use a fixed PowerShell program through the terminal service and
  documented Windows power APIs. Non-disruptive checks established only limited
  mode/capability behavior. Actual native power transitions and full restart recovery
  have not been live-proven by those checks.
- **Phonebooks:** Radmin 3.x version-4 `.rpb` identity fields and hierarchy.
  Development checks covered parsing/roundtrip behavior and native header/record
  reader acceptance under emulation. Full original Windows GUI import/resave is
  still unverified. Unsupported routing/authentication settings are rejected.

## Explicit nonclaims

There is no claim of vendor approval, conformance to a published protocol standard,
complete implementation, universal compatibility, security certification or
ownership of vendor formats. A successful greeting proves neither authorization
nor server state. Availability checks address only explicitly saved endpoints;
they do not discover neighboring machines or enumerate a subnet.

No chat/voice/Send Message GUI, Windows authentication, multi-monitor selector or
general automatic reconnect is provided. Auxiliary helper code is not evidence
that these user-facing features exist. Recovery observes an outage and return;
it does not prove a reboot or replay prior actions.

When extending support, document the precise observation and version/environment,
add redistributable synthetic cases, and retain unresolved conditions. Do not
include original executables, binary dumps, private screenshots or real credentials
to make a public explanation appear more authoritative.
