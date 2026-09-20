# v3.2 Pre-Device Release Gate

执行日期：2026-09-20（Asia/Shanghai）
起点提交：`10ab69e4fcd5d115dab5155bf2f512a4587425e0`
设备条件：**无可用 HarmonyOS 真机**；本文件只记录离线证据。

## 1. Executive Summary

```text
是否允许进入真机测试：YES（附条件，见第 9 节剩余风险）
RC SHA：见第 8 节（提交后填写）
full regression：607 项通过，0 failed / 0 error / 0 skip
pip check：No broken requirements found.
```

## 2. 重新审计基线（不采信台账数字）

```text
branch      feat/runtime-foundation
HEAD        10ab69e（与 origin 同步）
dirty tree  仅未跟踪 .codex/（本机工具配置，不提交）
Python      3.11.9
reproduce   Ran 562 tests → OK（Gate 修改前，独立复跑确认）
pip check   clean
```

台账声称的"562 项全绿"经复跑成立，随后本轮修复把测试数提升到 607。

## 3. Gate 结果总表

| Gate | 问题是否真实存在 | 修改 | 测试 | 结果 | 剩余风险 |
|---|---|---|---|---|---|
| A Candidate Authority / Expiry | **是**：`_dispatch()` 只用 `candidate_set.by_id()` + epoch 比较，从不调用 `registry.resolve()`，TTL 与注册表成员资格在派发前未复核 | `supervisor._dispatch()` 在派发前调用 `run.registry.resolve(candidate_id, observation_id, controller_epoch)`；失败映射为 `unknown_candidate` / `stale_observation` / `epoch_mismatch` / `candidate_expired` 且 `execution_status=not_dispatched`，不重建候选 | `test_pre_device_gate.py::AuthorityGateTests`（6 项） | 通过 | 无 |
| B Visual Provenance / Authority | **是（承诺与实现不一致）**：Direct caller 可自造 `Target.visual`；原 ADR 却写"不允许未登记候选执行" | 明确分层契约并收紧 Runtime：视觉句柄必须带 `observation_id` 且与本次请求一致；`Target.visual` 强制要求 `observation_id`；ADR 改写为"授权来自复核+Guard" | `VisualProvenanceGateTests`（8 项）、`test_visual_target_authority.py` | 通过 | 见第 9 节：Direct caller 仍可提议区域（与 v1 语义一致） |
| C Visual Risk Evidence | **是**：视觉目标的 label 由 proposer 提供，且是唯一风险证据；`label="继续"` 可掩盖其下"立即支付" | `_resolve_visual` 采集与该区域重叠的 UI-tree 文本/描述/资源 id 写入 `visual_evidence`，`risk.label_of` 将其纳入敏感词判定（取并集） | `VisualRiskEvidenceGateTests`（5 项） | 通过 | 完全无 UI-tree 文本的纯视觉区域仍只有 proposer label 作为语义证据（已记录为边界） |
| D `model_profile` 契约 | **是**：字段必填但被完全忽略；任务无法提升、也未被拒绝 | `effective_profile()`：部署 profile 为权威；任务可 inherit 或请求更严格 profile，请求更高权限 → `profile_escalation_denied`，未知 profile → `profile_unknown`；更严格 profile 按任务生效 | `ProfileAuthorityGateTests`（6 项） | 通过 | 无 |
| E Model-call budget | **是**：`if outcome.decision.provider != "rules" or outcome.shadow` 反推调用次数，超时/503/崩溃后回退 rules 的调用**不计费** | `RouterOutcome.provider_calls` 显式计数；router 在预算耗尽时**不调用** provider；`supervisor` 改为累加 `outcome.provider_calls` | `ProviderBudgetGateTests`（3 项） | 通过 | 无 |
| F OCR / VLM deadline | **是**：`timeout_seconds` 只是文档，同步调用永不返回的 backend 会永久卡住调用方 | 新增 `provider_deadline.BoundedCaller`：非 `deadline_bounded` backend 由有界调用器执行，首次超时后**隔离**该 provider，后续快速失败且不再新建线程；协议声明生产 backend 必须自持 deadline；`production_ready` 暴露 | `ProviderDeadlineGateTests`（5 项） | 通过 | 真正 hard timeout 仍需 backend 自带传输期限（已声明为生产要求） |
| G Canary calibration | **是**：`may_execute()` 只需任意非空 calibration 字符串 | 新增 `decision/calibration.py`：canary 必须加载完整 artifact（id / revision / 双阈值 / 数据集哈希）才可能 execute；字符串单独存在 → fail closed；阈值来源为 artifact | `test_agent_decision.py`、`CalibrationGateTests`（5 项） | 通过 | 尚未实现 calibration 训练/签发流程（P2，不在本轮） |

