<#
.SYNOPSIS
  Hardware-free smoke tests for scripts\kuaiqus_psu.ps1.

.DESCRIPTION
  Exercises only the commands that never touch a serial port or start the backend:
  version/help/port listing/status reporting, session persistence, ordered
  execution of offline commands, comma-decimal parsing, and frontend argument
  validation (hardware commands without a running backend, and -Connect without a
  port, fail with exit code 3). Assumes no backend is already running on the
  default control port.

.NOTES
  Version: 0.5.0
  Author: f-steff
  License: MIT
  Copyright: (c) 2026 Flemming Steffensen
#>

[CmdletBinding()]
param()

Set-StrictMode -Version 3.0
$script:Pass = 0
$script:Fail = 0

# Forward slashes keep the path valid on Windows, Linux, and macOS.
$ps1 = Join-Path $PSScriptRoot "../scripts/kuaiqus_psu.ps1"
# Use a temp INI so tests never read or write the operator's real session file.
$tmpIni = Join-Path ([System.IO.Path]::GetTempPath()) ("kuaiqus_psu_cli_test_{0}.ini" -f $PID)

function Assert([bool]$Condition, [string]$Message) {
    if ($Condition) {
        $script:Pass++
        Write-Host "  PASS  $Message"
    }
    else {
        $script:Fail++
        Write-Host "  FAIL  $Message"
    }
}

# Run the CLI and capture merged output and exit code. Arguments are passed as a
# plain string array, which the simple (no param block) script receives via $args
# in the order given, exactly like a real command line.
function Invoke-Tool([string[]]$Arguments) {
    $out = @(& $ps1 @Arguments 2>&1)
    $code = if ($null -eq $LASTEXITCODE) { -1 } else { $LASTEXITCODE }
    return @{ Output = ($out -join "`n"); ExitCode = $code }
}

# Usage errors are written with [Console]::Error.WriteLine, which goes straight to
# the process stderr handle and therefore bypasses in-process 2>&1 redirection. A
# child pwsh is the only way to capture that text, so it is used only where the
# message itself is under test.
function Invoke-ToolCaptured([string[]]$Arguments) {
    $errFile = [System.IO.Path]::GetTempFileName()
    try {
        $proc = Start-Process -FilePath "pwsh" -PassThru -Wait -NoNewWindow `
            -ArgumentList (@("-NoProfile", "-File", $ps1) + $Arguments) `
            -RedirectStandardError $errFile
        return @{ Output = (Get-Content $errFile -Raw); ExitCode = $proc.ExitCode }
    }
    finally {
        Remove-Item $errFile -ErrorAction SilentlyContinue
    }
}

Write-Host "[kuaiqus_psu.ps1 smoke tests]"

# Ensure no backend is running so the gating assertions hold. The temp INI keeps
# this from touching the operator's real remembered port/setpoints.
Invoke-Tool @("-disconnect", "-inipath", $tmpIni) | Out-Null

$r = Invoke-Tool @("-version")
Assert ($r.ExitCode -eq 0) "-Version exits 0"
# Match the shape, not a literal: pinning the number here would fail on every
# release even though the behaviour under test is unchanged.
Assert ($r.Output -match "kuaiqus_psu\s+\d+\.\d+\.\d+") "-Version prints a version string"

$r = Invoke-Tool @("-help")
Assert ($r.ExitCode -eq 0) "-Help exits 0"
Assert ($r.Output -match "SetVoltage") "-Help documents commands"

$r = Invoke-Tool @("-listports")
Assert ($r.ExitCode -eq 0) "-ListPorts exits 0"

# -ListPorts is offline: -Json must parse without ever opening a port.
$r = Invoke-Tool @("-listports", "-json")
Assert ($r.ExitCode -eq 0) "-ListPorts -Json exits 0"
try {
    $parsed = $r.Output | ConvertFrom-Json
    Assert ($parsed.PSObject.Properties["ports"] -ne $null) "-ListPorts -Json has a 'ports' field"
}
catch {
    Assert $false "-ListPorts -Json returns valid JSON"
}

$r = Invoke-Tool @("-status")
Assert ($r.ExitCode -eq 0) "-Status exits 0"

$r = Invoke-Tool @("-status", "-json")
Assert ($r.ExitCode -eq 0) "-Status -Json exits 0"
try {
    $parsed = $r.Output | ConvertFrom-Json
    Assert ($parsed.PSObject.Properties["running"] -ne $null) "-Status -Json has a 'running' field"
}
catch {
    Assert $false "-Status -Json returns valid JSON"
}

# Session persistence (offline, temp INI).
$r = Invoke-Tool @("-list", "-inipath", $tmpIni)
Assert ($r.ExitCode -eq 0) "-List exits 0"

$r = Invoke-Tool @("-persist", "Test #1 +x", "-comment", "unit test", "-inipath", $tmpIni)
Assert ($r.ExitCode -eq 0) "-Persist exits 0"

