# RSIAgent × Decider 开发计划落地台账

对照 `docs/architecture-rsi-decider-plan-2026-09-20.md`。计划本身不改写，本文件只记录
每个工作项的真实状态、证据和缺口。

状态词汇（沿用 v3.2 台账口径）：

```text
planned            已规划，未开始
in_progress        正在进行
implemented        代码已存在，未取得离线证据
verified_offline   离线测试/脚本通过
blocked_device     必须真机，当前无法执行
verified_device    真机证据通过
deferred           明确推迟
```

本轮证据目录：`docs/acceptance/2026-09-24/`（run `20260924T152342Z` @ `6b78c76`）。
历史批次见 `docs/acceptance/2026-09-20/`。代码基线见
`docs/acceptance/2026-09-24/run-manifest.json`。

## 0. 阶段总览

| Phase | 内容 | 状态 | 说明 |
|---:|---|---|---|
| 0 | 分支与基线冻结 | **verified_offline** | `baseline-freeze.json`、`secret-scan.json`、`scripts/experiment_report.py` |
| 1 | Runtime 发布阻塞项 | **partly verified_device / partly blocked_device** | P1-04 代码闭环并离线验证；M0 100/primitive 与 M1 30 次仍待真机长批 |
| 2 | 协议与事件骨架 | **verified_offline** | `Intent→CandidateSetRef→DecisionSuggestion→GuardedAction` 与事件 envelope 已落地 |
| 3 | RSIAgent 式离线学习闭环 | **verified_offline（mock 设备）** | wave barrier、experience gate、memory staging/freeze、`tools/rsi/*` |
| 4 | 真实低风险自治任务 | **partly in_progress** | Actor 协议与 Verifier 协议已就绪；真机低风险任务集待 Phase 1 门槛后执行 |
| 5 | Decider 数据、校准与 canary | **in_progress（工具已就绪，数据不足）** | `tools/rsi/evaluate_decider.py` 覆盖 coverage/wrong-allow/ECE/Brier/P50-P95；真实 300+ 分组数据仍缺 |
| 6 | 长任务、记忆召回与多客户端 | **planned** | 依赖 Phase 3 frozen memory 与 Phase 4 独立验证 |
| 7 | 发布与运营 | **planned** | 干净环境安装、300 次正式验收、回滚 runbook |

## 1. Phase 0 — 分支与基线冻结

| 工作项 | 状态 | 证据 |
|---|---|---|
| P0-01 记录 Runtime/RSIAgent commit、Python、依赖、设备基线 | **verified_offline** | `scripts/baseline_freeze.py` → `docs/acceptance/2026-09-20/baseline-freeze.json`（Runtime `5782657…`、RSIAgent `a9e5626…`、Python 3.11.9、`pip check` 退出码 0、`requirements.lock` 与设备基线 sha256） |
| P0-02 清理 token/截图/原始树进入 Git 的风险 | **verified_offline** | `scripts/secret_scan.py` → `secret-scan.json`：224 个受控文件、6 条规则、0 findings；8 条 device-serial 命中列入 `reviewed_allowlisted` 并给出理由 |
| P0-03 固定 RC、测试命令与报告 schema | **verified_offline** | `baseline-freeze.json.frozen_commands`；报告 schema 落在 `harmony_agent.reporting.ExperimentReport`（计划 §8.3 字段逐项对应），入口 `scripts/experiment_report.py` |

## 2. Phase 1 — Runtime 发布阻塞项

