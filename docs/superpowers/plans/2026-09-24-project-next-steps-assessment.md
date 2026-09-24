# HarmonyOS Runtime Release Closure and Capability Expansion Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move `feat/runtime-foundation` from a strong Direct-runtime candidate to a release-ready v1 by synchronizing evidence, closing device-only acceptance gaps, and keeping optional visual/Decider capabilities explicitly gated.

**Architecture:** Keep the six Direct MCP tools as the release-critical path and keep the Agent/Actor/Decider layer optional. All device writes continue through the Runtime guard, journal, worker queue, and postcondition verifier. Acceptance results must be revision-bound and must distinguish implemented, offline-verified, device-verified, blocked, and deferred work.

**Tech Stack:** Python 3.11+, `devhelmkit` pinned to the lock file, MCP 2.2.0, Pydantic 2, Pillow, SQLite journals, PowerShell acceptance scripts, `unittest`, and GitHub Actions on Windows Python 3.11/3.13.

**Spec:** `docs/development-plan.md`, `docs/execution-status.md`, `docs/acceptance/current-run/README.md`, `docs/v3.2/blocked-device.md`, and `docs/superpowers/plans/2026-09-22-runtime-goal-delivery.md`.

## Global Constraints

- Use the current authorized HarmonyOS device and its measured system/app build as the acceptance target.
- Keep `local_off` as the default Agent profile; a Decider must not enter canary without complete calibration and holdout evidence.
- Never dispatch from stale observations, raw model coordinates, or historical frames.
- Never replay an `execution_unknown` write; preserve the journal barrier until trusted reconciliation evidence exists.
- Do not test or dispatch publish, like, follow, delete, payment, account-change, or private-message actions.
- Do not count unsupported, blocked, inconclusive, or setup-failed samples as successes.
- Keep historical acceptance reports immutable; create a new run id for every new candidate revision.
- Do not introduce a second device driver or make HAP delivery a prerequisite for the current Direct-runtime goal.

## Review Focus

- Dynamic pages and unstable accessibility IDs must cause bounded re-observation or a stale refusal, never a blind click; pin this in M0/M1 and M2 device runs.
- Screen-off, locked, or unknown readiness must block tree/screenshot/action capture until a fresh confirmed state exists; pin this in wake/recovery and long-task runs.
- A disconnect after dispatch must remain `execution_unknown` unless the journal has independently verifiable evidence; pin this in unknown-write fault runs.
- OCR/VLM timeouts, malformed regions, bad rotation, and missing engines must degrade to an explicit capability result and never create a dispatchable target; pin this in visual-provider and device visual runs.
- Every acceptance number must reference the exact HEAD, task-set hash, device/app build, denominator, and run id; pin this in the evidence-manifest and release-gate checks.

---

## Current assessment

At `6b78c76` the branch and `origin/feat/runtime-foundation` are aligned; the worktree was clean during inspection before this plan document was added. The current local record at that HEAD reports 809 offline tests passing, `pip check` passing, and source compilation passing. The core Direct runtime is substantially complete: six MCP tools, device queueing, stale-target guards, journal barriers, automatic no-credential wake/unlock, typed waits/burst/history, and formal M0/M1 evidence are present.

The project is not yet a general-purpose v1 release. The current-run evidence reports M0 699/700 (gate pass), M1 30/30, 15-step and 50-step long-task passes, but a 100-step run is inconclusive and the real-Agent loop is 2/3 because the topic round-trip failed. Formal controlled performance, burst timing, 50/100-step cross-app runs, M2 30×10 execution, real OCR/VLM, clean-environment installation, and the three-client matrix remain open. The Decider is correctly `keep_shadow_only`; only 45 real decision states exist versus the planned 300+ minimum.

The evidence layer itself needs repair before another release decision. `docs/acceptance/current-run/README.md` and `docs/execution-status.md` still identify `81b5eab`/794 tests while the current HEAD is `6b78c76`/809 tests. `docs/v3.2/progress.md` and `docs/v3.2/blocked-device.md` retain pre-acceptance language saying that no device evidence exists, while newer current-run artifacts contain device evidence. These are historical records, but their current placement makes the repository's release state ambiguous.

