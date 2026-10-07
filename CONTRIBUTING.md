# Contributing

Contributions should improve a clearly bounded feature, preserve existing data
and keep compatibility claims tied to reproducible evidence.

## Development setup

This release targets Linux x86_64 and Windows x86_64. Python 3.12 is recommended
for development. From the source root, create a virtual environment and install
the application in editable mode:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -v
```

On Windows, create the environment with `py -3.12 -m venv .venv`, install with
`.venv\Scripts\python -m pip install -e .`, then in PowerShell:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.venv\Scripts\python -m unittest discover -s tests -v
Remove-Item Env:QT_QPA_PLATFORM
```

Unset offscreen mode before normal GUI use. Tests use `unittest`; no separate test
runner is required. Run relevant tests while developing and the complete suite
before submitting behavioral changes. Report the actual results, including skips
and platform limitations; do not treat simulated peers as live-server validation.

## Implementation expectations

- Follow the [architecture](docs/architecture.md) and
  [module comments guide](docs/module-comments.md).
- Keep protocol I/O on its owning worker. GUI widgets and OS clipboard access
  stay on the GUI thread. Use cooperative cancellation and bounded work.
- Preserve explicit unsupported outcomes, parser limits and credential-safe error
  messages. A missing reply must not become a fabricated success.
- Add focused tests for changed behavior, including relevant malformed input,
  cancellation or teardown cases. Documentation-only changes need link and
  factual checks rather than new implementation-mirroring tests.
- Update the manual, limitations and changelog when user-visible behavior changes.
- Follow [local build instructions](docs/build-release.md) for packaging changes.
  **Do not add GitHub Actions or `.github/workflows`.** Builds and checks are local.

## Material accepted into the repository

Submit independently authored code and synthetic, redistributable fixtures. Do
not include proprietary executables, vendor artwork, binary dumps, private
research folders, real address books, endpoints, credentials or remote-screen
captures. Public examples use placeholders such as `server.example` and
`operator`. Never convert a private sample into a fixture merely by renaming it.

When describing interoperability evidence, state its scope: static analysis,
synthetic/offscreen tests or an explicitly recorded live check. Explain what is
still unknown. Keep public summaries in [protocol notes](docs/protocol-notes.md),
without requiring access to private research materials.

## Pull requests and ownership

Describe the change, motivation, verification and remaining limitations. Use the
pull-request template; include screenshots only when they show a clean synthetic
environment. Report vulnerabilities through [SECURITY.md](SECURITY.md).

By intentionally submitting a contribution for inclusion, you agree to license
your independently authored contribution under this project's MIT license, unless
an alternative is explicitly agreed before inclusion. You retain your copyright;
submission is not a copyright assignment. Confirm that you have the necessary
rights and identify any third-party material and its license. The project cannot
accept a contribution that purports to relicense Radmin software or trademarks.
