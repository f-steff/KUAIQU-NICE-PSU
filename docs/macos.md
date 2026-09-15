# macOS Notes

How to run the KUAIQU PSU controller on macOS (Apple Silicon and Intel). The
tool itself is documented in [`scripts/kuaiqus_psu.md`](../scripts/kuaiqus_psu.md);
this page covers the macOS-specific setup.

> **Sourcing note.** The hardware details and the communication protocol
> documented here were gathered from various sources and forums around the web,
> because the published information is scarce and inconsistent, and were
> confirmed by bench testing where noted. Treat them as community knowledge
> rather than official manufacturer specifications.

## Prerequisites

- **PowerShell 7+ (`pwsh`)**: `brew install --cask powershell`.
- **Python 3.10+**: `brew install python` (or the python.org installer).
- The `pyserial` package, needed only to talk to real hardware. It is pure
  Python, so no compiler and no architecture-specific build are
  needed:

  ```bash
  python3 -m pip install --user pyserial
  ```

  If the system Python is managed and refuses `--user` installs, use a virtual
  environment: `python3 -m venv .venv && .venv/bin/pip install pyserial`.

## USB device

The PSU's USB port is a **Silicon Labs CP2102** bridge (`VID 10C4`,
`PID EA60`). Depending on the macOS/driver combination it appears as:

| Device name | Driver |
| --- | --- |
| `/dev/cu.SLAB_USBtoUART` | Silicon Labs VCP driver |
| `/dev/cu.usbserial-*` | Apple's built-in USB-serial driver |

On older macOS releases a separately installed Silicon Labs VCP driver may need
to be allowed under **System Settings -> Privacy & Security**. The controller
prefers `/dev/cu.*` over `/dev/tty.*` because call-out devices do not block
waiting for a carrier signal.

## Finding the port

```bash
# Passive table; the PSU's bridge is labelled CP2102 (never opens a port)
pwsh ./scripts/kuaiqus_psu.ps1 -ListPorts

# Active scan; adds a Psu column (True when the port answers)
pwsh ./scripts/kuaiqus_psu.ps1 -ListPorts -Probe
```

Then start the backend on the confirmed port; the CLI remembers it for later
commands:

```bash
pwsh ./scripts/kuaiqus_psu.ps1 -Connect -Port /dev/cu.SLAB_USBtoUART
pwsh ./scripts/kuaiqus_psu.ps1 -Get
pwsh ./scripts/kuaiqus_psu.ps1 -Disconnect
```

## Permissions and backend

No group membership is needed; the console user can open `/dev/cu.*` directly.

`-Connect` starts the Python backend detached. Because macOS does not support
`-WindowStyle`, the backend's output is redirected to
`scripts/.kuaiqus_psu_backend.out.log` and
`scripts/.kuaiqus_psu_backend.err.log` (git-ignored). The backend owns the serial
port until `-Disconnect`, so a `-ListPorts -Probe` scan reports the busy port as
`Psu = False`.