## 4. Gate B 的六个问题（明确回答）

1. **谁可以创建 visual `target_ref`？**
   Autonomous 层由 `CandidateRegistry` 签发；Direct 调用方只能提交他构造的 `Target.visual`，Runtime 不信任其声明。
2. **`candidate_id` 是否出现在 Runtime dispatch authority 中？**
   不出现。Runtime 的 `Target` 没有 `candidate_id` 字段，派发负载只有
   `target_ref` / `observation_id` / `local_fingerprint`（+ `visual`）。候选是 Agent 层概念。
3. **Direct Runtime 是否有意允许用户自行构造视觉 Target？**
   是，且与 v1 语义一致：v1 调用方本来就用 `action_id`/`text` 选择目标。差异在于视觉目标必须通过像素与几何复核。
4. **"不允许未登记候选执行"约束谁？**
   约束 Autonomous Agent 层（模型不得签发候选）。已改写 ADR，避免把它读成对 MCP 调用方的承诺。
5. **谁验证 server-issued provenance？**
   Agent 层：`CandidateRegistry.resolve()` + `supervisor._dispatch()`；Runtime 层：观察句柄、几何、像素摘要、策略与 journal。
6. **crop revalidation 证明什么？**
   证明"这块像素没变"，不证明"该目标获得授权"。两者在实现中是分开的两步，ADR 已明确区分。

## 5. 代码变更（本轮）

| 文件 | 变更 |
|---|---|
| `src/harmony_agent/supervisor.py` | 派发前 `registry.resolve()` 关卡；`candidate_rejected` 事件；预算改为累加 `provider_calls` |
| `src/harmony_agent/decision/router.py` | `RouterOutcome.provider_calls`；预算耗尽时不调用 provider；canary 需 `Calibration` 对象；阈值来源改为 artifact |
| `src/harmony_agent/decision/calibration.py` | 新增：artifact 结构校验、revision 匹配、状态码 |
| `src/harmony_agent/decision/providers/decider.py` | `health_snapshot()`；transport 异常归类为 `transport_error` |
| `src/harmony_agent/decision/providers/base.py` | `provider_health()` 通用健康快照 |
| `src/harmony_agent/provider_deadline.py` | 新增：有界调用 + 超时隔离 |
| `src/harmony_agent/ocr.py` | deadline 归属、隔离状态、`production_ready`、共享调用器 |
| `src/harmony_agent/vlm.py` | 同上 + `vlm_quarantined` |
| `src/harmony_agent/host.py` | `effective_profile()` 权限模型；calibration artifact 加载；诊断新增 `calibration_status` / `task_profile_policy`；提交任务时清理过期候选 |
| `src/harmony_runtime/contracts.py` | 视觉目标必须携带 `observation_id` |
| `src/harmony_runtime/runtime.py` | 句柄-观察绑定校验（派发前） |
| `src/harmony_runtime/observation.py` | 视觉区域的设备侧重叠证据 `visual_evidence` |
| `src/harmony_runtime/risk.py` | `label_of` 纳入 `visual_evidence` |

## 6. 新增测试（为什么能防回归）

