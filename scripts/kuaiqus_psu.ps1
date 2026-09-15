<#
.SYNOPSIS
  Non-blocking CLI for the KUAIQU-NICE-POWER SPPS series DC power supply.

.DESCRIPTION
  Frontend for scripts\kuaiqus_psu_backend.py. The backend is a persistent Python
  process that owns the RS232 / USB (CP2102) serial port and serves JSON-line commands
  over a localhost control socket. The backend is started explicitly with -Connect;
  every other hardware command requires a running backend. Output is readable text
  by default (or raw JSON with -Json).

  -ListPorts lists local serial ports and identifies the PSU's CP2102 bridge;
  -ListPorts -Probe actively scans candidates for the supply without needing a
  running backend, so a stable port can be found before -Connect. The script runs
  under pwsh 7+ on Windows, Linux, and macOS.

  Multiple commands given on one line run in the order they appear, including
  duplicates (e.g. -Connect -SetVoltage 25 -SetCurrent 10). Both '.' and ',' are
  accepted as the decimal separator in numeric values (e.g. -SetVoltage 25,2).

.NOTES
  Version: 1.0.0
  Author: f-steff
  License: MIT
  Copyright: (c) 2026 Flemming Steffensen
#>

# No param() block: this is a simple script so $args preserves every token in the
# order it was typed, including duplicate switches and comma decimals. Arguments
# are parsed into settings and an ordered command list below.

Set-StrictMode -Version 3.0
# Platform flag. $IsWindows exists only in PowerShell 6+; Windows PowerShell 5.1
# (Desktop edition) has no such variable, so treat a missing value as Windows.
# Every platform branch below keys off $script:IsWindowsHost so this same file
# runs under pwsh 7+ on Windows, Linux, and macOS.
$script:IsWindowsHost = if (Test-Path variable:IsWindows) { $IsWindows } else { $true }
# Force dot decimal separators in numeric output regardless of the host locale
# (e.g. a Danish system would otherwise print "0,000 V" instead of "0.000 V").
[System.Threading.Thread]::CurrentThread.CurrentCulture = [System.Globalization.CultureInfo]::InvariantCulture
$script:ExitCode = 0
$script:BackendScript = Join-Path $PSScriptRoot "kuaiqus_psu_backend.py"
$script:StateFile = Join-Path $PSScriptRoot ".kuaiqus_psu_backend.state.json"
$script:VersionString = "1.0.0"

# Settings (last occurrence wins).
$Json = $false
$Port = ""
$Baud = 9600
$BackendHost = "127.0.0.1"
$BackendPort = 17332
$TimeoutMs = 2000
$MonitorIntervalMs = 1000
# Interpreter for the backend. Empty means "probe for one that has pyserial";
# KUAIQUS_PSU_PYTHON does the same job as an environment variable.
$Python = ""
$Comment = ""
$IniPath = ""
# Ramp step interval; 0 lets the backend pick its default (200 ms per step, which is
# as fast as the device can acknowledge a set command).
$StepMs = 0
# -Wait applies to the ramp commands. Without it the frontend returns as soon as the
# backend has accepted the ramp and the backend performs it; with it the frontend
# blocks until the ramp has finished.
$Wait = $false

# Ordered commands: each element is @{ Name = ...; Value = ... }.
$script:Cmds = New-Object System.Collections.Generic.List[object]

function Add-Cmd([string]$Name, [string]$Value) {
    $script:Cmds.Add(@{ Name = $Name; Value = $Value }) | Out-Null
}

# Report a command-line usage error with guidance and exit as a frontend error.
# Writes directly to stderr so the message is clean (no PowerShell function-name
# prefix or error-record noise).
function Write-UsageError([string]$Message) {
    if ($Json) {
        @{ ok = $false; error = $Message } | ConvertTo-Json
    }
    else {
        [Console]::Error.WriteLine("ERROR: $Message Run '-help' for usage.")
    }
    exit 3
}

# Switches that consume the following argument as their value.
$script:ValueSwitches = @(
    "port", "baud", "backendhost", "backendport", "timeoutms",
    "monitorintervalms", "comment", "inipath", "persist", "restore", "python",
    "setvoltage", "setcurrent", "power", "powercycle", "stepms"
)

# Switches that consume the next three arguments (start, end, duration).
$script:TripleSwitches = @("rampv", "rampi")

# Parse $args. Each token is matched by name (leading dashes stripped,
# case-insensitive), so -SetVoltage / --setvoltage / -SETVOLTAGE are equivalent.
for ($i = 0; $i -lt $args.Count; $i++) {
    $tok = [string]$args[$i]
    $name = $tok.TrimStart('-').ToLowerInvariant()

    if ($script:TripleSwitches -contains $name) {
        # -RampV / -RampI take three values: start, end and duration in seconds.
        # A value written "25,2" in-session arrives as the array @(25,2), so array
        # elements are rejoined with ',' to recover the literal text.
        $triple = @()
        while ($triple.Count -lt 3 -and $i + 1 -lt $args.Count) {
            $next = $args[$i + 1]
            if ($next -is [System.Array]) {
                $triple += ($next -join ",")
                $i++
            }
            elseif (-not ([string]$next).StartsWith("-")) {
                $triple += [string]$next
                $i++
            }
            else {
                break
            }
        }
        if ($triple.Count -ne 3) {
            Write-UsageError "Option '$tok' requires three values: <start> <end> <seconds>."
        }
        Add-Cmd $name ($triple -join " ")
        continue
    }

    if ($script:ValueSwitches -contains $name) {
        # Read the following token as this switch's value. A value written
        # "25,2" in-session is parsed by PowerShell as the array @(25,2), so join
        # array elements with ',' to recover the literal text.
        $val = ""
        if ($i + 1 -lt $args.Count) {
            $next = $args[$i + 1]
            if ($next -is [System.Array]) {
                $val = $next -join ","
                $i++
            }
            elseif (-not ([string]$next).StartsWith("-")) {
                $val = [string]$next
                $i++
            }
        }
        if ([string]::IsNullOrWhiteSpace($val)) {
            Write-UsageError "Option '$tok' requires a value."
        }
        switch ($name) {
            "port" { $Port = $val }
            "baud" { $Baud = $val }
            "backendhost" { $BackendHost = $val }
            "backendport" { $BackendPort = $val }
            "timeoutms" { $TimeoutMs = $val }
            "monitorintervalms" { $MonitorIntervalMs = $val }
            "python" { $Python = $val }
            "comment" { $Comment = $val }
            "inipath" { $IniPath = $val }
            "persist" { Add-Cmd "persist" $val }
            "restore" { Add-Cmd "restore" $val }
            "setvoltage" { Add-Cmd "setvoltage" $val }
            "setcurrent" { Add-Cmd "setcurrent" $val }
            "power" { Add-Cmd "power" $val }
            "powercycle" { Add-Cmd "powercycle" $val }
            "stepms" { $StepMs = $val }
        }
        continue
    }

    switch ($name) {
        "help" { Add-Cmd "help" "" }
        "h" { Add-Cmd "help" "" }
        "version" { Add-Cmd "version" "" }
        "listports" { Add-Cmd "listports" "" }
        "probe" {
            # -Probe directly after -ListPorts upgrades that listing to an active
            # scan; anywhere else it keeps its backend-confirmation meaning.
            if ($script:Cmds.Count -gt 0 -and $script:Cmds[$script:Cmds.Count - 1].Name -eq "listports") {
                $script:Cmds[$script:Cmds.Count - 1]["Probe"] = $true
            }
            else {
                Add-Cmd "probe" ""
            }
        }
        "status" { Add-Cmd "status" "" }
        "get" { Add-Cmd "get" "" }
        "getversion" { Add-Cmd "getversion" "" }
        "monitor" { Add-Cmd "monitor" "" }
        "list" { Add-Cmd "list" "" }
        "connect" { Add-Cmd "connect" "" }
        "disconnect" { Add-Cmd "disconnect" "" }
        "stopramp" { Add-Cmd "stopramp" "" }
        "wait" { $Wait = $true }
        "delete" {
            # -Delete modifies the immediately preceding -Persist command.
            if ($script:Cmds.Count -gt 0 -and $script:Cmds[$script:Cmds.Count - 1].Name -eq "persist") {
                $script:Cmds[$script:Cmds.Count - 1]["Delete"] = $true
            }
            else {
                Write-UsageError "-Delete must follow -Persist <name>."
            }
        }
        "json" { $Json = $true }
        default { Write-UsageError "Unknown option '$tok'." }
    }
}

