"""A stand-in for the PSU backend's control socket, for testing the frontend.

The CLI's output contract cannot be tested against the real backend without a
power supply: the backend owns a serial port. This mock speaks the same
newline-delimited JSON protocol over TCP and answers from a canned scenario, which
makes the frontend's behaviour testable on any machine with no hardware at all.

Usage:
    python tests/mock_backend.py --scenario clean|cancelled [--port 0]

It prints one JSON line with the port it listened on, then serves until stopped.

Version: 1.0.0; Author: f-steff; License: MIT; Copyright (c) 2026 Flemming Steffensen.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading

# One cancelled voltage ramp, in the shape RampController reports.
CANCELLED_RAMP = {
    "kind": "voltage",
    "command": "rampVoltage",
    "status": "cancelled",
    "active": False,
    "fromVolts": 0.0,
    "toVolts": 12.0,
    "atVolts": 1.7,
    "seconds": 30.0,
    "steps": 121,
    "stepMs": 250,
    "writesDone": 18,
    "percent": 14.9,
    "elapsedSeconds": 4.3,
    "remainingSeconds": None,
    "error": None,
    "replaced": None,
    "reason": "setpointOverride",
}


def reply_for(request: dict, scenario: str) -> dict:
    """Canned reply for one request, keeping the real backend's key set."""
    command = request.get("command")
    cancelled = [dict(CANCELLED_RAMP)] if scenario == "cancelled" else []

    if command == "setVoltage":
        return {"ok": True, "command": "setVoltage",
                "requestedVolts": float(request.get("value", 0)),
                "cancelledRamps": cancelled}
    if command == "setCurrent":
        return {"ok": True, "command": "setCurrent",
                "requestedAmps": float(request.get("value", 0)),
                "cancelledRamps": cancelled}
    if command == "status":
        ramps = {"voltage": dict(CANCELLED_RAMP)} if scenario == "cancelled" else {}
        return {"ok": True, "command": "status",
                "voltageVolts": 1.7, "voltageMilli": 1700,
                "currentAmps": 0.0, "currentMilli": 0,
                "cvCc": "CV", "ramps": ramps}
    if command == "stopRamp":
        return {"ok": True, "command": "stopRamp",
                "cancelledRamps": cancelled, "stopped": len(cancelled)}
    if command == "shutdown":
        return {"ok": True, "command": "shutdown", "pid": 0}
    return {"ok": False, "error": f"mock backend does not implement {command}"}


def serve(scenario: str, port: int) -> None:
    """Accept connections and answer each JSON line until the process is stopped."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(8)
    actual = listener.getsockname()[1]
    # The caller needs the port before it can point the frontend at this process.
    print(json.dumps({"ok": True, "event": "listening", "port": actual}), flush=True)

    def handle(conn: socket.socket) -> None:
        with conn:
            buffer = b""
            while True:
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    return
                if not chunk:
                    return
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    text = line.decode("utf-8", errors="replace").strip()
                    if not text:
                        continue
                    try:
                        request = json.loads(text)
                    except ValueError:
                        response = {"ok": False, "error": "invalid JSON request"}
                    else:
                        response = reply_for(request, scenario)
                    conn.sendall((json.dumps(response) + "\n").encode("utf-8"))

    while True:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mock PSU backend control socket")
    parser.add_argument("--scenario", choices=("clean", "cancelled"), default="clean",
                        help="clean: nothing cancelled; cancelled: one ramp was stopped")
    parser.add_argument("--port", type=int, default=0, help="0 picks a free port")
    args = parser.parse_args(argv)
    serve(args.scenario, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
