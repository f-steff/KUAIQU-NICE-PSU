# Development Guide

Contributor and maintainer details for this repository. For what the project is
and how to use it, start at the [README](../README.md); for the tool itself see
[`scripts/kuaiqus_psu.md`](../scripts/kuaiqus_psu.md).

## Build, test, and dependency commands

- Install the one runtime dependency (needed only to talk to real hardware; the
  tests inject fake transports and do not require it), from `scripts/`:

  ```powershell
  python -m pip install --user pyserial       # Windows
  # sudo apt install python3-serial           # Debian/Ubuntu
  # python3 -m pip install --user pyserial     # macOS
  ```

  The only dependency is `pyserial`; there is no package manifest to maintain.
  `__pycache__/` and an optional `.venv/` are git-ignored.

- Run all tests (Python + PowerShell):

  ```powershell
  .\tests\run_all_tests.ps1
  ```

- The suite is hardware-free: protocol/backend/MCP tests use fake transports and
  a stubbed socket, and `tests/mock_backend.py` stands in for the control socket
  when the CLI's output contract is under test.
- Run Python tests only:
  `python3 -m unittest discover -s tests -p "test_*.py"`
- Run the PowerShell CLI smoke test only: `.\tests\cli_test.ps1`
- Discover the PSU's port: `.\scripts\kuaiqus_psu.ps1 -ListPorts` (passive) /
  `.\scripts\kuaiqus_psu.ps1 -ListPorts -Probe` (active scan)
- Read the supply: `.\scripts\kuaiqus_psu.ps1 -Get`

On Linux/macOS use `pwsh` explicitly (e.g.
`pwsh ./tests/run_all_tests.ps1`); all tooling runs on Windows, Linux, and
macOS under PowerShell 7+.

## Coding style and naming conventions

- Python: standard library plus `pyserial` only; keep the protocol codec
  side-effect free and the backend's transport injectable so tests can substitute
  a fake.
- PowerShell: `Set-StrictMode -Version 3.0`,
  `[CmdletBinding(PositionalBinding = $false)]`, param blocks at the top, exit
  codes `0`/`2`/`3` per the CLI documentation.
- Platform code must run under pwsh 7+ on Windows, Linux, and macOS. Guard
  Windows-only cmdlets (CIM, `-WindowStyle`) behind `$script:IsWindowsHost`, and
  keep paths platform-neutral.
- Rationale comments are a global, always-follow rule. For every function or
  major code block added **or edited**, include at least one comment describing
  *why* it exists, not merely what it does. This applies to Python, PowerShell,
  and Markdown alike.
- File headers carry `Version`, `Author: f-steff`, `License: MIT`, and the
  copyright line where a header is conventional (scripts and tests).
- `scripts/` and `skills/` are self-contained: docs may link into `scripts/`, but
  `scripts/` must not link out to `docs/` or the root README, so the tool folder
  can be copied out on its own.

## Testing guidelines

- Tests must run without the PSU attached. Inject fake transports in Python tests;
  never reach for a real COM port. `-ListPorts` tests may list ports but must
  never open one.
- Add a `test_protocol.py` case for any codec change, anchored to concrete vendor
  frames, and a `test_backend.py` case for any command-dispatch or serve-protocol
  change.
- Run `.\tests\run_all_tests.ps1` before finishing script or backend changes.
- Bench validation is manual and separate from the automated suite. The
  value-scaling and query-format ambiguities in
  [`scripts/kuaiqus_psu_protocol.md`](../scripts/kuaiqus_psu_protocol.md) must be
  confirmed against a real unit; do not silently change them to match the
  vendor's inconsistent examples.

## Commit guidelines

- Keep commits focused and logically grouped; use imperative summaries.
- Follow the existing history pattern (e.g. work-item prefixes when present).
- Do not commit `__pycache__/`, `.venv/`, the backend state file,
  `scripts/kuaiqus_psu.ini`, or logs (all git-ignored).

## AI agent compatibility (opencode, Codex & Claude Code)

- [`AGENTS.md`](../AGENTS.md) is the repository description and single source of
  truth for agents. Codex and opencode read it natively; `CLAUDE.md` at the repo
  root imports it via `@AGENTS.md`.
- Skills are authored once under `skills/<name>/SKILL.md` with Codex-compatible
  YAML frontmatter (`name` + `description`). `skills/` is a portable staging
  area: ask an agent to install a skill into the directory its host reads (for
  example Codex's `.agents/skills` or Claude Code's `.claude/skills`), and to
  recreate host-specific extras such as `agents/openai.yaml` there if wanted.
- opencode reads `skills/` directly through `opencode.jsonc` (`skills.paths`); it
  also launches the MCP server declared in the same file.
- The MCP server requires the backend to be running, or
  `KUAIQUS_PSU_SERIAL_PORT` to be set (see `scripts/kuaiqus_psu_mcp.py`) so it can
  auto-start the backend.

## License

MIT - Copyright (c) 2026 Flemming Steffensen. See [`LICENSE`](../LICENSE).
