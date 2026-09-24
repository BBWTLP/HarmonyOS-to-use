# Task 2 设备门禁 — 2026-09-24 窗口状态

run_id（设备批）：`20260924T160500Z`  
code_revision：`24330ee24c26f8028b9d5f77f7fd33d6731c8aac`  
编排产物：`task2-device-gate-status.json`

## 已完成（设备在线时）

| 检查 | 结果 |
|---|---|
| doctor 依赖 | ok（python/devhelmkit/mcp/pydantic/pillow/hdc） |
| device_discovery | ok，device_count=1 |
| service（`.runtime/agent-state`） | reachable |
| probe 只读 | **phone_observation_verified=true**，catalog 241，图树一致 |
| baseline | ok，绑定 `24330ee` + source hash `4575795d…` |
| unresolved journal | open incidents=33（leave open）；unresolved action rows=0 |

## 未完成（需设备）

| 步骤 | 状态 | 原因 |
|---|---|---|
| C03 burst 1/2/3 步 | failed | 设备发现为空 / 租约残留 |
| wake/unlock ×3 | failed | 同上 |
| M1 formal 10×3 | failed | 同上 |
| C01 20 样本 ×3 模式 | failed | 同上 |
| M0 formal 7×100 | failed | 同上 |

**不**把上述失败记为能力否定；它们是 `blocked_device`（连接丢失），不是原语失败。

## 真实断连事件（Task 6 故障矩阵证据）

2026-09-25 00:08 +0800 前后：`hdc list targets` → `[Empty]`。  
`hdc kill` + `hdc start` 后仍为空。属 **USB/HDC 断连**，满足 Task 6「断连后不重复派发、
不假成功」的前置观察：编排器各步以 exit≠0 干净收尾，未写入假成功报告，journal 未改写。

## 操作约束（本窗口踩坑，已固化）

1. **同一时间只允许一个设备客户端**。被 bash 工具杀掉的客户端会在服务端留下 300s 租约。
2. 长验收必须 **detached 单编排进程** 串行跑，前台 shell 超时会 `ChildProcess.kill`。
3. 路径含空格：`--state-dir` 用相对路径 `.runtime/agent-state`，或 Python `Popen` 传参列表。
4. 服务重启用 `.runtime/tmp_start_service.py`（或等价 `Popen`），不要依赖 `Start-Process` 拼路径。
5. 设备序列号不进普通报告；本文件不写序列号。

## 恢复后重跑

```powershell
# 1) 确认 hdc list targets 非 [Empty]
# 2) 停掉旧 serve，再起服务
.venv\Scripts\python.exe .runtime\tmp_start_service.py
# 3) 只跑编排器（不要并行其它设备客户端）
.venv\Scripts\python.exe .runtime\tmp_run_detached.py .runtime/tmp_task2_orchestrator.py
# 4) 轮询
Get-Content docs/acceptance/2026-09-24/task2-device-gate-status.json
```

建议把 `tmp_task2_orchestrator.py` / `tmp_start_service.py` / `tmp_run_detached.py`
收编进 `scripts/` 作为正式入口（后续提交）。
