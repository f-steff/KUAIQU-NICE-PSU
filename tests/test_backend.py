"""Backend tests - command dispatch, timeouts, ramps, and the serve lifecycle.

Hardware-free: a fake transport answers frames from a script, so no serial port is
opened and the tests run identically on Windows, Linux and macOS.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import sys
import threading
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import kuaiqus_psu_backend as backend  # noqa: E402
import kuaiqus_psu_protocol as protocol  # noqa: E402


class FakeTransport:
    """Records written frames and replays canned responses.

    `responses` maps a function code (the second payload character of the frame the
    PC sends) to the raw response frame the device would answer with. Missing codes
    produce no response, which is how the timeout path is exercised.
    """

    def __init__(self, responses: dict[str, str] | None = None) -> None:
        self.port = "FAKE"
        self.baud = 9600
        self.writes: list[str] = []
        self.responses = responses or {}
        self._pending = bytearray()
        self.opened = False
        self.closed = False

    def open(self) -> None:
        self.opened = True

    def write(self, text: str) -> None:
        self.writes.append(text)
        func = text[2] if len(text) > 2 else ""
        reply = self.responses.get(func)
        if reply:
            self._pending.extend(reply.encode("ascii"))

    def read(self, size: int = 256) -> bytes:
        if not self._pending:
            # Keep the request loop from spinning hot while it waits for a timeout.
            time.sleep(0.005)
            return b""
        chunk = bytes(self._pending[:size])
        del self._pending[:size]
        return chunk

    def close(self) -> None:
        self.closed = True

    # -- helpers used by the tests ----------------------------------------

    def functions_written(self) -> list[str]:
        """Function codes of every frame written so far, in order."""
        return [w[2] for w in self.writes if len(w) > 2]


READ_RESPONSES = {
    "2": "<12025000000>",   # read voltage -> 25.000 V, CV
    "4": "<14000500000>",   # read current -> 0.500 A, CV
    "6": "<16V1.300000>",   # read version
    "1": "<11OK0000000>",   # set voltage ack
    "3": "<13OK0000000>",   # set current ack
}


def make_session(responses=None, timeout_ms=300):
    """Build a session on a fake transport with a short timeout for fast tests."""
    transport = FakeTransport(responses if responses is not None else dict(READ_RESPONSES))
    transport.open()
    return backend.PsuSession(transport, timeout_ms), transport


class RequestTests(unittest.TestCase):
    """Framing, response matching and timeout behaviour."""

    def test_request_returns_device_frame(self):
        session, _ = make_session()
        frame = session.request(protocol.encode_read_voltage())
        self.assertEqual(frame.value_milli, 25000)
        self.assertEqual(frame.cv_cc, "CV")

    def test_request_times_out_when_silent(self):
        session, _ = make_session(responses={})
        with self.assertRaises(TimeoutError):
            session.request(protocol.encode_read_voltage())

    def test_request_ignores_echoed_pc_frames(self):
        # A frame with the PC direction indicator must not satisfy the request.
        session, transport = make_session(responses={"2": "<02000000000><12025000000>"})
        frame = session.request(protocol.encode_read_voltage())
        self.assertTrue(frame.from_mcu)
        self.assertEqual(frame.value_milli, 25000)


class ReadCommandTests(unittest.TestCase):
    """The read/status/probe commands and their JSON shape."""

    def test_read_voltage(self):
        session, _ = make_session()
        result = session.read_voltage()
        self.assertTrue(result["ok"])
        self.assertAlmostEqual(result["voltageVolts"], 25.0, places=6)
        self.assertEqual(result["cvCc"], "CV")

    def test_read_current(self):
        session, _ = make_session()
        result = session.read_current()
        self.assertAlmostEqual(result["currentAmps"], 0.5, places=6)

    def test_read_version(self):
        session, _ = make_session()
        result = session.read_version()
        self.assertEqual(result["version"], "1.3")
        self.assertEqual(result["versionRaw"], "V1.300000")

    def test_status_reads_both(self):
        session, transport = make_session()
        result = session.status()
        self.assertAlmostEqual(result["voltageVolts"], 25.0, places=6)
        self.assertAlmostEqual(result["currentAmps"], 0.5, places=6)
        self.assertEqual(transport.functions_written(), ["2", "4"])

    def test_probe_reports_a_responding_device(self):
        session, _ = make_session()
        result = session.probe()
        self.assertTrue(result["ok"])
        self.assertTrue(result["responded"])

    def test_probe_reports_a_silent_port_without_raising(self):
        session, _ = make_session(responses={})
        result = session.probe()
        self.assertTrue(result["ok"])
        self.assertFalse(result["responded"])
        self.assertIn("error", result)


class WriteCommandTests(unittest.TestCase):
    """Set/power commands, remote-mode handling and acknowledgement checks."""

    def test_set_voltage_requires_ack(self):
        session, _ = make_session()
        result = session.set_voltage(25)
        self.assertTrue(result["ok"])
        self.assertEqual(result["requestedVolts"], 25.0)

    def test_set_voltage_rejects_a_non_ack_response(self):
        session, _ = make_session(responses={"1": "<12025000000>"})
        with self.assertRaises(RuntimeError):
            session.set_voltage(25)

    def test_remote_mode_is_entered_once(self):
        session, transport = make_session()
        session.set_voltage(25)
        session.set_current(10)
        # Function 9 (mode switch) must appear exactly once for the session.
        self.assertEqual(transport.functions_written().count("9"), 1)

    def test_power_on_off(self):
        session, transport = make_session()
        session.power_on()
        session.power_off()
        self.assertIn("7", transport.functions_written())
        self.assertIn("8", transport.functions_written())

    def test_release_remote_mode_sends_local(self):
        session, transport = make_session()
        session.set_voltage(25)
        session.release_remote_mode()
        self.assertEqual(transport.writes[-1], protocol.encode_disconnect())
        self.assertFalse(session.remote_mode)


class PowerCycleTests(unittest.TestCase):
    """The power-cycle primitive."""

    def test_power_cycle_switches_off_then_on_and_measures(self):
        session, transport = make_session()
        result = session.power_cycle(off_seconds=0.01, settle_seconds=0.01)
        self.assertTrue(result["ok"])
        codes = transport.functions_written()
        self.assertLess(codes.index("8"), codes.index("7"))  # off before on
        self.assertAlmostEqual(result["voltageVolts"], 25.0, places=6)

    def test_negative_off_time_is_rejected(self):
        session, _ = make_session()
        with self.assertRaises(ValueError):
            session.power_cycle(off_seconds=-1)

    def test_measurement_failure_is_reported_not_raised(self):
        # Power on/off are fire-and-forget, so only the follow-up read can time out.
        session, _ = make_session(responses={})
        result = session.power_cycle(off_seconds=0.01, settle_seconds=0.01)
        self.assertTrue(result["ok"])
        self.assertIn("measurementError", result)


class RampTests(unittest.TestCase):
    """Setpoint ramps: endpoints, step count and validation."""

    def test_voltage_ramp_hits_both_endpoints(self):
        session, transport = make_session()
        result = session.ramp_voltage(0, 5, seconds=0.05, step_ms=10)
        self.assertTrue(result["ok"])
        self.assertEqual(result["fromVolts"], 0.0)
        self.assertEqual(result["toVolts"], 5.0)
        sets = [w for w in transport.writes if len(w) > 2 and w[2] == "1"]
        self.assertEqual(sets[0], protocol.encode_set_voltage(0))
        self.assertEqual(sets[-1], protocol.encode_set_voltage(5))
        self.assertEqual(len(sets), result["steps"])

    def test_current_ramp_uses_the_current_function_code(self):
        session, transport = make_session()
        result = session.ramp_current(0.1, 0.2, seconds=0.02, step_ms=10)
        self.assertEqual(result["command"], "rampCurrent")
        self.assertTrue(all(w[2] == "3" for w in transport.writes if len(w) > 2 and w[2] in "13"))

    def test_ramp_validates_endpoints_before_writing(self):
        session, transport = make_session()
        with self.assertRaises(ValueError):
            session.ramp_voltage(0, 1000, seconds=1)
        self.assertEqual(transport.writes, [])

    def test_ramp_step_count_is_bounded(self):
        # A long ramp with a 1 ms step asks for a million steps; the session clamps
        # the count to MAX_RAMP_STEPS because every step is an acknowledged write.
        #
        # Sleeping is patched out on purpose: the clamp does NOT shorten the ramp,
        # it stretches the interval so the caller still gets the duration they asked
        # for, and a real 1000-second ramp cannot be part of a unit test.
        session, _ = make_session()
        slept: list[float] = []
        with unittest.mock.patch.object(backend.time, "sleep", slept.append):
            result = session.ramp_voltage(0, 1, seconds=1000, step_ms=1)
        self.assertLessEqual(result["steps"], backend.MAX_RAMP_STEPS + 1)
        # Interval stretched to keep the requested total duration.
        self.assertAlmostEqual(result["stepMs"], 1000 * 1000 / backend.MAX_RAMP_STEPS, delta=1)
        # One sleep per step gap, plus the single remote-mode switch delay.
        self.assertLessEqual(len(slept), result["steps"])

    def test_zero_duration_still_sets_the_endpoint(self):
        session, transport = make_session()
        result = session.ramp_voltage(5, 5, seconds=0)
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(len(transport.writes), 1)


class DispatchTests(unittest.TestCase):
    """run_command maps control-socket requests onto session methods."""

    def test_every_command_name_is_routed(self):
        session, _ = make_session()
        cases = [
            ({"command": "status"}, "status"),
            ({"command": "readVoltage"}, "readVoltage"),
            ({"command": "readCurrent"}, "readCurrent"),
            ({"command": "readVersion"}, "readVersion"),
            ({"command": "setVoltage", "value": 12}, "setVoltage"),
            ({"command": "setCurrent", "value": 1}, "setCurrent"),
            ({"command": "powerOn"}, "powerOn"),
            ({"command": "powerOff"}, "powerOff"),
            ({"command": "connect"}, "connect"),
            ({"command": "disconnect"}, "disconnect"),
            ({"command": "powerCycle", "offSeconds": 0.01, "settleSeconds": 0.01}, "powerCycle"),
            ({"command": "rampVoltage", "from": 0, "to": 1, "seconds": 0.02, "stepMs": 10}, "rampVoltage"),
            ({"command": "rampCurrent", "from": 0, "to": 1, "seconds": 0.02, "stepMs": 10}, "rampCurrent"),
        ]
        for request, expected in cases:
            result = backend.run_command(request, session)
            self.assertEqual(result["command"], expected, request)

    def test_unknown_command_raises(self):
        session, _ = make_session()
        with self.assertRaises(ValueError):
            backend.run_command({"command": "nope"}, session)

    def test_argument_aliases(self):
        session, _ = make_session()
        result = backend.run_command(
            {"command": "rampVoltage", "start": 0, "end": 2, "duration": 0.02, "stepMs": 10}, session
        )
        self.assertEqual(result["toVolts"], 2.0)


class ArgumentParsingTests(unittest.TestCase):
    """The CLI parser used by one-shot mode."""

    def test_defaults(self):
        args = backend.build_parser().parse_args(["status"])
        self.assertEqual(args.command, "status")
        self.assertEqual(args.baud, backend.DEFAULT_BAUD)
        self.assertEqual(args.serve_port, backend.DEFAULT_SERVE_PORT)

    def test_ramp_options(self):
        args = backend.build_parser().parse_args(
            ["rampVoltage", "--from", "1", "--to", "2", "--seconds", "3", "--step-ms", "250"]
        )
        self.assertEqual((args.from_value, args.to_value, args.seconds, args.step_ms), (1.0, 2.0, 3.0, 250))

    def test_unknown_command_is_rejected(self):
        with self.assertRaises(SystemExit):
            backend.build_parser().parse_args(["bogus"])


class ServeLifecycleTests(unittest.TestCase):
    """Full control-socket lifecycle against a fake transport."""

    def test_serve_answers_commands_and_shuts_down(self):
        state_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".serve-test-state.json")
        args = backend.build_parser().parse_args([
            "serve", "--serve-port", "0", "--serve-state-file", state_file,
            "--serve-idle-timeout", "30", "--timeout-ms", "300",
        ])
        transport = FakeTransport(dict(READ_RESPONSES))
        runner = backend.ServeRunner(args, transport_factory=lambda _a: transport)

        thread = threading.Thread(target=runner.run, daemon=True)
        thread.start()
        try:
            # Wait for the state file so the ephemeral port is known.
            deadline = time.monotonic() + 5
            endpoint = None
            while time.monotonic() < deadline:
                try:
                    with open(state_file, "r", encoding="utf-8") as handle:
                        data = json.load(handle)
                    endpoint = (data["host"], int(data["port"]))
                    break
                except (OSError, ValueError, KeyError):
                    time.sleep(0.05)
            self.assertIsNotNone(endpoint, "serve mode never published its state file")

            with socket.create_connection(endpoint, timeout=5) as sock:
                sock.sendall(b'{"command": "status"}\n')
                reply = sock.makefile("r", encoding="utf-8").readline()
            parsed = json.loads(reply)
            self.assertTrue(parsed["ok"])
            self.assertAlmostEqual(parsed["voltageVolts"], 25.0, places=6)

            # An invalid line must be answered, not crash the server.
            with socket.create_connection(endpoint, timeout=5) as sock:
                sock.sendall(b"not json\n")
                reply = sock.makefile("r", encoding="utf-8").readline()
            self.assertFalse(json.loads(reply)["ok"])

            with socket.create_connection(endpoint, timeout=5) as sock:
                sock.sendall(b'{"command": "shutdown"}\n')
                reply = sock.makefile("r", encoding="utf-8").readline()
            self.assertEqual(json.loads(reply)["command"], "shutdown")

            thread.join(timeout=10)
            self.assertFalse(thread.is_alive(), "serve mode did not stop after shutdown")
            self.assertTrue(transport.closed, "serve mode did not close the transport")
            self.assertFalse(os.path.exists(state_file), "state file was not removed")
        finally:
            runner.request_shutdown()
            thread.join(timeout=5)
            if os.path.exists(state_file):
                os.unlink(state_file)


def make_controller(responses=None, timeout_ms=300):
    """Session plus a RampController sharing one port lock, as serve mode wires it."""
    session, transport = make_session(responses, timeout_ms)
    lock = threading.Lock()
    return session, transport, lock, backend.RampController(session, lock)


def make_executor(responses=None, timeout_ms=300):
    """A ServeExecutor over a fake transport, plus the parts the tests inspect."""
    session, transport, lock, controller = make_controller(responses, timeout_ms)
    return backend.ServeExecutor(session, lock, controller), session, transport, controller


def wait_until(predicate, timeout=5.0, interval=0.01):
    """Poll until `predicate` is true. Timing tests must not rely on fixed sleeps."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class BackgroundRampTests(unittest.TestCase):
    """Ramps run on a worker thread; the caller chooses whether to wait."""

    def test_returns_immediately_without_wait(self):
        _, _, _, controller = make_controller()
        began = time.monotonic()
        ack = controller.start("voltage", 0, 5, seconds=3, step_ms=50)
        elapsed = time.monotonic() - began
        try:
            self.assertTrue(ack["started"])
            self.assertFalse(ack["waited"])
            self.assertEqual(ack["status"], "running")
            # The point of the feature: the call does not block for the ramp.
            self.assertLess(elapsed, 0.5)
        finally:
            controller.cancel()

    def test_wait_blocks_until_the_ramp_is_done(self):
        session, transport, _, controller = make_controller()
        result = controller.start("voltage", 0, 1, seconds=0.2, step_ms=20, wait=True)
        self.assertTrue(result["ok"])
        self.assertTrue(result["waited"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["writesDone"], result["steps"])
        self.assertEqual(transport.writes[-1], protocol.encode_set_voltage(1))
        self.assertAlmostEqual(session.last_setpoint["voltage"], 1.0, places=6)

    def test_progress_is_visible_while_running(self):
        _, _, _, controller = make_controller()
        controller.start("voltage", 0, 5, seconds=2, step_ms=50)
        try:
            self.assertTrue(wait_until(lambda: controller.snapshot()["voltage"]["writesDone"] > 1))
            snap = controller.snapshot()["voltage"]
            self.assertTrue(snap["active"])
            self.assertEqual(snap["status"], "running")
            self.assertGreater(snap["percent"], 0)
            self.assertLess(snap["percent"], 100)
            self.assertGreater(snap["atVolts"], 0)
            self.assertLess(snap["atVolts"], 5)
            self.assertGreater(snap["remainingSeconds"], 0)
        finally:
            controller.cancel()

    def test_cancel_reports_where_it_stopped(self):
        session, _, _, controller = make_controller()
        controller.start("voltage", 0, 10, seconds=5, step_ms=50)
        self.assertTrue(wait_until(lambda: controller.snapshot()["voltage"]["writesDone"] > 1))
        cancelled = controller.cancel()
        self.assertEqual(len(cancelled), 1)
        info = cancelled[0]
        self.assertEqual(info["status"], "cancelled")
        self.assertEqual(info["reason"], "stopRamp")
        self.assertLess(info["writesDone"], info["steps"])
        self.assertFalse(controller.has_active())
        # The supply is left at the last setpoint that was actually acknowledged.
        self.assertAlmostEqual(session.last_setpoint["voltage"], info["atVolts"], places=3)

    def test_replacement_starts_from_the_current_setpoint(self):
        session, _, _, controller = make_controller()
        controller.start("voltage", 0, 10, seconds=5, step_ms=50)
        self.assertTrue(wait_until(lambda: controller.snapshot()["voltage"]["writesDone"] > 2))
        reached = session.last_setpoint["voltage"]
        self.assertGreater(reached, 0)
        # The requested start (0) is ignored: continuing from where the supply is
        # avoids stepping the output backwards discontinuously.
        ack = controller.start("voltage", 0, 0, seconds=1, step_ms=50)
        try:
            self.assertIsNotNone(ack.get("replaced"))
            self.assertEqual(ack["replaced"]["status"], "cancelled")
            self.assertEqual(ack["replaced"]["reason"], "replaced")
            self.assertAlmostEqual(ack["fromVolts"], reached, places=3)
        finally:
            controller.cancel()

    def test_voltage_and_current_ramps_run_concurrently(self):
        _, _, _, controller = make_controller()
        controller.start("voltage", 0, 5, seconds=2, step_ms=50)
        controller.start("current", 0, 1, seconds=2, step_ms=50)
        try:
            self.assertTrue(wait_until(
                lambda: controller.snapshot().get("voltage", {}).get("writesDone", 0) > 0
                and controller.snapshot().get("current", {}).get("writesDone", 0) > 0))
            snap = controller.snapshot()
            # Separate setpoints, so neither ramp cancels the other.
            self.assertTrue(snap["voltage"]["active"])
            self.assertTrue(snap["current"]["active"])
        finally:
            self.assertEqual(len(controller.cancel()), 2)

    def test_finished_ramps_drop_out_of_status_eventually(self):
        _, _, _, controller = make_controller()
        controller.start("voltage", 0, 1, seconds=0.05, step_ms=20, wait=True)
        # Just finished: still reported, so a status read explains the moved setpoint.
        self.assertIn("voltage", controller.snapshot())
        run = controller._runs["voltage"]
        run.finished_at = time.monotonic() - (backend.RAMP_HISTORY_SECONDS + 1)
        # Old news: status must not carry it forever.
        self.assertNotIn("voltage", controller.snapshot())

    def test_a_failing_write_is_reported_not_raised(self):
        # A silent device makes every set command time out.
        _, _, _, controller = make_controller(responses={})
        result = controller.start("voltage", 0, 1, seconds=0.1, step_ms=20, wait=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["error"])


class ServeExecutorTests(unittest.TestCase):
    """Serve-mode rules: ramp ownership, cancellation and lock discipline."""

    def test_manual_set_cancels_only_that_kind(self):
        executor, session, _, controller = make_executor()
        executor.execute({"command": "rampVoltage", "from": 0, "to": 5,
                          "seconds": 5, "stepMs": 50})
        executor.execute({"command": "rampCurrent", "from": 0, "to": 1,
                          "seconds": 5, "stepMs": 50})
        self.assertTrue(wait_until(
            lambda: controller.snapshot()["voltage"]["writesDone"] > 0
            and controller.snapshot()["current"]["writesDone"] > 0))
        try:
            result = executor.execute({"command": "setVoltage", "value": 12})
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["cancelledRamps"]), 1)
            self.assertEqual(result["cancelledRamps"][0]["kind"], "voltage")
            self.assertEqual(result["cancelledRamps"][0]["reason"], "setpointOverride")
            # The operator's value stands, and the other ramp is untouched.
            self.assertAlmostEqual(session.last_setpoint["voltage"], 12.0, places=6)
            self.assertTrue(controller.snapshot()["current"]["active"])
        finally:
            controller.cancel()

    def test_status_works_during_a_ramp_and_includes_progress(self):
        executor, _, _, controller = make_executor()
        executor.execute({"command": "rampVoltage", "from": 0, "to": 5,
                          "seconds": 2, "stepMs": 50})
        try:
            self.assertTrue(wait_until(lambda: controller.snapshot()["voltage"]["writesDone"] > 0))
            result = executor.execute({"command": "status"})
            # The read went through between ramp steps rather than waiting for the
            # whole ramp, which is the point of locking per step.
            self.assertTrue(result["ok"])
            self.assertAlmostEqual(result["voltageVolts"], 25.0, places=6)
            self.assertTrue(result["ramps"]["voltage"]["active"])
        finally:
            controller.cancel()

    def test_power_commands_do_not_cancel_a_ramp(self):
        executor, _, _, controller = make_executor()
        executor.execute({"command": "rampVoltage", "from": 0, "to": 5,
                          "seconds": 2, "stepMs": 50})
        try:
            result = executor.execute({"command": "powerOff"})
            self.assertTrue(result["ok"])
            self.assertNotIn("cancelledRamps", result)
            # A ramp is setpoint-only, so the output relay is unrelated to it.
            self.assertTrue(controller.has_active())
        finally:
            controller.cancel()

    def test_stop_ramp_with_nothing_running(self):
        executor, _, _, _ = make_executor()
        result = executor.execute({"command": "stopRamp"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["stopped"], 0)

    def test_stop_ramp_rejects_an_unknown_kind(self):
        executor, _, _, _ = make_executor()
        result = executor.execute({"command": "stopRamp", "kind": "power"})
        self.assertFalse(result["ok"])
        self.assertIn("voltage or current", result["error"])

    def test_wait_is_honoured_through_the_executor(self):
        executor, _, _, _ = make_executor()
        result = executor.execute({"command": "rampVoltage", "from": 0, "to": 1,
                                   "seconds": 0.2, "stepMs": 20, "wait": True})
        self.assertTrue(result["ok"])
        self.assertTrue(result["waited"])
        self.assertEqual(result["status"], "completed")


class SchemaInvarianceTests(unittest.TestCase):
    """A reply's keys must not depend on hidden state.

    "Absent" is a worse signal than "present but empty": a consumer would have to
    tell a command that cancelled nothing apart from an older backend that never
    reported cancellations at all. So each command answers with the same key set
    whether or not a ramp happened to be running.
    """

    def test_set_commands_always_report_cancelled_ramps(self):
        executor, _, _, controller = make_executor()
        idle = executor.execute({"command": "setVoltage", "value": 5})
        self.assertEqual(idle["cancelledRamps"], [])

        executor.execute({"command": "rampVoltage", "from": 0, "to": 5,
                          "seconds": 5, "stepMs": 50})
        self.assertTrue(wait_until(lambda: controller.snapshot()["voltage"]["writesDone"] > 0))
        try:
            busy = executor.execute({"command": "setVoltage", "value": 6})
        finally:
            controller.cancel()
        self.assertEqual(len(busy["cancelledRamps"]), 1)
        # Same shape either way.
        self.assertEqual(set(idle.keys()), set(busy.keys()))

    def test_status_always_reports_ramps(self):
        executor, _, _, controller = make_executor()
        idle = executor.execute({"command": "status"})
        self.assertEqual(idle["ramps"], {})

        executor.execute({"command": "rampVoltage", "from": 0, "to": 5,
                          "seconds": 5, "stepMs": 50})
        try:
            busy = executor.execute({"command": "status"})
        finally:
            controller.cancel()
        self.assertIn("voltage", busy["ramps"])
        self.assertEqual(set(idle.keys()), set(busy.keys()))

    def test_ramp_start_always_reports_what_it_replaced(self):
        _, _, _, controller = make_controller()
        first = controller.start("voltage", 0, 5, seconds=5, step_ms=50)
        self.assertIsNone(first["replaced"])
        self.assertTrue(wait_until(lambda: controller.snapshot()["voltage"]["writesDone"] > 1))
        second = controller.start("voltage", 0, 1, seconds=5, step_ms=50)
        try:
            self.assertIsNotNone(second["replaced"])
            self.assertEqual(set(first.keys()), set(second.keys()))
        finally:
            controller.cancel()

    def test_ramp_progress_keys_are_stable(self):
        _, _, _, controller = make_controller()
        controller.start("voltage", 0, 5, seconds=5, step_ms=50)
        self.assertTrue(wait_until(lambda: controller.snapshot()["voltage"]["writesDone"] > 0))
        running = controller.snapshot()["voltage"]
        controller.cancel()
        finished = controller.snapshot()["voltage"]
        self.assertEqual(set(running.keys()), set(finished.keys()))
        # Present-but-null rather than missing when they do not apply.
        for key in ("error", "remainingSeconds", "replaced"):
            self.assertIn(key, running)
            self.assertIn(key, finished)
        self.assertIsNone(finished["remainingSeconds"])


class OneShotRampTests(unittest.TestCase):
    """One-shot mode owns the port for a single command, so it always waits."""

    def _run(self, argv):
        args = backend.build_parser().parse_args(argv)
        buffer = io.StringIO()
        factory = lambda _a: FakeTransport(dict(READ_RESPONSES))  # noqa: E731
        with contextlib.redirect_stdout(buffer):
            code = backend.run_one_shot(args, transport_factory=factory)
        return code, buffer.getvalue()

    def test_no_wait_is_rejected(self):
        code, output = self._run(["rampVoltage", "--from", "0", "--to", "1",
                                  "--seconds", "1", "--no-wait"])
        self.assertEqual(code, 2)
        self.assertIn("needs a running backend", output)

    def test_ramp_without_wait_flag_still_runs(self):
        code, output = self._run(["rampVoltage", "--from", "0", "--to", "1",
                                  "--seconds", "0.05", "--step-ms", "20"])
        self.assertEqual(code, 0)
        self.assertIn("\"status\": \"completed\"", output)

    def test_stop_ramp_is_rejected(self):
        code, output = self._run(["stopRamp"])
        self.assertEqual(code, 2)
        self.assertIn("needs a running backend", output)


class IdleTimeoutTests(unittest.TestCase):
    """The idle timer must not stop the backend in the middle of a ramp."""

    def _runner(self):
        args = backend.build_parser().parse_args(
            ["serve", "--serve-idle-timeout", "0.05", "--serve-port", "0"])
        transport = FakeTransport(dict(READ_RESPONSES))
        return backend.ServeRunner(args, transport_factory=lambda _a: transport)

    def test_idle_expiry_defers_while_a_ramp_runs(self):
        runner = self._runner()
        runner.controller.has_active = lambda: True  # type: ignore[assignment]
        runner._idle_expired()
        try:
            self.assertFalse(runner._shutdown.is_set())
        finally:
            if runner._idle_timer is not None:
                runner._idle_timer.cancel()

    def test_idle_expiry_shuts_down_when_no_ramp_runs(self):
        runner = self._runner()
        runner.controller.has_active = lambda: False  # type: ignore[assignment]
        runner._idle_expired()
        self.assertTrue(runner._shutdown.is_set())


if __name__ == "__main__":
    unittest.main()