| 测试 | 作用 |
|---|---|
| `tests/test_pre_device_gate.py`（38 项） | 每个 Gate 的攻击面：过期/未签发/旧观察/旧 epoch 候选 0 派发；伪造区域/摘要/几何/旋转/跨页复制被拒；benign label + 敏感重叠文本被拒；预算耗尽不再调用 provider；挂死 provider 有界且隔离；profile 不得提权；calibration 缺件即关闭 |
| `tests/test_soak_offline.py`（5 项） | 1200 次观察/动作/历史循环 + 24 个任务生命周期 + provider 反复失败的限额与资源报告 |
| `tests/test_agent_decision.py` 新增 | 任意非空 calibration 字符串不能再开启 canary |
| `tests/test_visual_target_authority.py` 新增 | 视觉区域必须声明来源 observation |

## 7. Soak 结果（默认 1200 循环 / 24 任务）

```json
{"cycles": 1200, "tasks": 24, "errors": 0, "dispatches": 300,
 "task_statuses": "24/24 SUCCEEDED", "task_rows": 24,
 "threads": "1 → 1", "journal_bytes": "4096 → 245760",
 "artifact_bytes": "0 → 61964", "provider_calls": 12,
 "candidate_registry_after_tasks": 24}
```

结论（只断言结构性不变量，不编造阈值）：

```text
无单调线程增长（1 → 1）
无重复派发（1200 循环恰好 300 次写，与循环比例一致）
无未决写入（recovery_required=false，unresolved_actions=[]）
任务状态全部 SUCCEEDED，无 RECONCILIATION_REQUIRED
结束可 clean close，无未关闭 DB 错误
```

观察项（非缺陷，已记录）：任务期内签发的候选会保留到 TTL 到期；本轮在提交任务时清理
已过期候选，避免长驻宿主无限累积。

## 8. RC 冻结

```text
branch           feat/runtime-foundation
RC SHA           80e28988c98e4f8cb75c3f4d976d637eaa31c22a
test count       607（0 failed / 0 error / 0 skip）
dependency lock  18a991d559cde14ec9d7d2e06dffd4d9140c4ac949ccf7e61b7719e62a126055
Python           3.11.9
date/time        2026-09-20（Asia/Shanghai）
```

RC 之后的提交只允许是文档（例如把本 SHA 写进交接单）。如出现新的代码提交，
必须重新执行本 Gate 并给出新的 RC SHA。

---

# 9. RC 重新冻结（第二轮，2026-09-20）

## 9.1 为什么第一轮 RC 被拒绝

真机测试机在**全新 clone** 上重跑 Stage 3 Offline Regression 时，3/3 次运行都失败
（每次 1–15 项）。两个真实缺陷被确认，第一轮 RC
`80e28988c98e4f8cb75c3f4d976d637eaa31c22a` 因此只保留
`DEVICE_BASELINE_READY`，**不允许继续真机写路径验收**。证据见
`docs/acceptance/2026-09-20/rc80e2898-device-reacceptance-stage0-7.md`。

```text
OFFLINE-2  任务状态先于 result 持久化：
           RECONCILIATION_REQUIRED 已对外可见，但 result 尚不可读。
           另外 _finish() 也是 set_state() 与 save_result() 两次提交。

OFFLINE-1  DeciderProvider 单测隐式依赖 services/decider/.runtime/api-token：
           该文件被 gitignore，fresh clone 不存在，14 项测试在假 transport 之前
           就抛 token_unavailable。Gate 声称的「607 全绿」依赖未提交的本地工件。
```

## 9.2 本轮修复

| Blocker | 修复 | 回归 |
|---|---|---|
| OFFLINE-2 | 新增 `TaskStore.finalize_task()`：在**一个事务**内写 state + result + 收尾事件；`_finish()` 改为调用它；删除 `_run_subgoal()` 里提前发布 `RECONCILIATION_REQUIRED` 的写操作 | `tests/test_task_finalize_atomicity.py`：5 个 result-bearing 状态 × 100 次 = **500 次终态竞态轮询**，外加 10 次端到端 unknown-write 可见性检查 |
| OFFLINE-1 | 测试改用 `agent_fakes.make_decider_provider()` / `temp_token_file()` 提供的临时 token；**生产 token 逻辑未改**（默认仍是仓库相对路径，可用 `HARMONY_DECIDER_TOKEN_FILE` 覆盖） | `tests/test_fresh_clone_semantics.py`：Case A（无 token + `local_off` 不构造 provider）、Case B（无 token + `local_shadow` 安全回退）、Case C（假 transport + 临时 token 可复现） |

