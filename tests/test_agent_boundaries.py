"""v3.2 Phase 2: Direct Runtime and the optional Agent layer stay separable.

The six v1 tools must work with no agent package, no agent host and no model.
An agent failure must not be able to take the direct runtime down, and closing
an agent host must not close the runtime.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_runtime import FakeDevice
from harmony_agent.host import AgentHost, host_from_env
from harmony_runtime.contracts import RuntimeFault
from harmony_runtime.runtime import Runtime
from harmony_runtime.service import Client, build_host, serve


def runtime_factory(devices, runtimes):
    def factory(serial):
        device = FakeDevice(serial)
        devices.append(device)
        return device

    def build(path):
        runtime = Runtime(path, factory=factory, discover=lambda: ["fake"])
        runtimes.append(runtime)
        return runtime

    return build


class ServiceHarness:
    """Run the real service in a thread so the v1/v2 boundary is exercised."""

    def __init__(self, root, host_factory):
        self.root = root
        self.devices = []
        self.runtimes = []
        self.ready = threading.Event()
        self.servers = []
        self.client = None
        self.thread = threading.Thread(
            target=serve, args=(root,),
            kwargs={"runtime_factory": runtime_factory(self.devices, self.runtimes),
                    "ready": self.notify, "host_factory": host_factory})
        self.thread.daemon = True

    def notify(self, server):
        self.servers.append(server)
        self.ready.set()

    def __enter__(self):
        self.thread.start()
        if not self.ready.wait(20):
            raise RuntimeError("the runtime service did not start")
        self.client = Client(self.root)
        return self

    def __exit__(self, *exc):
        self.servers[0].shutdown()
        self.thread.join(20)
        return False


class DirectRuntimeBoundaryTests(unittest.TestCase):
    """The six v1 tools never need the agent layer."""

    def test_v1_six_tools_work_with_no_agent_host(self):
        with tempfile.TemporaryDirectory() as root:
            with ServiceHarness(root, lambda runtime, state: None) as harness:
                session = harness.client.call("session", operation="open")
                session_id = session["session_id"]
                observed = harness.client.call("observe", session_id=session_id)
                self.assertEqual(observed["mode"], "FAST")
                result = harness.client.call("act", arguments={
                    "session_id": session_id, "request_id": "v1-only",
                    "observation_id": observed["observation_id"],
                    "action": {"kind": "tap", "target": {"action_id": "n0"}}})
                self.assertEqual(result["execution_status"], "executed")
                waited = harness.client.call("wait", session_id=session_id,
                                             expected={"text": "After"}, timeout_ms=1000)
                self.assertEqual(waited["status"], "matched")
                history = harness.client.call("history", session_id=session_id)
                self.assertTrue(history["items"])
                self.assertEqual(harness.devices[-1].writes, 1)

    def test_agent_tool_names_are_absent_from_the_service_registry(self):
        with tempfile.TemporaryDirectory() as root:
            with ServiceHarness(root, lambda runtime, state: None) as harness:
                harness.client.call("session", operation="open")
                for method in ("agent_run_task", "agent_task_status", "agent_decide"):
                    with self.assertRaises(RuntimeFault) as error:
                        harness.client.call(method)
                    self.assertEqual(error.exception.code, "invalid_arguments")

    def test_runtime_lifecycle_is_independent_of_the_agent_layer(self):
        with tempfile.TemporaryDirectory() as root:
            device = FakeDevice("fake")
            runtime = Runtime(pathlib.Path(root), factory=lambda serial: device,
                              discover=lambda: ["fake"])
            try:
                session_id = runtime.session("owner", "open")["session_id"]
                observed = runtime.observe("owner", session_id)
                result = runtime.act("owner", {
                    "session_id": session_id, "request_id": "standalone",
                    "observation_id": observed["observation_id"],
                    "action": {"kind": "tap", "target": {"action_id": "n0"}}})
                self.assertEqual(result["execution_status"], "executed")
                self.assertEqual(device.writes, 1)
            finally:
                runtime.close()


class AgentHostFailureIsolationTests(unittest.TestCase):
    """A broken agent layer degrades to v1 instead of stopping the service."""

    @staticmethod
    def failing_factory(runtime, state):
        raise RuntimeError("agent state database is unavailable")

    def test_build_host_reports_the_failure_instead_of_raising(self):
        previous = os.environ.pop("HARMONY_AGENT_REQUIRED", None)
        try:
            host, error = build_host(self.failing_factory, object(), "root")
        finally:
            if previous is not None:
                os.environ["HARMONY_AGENT_REQUIRED"] = previous
        self.assertIsNone(host)
        self.assertEqual(error, "RuntimeError")

    def test_build_host_treats_an_absent_host_as_normal(self):
        host, error = build_host(lambda runtime, state: None, object(), "root")
        self.assertIsNone(host)
        self.assertIsNone(error)

    def test_required_agent_layer_fails_fast_when_requested(self):
        previous = os.environ.get("HARMONY_AGENT_REQUIRED")
        os.environ["HARMONY_AGENT_REQUIRED"] = "1"
        try:
            with self.assertRaises(RuntimeError):
                build_host(self.failing_factory, object(), "root")
        finally:
            if previous is None:
                os.environ.pop("HARMONY_AGENT_REQUIRED", None)
            else:
                os.environ["HARMONY_AGENT_REQUIRED"] = previous

    def test_a_failed_agent_host_leaves_the_six_v1_tools_serving(self):
        with tempfile.TemporaryDirectory() as root:
            with ServiceHarness(root, self.failing_factory) as harness:
                session = harness.client.call("session", operation="open")
                session_id = session["session_id"]
                observed = harness.client.call("observe", session_id=session_id)
                result = harness.client.call("act", arguments={
                    "session_id": session_id, "request_id": "degraded",
                    "observation_id": observed["observation_id"],
                    "action": {"kind": "tap", "target": {"action_id": "n0"}}})
                self.assertEqual(result["execution_status"], "executed")
                self.assertEqual(harness.devices[-1].writes, 1)
                with self.assertRaises(RuntimeFault):
                    harness.client.call("agent_run_task", session_id=session_id,
                                        task={"schema_version": "2.0"})


class AgentHostLifecycleTests(unittest.TestCase):
    """Closing the agent host must not close the runtime it borrowed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.device = FakeDevice("fake")
        self.runtime = Runtime(self.root / "runtime",
                               factory=lambda serial: self.device,
                               discover=lambda: ["fake"])
        self.session_id = self.runtime.session("owner", "open")["session_id"]

    def tearDown(self):
        self.runtime.close()
        self.tmp.cleanup()

    def test_host_close_does_not_close_the_runtime(self):
        host = AgentHost(self.runtime, self.root / "agent", profile="local_off")
        host.close()
        observed = self.runtime.observe("owner", self.session_id)
        result = self.runtime.act("owner", {
            "session_id": self.session_id, "request_id": "after-host-close",
            "observation_id": observed["observation_id"],
            "action": {"kind": "tap", "target": {"action_id": "n0"}}})
        self.assertEqual(result["execution_status"], "executed")
        self.assertEqual(self.device.writes, 1)

    def test_host_construction_does_not_touch_the_device(self):
        def forbidden(serial):
            raise AssertionError("the agent host must not open a device")

        runtime = Runtime(self.root / "quiet", factory=forbidden,
                          discover=lambda: ["fake"])
        try:
            host = AgentHost(runtime, self.root / "agent-quiet", profile="local_off")
            try:
                self.assertIsNone(host.fast_provider)
                self.assertEqual(host.diagnostics("owner")["provider_name"], "none")
            finally:
                host.close()
        finally:
            runtime.close()

    def test_host_from_env_returns_none_without_the_opt_in(self):
        previous = os.environ.pop("HARMONY_AGENT_TOOLS", None)
        try:
            self.assertIsNone(host_from_env(self.runtime, self.root / "agent",
                                            self.root))
        finally:
            if previous is not None:
                os.environ["HARMONY_AGENT_TOOLS"] = previous

    def test_agent_tools_opt_in_registers_the_v2_methods(self):
        with tempfile.TemporaryDirectory() as root:
            def host_factory(runtime, state):
                return AgentHost(runtime, pathlib.Path(state) / "agent",
                                 profile="local_off")

            with ServiceHarness(root, host_factory) as harness:
                session = harness.client.call("session", operation="open")
                self.assertIn("session_id", session)
                # The v2 method is registered: an unknown task is a task-level
                # error, never the "unknown method" rejection of a v1-only host.
                with self.assertRaises(RuntimeFault) as error:
                    harness.client.call("agent_task_status", task_id="missing")
                self.assertNotEqual(error.exception.code, "invalid_arguments")


if __name__ == "__main__":
    unittest.main()