## Priority plan

### Task 1: Re-freeze evidence at the current revision (P0)

**Files:**
- Modify: `docs/acceptance/current-run/README.md`, `docs/execution-status.md`, `docs/agent-status.md`, and `docs/rsi-decider-plan-status.md` only where their status is stale.
- Create: `docs/acceptance/2026-09-24/README.md` and a revision-bound manifest/report set.
- Test: `scripts/evidence_manifest.py`, `scripts/secret_scan.py`, `scripts/reproduce.py`.

**Interfaces:** Consume the existing Runtime, task-set, and journal contracts. Produce one current run id tied to HEAD `6b78c76`, the dependency lock hash, task-set hashes, and the measured device/app build.

- [x] Run `git fetch origin --prune`, record branch/HEAD/worktree state, and verify local HEAD equals `origin/feat/runtime-foundation`.
- [x] Run the 809-test offline suite once in isolation, `pip check`, `compileall`, `secret_scan.py`, and the evidence manifest; save exit codes and hashes under the new run id.
- [x] Copy forward only evidence whose source revision and denominator match the new run; label older 81b5eab/794 and pre-acceptance reports as historical instead of editing them.
- [x] Record the five open historical unknown incidents and the authoritative state directory; do not create a new journal or delete the existing barrier. (Live journal has **33** open incidents as of run `20260924T152342Z`; the “five” figure is stale. See `docs/acceptance/2026-09-24/unknown-incidents.json`.)
- [x] Update the top-level README and v3.2 index to link to the new current run and explain the status vocabulary.
- [x] Gate: `git status` is clean after documentation commit; every “current” number resolves to the same HEAD and run id; no secret-scan finding is introduced.

### Task 2: Close the Direct v1 device gate (P0)

**Files:**
- Reuse: `scripts/accept_m0_primitives.py`, `scripts/accept_m1_weibo.py`, `scripts/accept_p05_benchmark.py`, `scripts/accept_c03_burst.py`, `scripts/accept_wake_unlock.py`.
- Inspect/modify only if a reproduced defect exists: `src/harmony_runtime/runtime.py`, `snapshot.py`, `journal.py`, `scripts/agent_harness.py`.
- Test: current device with a new run id and unchanged task set.

**Interfaces:** Consume the six Direct MCP tools and the existing `.runtime/agent-state`; produce formal M0/M1, C01, C03, and readiness reports with attempted/failed/blocked/unattempted denominators.

- [ ] Verify `doctor`, service health, read-only probe, device baseline, foreground identity, and unresolved journal state before dispatching any business action.
- [ ] Keep the existing M0 result as evidence only if its source hash is verified; otherwise rerun 100 valid samples per primitive with the ≥99% per-primitive gate and zero unresolved actions.
- [ ] Run one uninterrupted M1 formal batch of 10 tasks × 3 runs, requiring at least 27/30 and zero error completions; record setup failures separately.
- [ ] Run controlled cold/warm C01 samples for FAST, FAST+image, and FULL. Report P50/P95 by phase and do not compare them to the old uncontrolled sample set.
- [ ] Run C03 1-step, 2-step, and 3-step burst decisions under the shared 3000 ms budget, including watch timeout and a dynamic-control case.
- [ ] Run the wake/unlock recovery and a low-risk continuation after a controlled screen-off event; preserve the unknown-write barrier if the interruption occurs after dispatch.
- [ ] Gate: all required denominators are complete, no unknown write is silently retried, and performance/burst reports state the conditions and failure denominator.

### Task 3: Finish the real-Agent and long-task proof (P1)

**Files:**
- Inspect/modify: `scripts/agent_harness.py`, `scripts/accept_weibo.py`, `src/harmony_agent/supervisor.py`, `src/harmony_agent/checker.py` only for reproduced failures.
- Reuse: `docs/acceptance/current-run/e2e-final.json`, `scripts/accept_long_task.py`, `evals/tasks/long-device.json`.
- Test: real configured MCP client plus the current device.