| 工作项 | 状态 | 证据 / 缺口 |
|---|---|---|
| P1-01 Stage 3 offline regression | **verified_offline** | 本轮 `python -m unittest discover -s tests` = 710 项通过（含本轮新增 35 项）；`scripts/reproduce.py` 可复跑 |
| P1-02 M0 原语矩阵（每原语 100 有效样本，≥99%） | **in_progress（真机）** | 本轮：先复现并修复 `input` 的字段解析/聚焦缺口（真机复测 3/3，见 `m0-input-focus-fix-3.json`），再跑全量 smoke 7×3 = 20/21（仅 `screenshot_inconsistent` 1 次，见 `m0-smoke-3.json`），随后启动 25/原语长批 `m0-primitives-25-20260920.json`。历史：RC `19ee190` 3/原语全绿、`m0-primitives.json` 25/原语 173/175（98.86%）。**100/原语仍未执行** |
| P1-03 M1 冻结代码单批 30 次（≥27/30） | **blocked_device（本轮尝试未完成）** | 2026-09-21 凌晨尝试：仅 5/30 次运行，全部 blocked 且 dispatches=0（m1_01 ×3 `stale_observation`、m1_02 ×2 `editor_unavailable`），第 6 次因手机密码锁屏 `screen_locked` 中止；脚本现已把设备级失败记为 blocked 并在连续 3 次后干净收尾。需要设备保持解锁的连续窗口重跑 |
| P1-04 unknown-write 可信对账 | **verified_offline（代码闭环）** | 新增 `mobile_session(operation="reconcile")`：`postcondition_verified`（原始语义后置条件成立）与 `not_executed`（页面指纹与派发前逐字节一致 + attestation）两种证据才能关闭事件；拒绝过期证据、证据不足、未绑定设备的旧记录；永不重放。agent 任务现在把任务自己的语义终态条件绑定到最后一步的恢复条件（`ActRequest.recovery`），因此也能用 `postcondition_verified` 关闭；未达成的目标不会造成额外派发。测试 `tests/test_unknown_write_reconciliation.py`（8 项）+ `tests/test_agent_planner.py`（3 项） |
| P1-05 Burst/Wait/取消/租约 | **verified_offline（离线）；真机语义待复跑** | 既有 `tests/test_burst*.py`、`test_wait.py`、`test_recovery.py` 全绿 |
| P1-06 privacy/retention/storage | **verified_offline** | `harmony_agent/artifacts.py` 配额/TTL/脱敏导出 + `tests/test_journal_privacy.py`、`test_storage_pressure.py`、本轮 `secret-scan.json` |

## 3. Phase 2 — 协议与事件骨架

| 工作项 | 状态 | 证据 |
|---|---|---|
| P2-01 `Intent/CandidateSet/Suggestion/GuardedAction` schema | **verified_offline** | `src/harmony_agent/contracts.py`（protocol 2.1）：Intent 拒绝坐标与内联输入、CandidateSetRef 绑定 id/TTL、DecisionSuggestion 只能引用被提供的候选且未校准模型不得 `execute`、GuardedAction 必须有注册 target 与指纹、高风险必须有运行时签发的 `authz_` 引用 |
| P2-02 事件 envelope | **verified_offline** | `EventEnvelope` + `TaskStore` 迁移列 `envelope`；每条事件写入 `observation_id / candidate_set_hash / controller_epoch / model_revision / calibration_version / evidence_refs`；旧 DB 自动迁移，历史 payload 形状不变 |
| P2-03 TaskSupervisor 事务 | **verified_offline** | 既有 `finalize_task` 单事务（状态/结果/结束事件一起可见）+ `tests/test_task_finalize_atomicity.py`；结束事件同样带 envelope |
| P2-04 ActorProvider 协议 | **verified_offline** | `src/harmony_agent/actor.py`：`SubgoalProposal` 或 `ControlProposal`（reobserve/escalate/wait/stop），`DeterministicActor` 无需模型即可跑完计划；跨任务 intent 被拒绝 |
| P2-05 VerifierProvider 协议 | **verified_offline** | `src/harmony_agent/verifier.py`：三态 verdict、`assert_read_only` 拒绝声明写能力的 verifier、`ReadOnlyVerifierMount` 的写方法一律抛错、pass 无证据自动降级 inconclusive；`TaskRun` 默认使用 `CodeVerifier`，结果里记录 verifier/revision/evidence |

## 4. Phase 3 — RSIAgent 式离线学习闭环

| 工作项 | 状态 | 证据 |
|---|---|---|
| P3-01 mock/fake device adapter | **verified_offline** | `src/harmony_agent/fake_device.py`：可被真实 Runtime 持有的脚本设备（页面图 + 故障注入 noop/unknown/lock），`open_practice_runtime` 除驱动外全部走生产路径 |
| P3-02 Phase 1 wave runner | **verified_offline** | `src/harmony_agent/rsi.py`：同一 frozen memory 起点、并行分支、全部分支结束后才合并（`barrier_held`），按 branch_id 固定顺序合并 |
| P3-03 Experience schema/脱敏 | **verified_offline** | `src/harmony_agent/experience.py`：scope/trigger/grounding/procedure/outcome/failure_modes/risk/evidence/hash；内联输入、坐标、raw tree、截图、token 一律拒绝 |
| P3-04 Memory staging/commit/freeze | **verified_offline** | `src/harmony_agent/memory_store.py`：父 manifest hash 链、manifest hash 覆盖版本+父哈希+条目哈希、原子写、冻结落在独立 `frozen/`（append-only 快照不被改写）、篡改可检测 |
| P3-05 Phase 2 target/practice | **verified_offline** | FAIL 与脆弱 PASS（attempts>1）进入 `practice_targets`；基础设施错误/执行未知/无证据不伪装成 FAIL，直接 blocked |
| P3-06 Phase 3 frozen execution | **verified_offline** | `FrozenMemory` 无写入方法，`AgentHost` 拒绝非 frozen 挂载；任务运行后 store 的 manifest hash 不变 |

