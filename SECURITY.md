# Security

## Reporting

Do not publish credentials, exploitable private endpoints, authentication material
or unredacted captures in an issue or pull request.

If the repository's **Security → Report a vulnerability** option is available,
use it for a private report. Private reporting availability must be checked on the
repository; this document does not imply that it has been enabled. Otherwise,
ask a maintainer to establish a private channel, sharing only a non-sensitive
description of the affected component until that channel is agreed. No dedicated
security mailbox or response-time commitment is currently published here.

Include the app version/source revision, OS and architecture, affected mode,
impact, minimal synthetic reproduction and whether it requires authentication.
Separate observations from suspected consequences. Use independently generated
fixtures rather than proprietary software or private research captures.

## Maintenance scope

This is an early interoperability project without an LTS or formal supported-version
matrix. Reproduce against the current source when practical and identify older
versions known to be affected. A release or passing test suite is not a security
audit or protocol certification.

## Relevant boundaries

- Radmin-security authentication is implemented; Windows/NTLM authentication is
  not. The viewer does not grant server permissions or replace server licensing.
- Remote messages and imported address books are untrusted input. Parser limits,
  validation and explicit unsupported results are part of the implementation's
  security boundary.
- Password saving uses approved OS-vault backends, only after successful login,
  with no plaintext fallback. Passwords are not saved in JSON or `.rpb` exports.
  Temporary process-memory copies exist; secure zeroization is not promised.
- Address books still contain potentially sensitive hostnames and usernames.
  Local file protections and OS account security remain relevant.
- Automatic clipboard sharing is opt-in and can transmit newly copied text.
  Remote commands, control, uploads and power requests act with the server account's
  permissions. An unknown power outcome must not trigger an automatic retry.
- A unique upload name and a preflight listing do not establish an atomic remote
  create-new guarantee. Failed uploads may leave partial files.

Build-time dependency scanning and bundled-license review belong to each release.
See [release validation](docs/build-release.md) for the required distinction between
smoke checks, desktop behavior and live interoperability.
