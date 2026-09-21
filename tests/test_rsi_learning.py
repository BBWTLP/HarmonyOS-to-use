"""Offline learning loop: experience gate, memory store, waves (P3-01 … P3-06).

Everything here runs against the mock device through the real Runtime, journal and
checker. No phone, model or network is involved, and no result here is a
device-verified claim.
"""
import json
import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from harmony_agent.experience import (ExperienceError, ExperienceGate,
                                      ExperienceGrounding, ExperienceScope,
                                      ExperienceTrigger, ProcedureStep,
                                      build_experience)
from harmony_agent.fake_device import MOCK_APP, FaultProfile, open_practice_runtime
from harmony_agent.memory_store import (FrozenMemory, MemoryReadOnlyError, MemoryStore,
                                        MemoryStoreError, manifest_hash_for,
                                        merge_experiences)
from harmony_agent.rsi import (PracticeBranch, PracticeSelector, WaveRunner,
                               grounding_layers_for, procedure_from_steps)

APPROVAL = "approval_" + "a" * 24


def practice_task(*, request_id, steps, criteria, goal="练习任务", query=None,
                  budget=None):
    arguments = {"steps": steps}
    if query is not None:
        arguments["query"] = query
    return {
        "schema_version": "2.0",
        "request_id": request_id,
        "mode": "auto",
        "goal": goal,
        "scope": {"device_ref": "mock-device", "allowed_apps": [MOCK_APP],
                  "allowed_actions": ["tap", "replace_text", "back"],
                  "cloud_data_policy": "disabled"},
        "success_criteria": criteria,
        "arguments": arguments,
        "budget": budget or {"max_dispatches": 6, "max_seconds": 60,
                             "max_model_calls": 4},
        "model_profile": "local_shadow",
    }


SEARCH_STEPS = "tap:搜索|replace_text:id:search_input=query|tap:id:search_confirm_btn"
SEARCH_CRITERIA = [
    {"id": "query", "type": "input_equals", "target_key": "id:search_input",
     "value_ref": "query"},
    {"id": "results", "type": "text_equals", "value": "鸿蒙 的相关结果"},
]


