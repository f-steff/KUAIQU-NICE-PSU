---
name: kuaiqus-psu
description: Use when operating or controlling the KUAIQU-NICE-POWER SPPS series DC power supply via scripts/kuaiqus_psu.ps1, including finding the COM port, reading or setting voltage/current, powering the output on/off, power-cycling a load, ramping a voltage or current setpoint over time (-RampV/-RampI, with -Wait or -StopRamp), monitoring status, and managing the persistent backend service.
---

# KUAIQU PSU Controller

> **Upstream source:** [f-steff/KUAIQU-NICE-PSU](https://github.com/f-steff/KUAIQU-NICE-PSU)
> (`skills/kuaiqus-psu/SKILL.md`). This file is copied into other repositories
> together with the `scripts/` and `skills/` files it documents. The GitHub
> repository is authoritative: fix issues there and re-copy, so the mirrors do
> not drift apart.

Use the repository root as the working directory. The tool is self-contained in
`scripts/`: the CLI reference is `scripts/kuaiqus_psu.md` and the wire protocol
is `scripts/kuaiqus_psu_protocol.md`. On Linux/macOS invoke the script through
`pwsh`, e.g. `pwsh ./scripts/kuaiqus_psu.ps1 -Get`.

> **Sourcing note.** The hardware details and the communication protocol
> documented here were gathered from various sources and forums around the web,
> because the published information is scarce and inconsistent, and were
> confirmed by bench testing where noted. Treat them as community knowledge
> rather than official manufacturer specifications.

## First Steps

1. Read `scripts/kuaiqus_psu.md` for the CLI reference and
   `scripts/kuaiqus_psu_protocol.md` for the wire protocol.
2. If the PSU's CP2102 bridge appears under Windows *Other Devices* without a
   driver, install the Silicon Labs CP210x driver first
   (<https://www.silabs.com/developers/usb-to-uart-bridge-vcp-drivers>).
3. Discover the PSU's port (COM numbers and `/dev` names vary per PC, dock, USB
   port, and OS; never assume a fixed one):
   `.\scripts\kuaiqus_psu.ps1 -ListPorts` then
   `.\scripts\kuaiqus_psu.ps1 -ListPorts -Probe`
4. Confirm the backend is not already holding the port:
   `.\scripts\kuaiqus_psu.ps1 -Status`

## Common Commands

- Start the backend (this is the only command that does):
  `.\scripts\kuaiqus_psu.ps1 -Connect -Port COM7`
- Read output voltage / current / CV-CC state:
  `.\scripts\kuaiqus_psu.ps1 -Get`
- Set voltage and current:
  `.\scripts\kuaiqus_psu.ps1 -SetVoltage 4.58`
  `.\scripts\kuaiqus_psu.ps1 -SetCurrent 0.5`
- Enable / disable the output:
  `.\scripts\kuaiqus_psu.ps1 -Power On`
  `.\scripts\kuaiqus_psu.ps1 -Power Off`
- Power-cycle the load (output off for N seconds, then on):
  `.\scripts\kuaiqus_psu.ps1 -PowerCycle 5`
- Ramp a setpoint (output relay untouched, so it works with the output off or on):
  `.\scripts\kuaiqus_psu.ps1 -RampV 0 12 30`      (returns at once; backend ramps)
  `.\scripts\kuaiqus_psu.ps1 -RampV 0 12 30 -Wait` (blocks until finished)
  `.\scripts\kuaiqus_psu.ps1 -RampI 0 1 10`
  `.\scripts\kuaiqus_psu.ps1 -StopRamp`            (stop early; setpoint stays put)
- Confirm the device responds:
  `.\scripts\kuaiqus_psu.ps1 -Probe`
- Save / list / restore / delete sessions:
  `.\scripts\kuaiqus_psu.ps1 -Persist "Bench 5V" -Comment "main load"`
  `.\scripts\kuaiqus_psu.ps1 -List`
  `.\scripts\kuaiqus_psu.ps1 -Restore "Bench 5V"`
  `.\scripts\kuaiqus_psu.ps1 -Persist "Bench 5V" -Delete`
- Machine-readable output: append `-Json`

## Ramps

- Without `-Wait` the command returns immediately and the backend performs the
  ramp. Poll `-Get` for progress (status prints a `Ramp ...` line while one runs).
- A voltage ramp owns the voltage setpoint, a current ramp owns the current
  setpoint, so both can run at once.
- `-SetVoltage` during a voltage ramp cancels that ramp and keeps the value you
  set; a second `-RampV` replaces the running one, continuing from the setpoint
  the supply reached. `-Power On/Off` and `-PowerCycle` never cancel a ramp.
- Do not assume a setpoint is final while a ramp is running - check `-Get` first.

## Output contract

- Use `-Json` for anything programmatic. Keys are always present: `cancelledRamps`
  is `[]` when nothing was interrupted, `replaced` is `null` when a ramp replaced
  nothing, `ramps` is `{}` when none is known. Absent never means "nothing
  happened", and failures carry the same payload plus `ok: false` and `error`.
- In text mode, stdout is the command's own result and is identical for a given
  command; side effects (a ramp cancelled by a set command, a replaced ramp) are
  written to stderr prefixed `note:`. Do not parse stdout line counts.
- Exit codes: `0` success, `2` the command ran but failed (including a `-Wait` ramp
  that was cancelled before finishing), `3` frontend/usage error.

## Backend Service

- `-Connect` starts the backend (and locks the front panel). Every other
  hardware command (`-Get`, `-Probe`, `-SetVoltage`, `-SetCurrent`, `-Power`,
  `-Monitor`) requires a running backend and fails with "run -Connect first".
- `-Disconnect` stops the backend, releases the serial port, and unlocks the
  front panel. `-Status`, `-List`, `-ListPorts`, and `-Persist` never start it.
- Run `-Status` before direct hardware work. If a backend is already running it
  owns the port, so `-Connect` reuses it instead of opening the port twice.
- `-ListPorts` is offline and never opens a port unless `-Probe` is given. A scan
  uses read-only queries and never enters remote mode, so it cannot lock a front
  panel. It cannot open a port the backend already owns; stop the backend first.
- On Linux/macOS the detached backend writes its output to
  `scripts/.kuaiqus_psu_backend.out.log` and
  `scripts/.kuaiqus_psu_backend.err.log` when troubleshooting.
- If the front panel stays locked after `-Disconnect`, power-cycle the PSU (the
  firmware can get stuck, e.g. after probing).
- Restart the backend with `-Disconnect` after changing backend/protocol code; a
  running backend keeps executing the old code until stopped.

## Output Rules

- Default output is readable text; add `-Json` only for machine-readable output.
- Exit codes: `0` success; `2` command ran but failed (device timeout, etc.);
  `3` frontend error (bad arguments, missing Python, backend start failure).

## Safety Notes

- Setting voltage/current and enabling the output acts on the live supply. Only
  drive a connected load intentionally.
- The value scaling (millivolts / milliamperes) and several query-format details
  are documented in `scripts/kuaiqus_psu_protocol.md` as bench-verification items;
  treat undocumented function codes (5, 6) as unsupported.