$r = Invoke-Tool @("-list", "-inipath", $tmpIni)
Assert ($r.ExitCode -eq 0) "-List (after persist) exits 0"
Assert ($r.Output -match "Test #1 \+x") "-List decodes the session name"

$r = Invoke-Tool @("-restore", "NoSuchSession", "-inipath", $tmpIni)
Assert ($r.ExitCode -eq 3) "-Restore of a missing session exits 3"

# Delete a saved session.
$r = Invoke-Tool @("-persist", "Test #1 +x", "-delete", "-inipath", $tmpIni)
Assert ($r.ExitCode -eq 0) "-Persist -Delete exits 0"
$r = Invoke-Tool @("-list", "-inipath", $tmpIni)
Assert ($r.Output -notmatch "Test #1 \+x") "-Persist -Delete removes the session"

# Deleting a missing session is a frontend error; -Delete must follow -Persist.
$r = Invoke-Tool @("-persist", "NoSuchSession", "-delete", "-inipath", $tmpIni)
Assert ($r.ExitCode -eq 3) "-Persist -Delete of a missing session exits 3"
$r = Invoke-Tool @("-delete", "-inipath", $tmpIni)
Assert ($r.ExitCode -eq 3) "-Delete without -Persist exits 3"

# Ordered execution of offline commands (including duplicates).
$r = Invoke-Tool @("-persist", "Seq A", "-persist", "Seq B", "-list", "-inipath", $tmpIni)
Assert ($r.ExitCode -eq 0) "ordered -Persist -Persist -List exits 0"
Assert ($r.Output -match "Seq A") "first persisted session is listed"
Assert ($r.Output -match "Seq B") "second persisted session is listed"

# Frontend validation without hardware.
$r = Invoke-Tool @("-setvoltage", "abc", "-inipath", $tmpIni)
Assert ($r.ExitCode -eq 3) "-SetVoltage with a non-numeric value exits 3"

# A hardware command without a running backend must fail gracefully (exit 3) and
# never hang or auto-start the backend. (The guidance text goes to stderr.)
$r = Invoke-Tool @("-get", "-inipath", $tmpIni)
Assert ($r.ExitCode -eq 3) "-Get without a running backend exits 3"

# -Wait is a modifier for the ramp commands only, so on its own it is a usage error
# rather than a silently ignored option.
$r = Invoke-ToolCaptured @("-wait", "-get", "-inipath", $tmpIni)
Assert ($r.ExitCode -eq 3) "-Wait without a ramp command exits 3"
Assert ($r.Output -match "applies to -RampV") "-Wait without a ramp explains itself"

# -StopRamp needs a backend to talk to, and must fail cleanly without one.
$r = Invoke-Tool @("-stopramp", "-inipath", $tmpIni)
Assert ($r.ExitCode -eq 3) "-StopRamp without a running backend exits 3"

# Both new switches must be discoverable from the help text.
$r = Invoke-Tool @("-help")
Assert ($r.Output -match "-Wait\s") "-help documents -Wait"
Assert ($r.Output -match "-StopRamp") "-help documents -StopRamp"

# -Connect is the only backend starter; without a remembered port it fails cleanly.
$r = Invoke-Tool @("-connect", "-inipath", $tmpIni)
Assert ($r.ExitCode -eq 3) "-Connect without a port exits 3"

# -disconnect forgets the tracked set values but keeps the remembered port.
$closeIni = Join-Path ([System.IO.Path]::GetTempPath()) ("kuaiqus_psu_cli_close_{0}.ini" -f $PID)
@"
[default]
Port=COM7
Baud=9600
Voltage=12
Current=2
"@ | Set-Content -LiteralPath $closeIni -Encoding UTF8
$r = Invoke-Tool @("-disconnect", "-inipath", $closeIni)
Assert ($r.ExitCode -eq 0) "-disconnect exits 0"
$closeContent = Get-Content -LiteralPath $closeIni -Raw
Assert ($closeContent -match "Port=COM7") "-disconnect keeps the remembered port"
Assert ($closeContent -notmatch "Voltage=") "-disconnect forgets the tracked voltage"
Assert ($closeContent -notmatch "Current=") "-disconnect forgets the tracked current"
Remove-Item -LiteralPath $closeIni -ErrorAction SilentlyContinue

# --- Output contract, against a mock backend -------------------------------
# The real backend owns a serial port, so these assertions use tests/mock_backend.py:
# it speaks the same JSON-line control protocol and can be told to report a
# cancelled ramp. That makes the frontend's output contract testable with no
# hardware: stdout must stay identical for a given command, and side effects must
# appear only as stderr notes.

function Get-TestPython {
    foreach ($candidate in @('python3', 'python', 'py')) {
        if (Get-Command $candidate -ErrorAction SilentlyContinue) { return $candidate }
    }
    return $null
}

