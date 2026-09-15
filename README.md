# KUAIQU-NICE-POWER SPPS Series - PSU Controller

Software controller for the KUAIQU-NICE-POWER SPPS series programmable DC power
supply. It provides a non-blocking PowerShell CLI that runs on Windows, Linux,
and macOS, a persistent Python backend that owns the RS232 / USB (CP2102)
serial port, and an MCP stdio server so the supply can be driven by AI agents
and other MCP clients.

The backend is the **only** serial-port owner. The CLI and MCP server never open
the port directly; they talk to the backend over a localhost control socket, so
multiple clients can never race on the same port.

> **Sourcing note.** The hardware details and the communication protocol
> documented here were gathered from various sources and forums around the web,
> because the published information is scarce and inconsistent, and were
> confirmed by bench testing where noted. Treat them as community knowledge
> rather than official manufacturer specifications.

## Documentation

- [docs/KUAIQU_SPPS3010D.md](docs/KUAIQU_SPPS3010D.md) - the supply itself
  (specifications, interfaces, safety).
- [scripts/kuaiqus_psu.md](scripts/kuaiqus_psu.md) - full CLI reference.
- [scripts/kuaiqus_psu_protocol.md](scripts/kuaiqus_psu_protocol.md) - wire
  protocol reference.
- [scripts/kuaiqus_psu_backend.md](scripts/kuaiqus_psu_backend.md) - backend
  contract and control-socket protocol.
- Platform setup: [Windows](docs/windows.md), [Linux](docs/linux.md),
  [macOS](docs/macos.md).
- [docs/development.md](docs/development.md) - contributor guide.

## Architecture

```
                 +----------------------------------------------+
PowerShell CLI --|   kuaiqus_psu_backend.py  (Python)           |
 (non-blocking)  |     - owns the RS232 / USB serial port       |--> RS232 / USB
  kuaiqus_psu.ps1|     - "serve" = persistent JSON-line TCP     |
                 |       control socket + state file            |
MCP stdio ------>|     - "one-shot" = JSON to stdout            |
 kuaiqus_psu_mcp |     - protocol isolated in kuaiqus_psu_      |
                 |       protocol.py                            |
                 +----------------------------------------------+
```

## Quick start

The CLI needs PowerShell 7+ (`pwsh`) on every platform. On Linux/macOS invoke it
as `pwsh ./scripts/kuaiqus_psu.ps1 ...`; the examples below use the Windows
form.

```powershell
# One-time: install the only runtime dependency (needed for real hardware only)
# (only pyserial; the unit tests need nothing at all)
python -m pip install --user pyserial       # Windows
# sudo apt install python3-serial           # Debian/Ubuntu
# python3 -m pip install --user pyserial     # macOS

# Find the PSU's port (passive; never opens a port), then confirm it (active scan)
.\scripts\kuaiqus_psu.ps1 -ListPorts
.\scripts\kuaiqus_psu.ps1 -ListPorts -Probe

# Start the backend and lock the front panel (remote control mode)
.\scripts\kuaiqus_psu.ps1 -Connect -Port COM7

# Set voltage and current, then enable the output
.\scripts\kuaiqus_psu.ps1 -SetVoltage 4.58
.\scripts\kuaiqus_psu.ps1 -SetCurrent 0.5
.\scripts\kuaiqus_psu.ps1 -Power On

# Read output voltage / current / CV-CC state
.\scripts\kuaiqus_psu.ps1 -Get

# Power-cycle the load (output off for 5 s, then on again)
.\scripts\kuaiqus_psu.ps1 -PowerCycle 5

# Ramp a setpoint. Without -Wait the command returns at once and the backend
# performs the ramp; -Get reports its progress, -StopRamp ends it early.
.\scripts\kuaiqus_psu.ps1 -RampV 0 12 30
.\scripts\kuaiqus_psu.ps1 -RampV 0 12 30 -Wait
.\scripts\kuaiqus_psu.ps1 -StopRamp

# Stop the backend when done (also unlocks the front panel)
.\scripts\kuaiqus_psu.ps1 -Disconnect
```

See [scripts/kuaiqus_psu.md](scripts/kuaiqus_psu.md) for the full command
reference.

## Finding the PSU's port

The PSU's USB interface is a **Silicon Labs CP2102** USB-to-UART bridge
(`VID 10C4`, `PID EA60`) - not a CDC class device. On Windows it needs the
Silicon Labs CP210x driver before a COM port appears; Linux and macOS need no
driver on current releases. Never hard-code a port: COM numbers and `/dev` names
change across PCs, docks, and USB ports.

`-ListPorts` labels the bridge `CP2102` in a passive table, and
`-ListPorts -Probe` actively scans the candidates for the supply. Platform
details and driver install steps are in [Windows](docs/windows.md),
[Linux](docs/linux.md), and [macOS](docs/macos.md) notes.

## MCP server

`scripts/kuaiqus_psu_mcp.py` exposes the supply as 12 MCP tools: `get_status`,
`read_voltage`, `read_current`, `read_version`, `set_voltage`, `set_current`,
`power_on`, `power_off`, `power_cycle`, `ramp_voltage`, `ramp_current` and
`stop_ramp`. It proxies to the persistent backend, so either the backend must
already be running, or `KUAIQUS_PSU_SERIAL_PORT` must be set so the MCP server can
auto-start it.

Like the CLI, `ramp_voltage`/`ramp_current` return as soon as the ramp is accepted
unless `wait: true` is passed; poll `get_status` for progress (`ramps`) or end a
ramp with `stop_ramp`. The server itself needs only the standard library - it
resolves an interpreter that has pyserial when it starts the backend.

opencode launches it automatically via [`opencode.jsonc`](opencode.jsonc); add the
`environment` entry there (or the equivalent MCP config for Codex/Claude Code) to
select your port.

## Tests

Hardware-free tests live in [`tests/`](tests/). They use fake transports and never
touch a real serial port.

```powershell
.\tests\run_all_tests.ps1   # Python unit tests + PowerShell CLI smoke test
```

## Repository layout

```
docs/      product, platform, and development documentation
scripts/   self-contained tool: CLI, backend, MCP server, codec + references
tests/     hardware-free unit tests and the CLI smoke test
skills/    agent-skill source/staging area (opencode / Codex / Claude Code)
```

## License

MIT - Copyright (c) 2026 Flemming Steffensen. See [`LICENSE`](LICENSE).
