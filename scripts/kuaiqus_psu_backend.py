#!/usr/bin/env python3
"""KUAIQU-NICE-POWER SPPS series DC power supply controller backend.

Owns the RS232 / USB (CP2102) serial port and exposes the PSU through two entry modes:
one-shot (run a single command and print JSON to stdout) and serve (a persistent
localhost JSON-line TCP control socket used by the PowerShell CLI and the MCP stdio
server). The protocol codec lives in kuaiqus_psu_protocol.py so this file stays
focused on transport and lifecycle.

Only pyserial is required, so the same backend runs on Windows, Linux and macOS.

Version: 1.0.0; Author: f-steff; License: MIT; Copyright (c) 2026 Flemming Steffensen.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import socketserver
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import kuaiqus_psu_protocol as protocol

try:  # pyserial is only needed for real hardware; tests inject a fake transport.
    import serial
except ImportError:  # pragma: no cover - reported through the JSON error path
    serial = None

# Defaults shared with the CLI and the MCP server.
DEFAULT_BAUD = 9600
DEFAULT_TIMEOUT_MS = 2000
DEFAULT_SERVE_HOST = "127.0.0.1"
DEFAULT_SERVE_PORT = 17332
DEFAULT_SERVE_IDLE_TIMEOUT = 300

# Device timing. A frame must be preceded by a pause of at least 3.5 character times;
# the remote/local mode switch and the output relay need a little longer to settle.
MODE_SWITCH_DELAY = 0.15
OUTPUT_SWITCH_DELAY = 0.20
INTER_FRAME_DELAY = 0.10

# Ramp defaults: each set command is a write plus an acknowledgement, so the device
# cannot absorb more than roughly five updates per second without frames colliding.
DEFAULT_RAMP_STEP_MS = 200
MAX_RAMP_STEPS = 120

# Background ramps: how often a sleeping step checks for cancellation, and how long a
# cancel waits for an in-flight acknowledged write before giving up on a clean stop.
RAMP_CANCEL_POLL = 0.05
RAMP_CANCEL_TIMEOUT = 5.0
# How long a finished ramp keeps showing up in status. Long enough to explain why a
# setpoint moved or why a ramp stopped, short enough that a status read hours later
# is not cluttered with history.
RAMP_HISTORY_SECONDS = 120.0

# Power-cycle default: long enough for the DUT's rails to discharge.
DEFAULT_POWER_CYCLE_OFF_SECONDS = 3.0
DEFAULT_POWER_CYCLE_SETTLE_SECONDS = 2.0


# ---------------------------------------------------------------------------
# Serial transport
# ---------------------------------------------------------------------------


class SerialTransport:
    """pyserial wrapper exposing the minimal write/read/close surface used here."""

    def __init__(self, port: str, baud: int = DEFAULT_BAUD) -> None:
        self.port = port
        self.baud = baud
        self._serial: Any = None

    def open(self) -> None:
        """Open the port as 8N1 with no flow control."""
        if serial is None:
            raise RuntimeError("pyserial is not installed. Install it with: pip install pyserial")
        if not self.port:
            raise RuntimeError("no serial port given (--port)")
        # Build the port unopened so RTS/DTR can be deasserted before it opens.
        # pyserial asserts both by default; on an RS-485 adapter RTS often
        # controls the transmit direction, and holding it asserted keeps the
        # driver enabled so replies are never heard. USB and RS-232 links are
        # unaffected. This follows a bench lesson from a sibling driver.
        self._serial = serial.Serial()
        self._serial.port = self.port
        self._serial.baudrate = self.baud
        self._serial.bytesize = serial.EIGHTBITS
        self._serial.parity = serial.PARITY_NONE
        self._serial.stopbits = serial.STOPBITS_ONE
        self._serial.timeout = 0.05
        self._serial.rts = False
        self._serial.dtr = False
        self._serial.open()
        # Drop anything the previous session left in the driver buffer so the first
        # response cannot be mistaken for a stale frame.
        self._serial.reset_input_buffer()
        self._serial.reset_output_buffer()

    def write(self, text: str) -> None:
        """Write one complete frame in a single call (satisfies the 3.5-char rule)."""
        if self._serial is None:
            raise RuntimeError("transport not open")
        self._serial.write(text.encode("ascii"))
        self._serial.flush()

    def read(self, size: int = 256) -> bytes:
        """Read whatever is available within the port's short read timeout."""
        if self._serial is None:
            return b""
        return self._serial.read(size)

    def close(self) -> None:
        """Close the port; errors during teardown are ignored on purpose."""
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:  # noqa: BLE001 - teardown is best effort
                pass
            self._serial = None


def create_serial_transport(args: argparse.Namespace) -> SerialTransport:
    """Factory used by both entry modes (tests substitute their own)."""
    return SerialTransport(getattr(args, "port", ""), getattr(args, "baud", DEFAULT_BAUD))