$script:IniPath = if (-not [string]::IsNullOrWhiteSpace($IniPath)) { $IniPath } else { (Join-Path $PSScriptRoot "kuaiqus_psu.ini") }

# -Wait only has a meaning for the ramp commands, so a lone -Wait is a usage error
# rather than a silently ignored option.
if ($Wait) {
    $rampCount = @($script:Cmds | Where-Object { $_.Name -eq "rampv" -or $_.Name -eq "rampi" }).Count
    if ($rampCount -eq 0) {
        Write-UsageError "-Wait applies to -RampV / -RampI, and neither was given."
    }
}

# Convert a raw value string to a double, accepting both '.' and ',' as the
# decimal separator (used by -SetVoltage / -SetCurrent). Returns $null when the
# value is not a number so callers can report a clean frontend error.
function ConvertTo-PsuNumber([string]$Text, [string]$Label) {
    $normalized = $Text.Trim().Replace(",", ".")
    $number = [double]0
    if ([double]::TryParse($normalized, [System.Globalization.NumberStyles]::Float, [System.Globalization.CultureInfo]::InvariantCulture, [ref]$number)) {
        return $number
    }
    return $null
}

# Print usage. Kept compact because the tool is usually run against a live bench.
function Show-Help {
    @"
kuaiqus_psu.ps1 - KUAIQU-NICE-POWER SPPS series DC power supply controller

Usage: kuaiqus_psu.ps1 <command> [<command> ...] [options]

Connection:
  -Connect                        Start the backend and lock the front panel
  -Disconnect                     Stop the backend and unlock the front panel
  -Status                         Show backend + device state

Measurement:
  -Get                            Read voltage, current, and CV/CC state
  -GetVersion                     Read the firmware version
  -Monitor                        Poll status until Ctrl+C
  -Probe                          Confirm the device responds (needs -Connect)

Control:
  -SetVoltage <volts>             Set output voltage (e.g. 4.58)
  -SetCurrent <amps>              Set output current limit (e.g. 0.5)
  -Power <On|Off>                 Enable or disable the output
  -PowerCycle <seconds>           Output off for <seconds>, then on again
  -RampV <startV> <endV> <sec>    Ramp the voltage setpoint over <sec> seconds
  -RampI <startA> <endA> <sec>    Ramp the current setpoint over <sec> seconds
  -Wait                           Ramps only: wait for the ramp to finish. Without
                                  it the command returns at once and the backend
                                  performs the ramp (watch it with -Get).
  -StopRamp                       Stop a running ramp; the setpoint stays where the
                                  ramp had reached

Sessions:
  -Persist <"name"> [-delete]     Save a named session, or delete it with -delete
  -Persist <name> [-comment <"text">]
                                  Save with an optional comment
  -List                           List saved sessions
  -Restore <"name">               Apply a saved session's voltage/current

Information:
  -ListPorts [-Probe]             List local serial ports; -Probe scans each
                                  candidate for the PSU (no backend needed)
  -Version                        Print tool version
  -Help                           Print this help

Options:
  -Port <COMx>                    Serial port (used by -Connect to start the backend)
  -Baud <n>                       Baud rate (1200..19200, default 9600)
  -BackendHost <host>             Control socket host (default 127.0.0.1)
  -BackendPort <n>                Control socket port (default 17332)
  -TimeoutMs <n>                  Device response timeout (default 2000)
  -StepMs <n>                     Ramp step interval (default 200, the fastest the
                                  device acknowledges set commands)
  -Python <path>                  Interpreter for the backend (default: the first
                                  of python3/python/py that has pyserial)
  -IniPath <file>                 Session file (default scripts\kuaiqus_psu.ini)
  -Json                           Machine-readable JSON output

Notes:
  - Multiple commands run in order, including duplicates
    (e.g. -SetVoltage 25 -SetCurrent 10).
  - Both '.' and ',' work as the decimal separator (e.g. -SetVoltage 25,2).
  - -Connect starts the backend; every other hardware command needs it.
  - -ListPorts is offline and never opens a port unless -Probe is given.
  - Ramps change only the setpoint, so they work with the output off or on.
  - The backend is Python (kuaiqus_psu_backend.py) and needs Python 3 with
    pyserial: pip install pyserial
  - If the front panel stays locked after -Disconnect, power-cycle the PSU
    (the firmware can get stuck, e.g. after probing).

Exit codes: 0 success, 2 command ran but failed, 3 frontend error.
"@
}

# ---------------------------------------------------------------------------
# Session persistence (kuaiqus_psu.ini)
# ---------------------------------------------------------------------------

# Minimal INI reader: sections -> ordered key/value maps. Comments (#/;) and
# blank lines are skipped; section names are preserved verbatim (encoded).
function ConvertTo-IniMap([string]$Path) {
    $map = [ordered]@{}
    if ([string]::IsNullOrWhiteSpace($Path) -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $map
    }
    $section = ""
    foreach ($rawLine in Get-Content -LiteralPath $Path) {
        $line = $rawLine.Trim()
        if ([string]::IsNullOrWhiteSpace($line) -or $line.StartsWith("#") -or $line.StartsWith(";")) {
            continue
        }
        if ($line.StartsWith("[") -and $line.EndsWith("]")) {
            $section = $line.Substring(1, $line.Length - 2).Trim()
            if (-not $map.Contains($section)) {
                $map[$section] = [ordered]@{}
            }
            continue
        }
        $eq = $line.IndexOf("=")
        if ($eq -lt 1 -or [string]::IsNullOrWhiteSpace($section)) {
            continue
        }
        $key = $line.Substring(0, $eq).Trim()
        $value = $line.Substring($eq + 1).Trim()
        $map[$section][$key] = $value
    }
    return $map
}

# Encode a free-form session name into an INI-safe section identifier. Anything
# outside [A-Za-z0-9._-] is percent-encoded (UTF-8), so spaces, #, %, +, - and
# non-ASCII all round-trip through an INI section header.
function Encode-IniName([string]$Name) {
    $sb = New-Object System.Text.StringBuilder
    foreach ($ch in $Name.ToCharArray()) {
        if ([string]$ch -match '[A-Za-z0-9._-]') {
            [void]$sb.Append($ch)
        }
        else {
            foreach ($b in [System.Text.Encoding]::UTF8.GetBytes([string]$ch)) {
                [void]$sb.Append(('%{0:X2}' -f $b))
            }
        }
    }
    return $sb.ToString()
}

# Reverse of Encode-IniName.
function Decode-IniName([string]$Encoded) {
    $bytes = New-Object System.Collections.Generic.List[byte]
    $i = 0
    while ($i -lt $Encoded.Length) {
        $c = $Encoded[$i]
        if ($c -eq '%' -and ($i + 2) -lt $Encoded.Length) {
            $bytes.Add([Convert]::ToByte($Encoded.Substring($i + 1, 2), 16))
            $i += 3
        }
        else {
            $bytes.Add([byte][char]$c)
            $i++
        }
    }
    return [System.Text.Encoding]::UTF8.GetString($bytes.ToArray())
}

