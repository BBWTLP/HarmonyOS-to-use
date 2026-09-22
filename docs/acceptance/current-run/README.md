# 当轮验收（current-run）

run_id：`20260922T172053Z`  
code_revision：`81b5eab8bc11b8141a71d9a81be02a06a9a5d7a9`  
核查日期：2026-09-22（本地 +08:00 执行于 2026-09-23 凌晨）  
分支：`feat/runtime-foundation`

## 范围说明

本目录汇总 **本轮** 在用户 Windows 环境取得的证据摘要。原始序列号、token、截图、输入留在 `.runtime/execution-*/` 与 journal retention，不进 Git。

历史条件已改变：系统锁屏密码已取消；10 分钟无触碰自动息屏；Runtime 获准自动唤醒并无凭据解锁。旧报告中“必须人工保持解锁窗口”的结论不再适用于当前设备条件。

## 基线

| 项 | 结果 |
|---|---|
| git | clean @ `81b5eab`，与 origin/feat/runtime-foundation 一致 |
| 离线回归 | **789/789 通过**（本版复跑，`offline.json`） |
| pip check | 通过 |
| requirements.lock | sha256 `18a991d5…126055` |
| source hash | `008bebf8…ac672`（147 个 src/tests/scripts Python + pyproject + lock） |
| 设备 | 1×SGT-AL10，OpenHarmony-6.1.1.120，API 24 |
| 服务 | loopback reachable；journal 保留 |

## 核心成功率（累计）

| 层级 | planned | attempted | passed | failed | blocked | unknown | 说明 |
|---|---:|---:|---:|---:|---:|---:|---|
| 离线单测 | 789 | 789 | 789 | 0 | 0 | 0 | 本版复跑 |
| T00 设备读屏 | 1 | 1 | 1* | 0 | 0 | 0 | *catalog/前台可用；动态页 image_tree_consistent=false 记问题不记假成功 |
| 历史 unknown incident | 5 | 5 | 0 | 0 | 5 | 5 | 保留 open，待 T02 对账；不盲重放 |

`planned = attempted + unattempted` 在后续正式批强制。

## 端到端性能

尚未形成正式性能批。T00 单次观察：capture_ms≈3656–4843（FAST+image，微博动态页，batched provider，round_trips=2）。**不**与历史 A.1/A 轮数字直接比收益。

## 故障恢复

| 场景 | 状态 |
|---|---|
| 无密码息屏自动唤醒 | 已实现（`_wake_unlock`），T01 验收中 |
| 未知写入屏障 | 保留；5 条 open incident 未关闭 |
| 服务受控重启 | 已验证：旧 PID 退出 → 保留 journal → 新服务 reachable |

## 验收结论（当前）

**部分完成。** T00 基线通过；P0 业务闭环（T01–T04）进行中。  
不能宣称“通用 Agent 已验收”或“全量功能已覆盖”。

## 复现入口

```powershell
$ProjectRoot = 'D:\music mv\HarmonyOS-to-use'
$Py = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$State = Join-Path $ProjectRoot '.runtime\agent-state'
& $Py scripts/reproduce.py --report .runtime/execution-20260922T172053Z/offline.json
& $Py -m harmony_runtime.cli service --state-dir $State
& $Py -m harmony_runtime.cli probe --state-dir $State
& $Py -m harmony_runtime.cli baseline --execute --state-dir $State
```

状态台账：`docs/execution-status.md`。
