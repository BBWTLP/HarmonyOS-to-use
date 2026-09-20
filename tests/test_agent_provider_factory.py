"""v3.2 Phase 1: the fast provider is optional, lazy and failure-isolated.

Every case here must end in a rules decision or an abstention. None of them may
crash the runtime, dispatch a device action twice, or require the Decider
package, service or model to exist.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from agent_fakes import (WEIBO, FakeTransportProvider, FakeWeiboDevice, calibration,
                         make_decider_provider, weibo_home)
from harmony_agent.candidates import CandidateError, CandidateRegistry
from harmony_agent.contracts import Predicate
from harmony_agent.decision.factory import (ProviderConfig, build_fast_provider,
                                            resolve_fast_provider)
from harmony_agent.decision.providers.base import ProviderUnavailable
from harmony_agent.decision.providers.decider import (CircuitBreaker, DeciderError,
                                                      DeciderProvider, DEFAULT_REVISION)
from harmony_agent.decision.router import Router
from harmony_agent.grounding import GroundingIntent
from harmony_agent.host import AgentHost
from harmony_runtime.observation import snapshot
from harmony_runtime.runtime import Runtime

DECIDER_MODULE = "harmony_agent.decision.providers.decider"
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def observation(tree, observation_id="obs_1", epoch=0):
    obs = snapshot(tree, (1080, 2340, 0), {"status": "ok", "bundle": WEIBO})
    obs["observation_id"] = observation_id
    obs["controller_epoch"] = epoch
    obs["actionable"] = True
    obs["mode"] = "FULL"
    return obs


def candidate_set(registry, obs, **overrides):
    kwargs = dict(task_id="task_1", subgoal_id="sub_1", scope_id="scope_1",
                  observation=obs, controller_epoch=0,
                  intent=GroundingIntent(action_kind="tap", resource_id="search_entry"),
                  action_kind="tap", arguments={},
                  expected_predicates=[Predicate(id="p", type="foreground_is", value=WEIBO)])
    kwargs.update(overrides)
    return registry.build(**kwargs)


def decider_answer(candidate_id):
    return {"action": {"type": "choice", "choice": candidate_id, "confidence": 0.99,
                       "certainty": 0.99,
                       "probabilities": {candidate_id: 0.99,
                                         "cand_none_applicable": 0.01}}}


class NoDeciderModule:
    """Temporarily make ``import harmony_agent.decision.providers.decider`` fail."""

    def __enter__(self):
        self.previous = sys.modules.get(DECIDER_MODULE, self)
        sys.modules[DECIDER_MODULE] = None
        return self

    def __exit__(self, *exc):
        if self.previous is self:
            sys.modules.pop(DECIDER_MODULE, None)
        else:
            sys.modules[DECIDER_MODULE] = self.previous
        return False


class ProviderResolutionTests(unittest.TestCase):
    def test_rules_only_profiles_never_construct_a_provider(self):
        for profile in ("rules_only", "local_off"):
            config = ProviderConfig(profile=profile)
            build = resolve_fast_provider(profile, config)
            self.assertIsNone(build.provider, profile)
            self.assertFalse(build.available(), profile)
            self.assertEqual(build.status, "rules_only", profile)
            self.assertEqual(build.reason, f"profile_{profile}")
            self.assertFalse(config.wants_fast_provider())

    def test_unknown_profile_is_not_silently_upgraded(self):
        build = resolve_fast_provider("active", ProviderConfig(profile="active"))
        self.assertIsNone(build.provider)
        self.assertEqual(build.status, "unknown_profile")

    def test_disabled_fast_provider_returns_none(self):
        config = ProviderConfig(profile="local_shadow", requested=False)
        build = resolve_fast_provider("local_shadow", config)
        self.assertIsNone(build.provider)
        self.assertEqual(build.status, "not_requested")
        self.assertIsNone(build_fast_provider("local_shadow", config))
        self.assertFalse(config.wants_fast_provider())

    def test_missing_module_degrades_instead_of_raising(self):
        with NoDeciderModule():
            build = resolve_fast_provider("local_shadow",
                                          ProviderConfig(profile="local_shadow"))
        self.assertIsNone(build.provider)
        self.assertEqual(build.status, "module_missing")
        self.assertFalse(build.as_dict()["provider_available"])

    def test_shadow_profile_builds_a_provider_lazily_from_config(self):
        config = ProviderConfig(profile="local_shadow", repo_root=REPO_ROOT,
                                base_url="http://127.0.0.1:1", revision="pinned",
                                timeout_seconds=0.5)
        build = resolve_fast_provider("local_shadow", config)
        self.assertTrue(build.available())
        self.assertEqual(build.status, "ready")
        self.assertEqual(build.provider.base_url, "http://127.0.0.1:1")
        self.assertEqual(build.provider.revision, "pinned")
        self.assertEqual(build.provider.timeout_seconds, 0.5)
        # Construction performs no I/O: the token is read on the first evaluate.
        self.assertIsNone(build.provider._token)

    def test_provider_config_carries_the_repo_scoped_token_path(self):
        build = resolve_fast_provider("local_shadow",
                                      ProviderConfig(profile="local_shadow",
                                                     repo_root=REPO_ROOT))
        self.assertEqual(build.provider.token_file,
                         REPO_ROOT / "services" / "decider" / ".runtime" / "api-token")


class ProviderImportIsolationTests(unittest.TestCase):
    """The strongest isolation claim: the adapter is never imported at all."""

    def run_python(self, source: str, **env):
        environment = dict(os.environ)
        path = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (str(REPO_ROOT / "src")
                                     + (os.pathsep + path if path else ""))
        environment.update({key: str(value) for key, value in env.items()})
        return subprocess.run([sys.executable, "-c", source], capture_output=True,
                              text=True, cwd=str(REPO_ROOT), env=environment, timeout=180)

    def test_local_off_host_never_imports_the_decider_adapter(self):
        source = (
            "import sys, tempfile\n"
            "from harmony_agent.host import AgentHost\n"
            "class Runtime:\n"
            "    def session(self, *a, **k):\n"
            "        return {'controller_epoch': 0}\n"
            "host = AgentHost(Runtime(), tempfile.mkdtemp(), profile='local_off')\n"
            "assert host.fast_provider is None, host.fast_provider\n"
            "assert host.provider_status == 'rules_only', host.provider_status\n"
            f"assert {DECIDER_MODULE!r} not in sys.modules, 'adapter was imported'\n"
            "print('clean')\n"
        )
        completed = self.run_python(source)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("clean", completed.stdout)

    def test_agent_tools_off_does_not_build_a_host(self):
        source = (
            "from harmony_agent.host import host_from_env\n"
            "assert host_from_env(object(), 'unused', 'unused') is None\n"
            "print('clean')\n"
        )
        completed = self.run_python(source)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("clean", completed.stdout)

    def test_local_off_ignores_a_broken_provider_configuration(self):
        """A nonsense service URL must not matter when the profile is rules-only."""
        source = (
            "import tempfile\n"
            "from harmony_agent.host import AgentHost\n"
            "class Runtime:\n"
            "    def session(self, *a, **k):\n"
            "        return {'controller_epoch': 0}\n"
            "host = AgentHost(Runtime(), tempfile.mkdtemp(), profile='local_off')\n"
            "assert host.fast_provider is None\n"
            "print('clean')\n"
        )
        completed = self.run_python(source, HARMONY_DECIDER_URL="http://256.256.256.256:1",
                                    HARMONY_AGENT_FAST_PROVIDER="1")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("clean", completed.stdout)

    def test_agent_tools_default_profile_is_local_off(self):
        source = (
            "import os, tempfile\n"
            "os.environ['HARMONY_AGENT_TOOLS'] = '1'\n"
            "from harmony_agent.host import host_from_env\n"
            "class Runtime:\n"
            "    def session(self, *a, **k):\n"
            "        return {'controller_epoch': 0}\n"
            "host = host_from_env(Runtime(), tempfile.mkdtemp(), tempfile.mkdtemp())\n"
            "assert host is not None\n"
            "assert host.profile == 'local_off', host.profile\n"
            "assert host.fast_provider is None\n"
            "print('clean')\n"
        )
        completed = self.run_python(source)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("clean", completed.stdout)


class CandidateAuthorityTests(unittest.TestCase):
    """A provider answer can only ever name a candidate the server issued."""

    def setUp(self):
        self.obs = observation(weibo_home())
        self.registry = CandidateRegistry()
        self.candidates = candidate_set(self.registry, self.obs)
        self.selected = self.candidates.candidates[0]

    def test_an_unknown_candidate_id_cannot_be_resolved(self):
        with self.assertRaises(CandidateError) as error:
            self.registry.resolve("cand_deadbeef", observation_id="obs_1",
                                  controller_epoch=0)
        self.assertEqual(error.exception.code, "unknown_candidate")

    def test_an_expired_candidate_cannot_be_resolved(self):
        expired_registry = CandidateRegistry(ttl_seconds=-5)
        expired = candidate_set(expired_registry, self.obs)
        issued = expired.candidates[0].candidate.candidate_id
        with self.assertRaises(CandidateError) as error:
            expired_registry.resolve(issued, observation_id="obs_1", controller_epoch=0)
        self.assertEqual(error.exception.code, "candidate_expired")

    def test_a_candidate_from_an_old_epoch_cannot_be_resolved(self):
        issued = self.selected.candidate.candidate_id
        with self.assertRaises(CandidateError) as error:
            self.registry.resolve(issued, observation_id="obs_1", controller_epoch=4)
        self.assertEqual(error.exception.code, "epoch_mismatch")

    def test_a_candidate_from_another_observation_cannot_be_resolved(self):
        issued = self.selected.candidate.candidate_id
        with self.assertRaises(CandidateError) as error:
            self.registry.resolve(issued, observation_id="obs_other", controller_epoch=0)
        self.assertEqual(error.exception.code, "stale_observation")


class ProviderFailureIsolationTests(unittest.TestCase):
    """Provider faults fall back to rules; they never author a device action."""

    def setUp(self):
        self.obs = observation(weibo_home())
        self.candidates = candidate_set(CandidateRegistry(), self.obs)
        # An intent that grounds nothing: rules abstain, so a provider is the
        # only thing that could possibly author a dispatch.
        self.empty_candidates = candidate_set(
            CandidateRegistry(), self.obs,
            intent=GroundingIntent(action_kind="tap", text="不存在的按钮"))
        self.selected = self.candidates.candidates[0].candidate.candidate_id

    def decide(self, provider, *, profile="local_canary",
               calibrated=True, confidence=0.0, certainty=0.0):
        router = Router(fast_provider=provider, profile=profile,
                        calibration=calibration() if calibrated else None,
                        calibration_version="cal-2026-09-20" if calibrated else None,
                        confidence_threshold=confidence,
                        certainty_threshold=certainty)
        return asyncio.run(router.decide(
            task_id="t", subgoal_id="s", scope_id="sc", observation=self.obs,
            candidate_set=self.candidates, controller_epoch=0, goal="搜索鸿蒙"))

    def evaluate(self, provider):
        from harmony_agent.state_builder import build_questions, build_state
        state = build_state(self.obs, self.candidates, goal="搜索鸿蒙")
        return asyncio.run(provider.evaluate(
            {"state": state, "controller_epoch": 0}, build_questions(self.candidates)))

    def test_crashing_provider_falls_back_without_dispatching(self):
        class Crashing:
            provider = "crashing"
            model_revision = "crash-1"

            async def evaluate(self, context, questions, *, remaining_model_calls=None):
                raise RuntimeError("the model runner died")

        outcome = self.decide(Crashing())
        self.assertEqual(outcome.decision.provider, "rules")
        self.assertEqual(outcome.fallback_reason, "provider_error:RuntimeError")
        self.assertFalse(outcome.provider_available)
        self.assertEqual(outcome.decision.route, "execute")
        # The provider authored nothing: the executed candidate is the one the
        # deterministic rules chose for the single unambiguous target.
        self.assertEqual(outcome.decision.selected_candidate_id, self.selected)

    def test_a_crash_cannot_turn_an_abstention_into_a_dispatch(self):
        class Crashing:
            provider = "crashing"
            model_revision = "crash-1"

            async def evaluate(self, context, questions, *, remaining_model_calls=None):
                raise RuntimeError("the model runner died")

        router = Router(fast_provider=Crashing(), profile="local_canary",
                        calibration_version="cal-1")
        outcome = asyncio.run(router.decide(
            task_id="t", subgoal_id="s", scope_id="sc", observation=self.obs,
            candidate_set=self.empty_candidates, controller_epoch=0, goal="g"))
        self.assertEqual(outcome.decision.provider, "rules")
        self.assertEqual(outcome.decision.route, "reobserve")
        self.assertIsNone(outcome.decision.selected_candidate_id)

    def test_malformed_provider_result_is_not_a_decision(self):
        class Malformed:
            provider = "decider"
            model_revision = "rev"

            async def evaluate(self, context, questions, *, remaining_model_calls=None):
                return "not-a-provider-result"

        outcome = self.decide(Malformed())
        self.assertEqual(outcome.decision.provider, "rules")
        self.assertEqual(outcome.fallback_reason, "malformed_result")

    def test_declared_unavailability_still_returns_the_rules_decision(self):
        class Unavailable:
            provider = "decider"
            model_revision = "rev"

            async def evaluate(self, context, questions, *, remaining_model_calls=None):
                raise ProviderUnavailable("model_busy", "busy")

        outcome = self.decide(Unavailable())
        self.assertEqual(outcome.decision.provider, "rules")
        self.assertEqual(outcome.decision.route, "execute")
        self.assertTrue(outcome.decision.selected_candidate_id)
        self.assertEqual(outcome.fallback_reason, "model_busy")

    def test_connection_refused_is_a_fallback_not_a_dispatch(self):
        def refused(payload, token, timeout):
            raise ConnectionError("connection refused")

        outcome = self.decide(make_decider_provider(self, refused))
        self.assertEqual(outcome.decision.provider, "rules")
        self.assertEqual(outcome.fallback_reason, "transport_error")
        self.assertEqual(outcome.decision.selected_candidate_id, self.selected)
        self.assertIsNone(outcome.shadow)

    def test_busy_and_deadline_are_reported_separately(self):
        for status, code in ((503, "model_busy"), (504, "model_timeout")):
            provider = make_decider_provider(
                self,
                transport=lambda payload, token, timeout, status=status: (status, {}, {}))
            outcome = self.decide(provider)
            self.assertEqual(outcome.fallback_reason, code, status)
            self.assertEqual(outcome.decision.provider, "rules", status)

    def test_a_body_without_a_deployment_is_rejected(self):
        provider = make_decider_provider(
            self, lambda payload, token, timeout: (200, {}, {}))
        with self.assertRaises(DeciderError) as error:
            self.evaluate(provider)
        self.assertEqual(error.exception.code, "revision_mismatch")

    def test_a_body_without_answers_is_malformed(self):
        def transport(payload, token, timeout):
            return 200, {"deployment": {"model_revision": DEFAULT_REVISION}}, {}

        with self.assertRaises(DeciderError) as error:
            self.evaluate(make_decider_provider(self, transport))
        self.assertEqual(error.exception.code, "malformed_response")

    def test_invalid_schema_is_rejected(self):
        def transport(payload, token, timeout):
            return 200, {"answers": {"action": {"type": "choice"}},
                         "deployment": {"model_revision": DEFAULT_REVISION}}, {}

        with self.assertRaises(DeciderError) as error:
            self.evaluate(make_decider_provider(self, transport))
        self.assertIn(error.exception.code,
                      ("malformed_response", "candidate_not_registered"))

    def test_infinite_score_is_rejected(self):
        def transport(payload, token, timeout):
            criteria = list(payload["questions"]["action"]["criteria"])
            probabilities = {name: 0.0 for name in criteria}
            probabilities[criteria[0]] = float("inf")
            return 200, {"answers": {"action": {"type": "choice", "choice": criteria[0],
                                                "confidence": 0.5, "certainty": 0.5,
                                                "probabilities": probabilities}},
                         "deployment": {"model_revision": DEFAULT_REVISION}}, {}

        with self.assertRaises(DeciderError) as error:
            self.evaluate(make_decider_provider(self, transport))
        self.assertEqual(error.exception.code, "non_finite_score")

    def test_expired_candidate_never_becomes_a_decider_execution(self):
        expired_registry = CandidateRegistry(ttl_seconds=-5)
        expired = observation(weibo_home())
        expired["expires_at"] = "2999-01-01T00:00:00+0000"
        candidates = candidate_set(expired_registry, expired)
        chosen = candidates.candidates[0].candidate.candidate_id
        provider = FakeTransportProvider(decider_answer(chosen))
        outcome = asyncio.run(Router(fast_provider=provider, profile="local_canary",
                                     calibration=calibration(),
                                     calibration_version="cal-1").decide(
            task_id="t", subgoal_id="s", scope_id="sc", observation=expired,
            candidate_set=candidates, controller_epoch=0, goal="g"))
        self.assertEqual(outcome.decision.provider, "rules")
        self.assertIsNotNone(outcome.shadow)
        self.assertEqual(outcome.shadow.reason_code, "candidate_expired")
        self.assertIsNone(outcome.shadow.selected_candidate_id)
        self.assertEqual(outcome.shadow.route, "escalate")

    def test_decision_records_the_epoch_it_was_computed_under(self):
        provider = FakeTransportProvider(decider_answer(self.selected))
        router = Router(fast_provider=provider, profile="local_canary",
                        calibration=calibration(), calibration_version="cal-1")
        outcome = asyncio.run(router.decide(
            task_id="t", subgoal_id="s", scope_id="sc", observation=self.obs,
            candidate_set=self.candidates, controller_epoch=7, goal="g"))
        # A late answer cannot claim a newer epoch than the one it saw; the
        # supervisor re-reads the epoch and rejects the mismatch before dispatch.
        self.assertEqual(outcome.decision.controller_epoch, 7)

    def test_shadow_records_a_late_answer_without_executing_it(self):
        provider = FakeTransportProvider(decider_answer(self.selected))
        outcome = self.decide(provider, profile="local_shadow", calibrated=False)
        self.assertEqual(outcome.decision.provider, "rules")
        self.assertEqual(outcome.shadow.route, "escalate")
        self.assertEqual(outcome.shadow.reason_code, "shadow_only")
        self.assertIsNone(outcome.shadow.selected_candidate_id)


class AgentHostProviderDiagnosticsTests(unittest.TestCase):
    """The host reports provider state generically, without adapter internals."""

    class StubRuntime:
        def session(self, *args, **kwargs):
            return {"controller_epoch": 0}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.hosts = []

    def tearDown(self):
        # Close the SQLite handles before the temporary directory is removed.
        for host in self.hosts:
            host.close()
        self.tmp.cleanup()

    def host(self, **kwargs):
        host = AgentHost(self.StubRuntime(), self.root / "agent",
                         repo_root=kwargs.pop("repo_root", self.root), **kwargs)
        self.hosts.append(host)
        return host

    def test_rules_only_profile_reports_an_absent_provider(self):
        report = self.host(profile="local_off").diagnostics("owner")
        self.assertEqual(report["profile"], "local_off")
        self.assertEqual(report["provider_name"], "none")
        self.assertFalse(report["provider_available"])
        self.assertEqual(report["provider_status"], "rules_only")
        self.assertIsNone(report["provider_revision"])
        self.assertFalse(report["circuit_open"])
        self.assertIsNone(report["decider_revision"])

    def test_injected_provider_is_reported_by_its_public_name(self):
        provider = FakeTransportProvider({})
        report = self.host(profile="local_shadow",
                           fast_provider=provider).diagnostics("o")
        self.assertEqual(report["provider_name"], "decider")
        self.assertTrue(report["provider_available"])
        self.assertEqual(report["provider_status"], "injected")
        self.assertEqual(report["provider_revision"], "fake-revision")
        self.assertEqual(report["provider_health"], "ok")

    def test_deprecated_decider_keyword_still_injects_a_provider(self):
        provider = FakeTransportProvider({})
        host = self.host(profile="local_shadow", decider=provider)
        self.assertIs(host.fast_provider, provider)
        self.assertIs(host.decider, provider)

    def test_shadow_profile_reports_a_ready_provider_without_contacting_it(self):
        report = self.host(profile="local_shadow").diagnostics("owner")
        self.assertEqual(report["provider_status"], "ready")
        self.assertTrue(report["provider_available"])
        self.assertEqual(report["provider_name"], "decider")
        self.assertEqual(report["provider_health"], "ok")
        self.assertFalse(report["circuit_open"])

    def test_circuit_state_is_reported_without_calling_the_model(self):
        provider = DeciderProvider(breaker=CircuitBreaker(threshold=1, cooldown_seconds=60))
        provider.breaker.record_failure("model_busy")
        report = self.host(profile="local_shadow",
                           fast_provider=provider).diagnostics("o")
        self.assertTrue(report["circuit_open"])
        self.assertEqual(report["provider_health"], "circuit_open")
        self.assertEqual(report["provider_revision"], DEFAULT_REVISION)

    def test_broken_health_snapshot_does_not_break_diagnostics(self):
        class Broken:
            provider = "decider"
            model_revision = "rev"

            def health_snapshot(self):
                raise RuntimeError("cannot read the device")

        report = self.host(profile="local_shadow",
                           fast_provider=Broken()).diagnostics("o")
        self.assertEqual(report["provider_health"], "unavailable")
        self.assertEqual(report["provider_name"], "decider")


class ProviderFailureTaskIsolationTests(unittest.TestCase):
    """A failing provider must not add, drop or duplicate a device action."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.devices = []
        outer = self

        class Device(FakeWeiboDevice):
            def __init__(self, serial):
                super().__init__(serial)
                outer.devices.append(self)

        self.runtime = Runtime(self.root / "runtime", factory=Device,
                               discover=lambda: ["fake-device"])
        self.owner = "tester"
        opened = self.runtime.session(self.owner, "open", device_id="fake-device")
        self.session_id = opened["session_id"]
        self.host = AgentHost(self.runtime, self.root / "agent",
                              profile="local_shadow", repo_root=self.root)

    def tearDown(self):
        self.host.close()
        self.runtime.close()
        self.tmp.cleanup()

    def payload(self, request_id):
        return {
            "schema_version": "2.0",
            "request_id": request_id,
            "mode": "delegated",
            "goal": "打开微博搜索页",
            "scope": {"device_ref": "current-authorized-device",
                      "allowed_apps": [WEIBO],
                      "allowed_actions": ["tap", "replace_text", "back"],
                      "cloud_data_policy": "disabled"},
            "success_criteria": [{"id": "editor", "type": "element_present",
                                  "target_key": "搜索",
                                  "description": "打开微博搜索页"}],
            "arguments": {"steps": "tap:搜索"},
            "budget": {"max_dispatches": 8, "max_seconds": 60, "max_model_calls": 8},
            "model_profile": "local_shadow",
        }

    def run_task(self, request_id, timeout=30.0):
        created = self.host.run_task(self.owner, session_id=self.session_id,
                                     task=self.payload(request_id))
        task_id = created["task_id"]
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.host.task_status(self.owner, task_id=task_id)
            if status["terminal"] or status["status"] in ("RECONCILIATION_REQUIRED",
                                                          "PAUSED", "WAITING_USER"):
                return task_id, status
            time.sleep(0.02)
        self.fail("task did not finish")

    def test_crashing_provider_does_not_add_or_duplicate_actions(self):
        class Crashing:
            provider = "decider"
            model_revision = "crash"

            async def evaluate(self, context, questions, *, remaining_model_calls=None):
                raise RuntimeError("provider crashed")

        self.host.fast_provider = Crashing()
        task_id, status = self.run_task("isolation-crash")
        self.assertEqual(status["status"], "SUCCEEDED", status)
        self.assertEqual(self.devices[-1].stage, "editor")
        self.assertEqual(len(self.devices[-1].writes), 1)
        self.assertEqual(self.devices[-1].writes[0]["kind"], "tap")
        events = self.host.task_events(self.owner, task_id=task_id)["items"]
        decisions = [item for item in events if item["type"] == "decision"]
        self.assertTrue(decisions)
        self.assertEqual(decisions[0]["payload"]["provider"], "rules")

    def test_absent_provider_still_completes_the_task_on_rules(self):
        self.host.fast_provider = None
        self.host.provider_status = "rules_only"
        task_id, status = self.run_task("isolation-absent")
        self.assertEqual(status["status"], "SUCCEEDED", status)
        self.assertEqual(self.devices[-1].stage, "editor")
        self.assertEqual(len(self.devices[-1].writes), 1)
        result = self.host.task_result(self.owner, task_id=task_id)["result"]
        self.assertEqual(result["conditions"][0]["verdict"], "pass")


if __name__ == "__main__":
    unittest.main()
