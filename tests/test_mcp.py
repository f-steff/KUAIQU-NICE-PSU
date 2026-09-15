"""Tests for the MCP stdio server (scripts/kuaiqus_psu_mcp.py).

Hardware-free and backend-free: the control socket call is stubbed, so these tests
cover the parts that are easy to get wrong and impossible to notice by hand - the
JSON-RPC envelope, the tool schemas, and the mapping from tool arguments onto
backend requests.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import kuaiqus_psu_mcp as mcp  # noqa: E402


def backend_request(tool_name: str, args: dict | None = None) -> dict:
    """The control-socket request a tool would send for the given arguments."""
    tool = next(t for t in mcp.TOOLS if t["name"] == tool_name)
    return tool["backend"](args or {})


class EnvelopeTests(unittest.TestCase):
    """JSON-RPC handling that every MCP client depends on."""

    def test_initialize_reports_protocol_and_server(self):
        reply = mcp.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                    "params": {}})
        result = reply["result"]
        self.assertEqual(result["protocolVersion"], mcp.MCP_PROTOCOL_VERSION)
        self.assertEqual(result["serverInfo"]["name"], mcp.SERVER_NAME)
        self.assertEqual(result["serverInfo"]["version"], mcp.SERVER_VERSION)
        self.assertIn("tools", result["capabilities"])

    def test_notifications_get_no_reply(self):
        # A notification has no id; answering one is a protocol violation.
        self.assertIsNone(mcp.handle_message({"jsonrpc": "2.0",
                                              "method": "notifications/initialized"}))

    def test_a_missing_method_is_an_invalid_request(self):
        reply = mcp.handle_message({"jsonrpc": "2.0", "id": 1})
        self.assertIn("error", reply)
        self.assertEqual(reply["error"]["code"], -32600)

    def test_unknown_method_is_reported_as_an_error(self):
        reply = mcp.handle_message({"jsonrpc": "2.0", "id": 2, "method": "no/such/method"})
        self.assertIn("error", reply)


class ToolListTests(unittest.TestCase):
    """The advertised tool surface."""

    EXPECTED = {
        "get_status", "read_voltage", "read_current", "read_version",
        "set_voltage", "set_current", "power_on", "power_off", "power_cycle",
        "ramp_voltage", "ramp_current", "stop_ramp",
    }

    def test_every_tool_is_advertised(self):
        reply = mcp.handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/list",
                                    "params": {}})
        names = {t["name"] for t in reply["result"]["tools"]}
        self.assertEqual(names, self.EXPECTED)

    def test_schemas_are_well_formed(self):
        for tool in mcp.TOOLS:
            with self.subTest(tool=tool["name"]):
                schema = tool["inputSchema"]
                self.assertEqual(schema["type"], "object")
                self.assertIsInstance(schema["properties"], dict)
                self.assertTrue(tool["description"].strip())
                # Required arguments must exist in properties, or a client cannot
                # render or validate the call.
                for name in schema.get("required", []):
                    self.assertIn(name, schema["properties"])
                for name, spec in schema["properties"].items():
                    self.assertIn("type", spec, f"{tool['name']}.{name} has no type")

    def test_list_is_json_serialisable(self):
        # The transport is newline-delimited JSON; a non-serialisable schema would
        # break the server at runtime rather than here.
        reply = mcp.handle_message({"jsonrpc": "2.0", "id": 4, "method": "tools/list",
                                    "params": {}})
        json.dumps(reply)


class BackendMappingTests(unittest.TestCase):
    """Tool arguments must land on the right control-socket request."""

    def test_read_and_power_tools(self):
        self.assertEqual(backend_request("get_status")["command"], "status")
        self.assertEqual(backend_request("read_voltage")["command"], "readVoltage")
        self.assertEqual(backend_request("read_current")["command"], "readCurrent")
        self.assertEqual(backend_request("read_version")["command"], "readVersion")
        self.assertEqual(backend_request("power_on")["command"], "powerOn")
        self.assertEqual(backend_request("power_off")["command"], "powerOff")

    def test_set_tools_pass_their_value(self):
        self.assertEqual(backend_request("set_voltage", {"voltage": 12.5}),
                         {"command": "setVoltage", "value": 12.5})
        self.assertEqual(backend_request("set_current", {"current": 0.75}),
                         {"command": "setCurrent", "value": 0.75})

    def test_power_cycle_defaults(self):
        request = backend_request("power_cycle")
        self.assertEqual(request["command"], "powerCycle")
        self.assertEqual(request["offSeconds"], 3)
        self.assertEqual(request["settleSeconds"], 2)

    def test_ramp_tools_default_to_not_waiting(self):
        # Parity with the CLI: a ramp returns immediately unless asked to wait.
        for name, command in (("ramp_voltage", "rampVoltage"),
                              ("ramp_current", "rampCurrent")):
            with self.subTest(tool=name):
                request = backend_request(name, {"start": 0, "end": 5, "seconds": 10})
                self.assertEqual(request["command"], command)
                self.assertEqual(request["from"], 0)
                self.assertEqual(request["to"], 5)
                self.assertEqual(request["seconds"], 10)
                self.assertFalse(request["wait"])

    def test_ramp_wait_is_passed_through(self):
        request = backend_request("ramp_voltage",
                                  {"start": 0, "end": 5, "seconds": 2, "wait": True})
        self.assertTrue(request["wait"])

    def test_ramp_step_ms_is_optional(self):
        request = backend_request("ramp_current", {"start": 0, "end": 1, "seconds": 5})
        self.assertIsNone(request["stepMs"])
        request = backend_request("ramp_current",
                                  {"start": 0, "end": 1, "seconds": 5, "step_ms": 50})
        self.assertEqual(request["stepMs"], 50)

    def test_stop_ramp_kind_is_optional(self):
        self.assertEqual(backend_request("stop_ramp"), {"command": "stopRamp", "kind": None})
        self.assertEqual(backend_request("stop_ramp", {"kind": "voltage"}),
                         {"command": "stopRamp", "kind": "voltage"})

    def test_stop_ramp_kind_is_constrained(self):
        schema = next(t for t in mcp.TOOLS if t["name"] == "stop_ramp")["inputSchema"]
        self.assertEqual(set(schema["properties"]["kind"]["enum"]), {"voltage", "current"})


class CallTimeoutTests(unittest.TestCase):
    """The socket timeout a tool call allows itself."""

    def _timeout_for(self, name: str, args: dict) -> float:
        seen: dict[str, float] = {}

        def fake_call(request, endpoint, timeout):
            seen["timeout"] = timeout
            return {"ok": True}

        with unittest.mock.patch.object(mcp, "ensure_backend", return_value=("127.0.0.1", 1)), \
                unittest.mock.patch.object(mcp, "call_backend", side_effect=fake_call):
            mcp.call_tool(name, args)
        return seen["timeout"]

    def test_a_non_waiting_ramp_does_not_inflate_the_timeout(self):
        # The call returns as soon as the ramp is accepted, so a 600 s ramp must not
        # make the client wait 600 s for the reply.
        timeout = self._timeout_for("ramp_voltage", {"start": 0, "end": 5, "seconds": 600})
        self.assertLessEqual(timeout, 30.0)

    def test_a_waiting_ramp_allows_for_its_duration(self):
        timeout = self._timeout_for("ramp_voltage",
                                    {"start": 0, "end": 5, "seconds": 120, "wait": True})
        self.assertGreater(timeout, 120.0)

    def test_power_cycle_allows_for_its_off_time(self):
        timeout = self._timeout_for("power_cycle", {"off_seconds": 90})
        self.assertGreater(timeout, 90.0)

    def test_unknown_tool_is_rejected(self):
        with unittest.mock.patch.object(mcp, "ensure_backend", return_value=("127.0.0.1", 1)):
            with self.assertRaises(ValueError):
                mcp.call_tool("no_such_tool", {})


class InterpreterResolutionTests(unittest.TestCase):
    """The backend must be started by a Python that actually has pyserial."""

    def test_an_explicit_interpreter_is_authoritative(self):
        # Substituting a different interpreter than the one configured would be
        # worse than failing, so the explicit choice is the only candidate.
        with unittest.mock.patch.dict(os.environ, {"KUAIQUS_PSU_PYTHON": "/opt/py/bin/python"}):
            self.assertEqual(mcp.python_candidates(), ["/opt/py/bin/python"])

    def test_own_interpreter_is_tried_first_then_the_usual_names(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            candidates = mcp.python_candidates()
        self.assertEqual(candidates[0], sys.executable)
        self.assertEqual(candidates[1:], ["python3", "python", "py"])

    def test_resolution_skips_interpreters_without_pyserial(self):
        calls = []

        class Result:
            def __init__(self, code):
                self.returncode = code

        def fake_run(argv, **kwargs):
            calls.append(argv[0])
            # Only the third candidate can import serial.
            return Result(0 if len(calls) == 3 else 1)

        with unittest.mock.patch.object(mcp, "python_candidates",
                                        return_value=["a", "b", "c", "d"]), \
                unittest.mock.patch.object(mcp.subprocess, "run", side_effect=fake_run):
            self.assertEqual(mcp.resolve_python(), "c")
        self.assertEqual(calls, ["a", "b", "c"])

    def test_resolution_failure_names_what_was_tried(self):
        class Result:
            returncode = 1

        with unittest.mock.patch.object(mcp, "python_candidates", return_value=["only"]), \
                unittest.mock.patch.object(mcp.subprocess, "run", return_value=Result()):
            with self.assertRaises(RuntimeError) as caught:
                mcp.resolve_python()
        message = str(caught.exception)
        self.assertIn("only", message)
        self.assertIn("pyserial", message)


if __name__ == "__main__":
    unittest.main()
