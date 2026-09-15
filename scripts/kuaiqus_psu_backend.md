# KUAIQU PSU Backend Contract

> **Upstream source:** [f-steff/KUAIQU-NICE-PSU](https://github.com/f-steff/KUAIQU-NICE-PSU)
> (`scripts/kuaiqus_psu_backend.md`). This file is copied into other repositories
> together with the `scripts/` and `skills/` files it documents. The GitHub
> repository is authoritative: fix issues there and re-copy, so the mirrors do
> not drift apart.

`kuaiqus_psu_backend.py` is the single owner of the RS232 / USB (CP2102) serial
port.
It runs in two modes:

- **one-shot** - `python3 kuaiqus_psu_backend.py <command> --port COM6 ...` runs one
  command and prints JSON to stdout.
- **serve** - a persistent process that keeps the port open and accepts
  JSON-line commands over a localhost TCP control socket. This is what
  `kuaiqus_psu.ps1` and `kuaiqus_psu_mcp.py` use.

The PowerShell frontend is the supported entry point; run the backend directly
only for debugging.

> **Sourcing note.** The hardware details and the communication protocol
> documented here were gathered from various sources and forums around the web,
> because the published information is scarce and inconsistent, and were
> confirmed by bench testing where noted. Treat them as community knowledge
> rather than official manufacturer specifications.

## Commands

| Command | Data | Description |
| --- | --- | --- |
| `serve` | - | Start persistent mode |
| `probe` | `port`, `baud` | Open and confirm device responds |
| `status` | - | Read voltage + current + CV/CC |
| `readVoltage` | - | Read voltage |
| `readCurrent` | - | Read current |
| `readVersion` | - | Read firmware version |
| `setVoltage` | `value` (V) | Set voltage (enters remote mode first) |
| `setCurrent` | `value` (A) | Set current (enters remote mode first) |
| `powerOn` | - | Enable output (enters remote mode first) |
| `powerOff` | - | Disable output (enters remote mode first) |
| `powerCycle` | `offSeconds`, `settleSeconds` | Output off, then on again |
| `rampVoltage` | `from`, `to`, `seconds`, `stepMs`, `wait` | Ramp the voltage setpoint |
| `rampCurrent` | `from`, `to`, `seconds`, `stepMs`, `wait` | Ramp the current-limit setpoint |
| `stopRamp` | `kind` (optional) | *(serve only)* cancel a running ramp |
| `connect` | - | Enter remote control mode (locks front panel) |
| `disconnect` | - | Send the disconnect frame |
| `shutdown` | - | *(serve only)* stop the backend |

## One-shot invocation

```powershell
python3 scripts/kuaiqus_psu_backend.py status --port COM6 --baud 9600
```

Prints a single JSON result object to stdout and exits:

- `0` on success, `2` on command failure, `1` on internal error.

## Serve mode

```powershell
python3 scripts/kuaiqus_psu_backend.py serve --port COM6 --baud 9600 `
  --serve-port 17332 --serve-state-file scripts/.kuaiqus_psu_backend.state.json
```

- Opens the port once and keeps it open.
- Serializes commands from all clients onto one queue, so concurrent requests
  cannot interleave frames on the shared serial port or consume each other's
  responses.
- Prints a single `{"ok":true,"mode":"serve","event":"listening",...}` line to
  stdout on startup.
- Writes a **state file** (`--serve-state-file`) containing `pid`, `host`,
  `port`, and `startedAt` so clients can discover the endpoint (including when
  `--serve-port 0` selects an ephemeral port). It is JSON because the backend
  writes it in Python and both the PowerShell CLI and the MCP server read it,
  unlike the hand-editable INI session file.
- Removes the state file on shutdown.

### Idle watchdog

`--serve-idle-timeout <seconds>` (default `300`, `0` disables) shuts the backend
down after that many seconds without a control request, so a forgotten service
does not hold the port forever. A running background ramp produces no client
traffic, so the watchdog checks for one and defers instead of stopping the
backend mid-ramp.

### Background ramps

`rampVoltage` / `rampCurrent` accept `wait`:

- `wait: false` (default): the reply comes back immediately with the accepted
  plan (`status: "running"`, `started: true`) and a worker thread performs the
  ramp.
- `wait: true`: the same worker performs it, but the reply is withheld until the
  ramp ends. `ok` is true only when `status` is `completed`; a cancelled ramp
  answers with `ok: false` and an `error` explaining why it ended early.

Two properties matter for anything scripting this:

- **The port lock is taken per step, not for the whole ramp.** Reads such as
  `status` are answered between steps, so a ramp does not block the socket. The
  cost is that each foreground command adds its own duration as jitter to the
  step it lands in; step deadlines are absolute, so the ramp does not drift.
- **Ownership is per setpoint.** A voltage ramp owns the voltage setpoint and a
  current ramp owns the current setpoint, so both can run at once:

| Request | Effect on a running ramp |
| --- | --- |
| `rampVoltage` while a voltage ramp runs | Cancels it and starts a new one **from the last acknowledged setpoint**, ignoring the requested `from`. The reply carries `replaced` describing the abandoned ramp. |
| `setVoltage` / `setCurrent` | Cancels the ramp **of that kind only**, then performs the write. The reply carries `cancelledRamps`. |
| `powerOn` / `powerOff` / `powerCycle` | No effect; ramps are setpoint-only. |
| `stopRamp` (optional `kind`) | Cancels that kind, or both. |
| `shutdown` | Ramps are cancelled before the port is released. |

A cancel waits for the in-flight acknowledged write to finish, so the supply is
always left at a setpoint the device confirmed.

### Reply keys are invariant

A reply's key set does not depend on hidden state, because "absent" is a weaker
signal than "present but empty" - a consumer would otherwise have to tell a
command that cancelled nothing apart from an older backend that never reported
cancellations:

| Key | Always present on | Value when nothing happened |
| --- | --- | --- |
| `cancelledRamps` | `setVoltage`, `setCurrent`, `stopRamp` | `[]` |
| `replaced` | `rampVoltage`, `rampCurrent` | `null` |
| `ramps` | `status` | `{}` |
| `error`, `remainingSeconds`, `replaced` | every entry in `ramps` | `null` |

`status` reports progress under `ramps`:

```json
{"kind":"voltage","command":"rampVoltage","status":"running","active":true,
 "fromVolts":0.0,"toVolts":12.0,"atVolts":4.0,"seconds":30.0,"steps":121,
 "stepMs":250,"writesDone":40,"percent":33.1,"elapsedSeconds":9.9,
 "remainingSeconds":20.1}
```

One-shot mode always waits, because the process exits when the command returns:
`--no-wait` and `stopRamp` are rejected there with a message pointing at `serve`.

## Control socket protocol

The control socket is a plain TCP connection speaking **newline-delimited JSON**:
one UTF-8 JSON object per line in, one JSON object per line out.

Request (one line):

```json
{"command":"status"}
```

Response (one line, newlines escaped away):

```json
{"ok":true,"command":"status","voltageVolts":4.58,"voltageMilli":4580,"currentAmps":0.183,"currentMilli":183,"cvCc":"CV"}
```

Errors return `ok:false` with an `error` string:

```json
{"ok":false,"error":"No response from device within 2000 ms"}
```

Malformed JSON is answered with `{"ok":false,"error":"invalid JSON request"}`.

## Result shapes

| Command | Success fields |
| --- | --- |
| `status` | `ok`, `command`, `voltageVolts`, `voltageMilli`, `currentAmps`, `currentMilli`, `cvCc` |
| `readVoltage` | `ok`, `command`, `voltageVolts`, `voltageMilli`, `cvCc` |
| `readCurrent` | `ok`, `command`, `currentAmps`, `currentMilli`, `cvCc` |
| `readVersion` | `ok`, `command`, `version`, `versionRaw` |
| `setVoltage` | `ok`, `command`, `requestedVolts` |
| `setCurrent` | `ok`, `command`, `requestedAmps` |
| `probe` | `ok`, `command`, `port`, `baud`, `responded`, optional `voltageVolts`/`currentAmps`/`cvCc` |
| `powerOn` / `powerOff` / `connect` / `disconnect` | `ok`, `command` |
| `rampVoltage` / `rampCurrent` (started) | `ok`, `command`, `from*`, `to*`, `seconds`, `steps`, `stepMs`, `status`, `started`, `waited`, optional `replaced` |
| `rampVoltage` / `rampCurrent` (`wait`) | the progress shape below, plus `waited`, and `ok` only when `status` is `completed` |
| `stopRamp` | `ok`, `command`, `stopped`, `cancelledRamps[]` |
| `status` (during a ramp) | the fields above plus `ramps` keyed by `voltage` / `current` |

## Protocol notes

- Frames are built by `kuaiqus_psu_protocol.py`; the backend only owns transport
  and lifecycle. See [kuaiqus_psu_protocol.md](kuaiqus_psu_protocol.md) for the
  wire format.
- Set commands wait for the `<11OK...>` / `<13OK...>` acknowledgement and fail
  if it does not arrive within `--timeout-ms`.
- `powerOn`, `powerOff`, `connect`, and `disconnect` are fire-and-forget: the
  manual documents no acknowledgement for them.
- The value field uses **millivolt/milliamp** units (six digits + `"000"`); see the
  protocol reference for the corrected scaling.

## Bench-verification TODOs

- Confirm the `<091...>` remote-mode handshake is required before set commands on
  the specific unit (the vendor software sends it).
