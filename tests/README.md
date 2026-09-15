# Tests

Hardware-free tests for the KUAIQU-NICE-POWER PSU controller. Nothing here opens
a real serial port: the Python tests inject a fake transport, and the PowerShell
smoke test only exercises commands that never touch hardware.

## Layout

| File | What it covers |
| --- | --- |
| `test_protocol.py` | Protocol codec - every vendor frame example, uV/uA scaling, CC/CV and `OK` decoding, malformed-frame rejection, and the streaming frame parser. |
| `test_backend.py` | Backend - argument parsing, command dispatch against a fake transport, response/timeout/ack handling, background ramps (wait/no-wait, progress, cancel, replace-from-current-setpoint, per-kind ownership), the idle watchdog, and the full serve-mode control-socket lifecycle. |
| `cli_test.ps1` | PowerShell CLI - version/help/port-list/status output, frontend exit-code validation, and the output contract (stdout stable, side effects as stderr notes, invariant `-Json` keys) against the mock backend. |
| `test_mcp.py` | MCP stdio server - JSON-RPC envelope, the 12 advertised tools and their schemas, argument-to-request mapping, call timeouts, and capability-based interpreter resolution. |
| `mock_backend.py` | Stand-in for the backend's control socket, so frontend behaviour can be tested without a power supply. |
| `run_all_tests.ps1` | Orchestrator - runs the Python unittest discovery and `cli_test.ps1`, aggregates exit codes. |

## Running

All commands work under PowerShell 7+ on Windows, Linux, and macOS; on Linux and
macOS invoke the `.ps1` files through `pwsh` and use forward slashes.

```powershell
# Everything (from the repository root)
.\tests\run_all_tests.ps1

# Python unit tests only
python3 -m unittest discover -s tests -p "test_*.py"

# PowerShell CLI smoke test only
.\tests\cli_test.ps1
```

The `pyserial` dependency is not needed for the tests (fake transports), so a
bare Python 3.10+ interpreter is enough.

## Guidelines

- Tests must be runnable **without the PSU attached**. Inject fake transports
  rather than reaching for a real COM port; the device is added later for bench
  validation, not unit tests.
- Keep protocol tests anchored to concrete frames from the vendor manual so any
  change to the codec that breaks a documented example is caught immediately.
- Add a protocol test whenever the codec's encoding/decoding changes, and a
  backend test whenever command dispatch or the serve protocol changes.
- Exit codes: `0` all pass, `1` one or more failures, `2` runner/setup error.

## Bench validation (separate from unit tests)

The value-scaling and query-format ambiguities noted in
`scripts/kuaiqus_psu_protocol.md` must be confirmed against a real unit. Those
checks are manual bench steps, not part of the automated suite.
