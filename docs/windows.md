# Windows Notes

How to run the KUAIQU PSU controller on Windows. The tool itself is documented
in [`scripts/kuaiqus_psu.md`](../scripts/kuaiqus_psu.md); this page covers the
Windows-specific setup.

> **Sourcing note.** The hardware details and the communication protocol
> documented here were gathered from various sources and forums around the web,
> because the published information is scarce and inconsistent, and were
> confirmed by bench testing where noted. Treat them as community knowledge
> rather than official manufacturer specifications.

## Prerequisites

- **PowerShell 7+ (`pwsh`)**. Windows PowerShell 5.1 is not supported. Install
  with `winget install --id Microsoft.PowerShell` (or from the Microsoft Store).
- **Python 3.10+**, e.g. `winget install --id Python.Python.3.13`.
- The `pyserial` package, needed only to talk to real hardware. Install it with
  pip:

  ```powershell
  python -m pip install --user pyserial
  ```

  **Watch which Python gets it.** Several interpreters commonly share one PATH on
  Windows (python.org, the Microsoft Store, msys2, a venv), and installing into
  one says nothing about the others. This is why the tool does not trust names: it
  probes `python3`, `python` and `py` and uses the first that can `import serial`.
  On a machine where msys2 owns `python3`, a name-based choice would start a
  backend that cannot open a serial port while a working interpreter sits next to
  it. Check what you have with:

  ```powershell
  foreach ($c in 'python3','python','py') {
      $p = (Get-Command $c -ErrorAction SilentlyContinue).Source
      if ($p) { "$c -> $p : $(& $c -c 'import serial; print(serial.__version__)' 2>&1)" }
  }
  ```

  Pin one explicitly if you prefer: `-Python <path>` or `$env:KUAIQUS_PSU_PYTHON`.
  An explicit choice is never silently replaced - it fails if that interpreter has
  no pyserial.

## USB driver (CP2102 bridge)

The PSU's USB port is a **Silicon Labs CP2102** USB-to-UART bridge
(`VID 10C4`, `PID EA60`) - not a CDC class device - so Windows does not
auto-install a driver and lists it under *Other Devices*
("CP2102 USB to UART Bridge Controller") until the Silicon Labs CP210x driver is
present.

1. Download **"CP210x Universal Windows Driver"** (a ZIP) from
   <https://www.silabs.com/developers/usb-to-uart-bridge-vcp-drivers> and extract it.
2. Install it with either method:
   - **Device Manager:** right-click *CP2102 USB to UART Bridge Controller* under
     *Other Devices* -> **Update driver** -> **Browse my computer for drivers** ->
     select the extracted folder (it contains `silabser.inf`).
   - **Command line (admin):** `pnputil /add-driver <extracted>\silabser.inf /install`
3. The device moves to *Ports (COM & LPT)* as
   `Silicon Labs CP210x USB to UART Bridge (COMx)`.

If the bridge is still driverless, `-ListPorts` prints a warning with the same
guidance.

## Finding the port

COM numbers change across PCs, docks, and USB ports, so never hard-code one:

```powershell
# Passive table; the PSU is labelled CP2102 (never opens a port)
.\scripts\kuaiqus_psu.ps1 -ListPorts

# Active scan; adds a Psu column (True when the port answers)
.\scripts\kuaiqus_psu.ps1 -ListPorts -Probe
```

Then start the backend on the confirmed port; the CLI remembers it for later
commands:

```powershell
.\scripts\kuaiqus_psu.ps1 -Connect -Port COM7
.\scripts\kuaiqus_psu.ps1 -Get
.\scripts\kuaiqus_psu.ps1 -Disconnect
```

## Backend

`-Connect` starts the Python backend hidden in the background. It owns the serial
port until `-Disconnect`, so a scan with `-ListPorts -Probe` fails to open the
PSU port while the backend is running (reported `Psu = False`). Restart the
backend after editing backend or protocol code.