# Write (or replace) one INI section's key/value pairs, preserving other sections
# and their order. The section is located by its header and spliced in place so
# blank separators and neighbouring sections are left intact.
function Write-IniSection([string]$Path, [string]$Section, [System.Collections.IDictionary]$Keys) {
    $lines = @()
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        $lines = @(Get-Content -LiteralPath $Path)
    }
    $newSection = [System.Collections.Generic.List[string]]::new()
    [void]$newSection.Add("[$Section]")
    foreach ($k in $Keys.Keys) { [void]$newSection.Add("$k=$($Keys[$k])") }

    # Locate the target section's line range: from its header to the next header
    # (or end of file). Blank/comment lines inside are absorbed into the replace.
    $start = -1
    $end = $lines.Count
    for ($i = 0; $i -lt $lines.Count; $i++) {
        $t = $lines[$i].Trim()
        if ($t.StartsWith("[") -and $t.EndsWith("]")) {
            if ($start -ge 0) { $end = $i; break }
            if ([string]::Equals($t.Trim("[", "]"), $Section, [System.StringComparison]::OrdinalIgnoreCase)) {
                $start = $i
            }
        }
    }

    $output = [System.Collections.Generic.List[string]]::new()
    if ($start -ge 0) {
        for ($i = 0; $i -lt $start; $i++) { [void]$output.Add($lines[$i]) }
        foreach ($l in $newSection) { [void]$output.Add($l) }
        for ($i = $end; $i -lt $lines.Count; $i++) { [void]$output.Add($lines[$i]) }
    }
    else {
        foreach ($l in $lines) { [void]$output.Add($l) }
        if ($output.Count -gt 0 -and -not [string]::IsNullOrWhiteSpace($output[$output.Count - 1])) {
            [void]$output.Add("")
        }
        foreach ($l in $newSection) { [void]$output.Add($l) }
    }
    Set-Content -LiteralPath $Path -Value $output -Encoding UTF8
}

# Remove an entire section (its header plus all keys) from the INI, preserving all
# other sections and their order. Used by -Persist <name> -Delete.
function Remove-IniSection([string]$Path, [string]$Section) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return }
    $lines = @(Get-Content -LiteralPath $Path)

    # Locate the target section's line range: from its header to the next header
    # (or end of file).
    $start = -1
    $end = $lines.Count
    for ($i = 0; $i -lt $lines.Count; $i++) {
        $t = $lines[$i].Trim()
        if ($t.StartsWith("[") -and $t.EndsWith("]")) {
            if ($start -ge 0) { $end = $i; break }
            if ([string]::Equals($t.Trim("[", "]"), $Section, [System.StringComparison]::OrdinalIgnoreCase)) {
                $start = $i
            }
        }
    }
    if ($start -lt 0) { return }

    $output = [System.Collections.Generic.List[string]]::new()
    for ($i = 0; $i -lt $start; $i++) { [void]$output.Add($lines[$i]) }
    for ($i = $end; $i -lt $lines.Count; $i++) { [void]$output.Add($lines[$i]) }

    # Drop a single trailing blank line left behind by the removed section.
    if ($output.Count -gt 0 -and [string]::IsNullOrWhiteSpace($output[$output.Count - 1])) {
        $output.RemoveAt($output.Count - 1)
    }
    Set-Content -LiteralPath $Path -Value $output -Encoding UTF8
}

# Merge new values into the [default] section (the last-used runtime state) and
# stamp an update time. Empty/whitespace values are not persisted.
function Update-DefaultSession([hashtable]$Updates) {
    $ini = ConvertTo-IniMap $script:IniPath
    $default = if ($ini.Contains("default")) { $ini["default"] } else { [ordered]@{} }
    foreach ($k in $Updates.Keys) {
        $v = [string]$Updates[$k]
        if (-not [string]::IsNullOrWhiteSpace($v)) {
            $default[$k] = $v
        }
    }
    $default["UpdatedAt"] = (Get-Date).ToString("o")
    Write-IniSection -Path $script:IniPath -Section "default" -Keys $default
}

# Forget the tracked set values (voltage/current/power) after the backend closes,
# since the device's setpoint is no longer known once the CLI stops controlling it.
# The remembered port/baud are kept for the next auto-open.
function Clear-DefaultSetValues {
    $ini = ConvertTo-IniMap $script:IniPath
    if (-not $ini.Contains("default")) { return }
    $d = $ini["default"]
    $keep = [ordered]@{}
    if ($d.Contains("Port")) { $keep["Port"] = $d["Port"] }
    if ($d.Contains("Baud")) { $keep["Baud"] = $d["Baud"] }
    $keep["UpdatedAt"] = (Get-Date).ToString("o")
    Write-IniSection -Path $script:IniPath -Section "default" -Keys $keep
}

# Resolve the backend endpoint from its state file. Returns a hashtable or null.
function Get-BackendEndpoint {
    if (-not (Test-Path -LiteralPath $script:StateFile -PathType Leaf)) {
        return $null
    }
    try {
        $state = Get-Content -LiteralPath $script:StateFile -Raw | ConvertFrom-Json
        if ($state -and $state.port) {
            return @{ Host = [string]$state.host; Port = [int]$state.port }
        }
    }
    catch {
        # Stale or malformed state file; treat as not running.
    }
    return $null
}

# Resolve the control endpoint, state file first.
#
# The state file is authoritative for a backend this tool started. Without one, the
# configured -BackendHost/-BackendPort is tried as well, so a backend started by
# other means (another checkout, a service, a test harness) is still reachable -
# otherwise those options would only work for starting a backend, never for talking
# to one. The fallback is gated on something actually answering, so "not running"
# is still reported correctly rather than assumed from a default port number.
function Resolve-BackendEndpoint {
    $fromState = Get-BackendEndpoint
    if ($fromState) { return $fromState }
    $configured = @{ Host = $BackendHost; Port = [int]$BackendPort }
    if (Test-BackendReachable $configured) { return $configured }
    return $null
}

# Test whether a backend is reachable without disturbing it (connects and drops).
function Test-BackendReachable([hashtable]$Endpoint) {
    if (-not $Endpoint) {
        return $false
    }
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect($Endpoint.Host, $Endpoint.Port, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne(500, $false)
        if (-not $ok -or -not $client.Connected) {
            return $false
        }
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Close()
    }
}

# Send one JSON-line request to the backend and return the parsed response object.
#
# -WaitSeconds caps how long the reply may take. It exists for -Wait ramps, where the
# backend only answers once the ramp has finished: without a cap a wedged backend
# would block the shell indefinitely.
function Invoke-Backend([hashtable]$Request, [hashtable]$Endpoint, [double]$WaitSeconds = 0) {
    if (-not $Endpoint) {
        $Endpoint = Resolve-BackendEndpoint
    }
    if (-not $Endpoint) {
        throw "PSU backend is not running. Provide -Port to start it."
    }
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $client.Connect($Endpoint.Host, $Endpoint.Port)
        $stream = $client.GetStream()
        # Default: the device timeout plus a margin. A read timeout of 0 (infinite)
        # is deliberately never used.
        $budgetMs = if ($WaitSeconds -gt 0) {
            [int][Math]::Ceiling($WaitSeconds * 1000)
        } else {
            [int]$TimeoutMs + 15000
        }
        $stream.ReadTimeout = $budgetMs
        $payload = ($Request | ConvertTo-Json -Compress) + "`n"
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($payload)
        $stream.Write($bytes, 0, $bytes.Length)
        $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::UTF8)
        try {
            $line = $reader.ReadLine()
        }
        catch [System.IO.IOException] {
            throw ("The backend did not answer '{0}' within {1:N1} s. The ramp may still be running; check with -Get or stop it with -StopRamp." -f $Request["command"], ($budgetMs / 1000))
        }
        if ([string]::IsNullOrWhiteSpace($line)) {
            throw "Backend returned an empty response."
        }
        return ($line | ConvertFrom-Json)
    }
    finally {
        if ($client) { $client.Close() }
    }
}

# Wall-clock budget for a -Wait ramp: the ramp itself, plus room for the per-step
# device timeouts and the remote-mode switch at the start.
function Get-RampWaitBudget([double]$Seconds) {
    return [double]$Seconds + 10.0 + ([double]$TimeoutMs / 1000.0)
}

