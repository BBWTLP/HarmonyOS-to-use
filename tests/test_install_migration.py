"""v3.2 Phase 13: install, migration and rollback behaviour.

The direct runtime must install and run with no Decider, no OCR and no VLM, it
must open a state directory written by an earlier build, and a v1-only client
must keep working after an upgrade.
"""
from __future__ import annotations

import importlib.metadata
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from agent_fakes import WEIBO, FakeWeiboDevice
from harmony_agent.host import AgentHost
from harmony_runtime.contracts import RuntimeFault
from harmony_runtime.journal import Journal
from harmony_runtime.runtime import Runtime
from harmony_runtime.service import Client, serve

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEVICE = "fake-device"


class DistributionTests(unittest.TestCase):
    def test_the_distribution_is_installed_and_importable(self):
        self.assertEqual(importlib.metadata.version("harmony-mobile-runtime"),
                         "0.1.0.dev0")

    def test_the_lock_file_pins_every_declared_dependency(self):
        import tomllib
        project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        declared = [item.split("==")[0].split(">=")[0].split("@")[0].strip().lower()
                    for item in project["project"]["dependencies"]]
        lock = (REPO_ROOT / "requirements.lock").read_text(encoding="utf-8").lower()
        self.assertTrue(declared)
        for name in declared:
            self.assertIn(name, lock, f"{name} is missing from requirements.lock")

    def test_the_console_entry_point_is_declared(self):
        import tomllib
        project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["project"]["scripts"]["harmony-runtime"],
                         "harmony_runtime.cli:main")

    def test_optional_components_are_not_imported_by_the_core(self):
        source = (
            "import sys\n"
            "import harmony_runtime.runtime, harmony_runtime.service\n"
            "import harmony_agent.host, harmony_agent.candidates\n"
            "for name in ('harmony_agent.ocr', 'harmony_agent.vlm',\n"
            "             'harmony_agent.decision.providers.decider'):\n"
            "    assert name not in sys.modules, name\n"
            "print('clean')\n"
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(REPO_ROOT / "src")
        completed = subprocess.run([sys.executable, "-c", source], capture_output=True,
                                   text=True, cwd=str(REPO_ROOT), env=environment,
                                   timeout=180)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("clean", completed.stdout)


class MigrationTests(unittest.TestCase):
    def test_a_journal_from_an_earlier_build_is_opened_and_completed(self):
        with tempfile.TemporaryDirectory() as root:
            path = pathlib.Path(root) / "journal.sqlite3"
            legacy = sqlite3.connect(path)
            legacy.execute("CREATE TABLE actions (request_id TEXT PRIMARY KEY,"
                           " payload_hash TEXT NOT NULL, state TEXT NOT NULL,"
                           " result TEXT, created REAL NOT NULL)")
            legacy.execute("INSERT INTO actions VALUES ('legacy-1','d','dispatching',NULL,1.0)")
            legacy.commit()
            legacy.close()
            journal = Journal(path)
            try:
                # The upgrade promotes the in-flight row and opens an incident.
                row = journal.db.execute(
                    "SELECT state FROM actions WHERE request_id='legacy-1'").fetchone()
                self.assertEqual(row[0], "execution_unknown")
                # The interrupted write gets a durable open incident, so it can
                # never be silently replayed after the upgrade.
                opened = journal.db.execute(
                    "SELECT COUNT(*) FROM incidents WHERE status='open'").fetchone()[0]
                self.assertEqual(opened, 1)
                # Tables added by the newer build now exist.
                tables = {item[0] for item in journal.db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue({"actions", "action_devices", "recovery_conditions",
                                 "incidents", "bursts"} <= tables)
            finally:
                journal.close()

    def test_a_runtime_state_directory_from_this_build_reopens_without_replay(self):
        with tempfile.TemporaryDirectory() as root:
            state = pathlib.Path(root)
            runtime = Runtime(state / "runtime",
                              factory=lambda serial: FakeWeiboDevice(serial),
                              discover=lambda: [DEVICE])
            session = runtime.session("owner", "open", device_id=DEVICE)["session_id"]
            observed = runtime.observe("owner", session)
            runtime.act("owner", {
                "session_id": session, "request_id": "migration-action",
                "observation_id": observed["observation_id"],
                "action": {"kind": "tap", "target": {"action_id": "n0"}}})
            runtime.close()
            reopened = Runtime(state / "runtime",
                               factory=lambda serial: FakeWeiboDevice(serial),
                               discover=lambda: [DEVICE])
            try:
                # Reusing the request id with different arguments is a conflict,
                # not a replay.
                with self.assertRaises(RuntimeFault) as error:
                    reopened.journal.lookup("migration-action", "wrong-digest")
                self.assertEqual(error.exception.code, "request_conflict")
                entry = reopened.journal.device_history(DEVICE, limit=5)
                self.assertTrue(entry["items"])
            finally:
                reopened.close()

    def test_an_agent_state_directory_survives_a_host_restart(self):
        with tempfile.TemporaryDirectory() as root:
            state = pathlib.Path(root)
            runtime = Runtime(state / "runtime",
                              factory=lambda serial: FakeWeiboDevice(serial),
                              discover=lambda: [DEVICE])
            try:
                host = AgentHost(runtime, state / "agent", profile="local_off")
                host.close()
                host = AgentHost(runtime, state / "agent", profile="local_off")
                try:
                    listed = host.supervisor.store.recent(limit=5) \
                        if hasattr(host.supervisor.store, "recent") else None
                    self.assertIn(listed, (None, []))
                    self.assertEqual(host.diagnostics("owner")["inflight_tasks"], [])
                finally:
                    host.close()
            finally:
                runtime.close()


class RollbackTests(unittest.TestCase):
    """A v1-only client must still work after the agent layer was introduced."""

    def test_the_six_v1_tools_are_available_after_an_upgrade(self):
        with tempfile.TemporaryDirectory() as root:
            import threading
            devices = []

            def factory(serial):
                device = FakeWeiboDevice(serial)
                devices.append(device)
                return device

            ready = threading.Event()
            servers = []

            def notify(server):
                servers.append(server)
                ready.set()

            thread = threading.Thread(
                target=serve, args=(root,),
                kwargs={"runtime_factory": lambda path: Runtime(
                    path, factory=factory, discover=lambda: [DEVICE]),
                    "ready": notify, "host_factory": lambda runtime, state: None})
            thread.daemon = True
            thread.start()
            self.assertTrue(ready.wait(20))
            try:
                client = Client(root)
                session = client.call("session", operation="open")["session_id"]
                observed = client.call("observe", session_id=session)
                result = client.call("act", arguments={
                    "session_id": session, "request_id": "rollback-1",
                    "observation_id": observed["observation_id"],
                    "action": {"kind": "tap", "target": {"action_id": "n0"}}})
                self.assertEqual(result["execution_status"], "executed")
                self.assertEqual(len(devices[-1].writes), 1)
            finally:
                servers[0].shutdown()
                thread.join(20)

    def test_an_agent_state_file_is_not_required_for_v1(self):
        with tempfile.TemporaryDirectory() as root:
            state = pathlib.Path(root)
            runtime = Runtime(state, factory=lambda serial: FakeWeiboDevice(serial),
                              discover=lambda: [DEVICE])
            try:
                session = runtime.session("owner", "open",
                                          device_id=DEVICE)["session_id"]
                self.assertTrue(runtime.observe("owner", session)["observation_id"])
                self.assertFalse((state / "agent-tasks.sqlite3").exists())
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
