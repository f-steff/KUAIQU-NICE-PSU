# KUAIQU PSU CLI Tool

> **Upstream source:** [f-steff/KUAIQU-NICE-PSU](https://github.com/f-steff/KUAIQU-NICE-PSU)
> (`scripts/kuaiqus_psu.md`). This file is copied into other repositories together
> with the `scripts/` and `skills/` files it documents. The GitHub repository is
> authoritative: fix issues there and re-copy, so the mirrors do not drift apart.

`kuaiqus_psu.ps1` is the non-blocking command-line interface for the
KUAIQU-NICE-POWER SPPS series DC power supply. It talks to a persistent Python
backend (`kuaiqus_psu_backend.py`) that owns the serial port, so repeated
commands are fast and never re-open the port.

This file is the complete command reference and is intentionally self-contained:
it can be copied out together with the other `kuaiqus_psu*` files in `scripts/`
and used on its own. The wire protocol is documented in
[kuaiqus_psu_protocol.md](kuaiqus_psu_protocol.md) and the backend contract in
[kuaiqus_psu_backend.md](kuaiqus_psu_backend.md).

> **Sourcing note.** The hardware details and the communication protocol
> documented here were gathered from various sources and forums around the web,
> because the published information is scarce and inconsistent, and were
> confirmed by bench testing where noted. Treat them as community knowledge
> rather than official manufacturer specifications.

Multiple commands on one line run **in the order given, including duplicates**:

```powershell
.\scripts\kuaiqus_psu.ps1 -SetVoltage 25 -SetCurrent 10     # voltage, then current
.\scripts\kuaiqus_psu.ps1 -SetVoltage 25 -SetVoltage 25.1   # two voltage writes in order
```

Numeric values accept both `.` and `,` as the decimal separator
(`-SetVoltage 25,2` is 25.2 V).

## Requirements

- PowerShell 7+ (`pwsh`) on Windows, Linux, or macOS. On Linux and macOS invoke
  the script through `pwsh`, e.g. `pwsh ./scripts/kuaiqus_psu.ps1 -Get`.
- Python 3.10+ for the backend.
- A Python interpreter that has `pyserial`. The tool finds one itself: it tries
  `python3`, `python` and `py` and picks the **first that can actually import
  serial**, because several Pythons often share one PATH and the first `python3`
  is frequently not the one with pyserial. Name one explicitly with `-Python
  <path>` or `KUAIQUS_PSU_PYTHON`; an explicit choice is never second-guessed, so
  it fails loudly if that interpreter lacks pyserial.
- The `pyserial` package, needed only for real hardware (the unit tests inject
  fake transports and do not need it):

  ```powershell
  python -m pip install --user pyserial       # Windows
  # sudo apt install python3-serial           # Debian/Ubuntu
  # python3 -m pip install --user pyserial     # macOS
  ```

  There is no package manifest to maintain; `__pycache__/` and an optional
  `.venv/` are git-ignored.

## Quick start

```powershell
# List local serial ports; the PSU's bridge is labelled CP2102 (passive, never opens a port)
.\scripts\kuaiqus_psu.ps1 -ListPorts

# Actively scan candidates to confirm which one is the PSU
.\scripts\kuaiqus_psu.ps1 -ListPorts -Probe

# Start the backend and lock the front panel (remote control mode)
.\scripts\kuaiqus_psu.ps1 -Connect -Port COM7

# Set voltage and current, then power on
.\scripts\kuaiqus_psu.ps1 -SetVoltage 4.58
.\scripts\kuaiqus_psu.ps1 -SetCurrent 0.5
.\scripts\kuaiqus_psu.ps1 -Power On

# Read output voltage / current / CV-CC state
.\scripts\kuaiqus_psu.ps1 -Get

# Stop the backend when done (also unlocks the front panel)
.\scripts\kuaiqus_psu.ps1 -Disconnect
```

## Port discovery

`-ListPorts` maps local serial ports to friendly identities so a stable role can
be found without hard-coding a port name (COM numbers and `/dev` names change
across PCs, docks, USB ports, and operating systems). The PSU's USB interface is
a **Silicon Labs CP2102** USB-to-UART bridge (`VID 10C4`, `PID EA60`), and its
port is labelled `CP2102` in the `Chipset` column:

```
Port Chipset Description
---- ------- -----------
COM3          XDS110 Class Auxiliary Data Port (COM3)
COM7 CP2102   Silicon Labs CP210x USB to UART Bridge (COM7)
```

- **Windows** lists `COMx` ports and flags a CP2102 that is present but has no
  driver (Windows error code 28). Such a device is hidden under *Other Devices*
  and has no COM port, so the listing prints a warning telling you to install the
  Silicon Labs CP210x driver.
- **Linux** prefers the stable `/dev/serial/by-id/*` symlinks over `/dev/ttyUSBn`
  and also lists any remaining `.NET`-visible ports.
- **macOS** prefers `/dev/cu.*` (call-out devices that do not block waiting for a
  carrier) over `/dev/tty.*`.

`-ListPorts -Probe` opens each candidate at 9600 8N1 and sends only read
queries (read voltage, then read version if the first is silent); it never enters
remote mode, so it cannot lock a front panel. It adds a `Psu` column (`True`
when the port answered). Debug ports (XDS110/JTAG) are skipped. A port already
owned by the backend fails to open and is simply reported `Psu = False`; stop
the backend before scanning.

With `-Json`, both forms emit machine-readable output; `driverless` is present
only when a driverless CP2102 was found:

```json
{ "ports": [ { "Port": "COM7", "Chipset": "CP2102", "Psu": true, "Description": "..." } ],
  "driverless": [ "CP2102 USB to UART Bridge Controller" ] }
```

Do not hard-code a port. Resolve the CP2102 port here first, then pass it to
`-Connect -Port`; after that the CLI remembers the port in its session file.

## Commands

**Connection**

| Command | Description |
| --- | --- |
| `-Connect` | Start the backend and lock the front panel (remote control mode) |
| `-Disconnect` | Stop the backend and unlock the front panel |
| `-Status` | Show backend + device state **without starting it** |

**Measurement**

| Command | Description |
| --- | --- |
| `-Get` | Read output voltage, current, and CV/CC state |
| `-GetVersion` | Read the device firmware version |
| `-Monitor` | Poll status repeatedly until Ctrl+C |
| `-Probe` | Confirm the configured device responds (requires `-Connect` first) |

**Control**

| Command | Description |
| --- | --- |
| `-SetVoltage <v>` | Set output voltage in volts |
| `-SetCurrent <a>` | Set output current limit in amperes |
| `-Power <On\|Off>` | Enable or disable the output |
| `-PowerCycle <sec>` | Output off for `<sec>` seconds, then on again |
| `-RampV <from> <to> <sec>` | Ramp the voltage setpoint over `<sec>` seconds |
| `-RampI <from> <to> <sec>` | Ramp the current-limit setpoint over `<sec>` seconds |
| `-Wait` | Ramps only: block until the ramp finishes (see [Ramp behaviour](#ramp-behaviour)) |
| `-StopRamp` | Stop a running ramp; the setpoint stays where it reached |

**Sessions**

| Command | Description |
| --- | --- |
| `-Persist <"name">` | Save the last-known settings as a named session |
| `-Persist <name> -Delete` | Delete a named session |
| `-Persist <name> -Comment <"text">` | Save with an optional comment |
| `-List` | List saved sessions |
| `-Restore <"name">` | Load a session and apply its voltage/current |

**Information**

| Command | Description |
| --- | --- |
| `-ListPorts [-Probe]` | List local serial ports; `-Probe` scans candidates for the PSU |
| `-Version` | Print tool version |
| `-Help` | Print usage |

## Command examples

Every transcript below was captured from a real SPPS3010D (firmware 1.3) with no
load attached. Windows examples use `.\scripts\...`; on Linux/macOS the same
commands run as `pwsh ./scripts/...` - these were captured on Linux.

### Before a backend is running

`-Version`, `-Help`, `-ListPorts`, `-Status`, `-List` and `-Persist` never start
the backend and never open a port.

```
$ ./scripts/kuaiqus_psu.ps1 -Version
kuaiqus_psu 1.0.0
```

`-ListPorts` is passive - it only reads what the OS knows:

```
$ ./scripts/kuaiqus_psu.ps1 -ListPorts

Port                                                                                     Chipset Description
----                                                                                     ------- -----------
/dev/serial/by-id/usb-FTDI_TTL232R-3V3_FTF8YNMI-if00-port0                                       usb-FTDI_TTL232...
/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0  CP2102  usb-Silicon_Lab...
/dev/serial/by-id/usb-Texas_Instruments_XDS110__03.00.00.43__Probe_with_CMSIS-DAP-if00           usb-Texas_Instr...
/dev/serial/by-id/usb-WeAct_Studio_USB2CANV1_ComPort_AAAD203D387C-if00                           usb-WeAct_Studi...
```

`-ListPorts -Probe` adds a `Psu` column by briefly opening each candidate with
read-only queries (it never switches the device to remote mode). It cannot open
a port the backend already owns, so run it **before** `-Connect`:

```
$ ./scripts/kuaiqus_psu.ps1 -ListPorts -Probe

Port                                                                                     Chipset   Psu Description
----                                                                                     -------   --- -----------
/dev/serial/by-id/usb-FTDI_TTL232R-3V3_FTF8YNMI-if00-port0                                       False usb-FTDI_...
/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0  CP2102   True  usb-Silic...
/dev/serial/by-id/usb-WeAct_Studio_USB2CANV1_ComPort_AAAD203D387C-if00                           False usb-WeAct...
```

```
$ ./scripts/kuaiqus_psu.ps1 -Status
PSU backend: not running (run -Connect to start it).
```

`-Help` prints the full option list (abridged here; the tables above cover the
same ground):

```
$ ./scripts/kuaiqus_psu.ps1 -Help
kuaiqus_psu.ps1 - KUAIQU-NICE-POWER SPPS series DC power supply controller

Usage: kuaiqus_psu.ps1 <command> [<command> ...] [options]

Connection:
  -Connect                        Start the backend and lock the front panel
  -Disconnect                     Stop the backend and unlock the front panel
  -Status                         Show backend + device state
...
Control:
  -RampV <startV> <endV> <sec>    Ramp the voltage setpoint over <sec> seconds
  -RampI <startA> <endA> <sec>    Ramp the current setpoint over <sec> seconds
  -Wait                           Ramps only: wait for the ramp to finish. Without
                                  it the command returns at once and the backend
                                  performs the ramp (watch it with -Get).
  -StopRamp                       Stop a running ramp; the setpoint stays where the
                                  ramp had reached
...
Exit codes: 0 success, 2 command ran but failed, 3 frontend error.
```

A hardware command without a backend fails cleanly with exit code 3 instead of
starting anything:

```
$ ./scripts/kuaiqus_psu.ps1 -Get
ERROR: PSU backend is not running. Run -Connect first (e.g. -Connect -Port COM7).
$ echo $LASTEXITCODE
3
```

### Connecting

```
$ ./scripts/kuaiqus_psu.ps1 -Connect -Port /dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0
Connected (remote control mode). Front panel is locked.

$ ./scripts/kuaiqus_psu.ps1 -Status
PSU backend: running on 127.0.0.1:17332
Voltage : 0.000 V (set ?? V)
Current : 0.000 A (set ?? A)
State   : CV
```

### Measurement

```
$ ./scripts/kuaiqus_psu.ps1 -Get
Voltage : 0.000 V (set ?? V)
Current : 0.000 A (set ?? A)
State   : CV

$ ./scripts/kuaiqus_psu.ps1 -GetVersion
Firmware : 1.3

$ ./scripts/kuaiqus_psu.ps1 -Probe
Port /dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0 @ 9600: responded
Voltage : 0.000 V (state CV)
Current : 0.000 A
```

`-Monitor` polls until Ctrl+C. Each line shows `measured/set`:

```
$ ./scripts/kuaiqus_psu.ps1 -Monitor
09:22:56  2.000/0.000 V  0.000/0.500 A  CV
09:22:57  2.000/0.000 V  0.000/0.500 A  CV
09:22:58  2.000/0.000 V  0.000/0.500 A  CV
```

### Control

```
$ ./scripts/kuaiqus_psu.ps1 -SetVoltage 5 -SetCurrent 0.5
Set voltage to 5.000 V
Set current to 0.500 A

$ ./scripts/kuaiqus_psu.ps1 -Get
Voltage : 0.000 V (set 5.000 V)
Current : 0.000 A (set 0.500 A)
State   : CV

$ ./scripts/kuaiqus_psu.ps1 -Power On
Output: on

$ ./scripts/kuaiqus_psu.ps1 -Power Off
Output: off
```

A `-Get` issued immediately after `-Power On` can catch the output still
settling - here the supply was charging its output capacitance at the 0.5 A
limit, so the reading is not yet the steady-state value:

```
$ ./scripts/kuaiqus_psu.ps1 -Get
Voltage : 0.040 V (set 5.000 V)
Current : 0.500 A (set 0.500 A)
State   : CV
```

`-PowerCycle` drops the output for the given time, brings it back and then
measures, so the reply shows what the load came back to:

```
$ ./scripts/kuaiqus_psu.ps1 -PowerCycle 2
Power cycled: off 2.0 s, settle 2.0 s
Voltage : 5.000 V
Current : 0.000 A
```

### Ramps

Without `-Wait` the command returns immediately (see [Ramp behaviour](#ramp-behaviour) for the
ownership and cancellation rules):

```
$ ./scripts/kuaiqus_psu.ps1 -RampV 0 5 8
Ramp started: voltage 0.000 -> 5.000 V over 8.0 s (41 steps of 200 ms)
The backend is running it; watch with -Get, stop with -StopRamp, or use -Wait to block.

$ ./scripts/kuaiqus_psu.ps1 -Get
Voltage : 1.870 V (set 5.000 V)
Current : 0.000 A (set 0.500 A)
State   : CV
Ramp voltage: running 0.000 -> 5.000 V, at 2.000 V (41.5%, step 17 of 41), 4.5 s left

$ ./scripts/kuaiqus_psu.ps1 -StopRamp
Ramp voltage: cancelled 0.000 -> 5.000 V, at 2.250 V (46.3%, step 19 of 41)

$ ./scripts/kuaiqus_psu.ps1 -StopRamp
No ramp was running.
```

With `-Wait` the command blocks until the ramp has finished:

```
$ ./scripts/kuaiqus_psu.ps1 -RampV 0 2 3 -Wait
Ramped voltage 0.000 -> 2.000 V over 3.0 s (16 steps of 200 ms)

$ ./scripts/kuaiqus_psu.ps1 -RampI 0 0.5 3 -Wait
Ramped current 0.000 -> 0.500 A over 3.0 s (16 steps of 200 ms)
```

A second ramp of the same kind replaces the running one, continuing from the
setpoint the supply actually reached. The replacement is reported as a note on
stderr, so the `Ramp started:` result on stdout stays the same as for any other
ramp (see [Output contract](#output-contract)):

```
$ ./scripts/kuaiqus_psu.ps1 -RampV 0 3 10          # stdout
Ramp started: voltage 2.100 -> 3.000 V over 10.0 s (51 steps of 200 ms)
The backend is running it; watch with -Get, stop with -StopRamp, or use -Wait to block.
                                                   # stderr
note: replaced the running voltage ramp at 2.100 V (18.2% done); the new ramp starts there.
```

A manual set takes the setpoint back, and says which ramp it stopped - again as a
note, so its own result line never changes:

```
$ ./scripts/kuaiqus_psu.ps1 -SetVoltage 6          # stdout
Set voltage to 6.000 V
                                                   # stderr
note: Ramp voltage: cancelled 0.000 -> 12.000 V, at 2.200 V (19.0%, step 23 of 121)
```

Both kinds can run at once, because they own different setpoints:

```
$ ./scripts/kuaiqus_psu.ps1 -Get
Voltage : 0.000 V (set 10.000 V)
Current : 0.000 A (set 1.000 A)
State   : CV
Ramp voltage: running 0.000 -> 10.000 V, at 1.667 V (17.4%, step 21 of 121), 20.3 s left
Ramp current: running 0.000 -> 1.000 A, at 0.158 A (16.5%, step 20 of 121), 20.5 s left
```

### Sessions

```
$ ./scripts/kuaiqus_psu.ps1 -Persist "Bench 5V" -Comment "main load"
Persisted session 'Bench 5V'.

$ ./scripts/kuaiqus_psu.ps1 -List

Name     Voltage Current Comment
----     ------- ------- -------
Bench 5V 5       0.5     main load

$ ./scripts/kuaiqus_psu.ps1 -Restore "Bench 5V"
Restored session 'Bench 5V'.

$ ./scripts/kuaiqus_psu.ps1 -Persist "Bench 5V" -Delete
Deleted session 'Bench 5V'.
```

### Disconnecting

```
$ ./scripts/kuaiqus_psu.ps1 -Disconnect
Disconnected (local control mode). Front panel is unlocked.
```

### `-Json` output

Any command accepts `-Json` for machine-readable output:

```
$ ./scripts/kuaiqus_psu.ps1 -Get -Json
{
  "ok": true,
  "command": "status",
  "voltageVolts": 5.0,
  "voltageMilli": 5000,
  "currentAmps": 0.0,
  "currentMilli": 0,
  "cvCc": "CV",
  "setVoltageVolts": 5.0,
  "setCurrentAmps": 0.5
}
```

```
$ ./scripts/kuaiqus_psu.ps1 -RampV 2 0 4 -Json
{
  "ok": true,
  "command": "rampVoltage",
  "fromVolts": 2.0,
  "toVolts": 0.0,
  "seconds": 4.0,
  "steps": 21,
  "stepMs": 200,
  "status": "running",
  "started": true,
  "waited": false
}
```

```
$ ./scripts/kuaiqus_psu.ps1 -StopRamp -Json
{
  "ok": true,
  "command": "stopRamp",
  "cancelledRamps": [
    {
      "kind": "voltage",
      "command": "rampVoltage",
      "status": "cancelled",
      "active": false,
      "fromVolts": 2.0,
      "toVolts": 0.0,
      "atVolts": 2.0,
      "seconds": 4.0,
      "steps": 21,
      "stepMs": 200,
      "writesDone": 1,
      "percent": 4.8,
      "elapsedSeconds": 0.063,
      "reason": "stopRamp"
    }
  ],
  "stopped": 1
}
```

```
$ ./scripts/kuaiqus_psu.ps1 -List -Json
{
  "Name": "Bench 5V",
  "Voltage": "5",
  "Current": "0.5",
  "Comment": "main load"
}
```

## Ramp behaviour

`-RampV` and `-RampI` walk a **setpoint** linearly from one value to another over
a duration. They never touch the output relay, so a ramp is valid with the output
both off (pre-staging a profile) and on (a live ramp).

```powershell
# Start a ramp and get the prompt straight back; the backend performs it
.\scripts\kuaiqus_psu.ps1 -RampV 0 12 30

# Watch it (status reports any running ramp)
.\scripts\kuaiqus_psu.ps1 -Get

# Block until it has finished
.\scripts\kuaiqus_psu.ps1 -RampV 0 12 30 -Wait

# Stop early; the setpoint stays where the ramp had reached
.\scripts\kuaiqus_psu.ps1 -StopRamp
```

**`-Wait` or not.** Without `-Wait` the command returns as soon as the backend
has accepted the ramp, which is what makes a 10-minute profile usable from a
script or an agent. With `-Wait` the frontend blocks until the ramp finishes; the
wait is capped at the ramp duration plus a margin, so a wedged backend reports a
timeout instead of hanging the shell. A waiting ramp is still cancellable from
another shell.

**Who owns a setpoint.** A voltage ramp owns the voltage setpoint and a current
ramp owns the current setpoint, so the two can run at the same time. That is what
makes the interruption rules predictable:

| Event during a running ramp | Effect |
| --- | --- |
| `-RampV` while a voltage ramp runs | Replaces it, **starting from the setpoint the supply is at** - not from the requested start, which would step the output backwards. The reply says what it replaced. |
| `-SetVoltage` while a voltage ramp runs | Cancels the voltage ramp and leaves the value you asked for |
| `-SetVoltage` while only a *current* ramp runs | Nothing is cancelled; separate setpoints |
| `-Power On`/`-Off`, `-PowerCycle` | Never cancel a ramp; a ramp is setpoint-only |
| `-StopRamp` | Cancels both kinds and reports where each stopped |
| `-Disconnect` | Stops the backend, which cancels any ramp first |

Progress appears in `-Get` / `-Status` output, because a background ramp moves the
setpoint without any command of its own:

```
Voltage : 4.021 V (set 12.000 V)
Current : 0.000 A (set 1.000 A)
State   : CV
Ramp voltage: running 0.000 -> 12.000 V, at 4.000 V (33.3%, step 40 of 121), 20.0 s left
```

Steps are bounded (120 by default) because every step is an acknowledged write,
and roughly five per second is all the device absorbs. When the bound applies the
interval is stretched rather than the ramp shortened, so a ramp always takes the
duration you asked for. `-StepMs` overrides the interval.

One-shot backend invocations always wait: a one-shot process exits as soon as the
command returns, so `--no-wait` there is rejected rather than abandoning a ramp.

## Output contract

The tool is driven both by people and by scripts and agents, so the two output
streams have distinct jobs:

| Stream | Content |
| --- | --- |
| **stdout** | The command's own result. For a given command this is stable: `-SetVoltage 6` always prints exactly `Set voltage to 6.000 V`, whether or not it interrupted a ramp. |
| **stderr** | Side effects and errors, side effects prefixed `note:` - for example a ramp that a set command cancelled, or a ramp replaced by a new one. Nothing is written here in `-Json` mode. |

So a set command that cancels a running ramp is never silent, yet parsing or
diffing stdout stays reliable:

```
$ ./scripts/kuaiqus_psu.ps1 -SetVoltage 6
Set voltage to 6.000 V                                            # stdout
note: Ramp voltage: cancelled 0.000 -> 12.000 V, at 1.700 V ...   # stderr
```

`-Json` is the contract for machines, and its **keys are always present**:

| Key | Always on | Value when nothing happened |
| --- | --- | --- |
| `cancelledRamps` | `setVoltage`, `setCurrent`, `stopRamp` | `[]` |
| `replaced` | ramp starts | `null` |
| `ramps` | `status` (`-Get`, `-Status`) | `{}` |
| `error`, `remainingSeconds` | every ramp entry | `null` |

An absent key therefore never means "nothing happened" - it means you are talking
to something other than this version. Failures keep the same shape as successes:
the backend payload is returned with `ok: false` and `error` added, so a cancelled
`-Wait` ramp still reports `status`, `writesDone` and the setpoint it reached.

## Output

`-Get`, `-Status`, and `-Monitor` report the **measured** output. Because the
protocol has no "read setpoint" command, the last-known set values are tracked
locally (in the `[default]` session) and shown alongside the measurement. Until
the first set is issued - or after `-Disconnect` - they appear as `??`:

```
Voltage : 0.047 V (set ?? V)
Current : 0.000 A (set ?? A)
State   : CV
```

`-Json` adds `setVoltageVolts` / `setCurrentAmps` (null until first set).

## Lifecycle and the front-panel lock

`-Connect` is the only command that starts the backend; every other hardware
command requires it and fails with "run -Connect first" if the backend is not
running. Controlling the supply remotely locks the front panel; `-Disconnect`
returns it to local control and unlocks the panel.

```powershell
.\scripts\kuaiqus_psu.ps1 -Connect -Port COM7            # start backend + lock panel
.\scripts\kuaiqus_psu.ps1 -SetVoltage 12 -SetCurrent 2   # panel is locked
.\scripts\kuaiqus_psu.ps1 -Get                           # read actual + set values
.\scripts\kuaiqus_psu.ps1 -Disconnect                    # stop backend + unlock panel
```

`-Disconnect` stops the backend and clears the tracked set values.
`-ListPorts -Probe` never enters remote mode, so it cannot lock the front panel.

> If the front panel stays locked after `-Disconnect`, power-cycle the PSU - the
> firmware can get stuck, for example after an interrupted session.

## Options

| Option | Default | Description |
| --- | --- | --- |
| `-Port <COMx>` | *(remembered)* | Serial port; used by `-Connect` to start the backend |
| `-Baud <n>` | `9600` | Baud rate (1200, 2400, 4800, 9600, 19200) |
| `-BackendHost <host>` | `127.0.0.1` | Control socket host |
| `-BackendPort <n>` | `17332` | Control socket port |
| `-TimeoutMs <n>` | `2000` | Device response timeout |
| `-Comment <text>` | | Optional comment stored with `-Persist` |
| `-IniPath <file>` | `scripts/kuaiqus_psu.ini` | Session file |
| `-Json` | off | Machine-readable JSON output |
| `-Version` | | Print tool version |
| `-Help` | | Print usage |

## Sessions (persistence)

The CLI remembers its last-used settings in `scripts/kuaiqus_psu.ini` (a
git-ignored local file next to the script). Every successful `-SetVoltage` /
`-SetCurrent` / `-Power` updates the `[default]` section, including the port and
baud rate.

- **Auto-port:** when `-Port` is omitted, `-Connect` reuses the remembered
  `[default]` port, so a remembered port is opened automatically.
- **`-Persist "<name>" [-Comment "<text>"]`** snapshots the last-known settings
  (voltage/current) into a named session. Names may contain spaces and symbols
  (`#`, `%`, `+`, `-`, ...); they are percent-encoded as the INI section header and
  decoded for display.
- **`-Persist "<name>" -Delete`** removes a named session.
- **`-List`** lists all saved sessions.
- **`-Restore "<name>"`** loads a session and applies its voltage and current
  only - it never enables the output.

```powershell
.\scripts\kuaiqus_psu.ps1 -Connect -Port COM7
.\scripts\kuaiqus_psu.ps1 -SetVoltage 5.0 -SetCurrent 1.0
.\scripts\kuaiqus_psu.ps1 -Persist "Bench +5V/-1A" -Comment "main load"
.\scripts\kuaiqus_psu.ps1 -List
.\scripts\kuaiqus_psu.ps1 -Restore "Bench +5V/-1A"
.\scripts\kuaiqus_psu.ps1 -Persist "Bench +5V/-1A" -Delete
```

`-Persist`, `-List`, and `-Delete` are offline (no hardware); `-Restore` applies
values to the device and therefore needs a running backend.

## Backend lifecycle

- `-Connect` is the **only** command that starts the backend. Every other
  hardware command (`-Get`, `-Probe`, `-SetVoltage`, `-SetCurrent`, `-Power`,
  `-GetVersion`, `-Restore`, `-Monitor`) requires a running backend and fails
  with "run -Connect first" otherwise.
- `-Disconnect` stops the backend and releases the serial port. `-Status`,
  `-List`, `-ListPorts`, and `-Persist` never start it.
- The backend owns the serial port. If it is already running from another shell
  or automation context, `-Connect` reuses it instead of opening the port twice.
- Restart the backend with `-Disconnect` after changing `kuaiqus_psu_backend.py`
  or `kuaiqus_psu_protocol.py`; a running backend keeps executing the old code
  until stopped.
- `-Disconnect` releases the adapter and removes the state file
  (`scripts/.kuaiqus_psu_backend.state.json`).
- On Windows the backend starts hidden. On Linux/macOS it starts detached with
  stdout/stderr redirected to `scripts/.kuaiqus_psu_backend.out.log` and
  `scripts/.kuaiqus_psu_backend.err.log` so the frontend never hangs on the
  inherited pipe; those logs are git-ignored.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success |
| `2` | Command ran but failed (backend returned `ok=false`) - a device timeout, or a `-Wait` ramp cancelled before it finished |
| `3` | Frontend error (bad arguments, no interpreter with pyserial, backend start failure) |

## Serial settings

The PSU uses 8N1 framing. Both RS232 and USB appear as a plain serial port to
the software; there is no software difference between them. Baud rate defaults
to 9600 and must match the device setting. The model was also sold as
RS485-capable, but that path is unconfirmed and not documented.

Port names are platform-specific:

| Platform | Typical PSU port | Notes |
| --- | --- | --- |
| Windows | `COM7` | Requires the Silicon Labs CP210x driver for the CP2102 bridge |
| Linux | `/dev/serial/by-id/usb-Silicon_Labs_CP2102_...` | Add the user to `dialout` (or install a udev rule) |
| macOS | `/dev/cu.SLAB_USBtoUART` or `/dev/cu.usbserial-*` | Prefer `cu.*` over `tty.*` |

## License

MIT - Copyright (c) 2026 Flemming Steffensen. See the repository `LICENSE`.
