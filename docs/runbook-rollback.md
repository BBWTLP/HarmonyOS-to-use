# 回滚 Runbook（P7-05）

覆盖三层回滚：Runtime、Decider（模型 profile）、学习记忆（memory snapshot）。
每一步都是**只读检查 + 显式命令**，没有隐式删除；历史记录一律保留。

以下命令都在仓库根目录执行，并假设：

```text
state_dir      .runtime/agent-state          （常驻 Runtime 状态与 journal）
memory_root    .runtime/rsi/memory           （版本化学习记忆快照）
```

## 0. 何时使用

| 触发信号 | 先用哪一层回滚 |
|---|---|
| 新 RC 后真机成功率下降、出现新的拒绝类型 | §1 Runtime |
| Decider canary 引入错误放行或显著延迟 | §2 Decider |
| 新 memory snapshot 导致任务重复同一错误 | §3 Memory |
| 未知写入未对账、且需要继续写操作 | 先 §4，再决定是否回滚 |

安全前提：**任何回滚都不会撤销手机侧已经执行的动作**。未知写入必须先按 §4 对账，
不允许用回滚当作重试。

## 1. Runtime 回滚

```text
1) 停止常驻服务（不要杀手机侧进程；服务停止只影响主机侧）
   Get-Process python | Where-Object { $_.CommandLine -like "*harmony_runtime.cli serve*" } | Stop-Process
   或记录 endpoint.json 里的 pid 后停止该进程

2) 只读确认状态可读、没有未决写入
   .venv\Scripts\python.exe -m harmony_runtime.cli service --state-dir .runtime/agent-state
   .venv\Scripts\python.exe -m harmony_runtime.cli doctor
   期望：service = ok / service_endpoint_stale（服务已停）；doctor 的 device_discovery 仍 ok

3) 切回上一个冻结 RC（示例；用实际记录的上一 RC SHA）
   git -C . log --oneline -5
   git -C . checkout <previous-rc-sha>
   .venv\Scripts\python.exe -m pip install -r requirements.lock
   .venv\Scripts\python.exe scripts\reproduce.py            # 期望 exit 0

4) 重新启动服务并复验只读路径
   $env:HARMONY_HDC='F:\DevEco Studio\sdk\default\openharmony\toolchains\hdc.exe'
   .venv\Scripts\python.exe -m harmony_runtime.cli serve --state-dir .runtime/agent-state
   .venv\Scripts\python.exe -m harmony_runtime.cli service --state-dir .runtime/agent-state
   .venv\Scripts\python.exe tests\smoke_device.py           # 只读真机冒烟
```

**不要**为了回滚执行 `git reset --hard`、删除 `.runtime/agent-state` 或覆盖 journal：
journal 与 incidents 是未知写入的唯一证据。

## 2. Decider 回滚

Decider 只影响"建议"，不承载授权，因此回滚是**改 profile**，不是改代码：

```text
local_canary  →  local_shadow  →  local_off

$env:HARMONY_AGENT_PROFILE = "local_shadow"    # 只记录、不派发
# 或
$env:HARMONY_AGENT_PROFILE = "local_off"       # 完全不构造、不调用模型
```

依据（离线证据）：`tools/rsi/evaluate_decider.py` 的 `canary_gate` 只有在 holdout
wrong-allow = 0、calibration artifact 完整、P95 在预算内、覆盖率非零、任务成功率不回退时
才输出 `local_canary_allowed`。当前结论是 `keep_shadow_only`。

回滚后确认：

```text
.venv\Scripts\python.exe tools\rsi\evaluate_decider.py --rules-only \
  --dataset .runtime/evals/decision/dataset.json --report .runtime/rsi/decider-rollback.json
```

## 3. Memory snapshot 回滚

记忆是 append-only 的：回滚**不会**删除或改写任何旧版本，而是把旧版本的条目作为新版本
追加（父哈希指向当前 tip），并把"放弃了哪个版本、为什么"记录到 `rollbacks/`。

```python
from harmony_agent.memory_store import MemoryStore
store = MemoryStore(".runtime/rsi/memory")
for version in store.versions():                 # 只读：有哪些版本
    manifest, entries = store.load(version)
    print(version, manifest.manifest_hash[:12], len(entries), manifest.frozen)
manifest = store.rollback(3, reason="v4 经验导致重复错误")
frozen = store.freeze(manifest.version)          # 在线任务只挂载 frozen 快照
print(frozen.version, frozen.manifest_hash, frozen.verify())
print(store.rollbacks())                         # 回滚审查记录
```

验证要点（离线测试已固定，见 `tests/test_rsi_learning.py`）：

```text
rollback 只追加新版本，parent_manifest_hash = 回滚前的 tip
旧版本仍然可读（load(1)/load(2) 不变）
rollbacks() 记录 from_version / to_version / reason / manifest_hash
frozen.verify() 为真，且条目集合等于目标旧版本
对当前 tip 或未知版本的回滚被拒绝（already_at_version / snapshot_missing）
```

在线任务只挂载 `FrozenMemory`（无写入方法），因此回滚不会与正在运行的任务相互污染：
先完成任务或取消，再挂载新快照。

## 4. 前置动作：未知写入对账（不是回滚）

```text
mobile_session(operation="status", session_id=...)          # 看 unresolved_actions
mobile_session(operation="action_status", session_id=..., request_id=...)
mobile_session(operation="reconcile", session_id=..., request_id=...,
               evidence_kind="postcondition_verified")       # 原始语义后置条件成立
mobile_session(operation="reconcile", session_id=..., request_id=...,
               evidence_kind="not_executed", attestation="...")  # 页面指纹与派发前一致
```

只有这两种证据能关闭事件；两者都不满足时保持阻塞，**绝不重放**。

## 5. 回滚后的记录要求

每次回滚都要在验收记录里补一条，包含：触发信号、回滚层级、from/to 标识（RC SHA /
profile / memory version + manifest hash）、复验命令与结果、以及是否仍需人工处理。
实验报告 schema 见 `harmony_agent.reporting.ExperimentReport`（计划 §8.3）。