## 5. Phase 4 — 真实低风险自治任务

| 工作项 | 状态 | 说明 |
|---|---|---|
| P4-01 Actor + RuntimeFacade 集成 | **in_progress（离线半程已验证）** | Actor 已接入任务循环：子目标 blocked/failed 时给一次有界重规划，提案经 Intent 契约校验后走同一 guard；请求脱敏（无 facade、无 catalog、无候选 id）；默认上限 2 次且仍受 dispatch/时间预算约束（`tests/test_agent_actor_integration.py` 6 项）。真机低风险任务集仍需设备解锁后执行 |
| P4-02 独立 Verifier | **implemented** | 协议与只读约束已就绪；尚未接入真机成功率取证 |
| P4-03 低风险任务集 | **planned** | 依赖 Phase 1 真机稳定性 |
| P4-04 预算/暂停/恢复 | **verified_offline** | 既有 supervisor 预算/暂停/取消/恢复测试全绿 |
| P4-05 真实客户端 smoke | **blocked_device** | 需要配置被加载的新客户端会话 |

## 6. Phase 5 — Decider 数据、校准与 canary

| 工作项 | 状态 | 说明 |
|---|---|---|
| P5-01 真实 state collector | **in_progress** | 入口 `scripts/collect_decision_dataset.py`；当前 45 条真实状态不足以作为准入样本量 |
| P5-02 rendering/candidate ablation | **planned** | 计划要求先解释 `cand_none_applicable` 的来源 |
| P5-03 rules/Decider/new revision 对照 | **verified_offline（工具）** | `tools/rsi/evaluate_decider.py` 输出 coverage/abstention/wrong-allow/ECE/Brier/P50-P95 与 rules 基线，`--rules-only` 可离线自检 |
| P5-04 calibration artifact | **verified_offline（工具）** | `--write-calibration` 生成含 revision、阈值、split、数据哈希的 artifact |
| P5-05 shadow soak | **planned** | 需要在真实数据的 shadow 连续运行 |
| P5-06 canary gate | **planned（默认保持 local_shadow）** | 工具内置 `keep_shadow_only` 判定，逐条检查 holdout wrong-allow、artifact 完整性、P95 预算、coverage、任务成功率回退 |

当前结论仍然是 **keep_shadow_only**：本轮没有新增真实决策数据，canary 门槛未触碰。

## 7. Phase 6 / Phase 7

| 工作项 | 状态 | 说明 |
|---|---|---|
| P6-01 checkpoint/living plan | **verified_offline（子目标级）** | 每个子目标在开始/结束时写入 `checkpoint` 事件：phase、plan_version、status、attempts、blocked_reason、remaining budget（dispatches/seconds/model_calls）、replan_reasons；`task_status` 增加 `current_subgoal`。真机长任务（50/100 步）仍需设备解锁 |
| P6-02 context compression | **verified_offline** | runner 在 recent-state 窗口真正超预算时触发 `Memory.compress()`，并写 `memory_compressed` 事件（states before/after、kept constraints/facts、unresolved incidents、evidence_refs）；压缩后立刻用任务自己的约束/未决事件/目标做 `can_answer` 校验，出现缺失就写 `context_loss` 事件并把 `context_loss:...` 加入结果 limitations。测试 `tests/test_agent_long_task.py`（4 项） |
| P6-03 50/100 步任务 | **blocked_device** | 需要真机连续窗口 |
| P6-04 三客户端矩阵 | **blocked_device** | Codex 已接入；OpenCode / DeepSeek Harness 需真机与新会话 |
| P6-05 frozen memory regression | **verified_offline（部分）** | 在线任务挂载 `FrozenMemory` 后 store 摘要不变已有测试；真机复跑待设备解锁 |
| P7-01 干净 Windows 安装 | **planned** | 需要干净环境/新机复跑 |
| P7-02 service lifecycle | **in_progress（诊断已验证）** | 新增只读 `harmony_runtime.cli service --state-dir`（endpoint/端口/pid 分开报告）并加入 `doctor`；真机证据：服务存活 `ok`（exit 0）、停止后 `service_endpoint_stale`（exit 1）。端口冲突与异常退出演练仍待补 |
| P7-03 300 次正式验收 | **blocked_device** | 依赖 P1 全部门槛 |
| P7-04 发布包与兼容矩阵 | **planned** | 依赖 P7-01 |
| P7-05 回滚 runbook | **verified_offline（记忆层可执行）** | `docs/runbook-rollback.md` 覆盖 Runtime / Decider profile / memory snapshot 三层；`MemoryStore.rollback()` 以"追加新版本（parent=当前 tip）"的方式回滚并写 `rollbacks/` 审查记录，旧版本保持可读，冻结快照 `verify()` 为真（`tests/test_rsi_learning.py` 2 项 + 实跑演练）。Runtime/Decider 层需要真机复验 |

