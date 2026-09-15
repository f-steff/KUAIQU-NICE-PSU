# AGENTS.md - KUAIQU-NICE-PSU

Repository description and agent entry point. Development details (commands,
coding style, testing, commits) live in
[`docs/development.md`](docs/development.md) - follow that document when making
changes.

## What This Is

Software controller for the **KUAIQU-NICE-POWER SPPS series** programmable DC
power supply (developed and verified against an SPPS3010D). It provides a
non-blocking PowerShell CLI that runs on Windows, Linux, and macOS, a persistent
Python backend that owns the RS232 / USB (CP2102) serial port, and an MCP stdio
server so the supply can be driven by AI agents and other MCP clients.

The backend is the **only** serial-port owner. The CLI and MCP server never open
the port directly; they talk to the backend over a localhost control socket, so
multiple clients can never race on the same port.

## Entry Points

- `scripts/kuaiqus_psu.ps1` - non-blocking PowerShell CLI (Windows/Linux/macOS),
  including port discovery (`-ListPorts [-Probe]`).
- `scripts/kuaiqus_psu_backend.py` - persistent Python backend; `serve` mode owns
  the port and exposes the JSON-line TCP control socket.
- `scripts/kuaiqus_psu_protocol.py` - side-effect-free ASCII protocol codec and
  the single source of truth for frame layout and value scaling.
- `scripts/kuaiqus_psu_mcp.py` - MCP stdio server that proxies to the backend.
- `scripts/kuaiqus_psu.ini` - local remembered session/port state (git-ignored).

## Repository Layout

```
docs/      product, platform, protocol, backend, and development documentation
scripts/   the self-contained tool: CLI, backend, MCP server, codec + references
tests/     hardware-free unit tests and the PowerShell CLI smoke test
skills/    agent-skill source/staging area (opencode / Codex / Claude Code)
```

`scripts/` and `skills/` are self-contained: they can be copied out of the
repository and used on their own, so they must not link to `docs/` or the root
README. Documentation elsewhere may link into `scripts/`. The `.md` files in
those two directories name this GitHub repository as their upstream, because
copies of them live in other repositories.

## Conventions To Preserve

Two rules are easy to break by accident and expensive to notice:

- **Reply keys are invariant.** A command's JSON keys must not depend on hidden
  state: `cancelledRamps` is `[]` when nothing was interrupted, `replaced` is
  `null` when a ramp replaced nothing, `ramps` is `{}` when no ramp is known, and
  ramp entries always carry `error` and `remainingSeconds` (null when they do not
  apply). Failures return the backend payload with `ok: false` and `error` added,
  not a reduced object. Never make a key conditional to "save noise".
- **stdout is the result, stderr is the note.** For a given command stdout stays
  identical whatever the hidden state; side effects (a ramp cancelled by a set
  command, a ramp replaced by a new one) go to stderr prefixed `note:`, and are
  suppressed entirely under `-Json`. `tests/cli_test.ps1` asserts this against
  `tests/mock_backend.py`, so a regression fails the suite.
- **Interpreters are chosen by capability, not by name.** Resolve the first of
  `python3`/`python`/`py` that can `import serial`; several Pythons share one PATH
  and the first `python3` is often not the one with pyserial. An interpreter named
  explicitly (`-Python`, `KUAIQUS_PSU_PYTHON`) is authoritative and must fail
  rather than be silently replaced.

## Documentation Map

- [README.md](README.md) - project overview and quick start.
- [docs/KUAIQU_SPPS3010D.md](docs/KUAIQU_SPPS3010D.md) - the supply itself.
- [docs/windows.md](docs/windows.md), [docs/linux.md](docs/linux.md),
  [docs/macos.md](docs/macos.md) - platform setup and port naming.
- [scripts/kuaiqus_psu.md](scripts/kuaiqus_psu.md) - full CLI reference.
- [scripts/kuaiqus_psu_protocol.md](scripts/kuaiqus_psu_protocol.md) - wire
  protocol reference.
- [scripts/kuaiqus_psu_backend.md](scripts/kuaiqus_psu_backend.md) - backend
  contract and control-socket protocol.
- [docs/development.md](docs/development.md) - contributor guide (build/test,
  style, testing, commits).

## AI Agent Compatibility (opencode, Codex & Claude Code)

- This file is the single source of truth for agents. Codex and opencode read it
  natively; [`CLAUDE.md`](CLAUDE.md) at the repo root imports it via `@AGENTS.md`.
- The operating skill for the supply lives in
  [`skills/kuaiqus-psu/SKILL.md`](skills/kuaiqus-psu/SKILL.md); opencode reads
  `skills/` directly through [`opencode.jsonc`](opencode.jsonc), which also
  launches the MCP server.
- The MCP server requires the backend to be running, or
  `KUAIQUS_PSU_SERIAL_PORT` to be set (see `scripts/kuaiqus_psu_mcp.py`) so it can
  auto-start the backend.

## License

MIT - Copyright (c) 2026 Flemming Steffensen. See [`LICENSE`](LICENSE).