# ---------------------------------------------------------------------------
# Command execution (shared by one-shot and serve modes)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RampPlan:
    """The step schedule for one ramp, shared by the blocking and background paths.

    `steps` counts the gaps between writes, so a plan performs `steps + 1` writes:
    one at the start value, one at the end value, and the interpolated ones between.
    """

    kind: str
    start: float
    end: float
    seconds: float
    steps: int
    interval: float

    def value_at(self, index: int) -> float:
        """Setpoint for write number `index` (0 = start, `steps` = end)."""
        fraction = index / self.steps if self.steps else 1.0
        return self.start + (self.end - self.start) * fraction

    @property
    def command(self) -> str:
        """Control-socket command name this plan belongs to."""
        return "rampVoltage" if self.kind == "voltage" else "rampCurrent"

    @property
    def unit_key(self) -> str:
        """Suffix used in the result keys (`fromVolts` / `fromAmps`)."""
        return "Volts" if self.kind == "voltage" else "Amps"

    def describe(self) -> dict[str, Any]:
        """Plan summary in the shape the CLI and MCP server report."""
        return {
            "ok": True,
            "command": self.command,
            f"from{self.unit_key}": float(self.start),
            f"to{self.unit_key}": float(self.end),
            "seconds": float(self.seconds),
            "steps": self.steps + 1,
            "stepMs": int(round(self.interval * 1000)),
        }


def plan_ramp(kind: str, start: float, end: float, seconds: float,
              step_ms: int | None) -> RampPlan:
    """Validate a ramp request and compute its step schedule.

    Both endpoints are encoded up front so an out-of-range value is rejected before
    the device is touched: a request that fails half-way would leave the supply at an
    arbitrary intermediate setpoint.

    The step count is bounded because every step is an acknowledged write. When the
    bound applies, the interval is stretched instead of the ramp being shortened, so
    the caller still gets the duration they asked for.
    """
    if kind not in ("voltage", "current"):
        raise ValueError(f"Unknown ramp kind: {kind}")
    if seconds < 0:
        raise ValueError("Invalid ramp duration: must not be negative")
    encode = protocol.encode_set_voltage if kind == "voltage" else protocol.encode_set_current
    encode(start)
    encode(end)

    interval = (step_ms if step_ms and step_ms > 0 else DEFAULT_RAMP_STEP_MS) / 1000.0
    steps = int(round(seconds / interval)) if interval > 0 else 1
    steps = max(1, min(steps, MAX_RAMP_STEPS))
    interval = (seconds / steps) if steps else 0.0
    return RampPlan(kind=kind, start=float(start), end=float(end),
                    seconds=float(seconds), steps=steps, interval=interval)


