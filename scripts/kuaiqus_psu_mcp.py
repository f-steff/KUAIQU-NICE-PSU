#!/usr/bin/env python3
"""KUAIQU-NICE-POWER SPPS series PSU MCP server (stdio transport).

Exposes the power supply as MCP tools by proxying JSON-line requests to the
persistent backend (kuaiqus_psu_backend.py serve mode). The JSON-RPC / MCP wire
format is implemented directly over stdio, so there is no dependency on an MCP SDK
release and the server runs anywhere Python runs. The backend remains the single
serial-port owner: the CLI, this MCP server and any other client share it through
the control socket.

Environment overrides let an MCP host (opencode/Codex/Claude) configure the port
without editing code:

    KUAIQUS_PSU_SERIAL_PORT   serial port used for backend auto-start
    KUAIQUS_PSU_BAUD          baud rate for auto-start (default 9600)
    KUAIQUS_PSU_STATE_FILE    backend discovery state file
    KUAIQUS_PSU_CONTROL_HOST  control socket host (default 127.0.0.1)
    KUAIQUS_PSU_CONTROL_PORT  control socket port (default 17332)

Version: 1.0.0; Author: f-steff; License: MIT; Copyright (c) 2026 Flemming Steffensen.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from typing import Any

SERVER_NAME = "kuaiqus-psu"
SERVER_VERSION = "1.0.0"
MCP_PROTOCOL_VERSION = "2024-11-05"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_FILE = os.path.join(SCRIPT_DIR, ".kuaiqus_psu_backend.state.json")
BACKEND_SCRIPT = os.path.join(SCRIPT_DIR, "kuaiqus_psu_backend.py")

# Tool definitions. `inputSchema` is JSON Schema so MCP clients can validate and
# render arguments; `backend` maps the tool arguments onto a control-socket request.
TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_status",
        "description": ("Read the power supply output voltage, current, and CV/CC regulation state. "
                        "Also reports any background ramp under 'ramps' (status, percent, the "
                        "setpoint reached, and seconds remaining)."),
        "inputSchema": {"type": "object", "properties": {}},
        "backend": lambda a: {"command": "status"},
    },
    {
        "name": "read_voltage",
        "description": "Read the current output voltage in volts.",
        "inputSchema": {"type": "object", "properties": {}},
        "backend": lambda a: {"command": "readVoltage"},
    },
    {
        "name": "read_current",
        "description": "Read the current output current in amperes.",
        "inputSchema": {"type": "object", "properties": {}},
        "backend": lambda a: {"command": "readCurrent"},
    },
    {
        "name": "read_version",
        "description": "Read the power supply firmware version.",
        "inputSchema": {"type": "object", "properties": {}},
        "backend": lambda a: {"command": "readVersion"},
    },
    {
        "name": "set_voltage",
        "description": "Set the output voltage in volts.",
        "inputSchema": {
            "type": "object",
            "properties": {"voltage": {"type": "number", "description": "Target voltage in volts (0..999.999)"}},
            "required": ["voltage"],
        },
        "backend": lambda a: {"command": "setVoltage", "value": a.get("voltage")},
    },
    {
        "name": "set_current",
        "description": "Set the output current limit in amperes.",
        "inputSchema": {
            "type": "object",
            "properties": {"current": {"type": "number", "description": "Target current in amperes (0..999.999)"}},
            "required": ["current"],
        },
        "backend": lambda a: {"command": "setCurrent", "value": a.get("current")},
    },
    {
        "name": "power_on",
        "description": "Enable the power supply output.",
        "inputSchema": {"type": "object", "properties": {}},
        "backend": lambda a: {"command": "powerOn"},
    },
    {
        "name": "power_off",
        "description": "Disable the power supply output.",
        "inputSchema": {"type": "object", "properties": {}},
        "backend": lambda a: {"command": "powerOff"},
    },
    {
        "name": "power_cycle",
        "description": ("Switch the output off for a number of seconds and back on. Use this to force "
                        "a connected device to restart from a real power-on."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "off_seconds": {"type": "number", "description": "Seconds to keep the output off (default 3)"},
                "settle_seconds": {"type": "number", "description": "Seconds to wait after power-on (default 2)"},
            },
        },
        "backend": lambda a: {
            "command": "powerCycle",
            "offSeconds": a.get("off_seconds", 3),
            "settleSeconds": a.get("settle_seconds", 2),
        },
    },
    {
        "name": "ramp_voltage",
        "description": ("Ramp the voltage setpoint linearly from one value to another over a duration. "
                        "Setpoint only - it works with the output both off and on."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "start": {"type": "number", "description": "Start voltage in volts"},
                "end": {"type": "number", "description": "End voltage in volts"},
                "seconds": {"type": "number", "description": "Ramp duration in seconds"},
                "step_ms": {"type": "number", "description": "Optional step interval in ms (default 200)"},
                "wait": {"type": "boolean",
                         "description": ("Wait for the ramp to finish (default false). When false the "
                                         "call returns at once and the backend performs the ramp; poll "
                                         "get_status for progress, or cancel with stop_ramp.")},
            },
            "required": ["start", "end", "seconds"],
        },
        "backend": lambda a: {
            "command": "rampVoltage",
            "from": a.get("start"),
            "to": a.get("end"),
            "seconds": a.get("seconds"),
            "stepMs": a.get("step_ms"),
            "wait": bool(a.get("wait", False)),
        },
    },
    {
        "name": "ramp_current",
        "description": ("Ramp the current-limit setpoint linearly from one value to another over a "
                        "duration. Setpoint only - it works with the output both off and on."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "start": {"type": "number", "description": "Start current in amperes"},
                "end": {"type": "number", "description": "End current in amperes"},
                "seconds": {"type": "number", "description": "Ramp duration in seconds"},
                "step_ms": {"type": "number", "description": "Optional step interval in ms (default 200)"},
                "wait": {"type": "boolean",
                         "description": ("Wait for the ramp to finish (default false). When false the "
                                         "call returns at once and the backend performs the ramp; poll "
                                         "get_status for progress, or cancel with stop_ramp.")},
            },
            "required": ["start", "end", "seconds"],
        },
        "backend": lambda a: {
            "command": "rampCurrent",
            "from": a.get("start"),
            "to": a.get("end"),
            "seconds": a.get("seconds"),
            "stepMs": a.get("step_ms"),
            "wait": bool(a.get("wait", False)),
        },
    },
    {
        "name": "stop_ramp",
        "description": ("Stop a running background ramp. The setpoint stays where the ramp had "
                        "reached. Omit 'kind' to stop both the voltage and current ramps."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["voltage", "current"],
                         "description": "Which ramp to stop; both if omitted"},
            },
        },
        "backend": lambda a: {
            "command": "stopRamp",
            "kind": a.get("kind"),
        },
    },
]

_backend_endpoint: tuple[str, int] | None = None


def resolve_endpoint() -> tuple[str, int]:
    """Resolve the backend control endpoint from the state file, then env/defaults."""
    state_file = os.environ.get("KUAIQUS_PSU_STATE_FILE", DEFAULT_STATE_FILE)
    if state_file:
        try:
            with open(state_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if data and data.get("port"):
                return (data.get("host") or "127.0.0.1", int(data["port"]))
        except (OSError, ValueError):
            # Missing or unreadable state file; fall through to defaults.
            pass
    return (
        os.environ.get("KUAIQUS_PSU_CONTROL_HOST", "127.0.0.1"),
        int(os.environ.get("KUAIQUS_PSU_CONTROL_PORT", "17332")),
    )


def call_backend(request: dict[str, Any], endpoint: tuple[str, int], timeout: float) -> dict[str, Any]:
    """Send one JSON-line request to the backend and return the parsed response.

    Each call uses a short-lived socket: the backend is the long-lived owner, so this
    server never needs to hold a connection open.
    """
    host, port = endpoint
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
        buffer = b""
        while b"\n" not in buffer:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("backend closed the connection")
            buffer += chunk
        line = buffer.split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
    try:
        return json.loads(line)
    except ValueError as exc:
        raise RuntimeError("Invalid backend response") from exc


def python_candidates() -> list[str]:
    """Interpreters to consider for the backend, best-known choice first.

    KUAIQUS_PSU_PYTHON is authoritative when set: silently starting the backend
    under a different interpreter than the one configured would be worse than
    failing with a clear message.
    """
    explicit = os.environ.get("KUAIQUS_PSU_PYTHON")
    if explicit:
        return [explicit]
    # This process's own interpreter is a good guess, but only a guess: this server
    # imports nothing beyond the standard library, so it may well be running under a
    # Python that has no pyserial.
    return [sys.executable, "python3", "python", "py"]


def resolve_python() -> str:
    """Return the first interpreter that can import serial.

    Selecting by name is not reliable when several Pythons share one PATH (msys2,
    the Microsoft Store, python.org, a venv): the first "python3" is often not the
    one with pyserial, and the backend it starts then cannot open a serial port.
    Asking each candidate is the only dependable test.
    """
    tried = []
    for candidate in python_candidates():
        if not candidate:
            continue
        try:
            probe = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [candidate, "-c", "import serial"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            tried.append(f"{candidate} (not runnable)")
            continue
        if probe.returncode == 0:
            return candidate
        tried.append(f"{candidate} (no pyserial)")
    raise RuntimeError(
        "No Python interpreter with pyserial was found for the backend. Tried: "
        + ", ".join(tried)
        + ". Install pyserial (python -m pip install --user pyserial) or set "
          "KUAIQUS_PSU_PYTHON to the interpreter that has it."
    )


def start_backend(serial_port: str, endpoint: tuple[str, int]) -> None:
    """Spawn the persistent backend detached so it outlives this MCP process."""
    baud = os.environ.get("KUAIQUS_PSU_BAUD", "9600")
    state_file = os.environ.get("KUAIQUS_PSU_STATE_FILE", DEFAULT_STATE_FILE)
    argv = [
        resolve_python(), BACKEND_SCRIPT, "serve",
        "--port", serial_port,
        "--baud", str(baud),
        "--serve-port", str(endpoint[1]),
        "--serve-state-file", state_file,
    ]
    kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
        "cwd": SCRIPT_DIR,
    }
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NO_WINDOW keeps the backend alive without a console.
        kwargs["creationflags"] = 0x00000008 | 0x08000000
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(argv, **kwargs)  # noqa: S603 - fixed argv, no shell


def wait_for_backend(endpoint: tuple[str, int], deadline_seconds: float = 6.0) -> tuple[str, int]:
    """Poll the control socket until the freshly spawned backend answers."""
    deadline = time.monotonic() + deadline_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            call_backend({"command": "status"}, endpoint, 0.8)
            return endpoint
        except Exception as exc:  # noqa: BLE001 - retry until the deadline
            last_error = exc
            time.sleep(0.15)
    raise RuntimeError(f"Backend did not become ready: {last_error}")


def ensure_backend() -> tuple[str, int]:
    """Return a reachable backend endpoint, auto-starting the backend if allowed."""
    global _backend_endpoint
    if _backend_endpoint is not None:
        return _backend_endpoint
    endpoint = resolve_endpoint()
    try:
        call_backend({"command": "status"}, endpoint, 1.0)
        _backend_endpoint = endpoint
        return endpoint
    except Exception:  # noqa: BLE001 - not running yet
        serial_port = os.environ.get("KUAIQUS_PSU_SERIAL_PORT")
        if not serial_port:
            raise RuntimeError(
                "PSU backend is not running and KUAIQUS_PSU_SERIAL_PORT is not set. "
                "Start it with scripts/kuaiqus_psu.ps1 -Connect -Port <port>, or set "
                "KUAIQUS_PSU_SERIAL_PORT to enable auto-start."
            )
        start_backend(serial_port, endpoint)
        _backend_endpoint = wait_for_backend(endpoint)
        return _backend_endpoint


def call_tool(name: str, args: dict[str, Any] | None) -> dict[str, Any]:
    """Execute one MCP tool call against the backend."""
    tool = next((t for t in TOOLS if t["name"] == name), None)
    if tool is None:
        raise ValueError(f"Unknown tool: {name}")
    endpoint = ensure_backend()
    request = tool["backend"](args or {})
    # Power cycles always take their time, and a ramp only occupies the call when the
    # caller asked to wait for it: a non-waiting ramp answers immediately, so its
    # duration must not inflate the timeout.
    timeout = 10.0
    if isinstance(request.get("offSeconds"), (int, float)):
        timeout = max(timeout, float(request["offSeconds"]) + 15.0)
    if request.get("wait") and isinstance(request.get("seconds"), (int, float)):
        timeout = max(timeout, float(request["seconds"]) + 15.0)
    return call_backend(request, endpoint, timeout)


def handle_message(msg: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one MCP/JSON-RPC message.

    Returns None for notifications, or {"result": ...} / {"error": ...} for requests.
    """
    method = msg.get("method")
    if not isinstance(method, str):
        return {"error": {"code": -32600, "message": "Invalid Request"}}

    if method == "initialize":
        return {"result": {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }}

    if method == "ping":
        return {"result": {}}

    if method == "tools/list":
        return {"result": {"tools": [
            {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
            for t in TOOLS
        ]}}

    if method == "tools/call":
        params = msg.get("params") or {}
        try:
            result = call_tool(params.get("name"), params.get("arguments") or {})
            return {"result": {
                "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                "isError": not result.get("ok", False),
            }}
        except Exception as exc:  # noqa: BLE001 - surfaced to the MCP client
            return {"result": {
                "content": [{"type": "text", "text": str(exc)}],
                "isError": True,
            }}

    # notifications/initialized and friends carry no id and need no reply.
    if "id" not in msg:
        return None
    return {"error": {"code": -32601, "message": f"Method not found: {method}"}}


def main() -> int:
    """Read newline-delimited JSON-RPC from stdin and write responses to stdout."""
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        try:
            msg = json.loads(text)
        except ValueError:
            continue
        try:
            response = handle_message(msg)
        except Exception as exc:  # noqa: BLE001 - never kill the server loop
            response = {"error": {"code": -32603, "message": str(exc)}}
        if response is None:
            continue
        envelope: dict[str, Any] = {"jsonrpc": "2.0", "id": msg.get("id")}
        if "error" in response:
            envelope["error"] = response["error"]
        else:
            envelope["result"] = response["result"]
        sys.stdout.write(json.dumps(envelope) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