**Interfaces:** Consume current observations and semantic postconditions; produce independent final verification, checkpoint, resume, and recovery evidence.

- [ ] Reproduce D3 from a fresh safe page and determine whether the failure is foreground evidence, surface classification, return navigation, or task cleanup.
- [ ] Run D1, D2, and D3 three times each with `client_type=actual_agent`; keep the two existing passes but do not call 2/3 a complete general-agent gate.
- [ ] Run 15-step, 50-step, and 100-step sequences three times, including one controlled screen-off and one service restart; budgets and dispatch counts must survive recovery.
- [ ] Run the cross-app variant on at least two authorized apps and three page classes without adding private-data actions.
- [ ] Gate: no inconclusive 100-step result is reported as pass; completed subgoals are not replayed; restart and screen-off preserve the journal barrier and remaining budget.

### Task 4: Execute M2 as a real benchmark (P1)

**Files:**
- Modify only if required by a failing task: `scripts/accept_m2.py`, `evals/tasks/m2-30.json`, `src/harmony_agent/planner.py`, `src/harmony_agent/checker.py`.
- Test: `tests/test_m2_runner.py`, `tests/test_m2_specification.py`, then device runs.

**Interfaces:** Consume typed M2 steps and capability declarations; produce per-task outcomes in `succeeded`, `failed`, `blocked`, `unsupported`, or `unknown`, with cleanup status separated from task verdict.

- [ ] Run all 30 tasks once and publish a capability matrix; treat the existing 4 succeeded / 10 failed / 16 unsupported smoke result as a diagnostic, not a score.
- [ ] Fix only common, reproducible causes such as stale target identity, missing preconditions, or weak postconditions; keep unsupported OCR/burst tasks visible.
- [ ] Run the formal 30 tasks × 10 runs only after every task has an executable or explicitly capability-blocked path.
- [ ] Require overall success ≥90%, L3 success ≥80%, zero unauthorized actions, zero duplicate irreversible writes, zero false completions, and no unresolved unknown writes.
- [ ] Gate: task-set version/hash, device/app build, and all 300 result records are persisted atomically; unsupported tasks cannot be silently removed from the denominator.

### Task 5: Decide the visual capability scope (P1/P2)

**Files:**
- Modify/configure: `src/harmony_agent/ocr.py`, `src/harmony_runtime/ocr.py`, `src/harmony_agent/vlm.py`, `src/harmony_agent/grounding.py` only after selecting an approved backend.
- Reuse: `docs/v3.2/visual-providers.md`, `docs/v3.2/adr-visual-target-authority.md`, `tests/test_visual_providers.py`.
- Test: real OCR/VLM provider and the device visual task.

**Interfaces:** Provider output remains regions only; Runtime revalidates observation, rotation, crop digest, risk, and freshness before dispatch.

- [ ] Choose and pin one permitted OCR backend and one optional VLM backend, including credentials/configuration ownership and a bounded timeout.
- [ ] Run Chinese small-text, rotation, icon-only, Canvas/WebView, malformed-output, timeout, and unavailable-engine cases.
- [ ] Run the full `ground → revalidate → guard → dispatch → verify` path on low-risk visual targets only.
- [ ] If no backend is approved, publish a tree-only release profile with `capability=false`; do not count M2 visual tasks as successful.
- [ ] Gate: no provider can create an action or raw coordinate, and all visual failures are explicit capability/grounding outcomes.

### Task 6: Close safety, retention, and operational release gaps (P1)

**Files:**
- Inspect/modify: `src/harmony_runtime/risk.py`, `src/harmony_runtime/journal.py`, `src/harmony_agent/artifacts.py`, CLI diagnostics, and `docs/runbook-rollback.md`.
- Test: `tests/test_unknown_write_reconciliation.py`, `tests/test_journal_privacy.py`, `tests/test_storage_pressure.py`, fault-injection tests, and a controlled device disconnect.