竞态测试的**敏感性已验证**：把发布顺序临时改回「两次提交 + 提前发布终态」后，
该测试在 attempt 0 抓到 2 次 `RECONCILIATION_REQUIRED` 无 result 的可见窗口；改回修复版后 6/6 通过。

## 9.3 本轮 Gate 结果

```text
fresh clone（无 api-token、无 .runtime、tree clean）
  Python 3.13.14   reproduce exit 0，Ran 618 tests，OK
  Python 3.11.16   reproduce exit 0，Ran 618 tests，OK
pip check          No broken requirements found.（3.11 / 3.13 均是）
稳定性             修复后连续 6 次全量回归全部 exit 0（修复前 3/3 失败）
adversarial        58 项通过（pre_device_gate + visual_target_authority）
fault matrix 等    93 项通过（offline_fault_matrix + agent_tasks + runner_boundaries
                            + agent_boundaries + visual_providers）
soak               HARMONY_SOAK_CYCLES=3000 / TASKS=48：5 项通过，
                   48/48 SUCCEEDED，750 次派发，0 错误，线程 1 → 1，
                   journal 4096 → 4096 bytes，无未决写入
requirements.lock  sha256 18a991d5…a126055（未变）
```

## 9.4 RC 冻结（第二轮）

```text
branch           feat/runtime-foundation
NEW RC SHA       b4049f50b851241817e0be577e080bcbcf8519a8
test count       618（0 failed / 0 error / 0 skip）
Python           3.11.16 与 3.13.14，均在 fresh clone 上全绿
date/time        2026-09-20（Asia/Shanghai）
```

`b4049f5` 之后只允许文档 / CI 提交；一旦再出现运行时代码或测试改动，
必须重新执行本 Gate 并给出新的 RC SHA。

## 9.5 进入真机的前置清单

```text
[x] OFFLINE-1 fixed
[x] OFFLINE-2 fixed
[x] race regression green（500 次竞态 + 端到端）
[x] fresh clone regression green（3.11.16 / 3.13.14）
[x] no runtime token dependency in local_off
[x] Python supported-version regression green
[x] full Pre-Device Gate green
[x] soak green（3000 周期 / 48 任务）
[x] working tree clean
[x] NEW_RC_SHA frozen
```

```text
ALLOW_DEVICE_TEST = YES
```

---

# 10. RC 重新冻结（第三轮，2026-09-20）：M0 动态页面 / acceptance harness

## 10.1 触发原因

第二轮 RC `b4049f5` 的真机结果：Stage 3–7 GREEN，M0 smoke NOT_READY
（launch/tree/screenshot/swipe/tap 各 3/3，input 1/3，back 0/3，
失败码全是 `search_editor_unavailable`，即 setup 没到达搜索编辑页）。
安全侧干净：36 条 journal 全 executed、incidents 0、unresolved 0。

## 10.2 根因（真机实测，不是推断）

```text
confirmed  setup 会计错误：ensure_search_editor() 的失败被计成 back/input 原语失败，
           样本从未被测量却进了成功率分母
confirmed  桌面误判：foreground_bundle=null 时，旧 surface_kind() 只用
           「顶部一个可点击 Flex」就把桌面（com.ohos.sceneboard）判成 discover
confirmed  目标子树漂移：target_fingerprint 对整棵子树取哈希，
           装饰性后代动画即改变它（fixture 已固化）
confirmed  选择器不稳定（主导）：微博「发现」页搜索入口的 accessibilityId
           是自增计数器（31985 → 31995 → 32000，同一节点、同一 bounds/hierarchy），
           目标比较包含该字段，故派发前被 stale_observation 拒绝
rejected   「动态 feed 让整页指纹变化」这一笼统解释不足以解释全部拒绝：
           navigation_fingerprint 在装饰性动画下并未改变（fixture 已证明）
```

## 10.3 本轮修改（只改 harness，Runtime 安全语义零改动）

