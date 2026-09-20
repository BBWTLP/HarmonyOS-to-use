# v3.2 Phase 9 / 10 / 12 / 13 — 离线加固

本文件合并记录四个"不新增功能、只补边界证据"的阶段。

## Phase 9 — Burst / Wait / History 边界

不重写任何功能。仓库已有覆盖（`tests/test_burst.py`、`test_wait.py`、
`test_history.py`、`test_incidents.py`）之外，新增
`tests/test_burst_wait_history_boundaries.py`（10 项）：

```text
共享 3000ms 预算耗尽：第一步已持久化，第二步未派发（status=stopped，
  steps[-1].execution_status=not_dispatched）
预算耗尽后的重复请求返回原结果且不重放
预算内的三步序列仍能完成
歧义目标（同名两节点）在 burst 与单动作两条路径都拒绝，0 次写入
wait 超时不等于成功（legacy expected 与 typed condition 各一项）
观察模式仅 FAST/FULL/TEMPORAL（LIGHT 被拒）
history 分页为最新优先且两页不重复
history 不含页面内容、输入值与恢复条件
```

真机时延与 3 步仲裁结论仍为 `blocked_device`。

## Phase 10 — Autonomous Runner 边界

新增 `tests/test_agent_runner_boundaries.py`（15 项）。

记忆（`memory.py`，此前无直接测试）：

```text
事实与假设分离；假设检索结果不含 observation_id（不是可执行观察）
压缩丢弃原始状态，但保留约束、事实、未决事件与证据索引
压缩后仍能回答用户约束；未决事件仍可查询
```

Checker（唯一成功 Authority）：

```text
不可观察条件 → inconclusive，不计成功
失败条件优先于通过条件
无 evaluator 的模型断言 → inconclusive/decided_by=unavailable
存在未决写入 → 整体 inconclusive
Checker 拒绝任何写能力（CheckerDenied）
判定始终带证据引用
```

任务级：

```text
步骤执行成功 != 任务成功（成功条件不成立时 status=FAILED）
条件不可观察 → PARTIAL
未知写入 → RECONCILIATION_REQUIRED + resolution_required=true
成功任务的条件必须由 code 判定（decided_by=code）
```

## Phase 12 — Offline Fault Matrix

新增 `tests/test_offline_fault_matrix.py`（14 项）。每个用例都检查四件事：
不重复写、不错误宣告成功、unknown 状态保留、journal barrier 保持。

```text
journal 准入失败（SQLite locked）→ 不派发任何动作
journal 完成失败 → execution_unknown + completion_persisted=false + 不报 verified
响应丢失后同 request_id 冲突被拒，不重放
未知写入后新 request_id 仍被 reconciliation_required 拒绝
worker 隔离（quarantine）作为传输状态单独上报，不等于未决写入
读故障不派发写动作
存储异常后 unknown 状态与 unresolved_actions 保留
取消竞态在派发前停止
epoch 变化使候选失效（epoch_mismatch）
视觉 provider 全部不可用时 UI-tree 路径照常
无图证据的视觉派发被拒（target_not_revalidated）
Agent 层故障不产生错误成功
artifact 配额上报且任务不丢失
任务在宿主重启后不自动续跑
```

## Phase 13 — 安装 / 迁移 / 回滚

新增 `tests/test_install_migration.py`（9 项）。

```text
发行版元数据与版本可导入
requirements.lock 覆盖 pyproject 声明的每个依赖
console entry point 已声明
核心导入不拉入 OCR / VLM / Decider 模块（子进程验证）
旧版本 journal（只有 actions 表、含 dispatching 行）可被打开：
  升级提升为 execution_unknown 并生成 open incident，新表补齐
本版本 Runtime 状态目录重开后 request_id 冲突被拒、历史仍可读
Agent 状态目录跨宿主重启可重开
v1 六工具在升级后仍可用（服务级端到端）
v1 不需要 agent 状态文件
```

## 仍未完成（诚实清单）

```text
Phase 9：真机 burst 仲裁与真实时延
Phase 10：真实 replan/记忆压缩的规模行为只在真机任务上可评估
Phase 12：真实 USB/HDC 断连、设备重启、真实 worker hang
Phase 13：真实干净环境 pip install（需要网络与目标 Python）、跨版本回滚演练
```