**Interfaces:** Consume journal incidents and artifact retention policies; produce auditable reconciliation, redacted history, quota behavior, and rollback evidence.

- [ ] Reconcile or explicitly leave open each existing unknown incident with evidence strength and next action; never close it with arbitrary attestation.
- [ ] Define the trusted approval boundary for R2/R3 actions or keep those actions disabled in the release profile; lexical risk blocking alone is not a complete approval workflow.
- [ ] Migrate or quarantine legacy plaintext recovery conditions and document retention/redaction limits.
- [ ] Exercise USB/HDC disconnect, worker hang, service restart, storage pressure, and artifact quota faults; verify no duplicate dispatch and no false success.
- [ ] Gate: the release checklist has zero unresolved release-blocking incidents, or clearly declares the profile blocked and explains why.

### Task 7: Validate installability and client compatibility (P1/P2)

**Files:**
- Modify: `.github/workflows/offline.yml`, `README.md`, `docs/agent-quickstart.md`, `docs/runbook-rollback.md`, and packaging metadata only where a clean-install failure is reproduced.
- Test: clean Windows environment, service lifecycle, and configured Codex/OpenCode/DeepSeek Harness clients.

**Interfaces:** Consume `requirements.lock` and the CLI entry point; produce a repeatable install, service start/stop/recover, and client capability matrix.

- [ ] Install from a fresh checkout on Python 3.11 and 3.13, run `pip check`, offline tests, and `doctor` without repository-local token/state files.
- [ ] Test service endpoint discovery, stale endpoint handling, orphan cleanup, controlled restart, and rollback from the documented commands.
- [ ] Run the same low-risk task set through each available client; separate tool-protocol compatibility from screenshot/image comprehension.
- [ ] Gate: a clean host can follow the quickstart, failures are diagnosable, and client-specific limitations are explicit.

### Task 8: Revisit Decider/Actor only after Direct v1 is closed (P2)

**Files:**
- Reuse: `scripts/collect_decision_dataset.py`, `tools/rsi/evaluate_decision.py`, `src/harmony_agent/decision/*`, `src/harmony_agent/actor.py`, `src/harmony_agent/supervisor.py`.
- Test: grouped holdout evaluation, shadow soak, and canary rollback.

**Interfaces:** Consume redacted, task-grouped decision states; produce calibration artifacts and a fail-closed router decision.

- [ ] Collect at least 300 real states, preferably 500–1000, with task/trajectory-grouped splits and no private content.
- [ ] Compare rules, pinned Decider, and any new revision on coverage, wrong-allow, abstention, calibration, latency, and task-success delta.
- [ ] Keep `local_shadow` until holdout, calibration, and latency thresholds pass; run a shadow soak before any canary.
- [ ] Keep Actor proposals bounded, read-only at the proposal boundary, and mapped to the Runtime's `screen_state`/observation contract.
- [ ] Gate: if the model does not beat the rules baseline inside safety and latency limits, retain `local_off` and close the work as an evidence-backed deferral.

## Recommended execution order

1. Task 1 immediately: create a single current evidence baseline and remove status ambiguity.
2. Tasks 2 and 3 in the next device window: finish the Direct v1 acceptance and the actual-Agent/long-task proof.
3. Task 4 after the device gate: run M2 once-per-task diagnostics, then the 300-run batch.
4. Tasks 5–7 in parallel where dependencies permit: visual scope, fault/retention hardening, clean install, and client matrix.
5. Task 8 last: Decider/Actor optimization is optional and must not delay a safe Direct release.

## Release decision

Declare a Direct v1 release only when Tasks 1–4, 6, and 7 meet their gates. Declare visual capability only when Task 5 passes; otherwise ship an explicit tree-only profile. Declare Decider canary only when Task 8 passes; otherwise keep `local_off`/`local_shadow`. A HAP or broad “all HarmonyOS apps” claim is outside this release decision.