| 文件 | 修改 |
|---|---|
| `scripts/agent_harness.py` | 新增 `SetupUnavailable`、`SetupBudget(max_actions/max_elapsed_ms/max_stale_refusals)`、`SetupStats`、`setup_act()`（拒绝后重新 observe + **重新定位**，绝不复用旧 `action_id`）、`has_weibo_evidence()`、`drift_metadata()`（只输出哈希/存在性）；`surface_kind()` 无 App 证据时返回 `unknown` |
| `scripts/accept_m0_primitives.py` | 会计改为 `requested_samples / valid_attempts / success / primitive_failures / insufficient_valid_samples` + 独立 `setup_*`；新增 `evaluate_gate()`；报告 `schema_version=2`；`MAX_SETUP_FAILURES` 上限；setup 失败不计入原语分母 |
| `tests/test_m0_setup_accounting.py` | Case 1–4 + stale 预算 + locator 缺失 |
| `tests/test_surface_classification.py` | 桌面/未知 surfaces → unknown；Weibo 三态分类；证据规则 |
| `tests/test_target_drift.py` | 子树漂移 fixture + 6 项对抗（位置/标签/禁用/替换/换页仍 stale）+ 未变化时正常派发 |

## 10.4 安全影响

```text
放宽 stale Guard？            否
改变 observation TTL？        否（仍 15s）
改变 journal / unknown-write？ 否
新增 allow_stale 之类开关？     否
```

`stale_observation` 行为完全未变；变的只是 harness「如何到达被测页面、如何计分、
如何报告 setup 健康度」。

## 10.5 本轮 Gate 结果

```text
fresh clone  Python 3.13.14 与 3.11.16 各 Ran 643 tests，OK（pip check clean）
新增测试      25 项（7 会计 + 8 分类 + 10 漂移/对抗）
旧测试        未删除
工作树        clean
```

## 10.6 RC 冻结（第三轮）

```text
branch           feat/runtime-foundation
NEW RC SHA       ef2731a6c1052781922d5386e9535ef3d678a86a
test count       643（0 failed / 0 error / 0 skip）
Python           3.11.16 与 3.13.14，均在 fresh clone 上全绿
date/time        2026-09-20（Asia/Shanghai）
```

真机验证顺序（下一阶段执行）：Stage 3 → Stage 5–7 → observe 稳定性采样
→ **M0 专项 back/input ×3** → 全量 M0 ×3 → 通过后才 25 / 100。
`back`/`input` 的 setup 若因上述自增 ID 目标而长期不足，正确结论是
`M0 = NOT_READY / insufficient_valid_samples`，不是把 setup 排除后宣称通过。

## 9. 剩余风险

```text
release blocker：无
visual-test blocker：
  - 真实 OCR / VLM 引擎接入与 deadline_bounded 声明（当前无生产 backend）
device-only uncertainty（blocked_device）：
  - M0/M1/C01/C03、真实 OCR/VLM 点击、M2 300 次、100 步稳定性、真机故障矩阵
P2 / deferred：
  - calibration 训练与签发流程（canary 目前只能 fail closed）
  - 完全无 UI-tree 文本的纯视觉区域只有 proposer label 作为语义证据
  - semantic 同义改写仍可绕过词表风险判定
  - Session.observations 的性能复用层（CaptureReuseCache）未实现
```

## 10. Definition of Done 核对

```text
[x] baseline regression green（独立复跑 562 → 本轮 607）
[x] candidate freshness revalidated immediately before dispatch
[x] model_profile authority documented and enforced
[x] failed provider attempts consume model budget
[x] visual target provenance semantics 与 ADR 一致（ADR 已改写）
[x] visual risk evidence 不仅依赖 proposer-controlled label
[x] OCR/VLM backend deadline contract bounded（并声明生产要求）
[x] local_canary cannot be casually enabled by arbitrary calibration string
[x] adversarial suite green（38 项）
[x] soak test completed without lifecycle leak / duplicate write
[x] full regression green（607）
[x] RC SHA frozen
[x] device-test-handoff generated
```