class ExperienceGateTests(unittest.TestCase):
    def experience(self, **overrides):
        payload = dict(
            scope=ExperienceScope(app=MOCK_APP, build="mock-1"),
            trigger=ExperienceTrigger(foreground=MOCK_APP, facts=["goal=搜索"],
                                      goal_pattern="搜索练习"),
            grounding=ExperienceGrounding(layers=["text_exact"], identity="tap:搜索"),
            procedure=procedure_from_steps("tap:搜索"),
            outcome="verified", risk_class="R1",
            evidence_refs=["artifact:art_1"], source_task_id="task_1")
        payload.update(overrides)
        return build_experience(**payload)

    def test_hash_and_semantic_validation(self):
        experience = self.experience()
        self.assertEqual(len(experience.content_hash), 64)
        tampered = experience.model_dump()
        tampered["outcome"] = "failed"
        with self.assertRaises(Exception):
            type(experience).model_validate(tampered)

    def test_private_payloads_are_rejected(self):
        for step in ({"action_kind": "tap", "text": "120,340"},
                     {"action_kind": "tap", "text": "搜索", "raw_tree": "{...}"},
                     {"action_kind": "tap", "text": "搜索",
                      "argument_refs": {"text": "明文输入"}}):
            with self.assertRaises(Exception):
                ProcedureStep(intent=step, expected={})
        with self.assertRaises(Exception):
            ProcedureStep(intent={"action_kind": "tap", "text": "搜索"},
                          expected={"predicate": "text_equals", "value": "鸿蒙"})

    def test_gate_refuses_inconclusive_unknown_and_evidence_free(self):
        gate = ExperienceGate()
        experience = self.experience()
        with self.assertRaises(ExperienceError) as caught:
            gate.stage(experience, verdict="inconclusive")
        self.assertEqual(caught.exception.code, "verifier_inconclusive")
        with self.assertRaises(ExperienceError) as caught:
            gate.stage(experience, verdict="pass", execution_status="unknown")
        self.assertEqual(caught.exception.code, "execution_unknown")
        with self.assertRaises(ExperienceError) as caught:
            gate.stage(self.experience(evidence_refs=[]), verdict="pass")
        self.assertEqual(caught.exception.code, "evidence_missing")
        with self.assertRaises(ExperienceError) as caught:
            gate.stage(self.experience(risk_class="R3"), verdict="pass")
        self.assertEqual(caught.exception.code, "risk_class_not_learnable")
        with self.assertRaises(ExperienceError) as caught:
            gate.stage(self.experience(outcome="failed"), verdict="pass")
        self.assertEqual(caught.exception.code, "outcome_mismatch")

    def test_commit_requires_an_approval_reference(self):
        gate = ExperienceGate()
        staged = gate.stage(self.experience(), verdict="pass")
        with self.assertRaises(ExperienceError) as caught:
            gate.commit(staged, approval_ref="model-approved=true")
        self.assertEqual(caught.exception.code, "approval_required")
        committed = gate.commit(staged, approval_ref=APPROVAL)
        self.assertEqual(committed.experience_id, staged.experience.experience_id)


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.store = MemoryStore(self.root / "memory")

    def tearDown(self):
        self.tmp.cleanup()

    def experience(self, source="task_1", goal="搜索练习", **overrides):
        payload = dict(
            scope=ExperienceScope(app=MOCK_APP, build="mock-1"),
            trigger=ExperienceTrigger(foreground=MOCK_APP, facts=[], goal_pattern=goal),
            grounding=ExperienceGrounding(layers=["text_exact"], identity="tap:搜索"),
            procedure=procedure_from_steps("tap:搜索"),
            outcome="verified", risk_class="R1", evidence_refs=["artifact:art"],
            source_task_id=source)
        payload.update(overrides)
        return build_experience(**payload)

    def test_snapshots_chain_parent_hashes_and_detect_tampering(self):
        self.assertIsNone(self.store.latest())
        first = self.store.commit([self.experience()])
        self.assertIsNone(first.parent_manifest_hash)
        second = self.store.commit([self.experience(source="task_2", goal="第二个练习")],
                                   parent=first)
        self.assertEqual(second.version, 2)
        self.assertEqual(second.parent_manifest_hash, first.manifest_hash)
        self.assertTrue(second.verify())
        # A fork or a stale parent is refused.
        with self.assertRaises(MemoryStoreError) as caught:
            self.store.commit([self.experience(source="task_3")], parent=first)
        self.assertEqual(caught.exception.code, "stale_parent")
        with self.assertRaises(MemoryStoreError) as caught:
            self.store.commit([self.experience(source="task_4")])
        self.assertEqual(caught.exception.code, "parent_required")
        # Rewriting a snapshot file is detectable.
        path = self.store.snapshots_dir / "v000001.json"
        body = json.loads(path.read_text(encoding="utf-8"))
        body["entries"][0]["outcome"] = "failed"
        path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(MemoryStoreError):
            self.store.load(1)

    def test_inconclusive_entries_never_enter_memory(self):
        own = self.experience()
        tampered = own.model_dump()
        tampered["outcome"] = "inconclusive"
        tampered["content_hash"] = own.content_hash
        broken = type(own).model_construct(**tampered)
        with self.assertRaises(MemoryStoreError) as caught:
            self.store.commit([broken])
        self.assertEqual(caught.exception.code, "inconclusive_not_committable")

    def test_frozen_memory_is_read_only_and_verifiable(self):
        manifest = self.store.commit([self.experience()])
        frozen = self.store.freeze(manifest.version)
        self.assertIsInstance(frozen, FrozenMemory)
        self.assertTrue(frozen.read_only)
        self.assertTrue(frozen.verify())
        self.assertIn("exp:搜索练习", frozen.facts()[0])
        for call in (lambda: frozen.stage("x"), lambda: frozen.commit("x")):
            with self.assertRaises(MemoryReadOnlyError):
                call()
        reloaded = self.store.load_frozen(manifest.version)
        self.assertEqual(reloaded.manifest_hash, frozen.manifest_hash)
        # An unfrozen snapshot is not mountable.
        with self.assertRaises(MemoryStoreError):
            MemoryStore(self.root / "other").load_frozen(1)
        # Tampering with the frozen artifact is detectable.
        frozen_path = self.store.marker_dir / "v000001.json"
        payload = json.loads(frozen_path.read_text(encoding="utf-8"))
        payload["manifest"]["manifest_hash"] = "0" * 64
        frozen_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(MemoryStoreError):
            self.store.load_frozen(1)

    def test_merge_order_is_deterministic(self):
        # `created_at` is pinned so the two identical claims really are identical:
        # the default timestamp has coarse resolution and would make this flaky.
        stamp = 1_700_000_000.0
        entries = [self.experience(source="t2", goal="b", created_at=stamp),
                   self.experience(source="t1", goal="a", created_at=stamp),
                   self.experience(source="t1", goal="a", created_at=stamp)]
        first = [item.experience_id for item in merge_experiences(entries)]
        second = [item.experience_id for item in merge_experiences(reversed(entries))]
        self.assertEqual(first, second)
        self.assertEqual(len(first), 2)  # the duplicate content hash is collapsed
        self.assertEqual(manifest_hash_for(1, None, []),
                         manifest_hash_for(1, None, []))

    # -- rollback (P7-05) ---------------------------------------------------
    def test_a_rollback_appends_history_instead_of_rewriting_it(self):
        first = self.store.commit([self.experience(source="t1", goal="a")])
        second = self.store.commit([self.experience(source="t2", goal="b")], parent=first)
        self.assertEqual(self.store.latest()[0].version, 2)
        rolled = self.store.rollback(1, reason="v2 experience was wrong")
        self.assertEqual(rolled.version, 3)                       # append-only
        self.assertEqual(rolled.parent_manifest_hash, second.manifest_hash)
        manifest, entries = self.store.latest()
        self.assertEqual([item.trigger.goal_pattern for item in entries], ["a"])
        # v1 and v2 are still readable: nothing was rewritten or deleted.
        self.assertEqual(len(self.store.load(1)[1]), 1)
        self.assertEqual(len(self.store.load(2)[1]), 1)
        review = self.store.rollbacks()
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0]["from_version"], 2)
        self.assertEqual(review[0]["to_version"], 1)
        self.assertEqual(review[0]["reason"], "v2 experience was wrong")
        frozen = self.store.freeze(rolled.version)
        self.assertTrue(frozen.verify())
        self.assertEqual([item.trigger.goal_pattern for item in frozen.entries], ["a"])

    def test_a_rollback_refuses_a_non_older_target(self):
        first = self.store.commit([self.experience(source="t1")])
        with self.assertRaises(MemoryStoreError) as caught:
            self.store.rollback(first.version, reason="nothing to do")
        self.assertEqual(caught.exception.code, "already_at_version")
        with self.assertRaises(MemoryStoreError) as caught:
            self.store.rollback(9, reason="unknown")
        self.assertEqual(caught.exception.code, "snapshot_missing")


class WaveRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.store = MemoryStore(self.root / "memory")
        self.runner = WaveRunner(self.root / "waves", memory_store=self.store,
                                 max_workers=4, branch_timeout=60.0)
        self.empty = self._empty_frozen()

    def _empty_frozen(self) -> FrozenMemory:
        manifest = self.store.commit([self._seed()])
        return self.store.freeze(manifest.version)

    def _seed(self):
        return build_experience(
            scope=ExperienceScope(app=MOCK_APP, build="mock-1"),
            trigger=ExperienceTrigger(foreground=MOCK_APP, facts=[],
                                      goal_pattern="种子经验"),
            grounding=ExperienceGrounding(layers=["text_exact"], identity="tap:搜索"),
            procedure=procedure_from_steps("tap:搜索"), outcome="verified",
            risk_class="R1", evidence_refs=["artifact:seed"], source_task_id="task_seed")

    def tearDown(self):
        self.tmp.cleanup()

    def branches(self):
        happy = PracticeBranch(
            branch_id="branch_a", label="搜索闭环",
            task=practice_task(request_id="wave-a", steps=SEARCH_STEPS,
                               criteria=SEARCH_CRITERIA, query="鸿蒙"))
        negative = PracticeBranch(
            branch_id="branch_b", label="目标未达成",
            task=practice_task(request_id="wave-b", steps="tap:搜索",
                               criteria=[{"id": "about", "type": "element_present",
                                          "target_key": "版本信息"}]))
        fragile = PracticeBranch(
            branch_id="branch_c", label="脆弱成功",
            task=practice_task(request_id="wave-c", steps="tap:搜索",
                               criteria=[{"id": "search", "type": "element_present",
                                          "target_key": "热搜榜"}]),
            faults=FaultProfile(noop_taps=1))
        unknown = PracticeBranch(
            branch_id="branch_d", label="未知写入",
            task=practice_task(request_id="wave-d", steps="tap:搜索",
                               criteria=[{"id": "search", "type": "element_present",
                                          "target_key": "热搜榜"}]),
            faults=FaultProfile(unknown_writes=1))
        return [happy, negative, fragile, unknown]

    def test_wave_barrier_merges_verified_and_failed_only(self):
        branches = self.branches()
        report = self.runner.run(branches, memory=self.empty, approval_ref=APPROVAL)
        self.assertTrue(report.barrier_held)
        self.assertEqual(len(report.branches), 4)
        kinds = {item.branch_id: item.outcome_kind for item in report.branches}
        self.assertEqual(kinds["branch_a"], "verified")
        self.assertEqual(kinds["branch_b"], "failed")
        self.assertEqual(kinds["branch_c"], "fragile_pass")
        self.assertEqual(kinds["branch_d"], "execution_unknown")
        self.assertIn("branch_d", report.blocked_branch_ids)
        self.assertNotIn("branch_a", report.blocked_branch_ids)
        self.assertIn("branch_b", report.practice_targets)
        self.assertIn("branch_c", report.practice_targets)
        self.assertEqual(report.status, "MERGED_WITH_BLOCKED")
        self.assertEqual(len(report.merged_experience_ids), 3)
        manifest, entries = self.store.latest()
        self.assertEqual(manifest.version, 2)
        self.assertEqual(report.memory_out["manifest_hash"], self.store.freeze(
            manifest.version).manifest_hash)
        self.assertEqual(len(entries), 4)  # seed + three merged experiences
        for outcome in report.branches:
            self.assertEqual(outcome.state_machine[0], "PRACTICE_AUTHORED")
            self.assertIn("ACTOR_ATTEMPTED", outcome.state_machine)
        merged = [item for item in report.branches if item.experience_id]
        for outcome in merged:
            self.assertIn("EXPERIENCE_STAGED", outcome.state_machine)
            self.assertIn("MEMORY_COMMITTED", outcome.state_machine)
            self.assertIn("MEMORY_FROZEN", outcome.state_machine)
        blocked = next(item for item in report.branches if item.branch_id == "branch_d")
        self.assertNotIn("EXPERIENCE_STAGED", blocked.state_machine)
        self.assertIn("VERIFIER_INCONCLUSIVE", blocked.state_machine)
        leftovers = [path for path in (self.root / "waves" / "branches").glob("*/*")]
        self.assertTrue(leftovers)  # per-branch state is isolated on disk

    def test_wave_without_approval_commits_nothing(self):
        report = self.runner.run([self.branches()[0]], memory=self.empty)
        self.assertEqual(report.status, "NO_MEMORY_CHANGE")
        self.assertEqual(report.merged_experience_ids, [])
        self.assertIn("branch_a", report.blocked_branch_ids)
        outcome = report.branches[0]
        self.assertIn("gate:", outcome.detail or "")

    def test_online_run_never_writes_to_frozen_memory(self):
        report = self.runner.run([self.branches()[0]], memory=self.empty,
                                 approval_ref=APPROVAL)
        frozen = self.store.load_frozen(self.store.latest()[0].version)
        before = frozen.manifest_hash
        task = self.branches()[0].task
        skills = open_practice_runtime(self.root / "online", owner="online",
                                      frozen_memory=frozen)
        try:
            created = skills.host.run_task("online", session_id=skills.session_id,
                                           task=task)
            deadline = time.time() + 30
            status = skills.host.task_status("online", task_id=created["task_id"])
            while not status["terminal"] and time.time() < deadline:
                time.sleep(0.02)
                status = skills.host.task_status("online", task_id=created["task_id"])
            self.assertTrue(status["terminal"], status)
            events = skills.host.task_events("online", task_id=created["task_id"])["items"]
            mounted = next(item for item in events if item["type"] == "memory_mounted")
            self.assertEqual(mounted["payload"]["manifest_hash"], before)
            self.assertTrue(mounted["payload"]["read_only"])
        finally:
            skills.close()
        self.assertTrue(self.store.is_unchanged(self.store.latest()[0].manifest_hash))
        self.assertEqual(self.store.load_frozen(self.store.latest()[0].version).manifest_hash,
                         before)
        # A non-frozen mount is refused before a task can start.
        from harmony_agent.host import AgentHost
        from harmony_runtime.contracts import RuntimeFault
        skills = open_practice_runtime(self.root / "bad-memory")
        try:
            with self.assertRaises(RuntimeFault):
                AgentHost(skills.runtime, self.root / "bad-agent",
                          frozen_memory=self.store.latest()[1])
        finally:
            skills.close()

    def test_practice_selector_targets_failures_and_fragile_successes(self):
        report = self.runner.run(self.branches(), memory=self.empty,
                                 approval_ref=APPROVAL)
        selector = PracticeSelector({
            "搜索闭环": PracticeBranch("follow_a", practice_task(
                request_id="follow-a", steps="tap:搜索",
                criteria=[{"id": "search", "type": "element_present",
                           "target_key": "热搜榜"}]), label="搜索闭环"),
            "目标未达成": PracticeBranch("follow_b", practice_task(
                request_id="follow-b", steps="tap:搜索",
                criteria=[{"id": "search", "type": "element_present",
                           "target_key": "热搜榜"}]), label="目标未达成"),
            "未知写入": PracticeBranch("follow_d", practice_task(
                request_id="follow-d", steps="tap:搜索",
                criteria=[{"id": "search", "type": "element_present",
                           "target_key": "热搜榜"}]), label="未知写入"),
        })
        selected = [branch.branch_id for branch in selector.select(report.branches)]
        self.assertIn("follow_b", selected)
        self.assertNotIn("follow_a", selected)
        self.assertNotIn("follow_d", selected)  # infrastructure error is not practice

    def test_procedure_parsing_and_layer_detection(self):
        steps = procedure_from_steps(SEARCH_STEPS)
        self.assertEqual([item.intent["action_kind"] for item in steps],
                         ["tap", "replace_text", "tap"])
        self.assertEqual(steps[0].intent["text"], "搜索")
        self.assertEqual(steps[1].intent["resource_id"], "search_input")
        self.assertEqual(steps[1].intent["argument_refs"]["text"], "arg.query")
        self.assertEqual(grounding_layers_for(SEARCH_STEPS),
                         ["text_exact", "resource_id"])


if __name__ == "__main__":
    unittest.main()