依赖关系未变：Phase 6 需要 Phase 3 的 frozen memory 与 Phase 4 的独立验证；Phase 7 需要
Phase 1 的 100/30/300 次真机分母。

## 8. 已知缺口（本轮新增或确认）

1. ~~Agent 任务的 unknown write 只能用 `not_executed` 关闭~~（**已修复**）：计划现在把
   任务的语义终态条件绑定到最后一步的 `recovery_expected`，运行时把它作为 durable 恢复
   条件写入 journal（`ActRequest.recovery`），因此 agent 任务也能用
   `postcondition_verified` 关闭；中间步骤仍保持 "page changed" 语义，未达成的目标不会
   触发重复派发。离线用例见 `tests/test_unknown_write_reconciliation.py`（8 项）与
   `tests/test_agent_planner.py`（3 项）。
2. **Phase 1 真机长批 — 部分历史证据、current 未绑定**：M0 formal 699/700 与 M1
   formal 30/30 已有真机产物，但缺 revision 绑定（见 `2026-09-24/run-manifest.json`）。
   Burst/Wait 真机语义、C01 受控性能、300 次正式验收仍待设备窗口。
3. **Phase 5 数据不足**：45 条真实状态不能作为准入样本；分组切分必须按任务轨迹。
4. **mock wave 不是设备证据**：`tools/rsi` 与 `fake_device` 的结论只能算离线验证。
5. **stdio 前端依赖常驻服务；异常退出会留下孤儿 stdio 子进程**（**已修复**）：
   `harmony_runtime.cli mcp` 只连接 `endpoint.json` 指向的常驻服务。缺服务时工具调用
   很快失败，但 harness 在异常路径上没有关闭 stdio context，子进程继续持有调用方管道，
   于是外部观察者看到"不返回、不输出、不退出"。已加启动前端点/TCP 存活探测（明确错误
   与启动命令）与 `__aexit__` 逐步兜底关闭，并用 3 项离线用例固定。证据见
   `docs/acceptance/2026-09-20/rsi-decider-plan-increment.md` 第 5.1 节。
6. **input 原语字段解析缺口**（**已修复并真机复测**）：真机编辑器的输入框 `resource_id`
   为空、只有自增 `accessibilityId`、且不总是自动聚焦，旧查找（focused 或
   `resource_id=search_input`）必然落空。现按 focused → resource_id → 结构解析，未聚焦
   时用一次有界 setup tap 聚焦；聚焦失败记 setup 失败，不进入原语分母。复测 3/3。
7. ~~长批量需要设备保持解锁~~（**条件已变**）：系统锁屏密码已取消，Runtime 已验收
   无凭据唤醒/解锁（T01）。旧「必须人工保持解锁窗口」不再适用；脚本仍保留
   blocked + 连续 3 次干净收尾。unknown 写入屏障未变（当前 open incident=33）。

## 9. 与计划 §6 目录骨架的差异（有意保留）

计划 §6 给出 `tests/{unit,decision,learning,fault_injection,device}/` 的分类骨架。本仓库
既有的冻结命令是 `python -m unittest discover -s tests -p "test*.py"`，全部测试为
`tests/test_*.py` 平铺文件。本轮新增测试沿用既有约定（`test_agent_protocol_v2.py`、
`test_rsi_learning.py`、`test_unknown_write_reconciliation.py`），以免改动冻结命令或让
新测试不被发现。其余骨架（`tools/rsi/*`、`services/decider/*`）按计划落位。
