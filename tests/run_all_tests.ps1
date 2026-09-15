<#
.SYNOPSIS
  Run all repository tests: the Python unit tests plus the PowerShell CLI smoke test.

.DESCRIPTION
  Discovers test_*.py in this folder and runs them with the Python unittest
  runner, then runs cli_test.ps1. Aggregates exit codes so CI can rely on a
  single command.

  The unit tests are hardware-free: a fake transport answers protocol frames, so
  no serial port is opened and no PSU has to be connected.

.PARAMETER Python
  Python interpreter to use. Defaults to the first of python3/python/py that
  exists, so the same command works on Windows, Linux and macOS.

.NOTES
  Version: 0.2.0
  Author: f-steff
  License: MIT
  Copyright: (c) 2026 Flemming Steffensen
#>

[CmdletBinding()]
param(
    [string]$Python
)

Set-StrictMode -Version 3.0

# Resolve an interpreter once, so a missing Python is reported clearly instead of
# failing inside the test runner.
#
# Preference order matters for fidelity: the tool itself runs the backend under the
# first interpreter that has pyserial, so the tests should use that same one where
# possible - otherwise they could pass under a different Python than the one the
# operator actually uses. The tests themselves need no pyserial (they inject a fake
# transport), so any interpreter is still better than none.
$reason = ''
if (-not $Python) {
    $available = @()
    foreach ($candidate in @('python3', 'python', 'py')) {
        if (-not (Get-Command $candidate -ErrorAction SilentlyContinue)) { continue }
        $available += $candidate
        & $candidate -c 'import serial' 2>$null
        if ($LASTEXITCODE -eq 0) {
            $Python = $candidate
            $reason = 'matches the interpreter the backend would use (has pyserial)'
            break
        }
    }
    if (-not $Python -and $available.Count -gt 0) {
        $Python = $available[0]
        $reason = 'no interpreter has pyserial; tests do not need it'
    }
}
if (-not $Python) {
    Write-Host 'No Python interpreter found (tried python3, python, py).'
    exit 2
}

$repoRoot = Split-Path -Parent $PSScriptRoot

Write-Host "== Python unit tests ($Python) ==$(if ($reason) { ""  [$reason]"" })"
$testFiles = @(Get-ChildItem -Path $PSScriptRoot -Filter 'test_*.py')
if ($testFiles.Count -eq 0) {
    Write-Host 'No test_*.py files found.'
    exit 2
}
# Run from the repository root so relative paths behave as in CI. The start
# directory is also the top-level directory: `tests` is deliberately not a Python
# package, and passing a different top level would make discovery fail with
# "Start directory is not importable".
Push-Location $repoRoot
try {
    & $Python -m unittest discover -s tests -p 'test_*.py' -v
    $pythonExit = $LASTEXITCODE
} finally {
    Pop-Location
}

Write-Host ''
Write-Host '== PowerShell CLI smoke test =='
& (Join-Path $PSScriptRoot 'cli_test.ps1')
$cliExit = $LASTEXITCODE

Write-Host ''
Write-Host "python -m unittest exit : $pythonExit"
Write-Host "cli_test exit           : $cliExit"

if ($pythonExit -ne 0 -or $cliExit -ne 0) {
    exit 1
}
exit 0