# Run the tool with stdout and stderr captured separately (the tool writes usage
# and note lines straight to the process stderr handle).
function Invoke-ToolStreams([string[]]$Arguments) {
    $outFile = [System.IO.Path]::GetTempFileName()
    $errFile = [System.IO.Path]::GetTempFileName()
    try {
        $proc = Start-Process -FilePath "pwsh" -PassThru -Wait -NoNewWindow `
            -ArgumentList (@("-NoProfile", "-File", $ps1) + $Arguments) `
            -RedirectStandardOutput $outFile -RedirectStandardError $errFile
        return @{
            Out      = (Get-Content -LiteralPath $outFile -Raw)
            Err      = (Get-Content -LiteralPath $errFile -Raw)
            ExitCode = $proc.ExitCode
        }
    }
    finally {
        Remove-Item -LiteralPath $outFile, $errFile -ErrorAction SilentlyContinue
    }
}

function Start-MockBackend([string]$Python, [string]$Scenario) {
    $log = [System.IO.Path]::GetTempFileName()
    $mock = Join-Path $PSScriptRoot "mock_backend.py"
    $proc = Start-Process -FilePath $Python -ArgumentList @($mock, "--scenario", $Scenario) `
        -PassThru -NoNewWindow -RedirectStandardOutput $log
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 100
        $line = @(Get-Content -LiteralPath $log -ErrorAction SilentlyContinue |
            Where-Object { $_ -match "listening" })
        if ($line.Count -gt 0) {
            return @{ Proc = $proc; Port = [int](($line[0] | ConvertFrom-Json).port); Log = $log }
        }
    }
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    return $null
}

function Stop-MockBackend($Mock) {
    if (-not $Mock) { return }
    Stop-Process -Id $Mock.Proc.Id -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $Mock.Log -ErrorAction SilentlyContinue
}

$testPython = Get-TestPython
if (-not $testPython) {
    Write-Host "  SKIP  output-contract tests (no Python interpreter found)"
}
else {
    $mockIni = Join-Path ([System.IO.Path]::GetTempPath()) ("kuaiqus_psu_cli_mock_{0}.ini" -f $PID)
    $clean = Start-MockBackend $testPython "clean"
    $cleanRun = Invoke-ToolStreams @("-SetVoltage", "5", "-BackendPort", $clean.Port, "-inipath", $mockIni)
    Stop-MockBackend $clean

    $busy = Start-MockBackend $testPython "cancelled"
    $busyRun = Invoke-ToolStreams @("-SetVoltage", "5", "-BackendPort", $busy.Port, "-inipath", $mockIni)
    Stop-MockBackend $busy

    Assert ($cleanRun.ExitCode -eq 0 -and $busyRun.ExitCode -eq 0) "-SetVoltage exits 0 whether or not it cancelled a ramp"
    Assert ($cleanRun.Out -eq $busyRun.Out) "-SetVoltage stdout is identical whether or not it cancelled a ramp"
    Assert ($busyRun.Err -match "note:") "a cancelled ramp is reported as a stderr note"
    Assert (-not ($cleanRun.Err -match "note:")) "no note is written when nothing was cancelled"

    # -Json is the machine contract: the key is always present, and no note text may
    # contaminate either stream.
    $jsonClean = Start-MockBackend $testPython "clean"
    $jsonRun = Invoke-ToolStreams @("-SetVoltage", "5", "-Json", "-BackendPort", $jsonClean.Port, "-inipath", $mockIni)
    Stop-MockBackend $jsonClean
    $parsed = $null
    try { $parsed = $jsonRun.Out | ConvertFrom-Json } catch { $parsed = $null }
    Assert ($null -ne $parsed) "-Json output parses as JSON"
    Assert ($null -ne $parsed -and $parsed.PSObject.Properties["cancelledRamps"]) "-Json always carries cancelledRamps"
    Assert (-not ($jsonRun.Err -match "note:")) "-Json mode writes no notes"

    # The stale-setpoint fix: a cancelled ramp pulls the remembered value back to
    # where the supply actually stopped (the mock reports 1.7 V).
    Remove-Item -LiteralPath $mockIni -ErrorAction SilentlyContinue
    $stopMock = Start-MockBackend $testPython "cancelled"
    Invoke-ToolStreams @("-StopRamp", "-BackendPort", $stopMock.Port, "-inipath", $mockIni) | Out-Null
    Stop-MockBackend $stopMock
    $mockIniText = if (Test-Path -LiteralPath $mockIni) { Get-Content -LiteralPath $mockIni -Raw } else { "" }
    Assert ($mockIniText -match "Voltage=1\.7") "-StopRamp remembers the setpoint the ramp actually reached"
    Remove-Item -LiteralPath $mockIni -ErrorAction SilentlyContinue
}

Remove-Item -LiteralPath $tmpIni -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Results: $($script:Pass) passed, $($script:Fail) failed"
if ($script:Fail -gt 0) { exit 1 }
exit 0