class PsuSession:
    """Runs PSU commands against an open transport.

    `remote_mode` is per-session state: the device must be switched to remote control
    before the first mutating command, and switched back when the session ends so the
    front panel does not stay locked.
    """

    def __init__(self, transport: Any, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
        self.transport = transport
        self.timeout_ms = timeout_ms
        self.remote_mode = False
        # Last setpoint this backend wrote, per kind. The device cannot be asked for
        # its setpoints (only for measured output), so this is the only record of
        # where a ramp or a manual set left the supply.
        self.last_setpoint: dict[str, float | None] = {"voltage": None, "current": None}

    # -- framing -----------------------------------------------------------

    def request(self, frame: str, timeout_ms: int | None = None) -> protocol.Frame:
        """Send a frame and return the first device response, or raise on timeout.

        A fresh parser per exchange means a late response from a previous command can
        never satisfy this one.
        """
        effective = (timeout_ms or self.timeout_ms) / 1000.0
        parser = protocol.FrameParser()
        self.transport.write(frame)
        deadline = time.monotonic() + effective
        while time.monotonic() < deadline:
            chunk = self.transport.read()
            if not chunk:
                continue
            for decoded in parser.push(chunk):
                if decoded.from_mcu:
                    return decoded
        raise TimeoutError(f"No response from device within {int(effective * 1000)} ms")

    def send_only(self, frame: str, settle: float) -> None:
        """Send a fire-and-forget frame (power on/off, mode switch) and settle."""
        self.transport.write(frame)
        time.sleep(settle)

    def ensure_remote_mode(self) -> None:
        """Enter remote control once per session before any mutating command."""
        if self.remote_mode:
            return
        self.send_only(protocol.encode_connect(), MODE_SWITCH_DELAY)
        self.remote_mode = True

    def release_remote_mode(self) -> None:
        """Best-effort return to front-panel control before the port closes."""
        try:
            self.send_only(protocol.encode_disconnect(), INTER_FRAME_DELAY)
            self.remote_mode = False
        except Exception:  # noqa: BLE001 - the port may already be gone
            pass

    # -- read commands -----------------------------------------------------

    def read_voltage(self) -> dict[str, Any]:
        """Read the measured output voltage."""
        resp = self.request(protocol.encode_read_voltage())
        return {
            "ok": True,
            "command": "readVoltage",
            "voltageVolts": protocol.millivolts_to_volts(resp.value_milli),
            "voltageMilli": resp.value_milli,
            "cvCc": resp.cv_cc,
        }

    def read_current(self) -> dict[str, Any]:
        """Read the measured output current."""
        resp = self.request(protocol.encode_read_current())
        return {
            "ok": True,
            "command": "readCurrent",
            "currentAmps": protocol.milliamps_to_amps(resp.value_milli),
            "currentMilli": resp.value_milli,
            "cvCc": resp.cv_cc,
        }

    def read_version(self) -> dict[str, Any]:
        """Read the firmware version (undocumented function 6)."""
        resp = self.request(protocol.encode_read_version())
        return {
            "ok": True,
            "command": "readVersion",
            "version": protocol.parse_version_text(resp.text),
            "versionRaw": resp.text,
        }

    def status(self) -> dict[str, Any]:
        """Read voltage and current plus the reported CV/CC regulation state."""
        v = self.request(protocol.encode_read_voltage())
        time.sleep(INTER_FRAME_DELAY)
        c = self.request(protocol.encode_read_current())
        return {
            "ok": True,
            "command": "status",
            "voltageVolts": protocol.millivolts_to_volts(v.value_milli),
            "voltageMilli": v.value_milli,
            "currentAmps": protocol.milliamps_to_amps(c.value_milli),
            "currentMilli": c.value_milli,
            "cvCc": v.cv_cc or c.cv_cc,
        }

    def probe(self) -> dict[str, Any]:
        """Confirm a device is present. A silent port is reported, not raised."""
        result: dict[str, Any] = {
            "ok": True,
            "command": "probe",
            "port": getattr(self.transport, "port", None),
            "baud": getattr(self.transport, "baud", None),
            "responded": False,
        }
        self.send_only(protocol.encode_connect(), INTER_FRAME_DELAY)
        try:
            v = self.request(protocol.encode_read_voltage())
        except TimeoutError as exc:
            result["error"] = str(exc)
            return result
        result["responded"] = True
        result["voltageVolts"] = protocol.millivolts_to_volts(v.value_milli)
        result["cvCc"] = v.cv_cc
        try:
            time.sleep(INTER_FRAME_DELAY)
            c = self.request(protocol.encode_read_current())
            result["currentAmps"] = protocol.milliamps_to_amps(c.value_milli)
        except TimeoutError:
            # The voltage response already proved the device; current is best effort.
            pass
        return result

    # -- write commands ----------------------------------------------------

    def write_setpoint(self, kind: str, value: float) -> None:
        """Write one acknowledged setpoint and remember it.

        Remembering the value is what lets a replacement ramp start from where the
        supply actually is: the device can report its measured output, but not the
        setpoint it was given, so the backend has to keep that itself.
        """
        encode = protocol.encode_set_voltage if kind == "voltage" else protocol.encode_set_current
        resp = self.request(encode(value))
        if not resp.is_ack:
            name = "setVoltage" if kind == "voltage" else "setCurrent"
            raise RuntimeError(f"Unexpected response to {name}: {resp.raw}")
        self.last_setpoint[kind] = float(value)

    def set_voltage(self, volts: float) -> dict[str, Any]:
        """Set the output voltage setpoint (requires remote mode)."""
        self.ensure_remote_mode()
        self.write_setpoint("voltage", volts)
        return {"ok": True, "command": "setVoltage", "requestedVolts": float(volts)}

    def set_current(self, amps: float) -> dict[str, Any]:
        """Set the output current limit (requires remote mode)."""
        self.ensure_remote_mode()
        self.write_setpoint("current", amps)
        return {"ok": True, "command": "setCurrent", "requestedAmps": float(amps)}

    def power_on(self) -> dict[str, Any]:
        """Enable the output."""
        self.ensure_remote_mode()
        self.send_only(protocol.encode_power_on(), OUTPUT_SWITCH_DELAY)
        return {"ok": True, "command": "powerOn"}

    def power_off(self) -> dict[str, Any]:
        """Disable the output."""
        self.ensure_remote_mode()
        self.send_only(protocol.encode_power_off(), OUTPUT_SWITCH_DELAY)
        return {"ok": True, "command": "powerOff"}

    def power_cycle(self, off_seconds: float = DEFAULT_POWER_CYCLE_OFF_SECONDS,
                    settle_seconds: float = DEFAULT_POWER_CYCLE_SETTLE_SECONDS) -> dict[str, Any]:
        """Switch the output off for `off_seconds`, back on, then report the result.

        This is the "make the load restart" primitive: a device that only boots from
        a real power-on (for example an MCU held in emulation boot mode by a
        debugger) needs the rails to drop, which is what the off time is for.
        """
        if off_seconds < 0:
            raise ValueError("Invalid power-cycle off time: must not be negative")
        self.ensure_remote_mode()
        self.send_only(protocol.encode_power_off(), OUTPUT_SWITCH_DELAY)
        time.sleep(off_seconds)
        self.send_only(protocol.encode_power_on(), OUTPUT_SWITCH_DELAY)
        time.sleep(max(0.0, settle_seconds))
        result: dict[str, Any] = {
            "ok": True,
            "command": "powerCycle",
            "offSeconds": float(off_seconds),
            "settleSeconds": float(settle_seconds),
        }
        # A post-cycle measurement proves the output came back and shows the load.
        try:
            v = self.request(protocol.encode_read_voltage())
            time.sleep(INTER_FRAME_DELAY)
            c = self.request(protocol.encode_read_current())
            result["voltageVolts"] = protocol.millivolts_to_volts(v.value_milli)
            result["currentAmps"] = protocol.milliamps_to_amps(c.value_milli)
            result["cvCc"] = v.cv_cc or c.cv_cc
        except TimeoutError as exc:
            result["measurementError"] = str(exc)
        return result

    def _ramp(self, kind: str, start: float, end: float, seconds: float,
              step_ms: int | None) -> dict[str, Any]:
        """Walk a setpoint linearly from `start` to `end` over `seconds`, blocking.

        Setpoint-only by design: the ramp never touches the output relay, so it works
        with the output both off (pre-staging a profile) and on (a live ramp).

        This is the one-shot path. In serve mode the same plan is executed by
        RampController, which takes the port lock per step so other commands stay
        responsive while the ramp runs.
        """
        plan = plan_ramp(kind, start, end, seconds, step_ms)
        self.ensure_remote_mode()
        began = time.monotonic()
        for index in range(plan.steps + 1):
            self.write_setpoint(kind, plan.value_at(index))
            if index < plan.steps:
                remaining = plan.interval - (time.monotonic() - began - index * plan.interval)
                if remaining > 0:
                    time.sleep(remaining)
        elapsed = time.monotonic() - began

        result = plan.describe()
        result["elapsedSeconds"] = round(elapsed, 3)
        result["status"] = "completed"
        return result

    def ramp_voltage(self, start: float, end: float, seconds: float,
                     step_ms: int | None = None) -> dict[str, Any]:
        """Ramp the voltage setpoint from `start` to `end` over `seconds`."""
        return self._ramp("voltage", start, end, seconds, step_ms)

    def ramp_current(self, start: float, end: float, seconds: float,
                     step_ms: int | None = None) -> dict[str, Any]:
        """Ramp the current-limit setpoint from `start` to `end` over `seconds`."""
        return self._ramp("current", start, end, seconds, step_ms)

    def connect(self) -> dict[str, Any]:
        """Enter remote control mode explicitly (locks the front panel)."""
        self.send_only(protocol.encode_connect(), MODE_SWITCH_DELAY)
        self.remote_mode = True
        return {"ok": True, "command": "connect"}

    def disconnect(self) -> dict[str, Any]:
        """Return to local (front-panel) control."""
        self.send_only(protocol.encode_disconnect(), MODE_SWITCH_DELAY)
        self.remote_mode = False
        return {"ok": True, "command": "disconnect"}


def _as_float(request: dict[str, Any], *names: str, default=None):
    """Pick the first present key from a control-socket request and coerce to float."""
    for name in names:
        if name in request and request[name] is not None:
            return float(request[name])
    return default


def run_command(request: dict[str, Any], session: PsuSession) -> dict[str, Any]:
    """Execute one command described by a dict (CLI args or control-socket request)."""
    command = request.get("command")
    if command == "probe":
        return session.probe()
    if command == "status":
        return session.status()
    if command == "readVoltage":
        return session.read_voltage()
    if command == "readCurrent":
        return session.read_current()
    if command == "readVersion":
        return session.read_version()
    if command == "setVoltage":
        return session.set_voltage(_as_float(request, "value", "volts"))
    if command == "setCurrent":
        return session.set_current(_as_float(request, "value", "amps"))
    if command == "powerOn":
        return session.power_on()
    if command == "powerOff":
        return session.power_off()
    if command == "powerCycle":
        return session.power_cycle(
            _as_float(request, "offSeconds", "off_seconds") or DEFAULT_POWER_CYCLE_OFF_SECONDS,
            _as_float(request, "settleSeconds", "settle_seconds")
            if request.get("settleSeconds") is not None or request.get("settle_seconds") is not None
            else DEFAULT_POWER_CYCLE_SETTLE_SECONDS,
        )
    if command == "rampVoltage":
        return session.ramp_voltage(
            _as_float(request, "from", "fromVolts", "start"),
            _as_float(request, "to", "toVolts", "end"),
            _as_float(request, "seconds", "duration") or 0.0,
            int(request["stepMs"]) if request.get("stepMs") else None,
        )
    if command == "rampCurrent":
        return session.ramp_current(
            _as_float(request, "from", "fromAmps", "start"),
            _as_float(request, "to", "toAmps", "end"),
            _as_float(request, "seconds", "duration") or 0.0,
            int(request["stepMs"]) if request.get("stepMs") else None,
        )
    if command == "connect":
        return session.connect()
    if command == "disconnect":
        return session.disconnect()
    raise ValueError(f"Unknown command: {command}")


# ---------------------------------------------------------------------------
# One-shot mode
# ---------------------------------------------------------------------------


def run_one_shot(args: argparse.Namespace,
                 transport_factory: Callable[..., Any] = create_serial_transport) -> int:
    """Open the port, run a single command, print JSON, release the device."""
    # A one-shot process exits as soon as the command returns, which would abandon a
    # background ramp mid-way and drop the port with the front panel still locked.
    if args.command in RAMP_COMMANDS and args.wait is False:
        print(json.dumps({
            "ok": False,
            "error": "--no-wait needs a running backend: a one-shot ramp cannot "
                     "outlive the process. Start the backend with 'serve' first.",
        }, indent=2))
        return 2
    if args.command in SERVE_ONLY_COMMANDS:
        print(json.dumps({
            "ok": False,
            "error": f"{args.command} needs a running backend (start it with 'serve').",
        }, indent=2))
        return 2
    transport = transport_factory(args)
    try:
        transport.open()
    except Exception as exc:  # noqa: BLE001 - open failures are reported as JSON
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 2
    session = PsuSession(transport, args.timeout_ms)
    try:
        result = run_command(_args_to_request(args), session)
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 2
    except Exception as exc:  # noqa: BLE001 - command failures are reported as JSON
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 2
    finally:
        # A one-shot command must never leave the front panel locked.
        session.release_remote_mode()
        transport.close()


def _args_to_request(args: argparse.Namespace) -> dict[str, Any]:
    """Translate parsed CLI arguments into the command dict run_command expects."""
    return {
        "command": args.command,
        "value": args.value,
        "offSeconds": args.off_seconds,
        "settleSeconds": args.settle_seconds,
        "from": getattr(args, "from_value", None),
        "to": args.to_value,
        "seconds": args.seconds,
        "stepMs": args.step_ms,
        # One-shot ramps always block; run_one_shot rejects --no-wait up front.
        "wait": True,
    }


# ---------------------------------------------------------------------------
# Persistent serve mode
# ---------------------------------------------------------------------------


def write_state_file(path: str, data: dict[str, Any]) -> None:
    """Record pid/host/port so the CLI and MCP server can discover the backend."""
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
    except OSError:
        # Discovery metadata is best effort, never a correctness requirement.
        pass


def remove_state_file(path: str) -> None:
    """Delete the discovery state file if it exists."""
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


class _RampRun:
    """Live state of one background ramp."""

    def __init__(self, plan: RampPlan) -> None:
        self.plan = plan
        self.cancel_event = threading.Event()
        self.done = threading.Event()
        self.thread: threading.Thread | None = None
        # Updated by the ramp thread and read by status requests. Both are single
        # attribute assignments, so a status read sees either the old or the new
        # value, never a torn one.
        self.writes = 0
        self.value = plan.start
        self.status = "running"
        self.error: str | None = None
        self.replaced: dict[str, Any] | None = None
        self.started_at = time.monotonic()
        self.finished_at: float | None = None

    def progress(self) -> dict[str, Any]:
        """Report what this ramp is doing, in the shape the CLI prints."""
        total = self.plan.steps + 1
        elapsed = (self.finished_at or time.monotonic()) - self.started_at
        info: dict[str, Any] = {
            "kind": self.plan.kind,
            "command": self.plan.command,
            "status": self.status,
            "active": self.status == "running",
            f"from{self.plan.unit_key}": self.plan.start,
            f"to{self.plan.unit_key}": self.plan.end,
            f"at{self.plan.unit_key}": round(self.value, 3),
            "seconds": self.plan.seconds,
            "steps": total,
            "stepMs": int(round(self.plan.interval * 1000)),
            "writesDone": self.writes,
            "percent": round(100.0 * self.writes / total, 1) if total else 100.0,
            "elapsedSeconds": round(elapsed, 3),
        }
        # Every key is always present, null when it does not apply: a consumer must
        # not have to tell "absent" apart from "nothing happened".
        info["remainingSeconds"] = (
            round(max(0.0, self.plan.seconds - elapsed), 3) if self.status == "running" else None
        )
        info["error"] = self.error
        info["replaced"] = self.replaced
        return info


class RampController:
    """Runs setpoint ramps in the background, one per setpoint kind.

    A voltage ramp owns the voltage setpoint and a current ramp owns the current
    setpoint, so the two can run at the same time. That ownership is what makes the
    cancellation rules predictable:

      * a new ramp of the same kind replaces the running one, starting from the
        setpoint the supply is actually at (jumping back to the requested start
        would step the output discontinuously),
      * a manual setVoltage/setCurrent cancels the ramp of that kind and leaves the
        value the operator asked for,
      * powerOn/powerOff/powerCycle never cancel a ramp: a ramp is setpoint-only.

    The port lock is taken per step rather than for the whole ramp, so reads such as
    status stay responsive while a ramp runs. Each foreground command therefore adds
    its own duration as jitter to the step it lands in.
    """

    def __init__(self, session: PsuSession, lock: threading.Lock) -> None:
        self.session = session
        self.lock = lock
        self._runs: dict[str, _RampRun] = {}
        self._guard = threading.Lock()

    # -- queries -----------------------------------------------------------

    def _active(self, kind: str) -> _RampRun | None:
        """The running ramp for `kind`, if any."""
        with self._guard:
            run = self._runs.get(kind)
        if run is not None and not run.done.is_set():
            return run
        return None

    def has_active(self) -> bool:
        """True while any ramp is still running (keeps the backend from idling out)."""
        return any(self._active(kind) is not None for kind in ("voltage", "current"))

    def snapshot(self) -> dict[str, Any]:
        """Progress of each running ramp, plus recently finished ones.

        A finished ramp is worth reporting for a while - it explains why a setpoint
        moved, or why a ramp stopped early - but not forever, or every status read
        for the rest of the backend's life carries stale history.
        """
        now = time.monotonic()
        with self._guard:
            runs = dict(self._runs)
        snapshot = {}
        for kind, run in runs.items():
            finished = run.finished_at
            if run.done.is_set() and finished is not None and (now - finished) > RAMP_HISTORY_SECONDS:
                continue
            snapshot[kind] = run.progress()
        return snapshot

    # -- lifecycle ---------------------------------------------------------

    def start(self, kind: str, start: float, end: float, seconds: float,
              step_ms: int | None = None, wait: bool = False) -> dict[str, Any]:
        """Start a ramp. Returns immediately unless `wait` is set."""
        replaced = None
        previous = self._active(kind)
        if previous is not None:
            replaced = self._cancel_run(previous, "replaced")
            known = self.session.last_setpoint.get(kind)
            if known is not None:
                start = known
        plan = plan_ramp(kind, start, end, seconds, step_ms)
        run = _RampRun(plan)
        run.replaced = replaced
        with self._guard:
            self._runs[kind] = run
        run.thread = threading.Thread(target=self._execute, args=(run,),
                                      name=f"ramp-{kind}", daemon=True)
        run.thread.start()

        if wait:
            # The caller asked to block; the ramp still runs on the worker thread, so
            # it remains cancellable by stopRamp or by a manual set.
            run.done.wait()
            result = run.progress()
            result["ok"] = run.status == "completed"
            result["waited"] = True
            if run.status == "cancelled" and not result.get("error"):
                # A waiting caller must be told why it came back early, otherwise the
                # reply looks like a failure with no explanation.
                result["error"] = (
                    "the ramp was cancelled before it finished (stopRamp, a manual "
                    "setpoint write, a replacing ramp, or backend shutdown)")
            return result
        ack = plan.describe()
        ack["status"] = "running"
        ack["started"] = True
        ack["waited"] = False
        ack["replaced"] = replaced   # null when nothing was replaced
        return ack

    def cancel(self, kind: str | None = None, reason: str = "stopRamp") -> list[dict[str, Any]]:
        """Cancel the ramp of `kind` (or every ramp) and report what was stopped."""
        kinds = (kind,) if kind else ("voltage", "current")
        cancelled = []
        for one in kinds:
            run = self._active(one)
            if run is not None:
                cancelled.append(self._cancel_run(run, reason))
        return cancelled

    def _cancel_run(self, run: _RampRun, reason: str) -> dict[str, Any]:
        """Signal a ramp to stop and wait for the in-flight step to finish."""
        run.cancel_event.set()
        # A step write may be in flight and holds the port lock; give it time to
        # complete so the supply is left at a known setpoint.
        run.done.wait(timeout=RAMP_CANCEL_TIMEOUT)
        if run.status == "running":
            run.status = "cancelled"
        info = run.progress()
        info["status"] = "cancelled"
        info["active"] = False
        info["reason"] = reason
        return info

    def _execute(self, run: _RampRun) -> None:
        """Ramp worker: one acknowledged write per step, lock held per step only."""
        plan = run.plan
        try:
            with self.lock:
                self.session.ensure_remote_mode()
            for index in range(plan.steps + 1):
                if run.cancel_event.is_set():
                    run.status = "cancelled"
                    break
                value = plan.value_at(index)
                with self.lock:
                    self.session.write_setpoint(plan.kind, value)
                run.value = value
                run.writes = index + 1
                if index < plan.steps:
                    # Absolute deadlines keep the ramp on schedule even when a
                    # foreground command delays a step.
                    deadline = run.started_at + (index + 1) * plan.interval
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        if run.cancel_event.wait(min(remaining, RAMP_CANCEL_POLL)):
                            break
            else:
                run.status = "completed"
        except Exception as exc:  # noqa: BLE001 - surfaced through status/stopRamp
            run.status = "failed"
            run.error = str(exc)
        finally:
            run.finished_at = time.monotonic()
            run.done.set()


# Commands whose ramp ownership rules are applied by ServeExecutor.
RAMP_COMMANDS = {"rampVoltage": "voltage", "rampCurrent": "current"}
SETPOINT_COMMANDS = {"setVoltage": "voltage", "setCurrent": "current"}


class ServeExecutor:
    """Applies serve-mode locking and ramp-ownership rules to one request.

    Ramp commands are deliberately executed outside the port lock: the controller
    takes that lock per step, so holding it here would deadlock a waiting ramp.
    """

    def __init__(self, session: PsuSession, lock: threading.Lock,
                 controller: RampController) -> None:
        self.session = session
        self.lock = lock
        self.controller = controller

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        """Run one control-socket request and return the reply payload."""
        command = request.get("command")
        try:
            if command in RAMP_COMMANDS:
                return self._start_ramp(command, request)
            if command == "stopRamp":
                return self._stop_ramp(request)

            cancelled: list[dict[str, Any]] = []
            if command in SETPOINT_COMMANDS:
                # A manual write takes ownership of the setpoint back from the ramp,
                # which must happen before the port lock is taken: the ramp thread
                # needs that lock to finish its in-flight step.
                cancelled = self.controller.cancel(SETPOINT_COMMANDS[command],
                                                   reason="setpointOverride")
            with self.lock:
                result = run_command(request, self.session)
            if command in SETPOINT_COMMANDS:
                # Always present, empty when no ramp was interrupted.
                result["cancelledRamps"] = cancelled
            if command == "status":
                result["ramps"] = self.controller.snapshot()
            return result
        except Exception as exc:  # noqa: BLE001 - reported to the client
            return {"ok": False, "error": str(exc)}

    def _start_ramp(self, command: str, request: dict[str, Any]) -> dict[str, Any]:
        """Translate a ramp request and hand it to the controller."""
        kind = RAMP_COMMANDS[command]
        unit = "Volts" if kind == "voltage" else "Amps"
        return self.controller.start(
            kind,
            _as_float(request, "from", f"from{unit}", "start"),
            _as_float(request, "to", f"to{unit}", "end"),
            _as_float(request, "seconds", "duration") or 0.0,
            int(request["stepMs"]) if request.get("stepMs") else None,
            wait=bool(request.get("wait", False)),
        )

    def _stop_ramp(self, request: dict[str, Any]) -> dict[str, Any]:
        """Cancel one kind of ramp, or both when no kind is given."""
        kind = request.get("kind") or None
        if kind is not None and kind not in RAMP_COMMANDS.values():
            raise ValueError(f"Unknown ramp kind: {kind} (expected voltage or current)")
        cancelled = self.controller.cancel(kind, reason="stopRamp")
        return {
            "ok": True,
            "command": "stopRamp",
            "cancelledRamps": cancelled,
            "stopped": len(cancelled),
        }


class _ControlServer(socketserver.ThreadingTCPServer):
    """Threaded JSON-line control socket; one shared serial session behind a lock."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, handler, session: PsuSession, owner: "ServeRunner") -> None:
        super().__init__(address, handler)
        self.session = session
        self.owner = owner


class _ControlHandler(socketserver.StreamRequestHandler):
    """One control-socket connection: newline-delimited JSON requests and replies."""

    def handle(self) -> None:  # noqa: D102 - socketserver interface
        server: _ControlServer = self.server  # type: ignore[assignment]
        owner = server.owner
        while True:
            line = self.rfile.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            owner.touch_idle()
            try:
                request = json.loads(text)
            except ValueError:
                self._reply({"ok": False, "error": "invalid JSON request"})
                continue
            if request.get("command") == "shutdown":
                self._reply({"ok": True, "command": "shutdown", "pid": os.getpid()})
                owner.request_shutdown()
                return
            # Every client shares one serial port, so commands are serialized: two
            # overlapping request/response exchanges would let one client's response
            # satisfy the other client's parser. The executor owns that discipline,
            # because ramps must not hold the lock for their whole duration.
            self._reply(owner.executor.execute(request))

    def _reply(self, payload: dict[str, Any]) -> None:
        """Write one response line (newlines inside the payload are collapsed)."""
        text = json.dumps(payload).replace("\r", " ").replace("\n", " ")
        try:
            self.wfile.write((text + "\n").encode("utf-8"))
            self.wfile.flush()
        except OSError:
            # The client vanished; that is not a backend failure.
            pass


class ServeRunner:
    """Runs the persistent backend until shutdown is requested or it goes idle."""

    def __init__(self, args: argparse.Namespace,
                 transport_factory: Callable[..., Any] = create_serial_transport) -> None:
        self.args = args
        self.transport = transport_factory(args)
        self.session = PsuSession(self.transport, args.timeout_ms)
        self.command_lock = threading.Lock()
        self.controller = RampController(self.session, self.command_lock)
        self.executor = ServeExecutor(self.session, self.command_lock, self.controller)
        self._server: _ControlServer | None = None
        self._idle_timer: threading.Timer | None = None
        self._shutdown = threading.Event()
        self._exit_code = 0

    def touch_idle(self) -> None:
        """(Re)arm the idle shutdown timer."""
        if self._idle_timer is not None:
            self._idle_timer.cancel()
        seconds = float(self.args.serve_idle_timeout or 0)
        if seconds <= 0:
            return
        self._idle_timer = threading.Timer(seconds, self._idle_expired)
        self._idle_timer.daemon = True
        self._idle_timer.start()

    def _idle_expired(self) -> None:
        """Idle timeout fired: shut down only if no ramp is still running.

        A long background ramp produces no client traffic, so without this check the
        idle timer would stop the backend in the middle of it.
        """
        if self.controller.has_active():
            self.touch_idle()
            return
        self.request_shutdown()

    def request_shutdown(self, code: int = 0) -> None:
        """Ask the serve loop to stop (safe from any thread)."""
        self._exit_code = code
        self._shutdown.set()
        if self._server is not None:
            threading.Thread(target=self._server.shutdown, daemon=True).start()

    def run(self) -> int:
        """Open the port, serve the control socket, and clean up on the way out."""
        try:
            self.transport.open()
        except Exception as exc:  # noqa: BLE001 - reported as JSON on stdout
            print(json.dumps({"ok": False, "error": f"serve open failed: {exc}"}), flush=True)
            return 1
        try:
            self._server = _ControlServer(
                (self.args.serve_host, self.args.serve_port), _ControlHandler, self.session, self
            )
        except OSError as exc:
            print(json.dumps({"ok": False, "error": f"serve listen failed: {exc}"}), flush=True)
            self.transport.close()
            return 1

        actual_port = self._server.server_address[1]
        write_state_file(self.args.serve_state_file, {
            "pid": os.getpid(),
            "host": self.args.serve_host,
            "port": actual_port,
            "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        })
        self.touch_idle()
        print(json.dumps({
            "ok": True, "mode": "serve", "event": "listening",
            "pid": os.getpid(), "port": actual_port,
        }), flush=True)

        try:
            self._server.serve_forever(poll_interval=0.2)
        finally:
            if self._idle_timer is not None:
                self._idle_timer.cancel()
            try:
                self._server.server_close()
            except OSError:
                pass
            remove_state_file(self.args.serve_state_file)
            # Stop any background ramp before taking the lock: the ramp thread needs
            # it per step, and the supply should be left at a known setpoint.
            self.controller.cancel(reason="shutdown")
            # Leave the supply under front-panel control and release the port.
            with self.command_lock:
                self.session.release_remote_mode()
                self.transport.close()
        return self._exit_code


def run_serve(args: argparse.Namespace,
              transport_factory: Callable[..., Any] = create_serial_transport) -> int:
    """Entry point for serve mode (kept as a function for symmetry with one-shot)."""
    return ServeRunner(args, transport_factory).run()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

COMMANDS = (
    "serve", "probe", "status", "readVoltage", "readCurrent", "readVersion",
    "setVoltage", "setCurrent", "powerOn", "powerOff", "powerCycle",
    "rampVoltage", "rampCurrent", "stopRamp", "connect", "disconnect",
)

# Commands that only make sense against a running backend: a one-shot process owns
# the port for the duration of a single command, so there is no ramp to stop.
SERVE_ONLY_COMMANDS = ("stopRamp",)


def build_parser() -> argparse.ArgumentParser:
    """Command-line parser shared by serve and one-shot modes."""
    parser = argparse.ArgumentParser(
        prog="kuaiqus_psu_backend.py",
        description="KUAIQU-NICE-POWER SPPS PSU serial backend",
        epilog="Commands: " + ", ".join(COMMANDS),
    )
    parser.add_argument("command", nargs="?", choices=COMMANDS, help="command to run")
    parser.add_argument("--port", default="", help="serial port (COMx, /dev/ttyUSBx, /dev/cu.*)")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help="baud rate (default 9600)")
    parser.add_argument("--value", type=float, default=None,
                        help="voltage (V) or current (A) for the set commands")
    parser.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS,
                        dest="timeout_ms", help="device response timeout (default 2000)")
    parser.add_argument("--off-seconds", type=float, default=DEFAULT_POWER_CYCLE_OFF_SECONDS,
                        dest="off_seconds", help="powerCycle: output-off time in seconds")
    parser.add_argument("--settle-seconds", type=float, default=DEFAULT_POWER_CYCLE_SETTLE_SECONDS,
                        dest="settle_seconds", help="powerCycle: settle time after power-on")
    parser.add_argument("--from", type=float, default=None, dest="from_value",
                        help="ramp start value (V or A)")
    parser.add_argument("--to", type=float, default=None, dest="to_value",
                        help="ramp end value (V or A)")
    parser.add_argument("--seconds", type=float, default=None,
                        help="ramp duration in seconds")
    parser.add_argument("--step-ms", type=int, default=None, dest="step_ms",
                        help="ramp step interval in milliseconds (default 200)")
    parser.add_argument("--wait", action=argparse.BooleanOptionalAction, default=None,
                        help="ramp: wait for completion (serve mode defaults to not "
                             "waiting; one-shot mode always waits)")
    parser.add_argument("--serve-host", default=DEFAULT_SERVE_HOST, dest="serve_host",
                        help="control socket host (default 127.0.0.1)")
    parser.add_argument("--serve-port", type=int, default=DEFAULT_SERVE_PORT, dest="serve_port",
                        help="control socket port (default 17332)")
    parser.add_argument("--serve-state-file", default="", dest="serve_state_file",
                        help="JSON state file for backend discovery")
    parser.add_argument("--serve-idle-timeout", type=float, default=DEFAULT_SERVE_IDLE_TIMEOUT,
                        dest="serve_idle_timeout",
                        help="auto-shutdown after idle seconds (0 disables)")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to serve or one-shot mode."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1
    if args.command == "serve":
        return run_serve(args)
    return run_one_shot(args)


if __name__ == "__main__":
    sys.exit(main())
