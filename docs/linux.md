# Linux Notes

How to run the KUAIQU PSU controller on Linux (bench-verified on Ubuntu 24.04;
the same steps apply to other distributions with `pwsh` and Python 3 available).
The tool itself is documented in
[`scripts/kuaiqus_psu.md`](../scripts/kuaiqus_psu.md).

> **Sourcing note.** The hardware details and the communication protocol
> documented here were gathered from various sources and forums around the web,
> because the published information is scarce and inconsistent, and were
> confirmed by bench testing where noted. Treat them as community knowledge
> rather than official manufacturer specifications.

## Prerequisites

- **PowerShell 7+ (`pwsh`)** - invoke the CLI through `pwsh`; there is no
  Windows-only code path left in the tool.
- **Python 3.10+** for the backend.
- The `pyserial` package, needed only for real hardware. Prefer the distribution
  package, because a system Python often ships without `pip` (the bench host does);
  use a virtual environment if you need a newer version than the distro ships:

  ```bash
  sudo apt install python3-serial
  # or: python3 -m venv .venv && .venv/bin/pip install pyserial
  ```

  `python3-serial` is a pure-Python package; no compiler is needed.

## Port discovery

The supply's CP2102 bridge appears as a plain tty. The CLI lists the stable
`/dev/serial/by-id/*` paths first (they survive replugging and reboots) and also
shows any remaining `/dev/ttyUSBn` ports:

```bash
pwsh ./scripts/kuaiqus_psu.ps1 -ListPorts
pwsh ./scripts/kuaiqus_psu.ps1 -ListPorts -Probe   # active scan
```

The by-id path looks like:

```text
/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0
```

Pass that path to `-Connect -Port` (the Python backend accepts the same value):

```bash
pwsh ./scripts/kuaiqus_psu.ps1 -Connect -Port /dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0
pwsh ./scripts/kuaiqus_psu.ps1 -Get
pwsh ./scripts/kuaiqus_psu.ps1 -Disconnect
```

The device answers its own ASCII protocol at **9600 8N1**; it does not speak
SCPI, so `*IDN?` style queries return nothing.

## Permissions

Add the user to the `dialout` group (log out/in afterwards), or install a udev
rule for the bridge:

```bash
sudo usermod -aG dialout "$USER"
# or, immediately effective without a re-login:
sudo chmod a+rw /dev/ttyUSB0
```

`-ListPorts -Probe` needs the same permission: without it every candidate fails
to open and is reported `Psu = False`.

## Backend

`-Connect` starts the Python backend detached. Because Linux does not support
`-WindowStyle`, the backend's output is redirected to
`scripts/.kuaiqus_psu_backend.out.log` and
`scripts/.kuaiqus_psu_backend.err.log` (git-ignored) so the frontend never hangs
on an inherited stdout pipe. The backend owns the serial port until
`-Disconnect`; while it runs, a `-ListPorts -Probe` scan reports the busy port as
`Psu = False`.