# Report the outcome of a ramp request: either the plan the backend accepted, or the
# final result when -Wait was used.
function Write-RampResult([object]$Result, [string]$Kind, [string]$Unit) {
    if ($Result.PSObject.Properties["replaced"] -and $Result.replaced) {
        # A side effect on the *previous* ramp, so it is a note, not part of this
        # command's result - but it is never silent.
        $old = $Result.replaced
        $at = if ($old.kind -eq "voltage") { $old.atVolts } else { $old.atAmps }
        $oldUnit = if ($old.kind -eq "voltage") { "V" } else { "A" }
        Write-Note ("replaced the running {0} ramp at {1:N3} {2} ({3:N1}% done); the new ramp starts there." -f `
            $old.kind, $at, $oldUnit, $old.percent)
    }
    $from = if ($Kind -eq "voltage") { $Result.fromVolts } else { $Result.fromAmps }
    $to = if ($Kind -eq "voltage") { $Result.toVolts } else { $Result.toAmps }
    if ($Result.waited) {
        "Ramped {0} {1:N3} -> {2:N3} {3} over {4:N1} s ({5} steps of {6} ms)" -f `
            $Kind, $from, $to, $Unit, $Result.seconds, $Result.steps, $Result.stepMs
        return
    }
    "Ramp started: {0} {1:N3} -> {2:N3} {3} over {4:N1} s ({5} steps of {6} ms)" -f `
        $Kind, $from, $to, $Unit, $Result.seconds, $Result.steps, $Result.stepMs
    "The backend is running it; watch with -Get, stop with -StopRamp, or use -Wait to block."
}

# Write a side-effect note.
#
# Notes go to stderr on purpose: stdout is the command's own result and stays
# byte-identical for a given command, so anything parsing or diffing output is
# unaffected, while an operator at the terminal still sees what happened. In -Json
# mode nothing is written at all - the JSON payload already carries the facts, and
# stray text would corrupt it for a machine consumer.
function Write-Note([string]$Message) {
    if ($Json) { return }
    [Console]::Error.WriteLine("note: $Message")
}

# Report ramps that a command cancelled as a side effect. Taking ownership of a
# setpoint away from a running ramp must never be silent: the operator has to see
# that their set command stopped a ramp, without having to run -Get afterwards.
function Write-CancelledRamps([object]$Result) {
    if (-not ($Result.PSObject.Properties["cancelledRamps"]) -or -not $Result.cancelledRamps) {
        return
    }
    foreach ($ramp in $Result.cancelledRamps) {
        # Format-RampLine already names the kind and the "cancelled" status.
        Write-Note (Format-RampLine $ramp)
    }
}

# Correct the remembered setpoint after a ramp was cut short.
#
# A non-waiting ramp records its target as the remembered "set" value when it
# starts, because that is the operator's intent. If the ramp is then cancelled, the
# supply never reached that target, so the remembered value has to be pulled back to
# where the ramp actually stopped - otherwise -Get keeps reporting a setpoint that
# was never applied, long after the ramp history has expired.
function Update-SessionFromCancelledRamps([object]$Result) {
    if (-not ($Result.PSObject.Properties["cancelledRamps"]) -or -not $Result.cancelledRamps) {
        return
    }
    foreach ($ramp in $Result.cancelledRamps) {
        if ($ramp.kind -eq "voltage" -and $null -ne $ramp.atVolts) {
            Update-DefaultSession @{ Voltage = [double]$ramp.atVolts }
        }
        elseif ($ramp.kind -eq "current" -and $null -ne $ramp.atAmps) {
            Update-DefaultSession @{ Current = [double]$ramp.atAmps }
        }
    }
}

# One human-readable line per ramp reported by the backend (progress or outcome).
function Format-RampLine($Ramp) {
    $unit = if ($Ramp.kind -eq "voltage") { "V" } else { "A" }
    $from = if ($Ramp.kind -eq "voltage") { $Ramp.fromVolts } else { $Ramp.fromAmps }
    $to = if ($Ramp.kind -eq "voltage") { $Ramp.toVolts } else { $Ramp.toAmps }
    $at = if ($Ramp.kind -eq "voltage") { $Ramp.atVolts } else { $Ramp.atAmps }
    $text = "Ramp {0,-7}: {1} {2:N3} -> {3:N3} {4}, at {5:N3} {4} ({6:N1}%, step {7} of {8})" -f `
        $Ramp.kind, $Ramp.status, $from, $to, $unit, $at, $Ramp.percent, $Ramp.writesDone, $Ramp.steps
    if ($Ramp.PSObject.Properties["remainingSeconds"] -and $Ramp.status -eq "running") {
        $text += (", {0:N1} s left" -f $Ramp.remainingSeconds)
    }
    if ($Ramp.PSObject.Properties["error"] -and $Ramp.error) {
        $text += " - $($Ramp.error)"
    }
    return $text
}

# Locate a Python interpreter that can actually run the backend.
#
# Picking by name is not enough: several Pythons commonly share one PATH (msys2,
# the Microsoft Store, python.org, a venv), and the first "python3" found is often
# not the one with pyserial installed. On a machine where msys2 owns python3, a
# name-based choice starts a backend that cannot open a serial port at all, while a
# perfectly good interpreter sits next to it. So each candidate is asked whether it
# can import serial, and the first that can wins.
#
# The winner is cached for the life of the process; probing costs one short child
# process per rejected candidate, and only on the first call.
$script:PythonProbeCache = $null
$script:PythonProbeReport = @()

function Get-PythonCandidates {
    # An explicitly named interpreter is authoritative: if the operator says which
    # Python to use, quietly running a different one would be worse than failing.
    # $script: qualified on purpose: PowerShell variable names are case-insensitive
    # and scoping is dynamic, so a caller's local $python would otherwise shadow this.
    if ($script:Python) { return @($script:Python) }
    if ($env:KUAIQUS_PSU_PYTHON) { return @($env:KUAIQUS_PSU_PYTHON) }
    return @("python3", "python", "py")
}

function Get-PythonCommand {
    if ($script:PythonProbeCache) { return $script:PythonProbeCache }
    $script:PythonProbeReport = @()
    foreach ($candidate in Get-PythonCandidates) {
        $found = Get-Command $candidate -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if (-not $found) {
            $script:PythonProbeReport += "  $candidate - not on PATH"
            continue
        }
        # Ask the interpreter itself; version strings cannot answer this.
        $probe = & $found.Source -c "import serial, sys; print(sys.version.split()[0])" 2>&1
        if ($LASTEXITCODE -eq 0) {
            $script:PythonProbeCache = $found
            Write-Verbose "python: $($found.Source) (Python $($probe -join '') with pyserial)"
            return $found
        }
        $version = (& $found.Source --version 2>&1) -join ""
        $script:PythonProbeReport += "  $candidate ($($found.Source), $version) - no pyserial"
    }
    return $null
}

# Message for the case where no interpreter qualifies: name every candidate and why
# it was rejected, so the fix is obvious instead of guesswork.
function Get-PythonFailureMessage {
    $explicit = if ($script:Python) { $script:Python } elseif ($env:KUAIQUS_PSU_PYTHON) { $env:KUAIQUS_PSU_PYTHON } else { "" }
    if ($explicit) {
        # No silent substitution: the operator named this interpreter.
        # Built by appending, not by nesting arrays in @(): a nested array would be
        # stringified as "System.Object[]" by -join instead of flattened.
        $lines = @("The interpreter you selected cannot run the backend: it has no pyserial.", "Tried:")
        $lines += $script:PythonProbeReport
        $lines += "Install it there (e.g. `"$explicit`" -m pip install --user pyserial),"
        $lines += "or drop -Python / KUAIQUS_PSU_PYTHON to let the tool find a capable interpreter."
        return ($lines -join [Environment]::NewLine)
    }
    $lines = @("No Python interpreter with pyserial was found; the backend needs one.")
    if ($script:PythonProbeReport.Count -gt 0) {
        $lines += "Tried:"
        $lines += $script:PythonProbeReport
    }
    $lines += "Install it for the interpreter you want to use, for example:"
    $lines += "  python -m pip install --user pyserial       # Windows"
    $lines += "  sudo apt install python3-serial             # Debian/Ubuntu"
    $lines += "Or name the interpreter explicitly with -Python <path>, or set KUAIQUS_PSU_PYTHON."
    return ($lines -join [Environment]::NewLine)
}

# Start the persistent backend detached and wait until it is reachable.
function Start-Backend {
    if (-not $Port) {
        throw "A serial port (-Port <COMx>) is required to start the backend."
    }
    # Not named $python: that would collide with the script-level $Python setting,
    # because variable names are case-insensitive.
    $pythonCmd = Get-PythonCommand
    if (-not $pythonCmd) {
        throw (Get-PythonFailureMessage)
    }
    if (Test-BackendReachable (Resolve-BackendEndpoint)) {
        return
    }
    $argList = @(
        ('"' + $script:BackendScript + '"'),
        "serve",
        "--port", $Port,
        "--baud", [string]$Baud,
        "--serve-port", [string]$BackendPort,
        "--serve-host", $BackendHost,
        "--serve-state-file", ('"' + $script:StateFile + '"')
    )
    # The backend's output is captured on every platform. It reports why it could not
    # start (a missing port, a port already in use) as a JSON line, and without this
    # the only symptom would be a timeout with no reason attached. A child that
    # inherits the frontend's stdout pipe can also keep the caller from returning.
    $outLog = Join-Path $PSScriptRoot ".kuaiqus_psu_backend.out.log"
    $errLog = Join-Path $PSScriptRoot ".kuaiqus_psu_backend.err.log"
    Remove-Item $outLog, $errLog -ErrorAction SilentlyContinue
    $startArgs = @{
        FilePath               = $pythonCmd.Source
        ArgumentList           = $argList
        RedirectStandardOutput = $outLog
        RedirectStandardError  = $errLog
    }
    if ($script:IsWindowsHost) {
        # A hidden window keeps the persistent backend from flashing a console.
        # -WindowStyle is Windows-only; Linux/macOS rejects it.
        $startArgs["WindowStyle"] = "Hidden"
    }
    Start-Process @startArgs | Out-Null

    # Poll the control socket for readiness so callers can proceed immediately.
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 150
        if (Test-BackendReachable (Resolve-BackendEndpoint)) {
            return
        }
    }
    throw ("Backend did not become reachable within 10 seconds." + (Get-BackendStartupError $outLog $errLog))
}

# Explain a failed backend start from its captured output.
#
# The backend reports startup failures as a JSON line on stdout (for example
# "serve open failed: could not open port"), so the useful text is usually there
# rather than on stderr.
function Get-BackendStartupError([string]$OutLog, [string]$ErrLog) {
    foreach ($log in @($OutLog, $ErrLog)) {
        if (-not (Test-Path $log)) { continue }
        $lines = @(Get-Content $log -ErrorAction SilentlyContinue | Where-Object { $_.Trim() })
        if ($lines.Count -eq 0) { continue }
        $last = $lines[-1].Trim()
        try {
            $parsed = $last | ConvertFrom-Json -ErrorAction Stop
            if ($parsed.PSObject.Properties["error"] -and $parsed.error) {
                return [Environment]::NewLine + "Backend reported: $($parsed.error)"
            }
        }
        catch {
            # Not JSON (a Python traceback, for instance); show it verbatim.
        }
        return [Environment]::NewLine + "Backend output: $last"
    }
    return [Environment]::NewLine + "The backend produced no output; see $OutLog and $ErrLog."
}

# Ask the running backend to shut down and release the port.
function Stop-Backend {
    $endpoint = Resolve-BackendEndpoint
    if (-not $endpoint) {
        return
    }
    try {
        Invoke-Backend @{ command = "shutdown" } $endpoint | Out-Null
    }
    catch {
        # Best effort: the process may already be gone.
    }
    # Give the process a moment to remove its state file.
    Start-Sleep -Milliseconds 200
}

# Ensure a backend is running for a hardware command, unless it is already up.
function Ensure-Backend {
    $endpoint = Resolve-BackendEndpoint
    if ($endpoint -and (Test-BackendReachable $endpoint)) {
        return $endpoint
    }
    Start-Backend | Out-Null
    return Resolve-BackendEndpoint
}

# Read the last-known setpoints from the [default] session. The protocol only
# reports the measured output, so the set value must come from local tracking.
# Returns a PSCustomObject with nullable Voltage/Current (null = not yet set).
function Get-SetValues {
    $voltage = $null
    $current = $null
    $ini = ConvertTo-IniMap $script:IniPath
    if ($ini.Contains("default")) {
        $d = $ini["default"]
        if ($d.Contains("Voltage")) { $voltage = ConvertTo-PsuNumber -Text $d["Voltage"] -Label "stored voltage" }
        if ($d.Contains("Current")) { $current = ConvertTo-PsuNumber -Text $d["Current"] -Label "stored current" }
    }
    return [pscustomobject]@{ Voltage = $voltage; Current = $current }
}

# Attach last-known setpoint fields to a device status object (for JSON output).
# Fields are always present, with $null meaning "not yet set".
function Add-SetValues([object]$Status) {
    $set = Get-SetValues
    $Status | Add-Member -NotePropertyName setVoltageVolts -NotePropertyValue $set.Voltage -Force
    $Status | Add-Member -NotePropertyName setCurrentAmps -NotePropertyValue $set.Current -Force
    return $Status
}

# Emit pretty JSON or a readable summary for a device result, showing both the
# measured output and the last-known setpoint ("??" when not yet set).
function Write-Result([object]$Result) {
    if (-not $Result) {
        return
    }
    if ($Result.PSObject.Properties["command"] -and $Result.command -eq "status") {
        $set = Get-SetValues
        if ($Json) {
            Add-SetValues $Result | ConvertTo-Json -Depth 8
            return
        }
        $state = if ($Result.cvCc) { $Result.cvCc } else { "?" }
        $vSet = if ($null -ne $set.Voltage) { "{0:N3}" -f $set.Voltage } else { "??" }
        $cSet = if ($null -ne $set.Current) { "{0:N3}" -f $set.Current } else { "??" }
        "Voltage : {0:N3} V (set {1} V)" -f $Result.voltageVolts, $vSet
        "Current : {0:N3} A (set {1} A)" -f $Result.currentAmps, $cSet
        "State   : $state"
        # A background ramp is otherwise invisible: it moves the setpoint without any
        # command of its own, so status is the place that reports it.
        if ($Result.PSObject.Properties["ramps"] -and $Result.ramps) {
            foreach ($property in $Result.ramps.PSObject.Properties) {
                Format-RampLine $property.Value
            }
        }
        return
    }
    if ($Json) {
        $Result | ConvertTo-Json -Depth 8
        return
    }
    if ($Result.PSObject.Properties["voltageVolts"]) {
        $state = if ($Result.cvCc) { $Result.cvCc } else { "?" }
        "Voltage : {0:N3} V (state $state)" -f $Result.voltageVolts
        return
    }
    if ($Result.PSObject.Properties["currentAmps"]) {
        $state = if ($Result.cvCc) { $Result.cvCc } else { "?" }
        "Current : {0:N3} A (state $state)" -f $Result.currentAmps
        return
    }
    $Result | ConvertTo-Json -Depth 8
}

# Format a backend error object into a readable line and set the failure code.
function Write-BackendError([object]$Result) {
    $script:ExitCode = 2
    $message = if ($Result.PSObject.Properties["error"] -and $Result.error) { $Result.error } else { "backend command failed" }
    if ($Json) {
        # Emit the backend's own payload rather than a reduced {ok,error} object: a
        # failure reply carries the detail that explains it (which command, a ramp's
        # status and the setpoint it reached), and a consumer should not have to
        # parse a different shape for failures than for successes.
        $payload = if ($Result) { $Result } else { [pscustomobject]@{} }
        $payload | Add-Member -NotePropertyName ok -NotePropertyValue $false -Force
        $payload | Add-Member -NotePropertyName error -NotePropertyValue $message -Force
        $payload | ConvertTo-Json -Depth 8
    }
    else {
        Write-Error "PSU command failed: $message"
    }
}

# ---------------------------------------------------------------------------
# Serial port discovery
# ---------------------------------------------------------------------------
# Absorbed from the former find_comports.ps1: map local serial ports to friendly
# identities so the PSU can be found without hard-coding a port name (COM numbers
# and /dev names change across PCs, docks, USB ports, and operating systems).
# Listing is passive by default; -ListPorts -Probe actively tests each candidate.

# Build a COMx -> PnP entity map for friendly descriptions on Windows.
# Win32_PnPEntity is used because it is available on stock Windows without extra
# modules. Non-Windows hosts return an empty map and fall back to bare ports.
function Get-PnpComMap {
    $map = @{}
    if (-not $script:IsWindowsHost) { return $map }
    try {
        Get-CimInstance -ClassName Win32_PnPEntity -ErrorAction Stop |
            Where-Object { $_.Name -match '\((COM\d+)\)' } |
            ForEach-Object {
                $com = $Matches[1]
                $map[$com] = $_
            }
    }
    catch {
        # PnP enumeration failed; fall back to bare port names below.
    }
    return $map
}

# Return every Silicon Labs CP210x device on Windows that has no driver (Windows
# error code 28). Such a PSU is hidden under "Other Devices" and would otherwise
# be invisible, so the listing warns about it.
function Get-DriverlessCp210x {
    if (-not $script:IsWindowsHost) { return @() }
    try {
        return @(Get-CimInstance -ClassName Win32_PnPEntity -ErrorAction Stop |
            Where-Object { $_.DeviceID -match 'VID_10C4' -and $_.ConfigManagerErrorCode -eq 28 })
    }
    catch {
        return @()
    }
}

# Windows: list the ports .NET knows about and label the PSU's CP2102 bridge.
function Get-WindowsComPorts {
    $pnp = Get-PnpComMap
    $result = @()
    foreach ($port in @([System.IO.Ports.SerialPort]::GetPortNames() | Sort-Object)) {
        $entry = [ordered]@{ Port = $port; Chipset = ""; Description = "" }
        if ($pnp.ContainsKey($port)) {
            $entity = $pnp[$port]
            $entry.Description = [string]$entity.Name
            if ($entity.DeviceID -match 'VID_10C4') { $entry.Chipset = "CP2102" }
        }
        $result += [pscustomobject]$entry
    }
    return $result
}

# Linux: prefer the stable /dev/serial/by-id symlinks (they survive replugging and
# reboots) and fall back to any remaining /dev/tty* port that .NET reports.
function Get-LinuxSerialPorts {
    $result = @()
    $covered = @{}
    $byIdDir = "/dev/serial/by-id"
    if (Test-Path -LiteralPath $byIdDir -PathType Container) {
        foreach ($link in @(Get-ChildItem -LiteralPath $byIdDir -ErrorAction SilentlyContinue |
                Where-Object { -not $_.PSIsContainer } | Sort-Object Name)) {
            $target = ""
            try {
                # LinkTarget is the raw symlink text (e.g. ../../ttyUSB0); it is
                # used only for display and duplicate suppression.
                $target = [string]([System.IO.FileInfo]::new($link.FullName).LinkTarget)
            }
            catch {
                # Unreadable link; leave the target blank and show the by-id name.
            }
            $chipset = if ($link.Name -match 'CP2102|Silicon_Labs') { "CP2102" } else { "" }
            $description = if ([string]::IsNullOrWhiteSpace($target)) { $link.Name } else { "$($link.Name) -> $target" }
            $result += [pscustomobject][ordered]@{ Port = $link.FullName; Chipset = $chipset; Description = $description }
            if ($target) { $covered[(Split-Path $target -Leaf)] = $true }
        }
    }
    foreach ($port in @([System.IO.Ports.SerialPort]::GetPortNames() | Sort-Object)) {
        # Skip a /dev/ttyUSBn already represented by its by-id symlink.
        if ($covered.ContainsKey((Split-Path $port -Leaf))) { continue }
        $result += [pscustomobject][ordered]@{ Port = $port; Chipset = ""; Description = "" }
    }
    return $result
}

# macOS: /dev/cu.* are the call-out devices that do not block waiting for a
# carrier, so prefer them over /dev/tty.* (which .NET may report) for both the
# listing and the active probe.
function Get-MacSerialPorts {
    $result = @()
    $seen = @{}
    foreach ($dev in @(Get-ChildItem -Path "/dev/cu.*" -ErrorAction SilentlyContinue | Sort-Object Name)) {
        $name = $dev.Name
        $chipset = if ($name -match 'SLAB_USBtoUART|usbserial') { "CP2102" } else { "" }
        $result += [pscustomobject][ordered]@{ Port = "/dev/$name"; Chipset = $chipset; Description = "call-out device" }
        $seen["/dev/$name"] = $true
    }
    foreach ($port in @([System.IO.Ports.SerialPort]::GetPortNames() | Sort-Object)) {
        if ($seen.ContainsKey($port)) { continue }
        # A tty.* port whose cu.* sibling is already listed is the same device.
        $cu = $port -replace '^/dev/tty\.', '/dev/cu.'
        if ($cu -ne $port -and (Test-Path -LiteralPath $cu)) { continue }
        $result += [pscustomobject][ordered]@{ Port = $port; Chipset = ""; Description = "" }
    }
    return $result
}

# Collect the local serial ports for the current platform. GetPortNames() is the
# only cross-platform source; the by-id (Linux) and cu.* (macOS) passes add the
# stable, non-blocking names that the PSU should be addressed by.
function Get-ComPorts {
    if ($script:IsWindowsHost) { return Get-WindowsComPorts }
    if ($IsMacOS) { return Get-MacSerialPorts }
    return Get-LinuxSerialPorts
}

# Actively probe a single port for the PSU. Returns $true when a framed '<'..'>'
# response is observed after the connect + read-voltage query. This opens the
# port directly, so it must not be used while the backend owns it: the open then
# fails and the port is simply reported as not the PSU.
function Test-PsuPort([string]$Port, [int]$Baud = 9600) {
    $serial = New-Object System.IO.Ports.SerialPort
    $serial.PortName = $Port
    $serial.BaudRate = $Baud
    $serial.DataBits = 8
    $serial.Parity = [System.IO.Ports.Parity]::None
    $serial.StopBits = [System.IO.Ports.StopBits]::One
    $serial.ReadTimeout = 500
    $serial.WriteTimeout = 500
    try {
        $serial.Open()
        # Connect handshake then read-voltage query. The device flushes a frame
        # when the next one starts within ~3.5 character times, so pause between
        # the two writes.
        $serial.Write("<09100000000>")
        Start-Sleep -Milliseconds 100
        $serial.Write("<02000000000>")
        $deadline = (Get-Date).AddMilliseconds(1500)
        $sawFrame = $false
        while ((Get-Date) -lt $deadline) {
            try {
                $byte = $serial.ReadByte()
                if ($byte -eq 60) { # '<'
                    $sawFrame = $true
                    break
                }
            }
            catch {
                break
            }
        }
        return $sawFrame
    }
    catch {
        return $false
    }
    finally {
        if ($serial.IsOpen) {
            # Return the device to local (front-panel) control so the keyboard is
            # not left locked in remote mode after probing.
            try {
                $serial.Write("<09200000000>")
                Start-Sleep -Milliseconds 100
            }
            catch {
                # Best effort; the port may already be in a bad state.
            }
            $serial.Close()
        }
        $serial.Dispose()
    }
}

# Print the passive port table, or scan every candidate with -Probe. Kept as one
# function so plain listing and scanning share the same data and JSON shape.
function Write-PortListing([switch]$Probe) {
    $ports = @(Get-ComPorts)
    $driverless = @(Get-DriverlessCp210x)
    if ($Probe) {
        foreach ($p in $ports) {
            $p | Add-Member -NotePropertyName Psu -NotePropertyValue $false -Force
            # Skip debug probes (XDS110/JTAG) so a scan cannot disturb an active
            # debug session on the bench.
            if (-not $p.Description -or $p.Description -notmatch 'XDS110|Debug|JTAG') {
                $p.Psu = Test-PsuPort -Port $p.Port
            }
        }
    }
    if ($Json) {
        $output = [ordered]@{ ports = @($ports) }
        if ($driverless.Count -gt 0) {
            $output.driverless = @($driverless | ForEach-Object { $_.Name })
        }
        $output | ConvertTo-Json -Depth 6
    }
    elseif ($ports.Count -eq 0) {
        "No serial ports found."
    }
    elseif ($Probe) {
        $ports | Format-Table Port, Chipset, Psu, Description -AutoSize | Out-String
    }
    else {
        $ports | Format-Table Port, Chipset, Description -AutoSize | Out-String
    }
    if ($driverless.Count -gt 0) {
        Write-Warning ("CP2102 found under Other Devices without a driver. Install the " +
            "Silicon Labs CP210x driver (see docs/windows.md in the project, or the " +
            "vendor page https://www.silabs.com/developers/usb-to-uart-bridge-vcp-drivers).")
    }
}

# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
# Commands run in the order they appeared on the command line, including
# duplicates. Settings (-Port, -Baud, -Json, ...) were already extracted.

if ($script:Cmds.Count -eq 0) {
    Show-Help
    exit 0
}

$script:endpoint = $null
$script:PortResolved = $false

# Resolve the serial port from the default session once, on first hardware use,
# so a remembered port is reused without an explicit -Port.
function Resolve-Port {
    if ($script:PortResolved) { return }
    $script:PortResolved = $true
    if ([string]::IsNullOrWhiteSpace($Port)) {
        $ini = ConvertTo-IniMap $script:IniPath
        if ($ini.Contains("default") -and $ini["default"].Contains("Port")) {
            $script:Port = [string]$ini["default"]["Port"]
        }
    }
}

# Return the running backend endpoint, or fail gracefully if none is running.
# The backend is started explicitly with -Connect; hardware commands never
# auto-start it, so the user gets clear guidance to -Connect first.
function Get-LiveEndpoint {
    if (-not $script:endpoint) {
        $script:endpoint = Resolve-BackendEndpoint
    }
    if (-not $script:endpoint -or -not (Test-BackendReachable $script:endpoint)) {
        if ($Json) {
            @{ ok = $false; error = "PSU backend is not running. Run -Connect first." } | ConvertTo-Json
        }
        else {
            [Console]::Error.WriteLine("ERROR: PSU backend is not running. Run -Connect first (e.g. -Connect -Port COM7).")
        }
        exit 3
    }
    return $script:endpoint
}

foreach ($cmd in $script:Cmds) {
    $name = $cmd.Name
    $value = $cmd.Value

    switch ($name) {
        "help" {
            Show-Help
            exit 0
        }
        "version" {
            "kuaiqus_psu $script:VersionString"
            exit 0
        }
        "listports" {
            Write-PortListing -Probe:($cmd.ContainsKey("Probe") -and $cmd["Probe"])
            exit 0
        }
        "list" {
            $ini = ConvertTo-IniMap $script:IniPath
            $sessions = @()
            foreach ($section in $ini.Keys) {
                if ($section -ieq "default") { continue }
                $s = $ini[$section]
                $sessions += [pscustomobject]@{
                    Name    = Decode-IniName $section
                    Voltage = if ($s.Contains("Voltage")) { $s["Voltage"] } else { "" }
                    Current = if ($s.Contains("Current")) { $s["Current"] } else { "" }
                    Comment = if ($s.Contains("Comment")) { $s["Comment"] } else { "" }
                }
            }
            if ($Json) {
                $sessions | ConvertTo-Json
            }
            else {
                if ($sessions.Count -eq 0) { "No saved sessions." }
                else { $sessions | Format-Table Name, Voltage, Current, Comment -AutoSize | Out-String }
            }
        }
        "persist" {
            $section = Encode-IniName $value
            # "default" is reserved for the last-used state, not a named session.
            if ($section -ieq "default") {
                Write-UsageError "The name 'default' is reserved for the last-used state."
            }
            if ($cmd.ContainsKey("Delete") -and $cmd["Delete"]) {
                $ini = ConvertTo-IniMap $script:IniPath
                if (-not $ini.Contains($section)) {
                    if ($Json) { @{ ok = $false; error = "No session named '$value'." } | ConvertTo-Json }
                    else { [Console]::Error.WriteLine("ERROR: No session named '$value'.") }
                    exit 3
                }
                Remove-IniSection -Path $script:IniPath -Section $section
                if ($Json) { @{ ok = $true; deleted = $value } | ConvertTo-Json }
                else { "Deleted session '$value'." }
            }
            else {
                $ini = ConvertTo-IniMap $script:IniPath
                $default = if ($ini.Contains("default")) { $ini["default"] } else { [ordered]@{} }
                $keys = [ordered]@{}
                if ($default.Contains("Voltage")) { $keys["Voltage"] = $default["Voltage"] }
                if ($default.Contains("Current")) { $keys["Current"] = $default["Current"] }
                if (-not [string]::IsNullOrWhiteSpace($Comment)) { $keys["Comment"] = $Comment }
                $keys["UpdatedAt"] = (Get-Date).ToString("o")
                Write-IniSection -Path $script:IniPath -Section $section -Keys $keys
                if ($Json) { @{ ok = $true; persisted = $value } | ConvertTo-Json }
                else { "Persisted session '$value'." }
            }
        }
        "status" {
            $ep = Resolve-BackendEndpoint
            if (-not $ep -or -not (Test-BackendReachable $ep)) {
                if ($Json) { @{ running = $false; port = $Port; backend = $null } | ConvertTo-Json }
                else { "PSU backend: not running (run -Connect to start it)." }
            }
            else {
                $backendStatus = Invoke-Backend @{ command = "status" } $ep
                if ($backendStatus.ok -eq $false) {
                    Write-BackendError $backendStatus
                    exit $script:ExitCode
                }
                if ($Json) { @{ running = $true; backend = $ep; device = (Add-SetValues $backendStatus) } | ConvertTo-Json -Depth 8 }
                else {
                    "PSU backend: running on $($ep.Host):$($ep.Port)"
                    Write-Result $backendStatus
                }
            }
        }
        "restore" {
            # Look up the session (offline) so a missing name fails fast.
            $ini = ConvertTo-IniMap $script:IniPath
            $session = $null
            foreach ($section in $ini.Keys) {
                if ($section -ieq "default") { continue }
                if ((Decode-IniName $section) -ieq $value) { $session = $ini[$section]; break }
            }
            if (-not $session) {
                if ($Json) { @{ ok = $false; error = "No session named '$value'." } | ConvertTo-Json }
                else { Write-Error "No session named '$value'." }
                exit 3
            }
            $ep = Get-LiveEndpoint
            if ($session.Contains("Voltage")) {
                $r = Invoke-Backend @{ command = "setVoltage"; value = [double]$session["Voltage"] } $ep
                if ($r.ok -eq $false) { Write-BackendError $r; exit $script:ExitCode }
            }
            if ($session.Contains("Current")) {
                $r = Invoke-Backend @{ command = "setCurrent"; value = [double]$session["Current"] } $ep
                if ($r.ok -eq $false) { Write-BackendError $r; exit $script:ExitCode }
            }
            $updates = @{ Port = $Port; Baud = $Baud }
            if ($session.Contains("Voltage")) { $updates["Voltage"] = $session["Voltage"] }
            if ($session.Contains("Current")) { $updates["Current"] = $session["Current"] }
            Update-DefaultSession $updates
            if ($Json) { @{ ok = $true; restored = $value } | ConvertTo-Json }
            else { "Restored session '$value'." }
        }
        "probe" {
            $ep = Get-LiveEndpoint
            $result = Invoke-Backend @{ command = "probe"; port = $Port; baud = $Baud } $ep
            if ($result.ok -eq $false) { Write-BackendError $result; exit $script:ExitCode }
            if ($Json) {
                $result | ConvertTo-Json -Depth 8
            }
            else {
                $state = if ($result.responded) { "responded" } else { "no response" }
                "Port $($result.port) @ $($result.baud): $state"
                if ($result.responded) {
                    if ($result.PSObject.Properties["voltageVolts"]) {
                        "Voltage : {0:N3} V (state $($result.cvCc))" -f $result.voltageVolts
                    }
                    if ($result.PSObject.Properties["currentAmps"]) {
                        "Current : {0:N3} A" -f $result.currentAmps
                    }
                }
            }
        }
        "get" {
            $ep = Get-LiveEndpoint
            $result = Invoke-Backend @{ command = "status" } $ep
            if ($result.ok -eq $false) { Write-BackendError $result; exit $script:ExitCode }
            Write-Result $result
        }
        "getversion" {
            $ep = Get-LiveEndpoint
            $result = Invoke-Backend @{ command = "readVersion" } $ep
            if ($result.ok -eq $false) { Write-BackendError $result; exit $script:ExitCode }
            if ($Json) { $result | ConvertTo-Json }
            else { "Firmware : $($result.version)" }
        }
        "setvoltage" {
            $volts = ConvertTo-PsuNumber -Text $value -Label "voltage"
            if ($null -eq $volts) {
                Write-UsageError "Invalid voltage value '$value': expected a number such as 25 or 25.2 ('.' or ',' decimal)."
            }
            $ep = Get-LiveEndpoint
            $result = Invoke-Backend @{ command = "setVoltage"; value = $volts } $ep
            if ($result.ok -eq $false) { Write-BackendError $result; exit $script:ExitCode }
            Update-DefaultSession @{ Port = $Port; Baud = $Baud; Voltage = $volts }
            if ($Json) { $result | ConvertTo-Json }
            else {
                "Set voltage to {0:N3} V" -f $volts
                Write-CancelledRamps $result
            }
        }
        "setcurrent" {
            $amps = ConvertTo-PsuNumber -Text $value -Label "current"
            if ($null -eq $amps) {
                Write-UsageError "Invalid current value '$value': expected a number such as 1 or 0.5 ('.' or ',' decimal)."
            }
            $ep = Get-LiveEndpoint
            $result = Invoke-Backend @{ command = "setCurrent"; value = $amps } $ep
            if ($result.ok -eq $false) { Write-BackendError $result; exit $script:ExitCode }
            Update-DefaultSession @{ Port = $Port; Baud = $Baud; Current = $amps }
            if ($Json) { $result | ConvertTo-Json }
            else {
                "Set current to {0:N3} A" -f $amps
                Write-CancelledRamps $result
            }
        }
        "power" {
            $pw = $value.Trim().ToLowerInvariant()
            if ($pw -ne "on" -and $pw -ne "off") {
                Write-UsageError "Invalid power state '$value': use On or Off."
            }
            $ep = Get-LiveEndpoint
            $command = if ($pw -eq "on") { "powerOn" } else { "powerOff" }
            $result = Invoke-Backend @{ command = $command } $ep
            if ($result.ok -eq $false) { Write-BackendError $result; exit $script:ExitCode }
            Update-DefaultSession @{ Port = $Port; Baud = $Baud; Power = $pw }
            if ($Json) { $result | ConvertTo-Json }
            else { "Output: $pw" }
        }
        "powercycle" {
            # Switch the output off for N seconds and back on. Used to force a
            # connected device to restart from a real power-on.
            $offSeconds = ConvertTo-PsuNumber -Text $value -Label "power-cycle seconds"
            if ($null -eq $offSeconds -or $offSeconds -lt 0) {
                Write-UsageError "Invalid power-cycle time '$value': expected seconds, such as 5 or 2.5."
            }
            $ep = Get-LiveEndpoint
            $result = Invoke-Backend @{ command = "powerCycle"; offSeconds = $offSeconds } $ep
            if ($result.ok -eq $false) { Write-BackendError $result; exit $script:ExitCode }
            Update-DefaultSession @{ Port = $Port; Baud = $Baud; Power = "on" }
            if ($Json) { $result | ConvertTo-Json }
            else {
                "Power cycled: off {0:N1} s, settle {1:N1} s" -f $result.offSeconds, $result.settleSeconds
                if ($null -ne $result.voltageVolts) {
                    "Voltage : {0:N3} V" -f $result.voltageVolts
                    "Current : {0:N3} A" -f $result.currentAmps
                }
            }
        }
        "rampv" {
            # Setpoint-only voltage ramp: valid with the output both off and on.
            $parts = $value -split "\s+"
            $from = ConvertTo-PsuNumber -Text $parts[0] -Label "ramp start voltage"
            $to = ConvertTo-PsuNumber -Text $parts[1] -Label "ramp end voltage"
            $secs = ConvertTo-PsuNumber -Text $parts[2] -Label "ramp duration"
            if ($null -eq $from -or $null -eq $to -or $null -eq $secs -or $secs -lt 0) {
                Write-UsageError "Invalid -RampV values '$value': expected <startV> <endV> <seconds>."
            }
            $ep = Get-LiveEndpoint
            $request = @{ command = "rampVoltage"; from = $from; to = $to; seconds = $secs; wait = $Wait }
            if ([double]$StepMs -gt 0) { $request["stepMs"] = [int]$StepMs }
            $budget = if ($Wait) { Get-RampWaitBudget $secs } else { 0 }
            $result = Invoke-Backend $request $ep $budget
            if ($result.ok -eq $false) {
                # A cut-short ramp left the supply mid-way, so remember that value.
                if ($null -ne $result.atVolts) { Update-DefaultSession @{ Voltage = [double]$result.atVolts } }
                Write-BackendError $result; exit $script:ExitCode
            }
            Update-DefaultSession @{ Port = $Port; Baud = $Baud; Voltage = $to }
            if ($Json) { $result | ConvertTo-Json -Depth 8 }
            else { Write-RampResult $result "voltage" "V" }
        }
        "rampi" {
            # Setpoint-only current-limit ramp: valid with the output both off and on.
            $parts = $value -split "\s+"
            $from = ConvertTo-PsuNumber -Text $parts[0] -Label "ramp start current"
            $to = ConvertTo-PsuNumber -Text $parts[1] -Label "ramp end current"
            $secs = ConvertTo-PsuNumber -Text $parts[2] -Label "ramp duration"
            if ($null -eq $from -or $null -eq $to -or $null -eq $secs -or $secs -lt 0) {
                Write-UsageError "Invalid -RampI values '$value': expected <startA> <endA> <seconds>."
            }
            $ep = Get-LiveEndpoint
            $request = @{ command = "rampCurrent"; from = $from; to = $to; seconds = $secs; wait = $Wait }
            if ([double]$StepMs -gt 0) { $request["stepMs"] = [int]$StepMs }
            $budget = if ($Wait) { Get-RampWaitBudget $secs } else { 0 }
            $result = Invoke-Backend $request $ep $budget
            if ($result.ok -eq $false) {
                if ($null -ne $result.atAmps) { Update-DefaultSession @{ Current = [double]$result.atAmps } }
                Write-BackendError $result; exit $script:ExitCode
            }
            Update-DefaultSession @{ Port = $Port; Baud = $Baud; Current = $to }
            if ($Json) { $result | ConvertTo-Json -Depth 8 }
            else { Write-RampResult $result "current" "A" }
        }
        "stopramp" {
            # Cancels both kinds; the supply keeps the setpoint the ramp reached.
            $ep = Get-LiveEndpoint
            $result = Invoke-Backend @{ command = "stopRamp" } $ep
            if ($result.ok -eq $false) { Write-BackendError $result; exit $script:ExitCode }
            Update-SessionFromCancelledRamps $result
            if ($Json) { $result | ConvertTo-Json -Depth 8 }
            elseif ([int]$result.stopped -eq 0) { "No ramp was running." }
            else {
                foreach ($ramp in $result.cancelledRamps) { Format-RampLine $ramp }
            }
        }
        "connect" {
            Resolve-Port
            try {
                $script:endpoint = Ensure-Backend
            }
            catch {
                if ($Json) { @{ ok = $false; error = $_.Exception.Message } | ConvertTo-Json }
                else { [Console]::Error.WriteLine("ERROR: " + $_.Exception.Message) }
                exit 3
            }
            $result = Invoke-Backend @{ command = "connect" } $script:endpoint
            if ($result.ok -eq $false) { Write-BackendError $result; exit $script:ExitCode }
            if ($Json) { $result | ConvertTo-Json } else { "Connected (remote control mode). Front panel is locked." }
        }
        "disconnect" {
            Stop-Backend
            Clear-DefaultSetValues
            if ($Json) { @{ ok = $true; command = "disconnect" } | ConvertTo-Json }
            else { "Disconnected (local control mode). Front panel is unlocked." }
        }
        "monitor" {
            $ep = Get-LiveEndpoint
            $set = Get-SetValues
            while ($true) {
                $result = Invoke-Backend @{ command = "status" } $ep
                if ($result.ok -eq $false) { Write-BackendError $result; exit $script:ExitCode }
                $vSet = if ($null -ne $set.Voltage) { "{0:N3}" -f $set.Voltage } else { "??" }
                $cSet = if ($null -ne $set.Current) { "{0:N3}" -f $set.Current } else { "??" }
                $line = "{0}  {1:N3}/{2} V  {3:N3}/{4} A  {5}" -f (Get-Date -Format "HH:mm:ss"), $result.voltageVolts, $vSet, $result.currentAmps, $cSet, $result.cvCc
                Write-Host $line
                Start-Sleep -Milliseconds $MonitorIntervalMs
            }
        }
        default {
            Write-Error "Unknown command: $name"
            exit 3
        }
    }
}

exit $script:ExitCode
