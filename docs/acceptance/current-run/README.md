# 历史验收（current-run → 已降级为 historical）

> **状态：`historical`。** 本目录汇总 `81b5eab` / `20260922T172053Z` 时代的记录，  
> **不进入 current 数字**。当前权威 run 见  
> [`docs/acceptance/2026-09-24/`](../2026-09-24/README.md)（`run_id=20260924T152342Z`，`code_revision=6b78c76`）。  
> 本文件不再改写历史数字；其中的 M0/M1/e2e formal 产物内无 revision 绑定，按  
> `2026-09-24/run-manifest.json` 标为 `historical_device_evidence_unbound`。

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
| 离线单测 | 794 | 794 | 794 | 0 | 0 | 0 | 本版复跑 `offline-final.json` |
| T00 设备读屏 | 1 | 1 | 1* | 0 | 0 | 0 | *catalog/前台可用 |
| T01 唤醒 | 6 | 6 | 6 | 0 | 0 | 0 | 5 受控 + 1 自然 650s |
| T02 对账反例 | 10 | 10 | 10 | 0 | 0 | 0 | 含 not_executed 收紧 |
| T04 Agent 闭环 | 3 | 3 | 2 | 1 | 0 | 0 | D1/D2 pass，D3 fail |
| 历史 unknown incident | 5 | 5 | 0 | 0 | 5 | 5 | 保留 open，不盲重放 |

`planned = attempted + unattempted` 在后续正式批强制。

## 端到端性能

尚未形成正式性能批。T00 单次观察：capture_ms≈3656–4843（FAST+image，微博动态页，batched provider，round_trips=2）。**不**与历史 A.1/A 轮数字直接比收益。

## 故障恢复

| 场景 | 状态 |
|---|---|
| 无密码息屏自动唤醒 | 已实现（`_wake_unlock`），T01 验收中 |
| 未知写入屏障 | 保留；5 条 open incident 未关闭 |
| 服务受控重启 | 已验证：旧 PID 退出 → 保留 journal → 新服务 reachable |

## T04 Agent 闭环（已收尾，无追加真机）

| 任务 | 结果 | 证据 |
|---|---|---|
| D1 搜索「鸿蒙」 | pass | 查询词 + App 身份（e2e-final.json） |
| D2 替换 harmony → 返回发现 | pass | `surface_is=discover` |
| D3 话题往返 | fail | 返回后身份不可观察 |

client_type：`mimo_agent_as_tool`（本会话即执行 Agent，非外部 Codex）。  
计划计数：3/3 attempted，**2 passed**。D1「综合」标签与 D3 稳定性记为缺口，不在本轮补跑。

## T06–T13 汇总

| 任务 | 结果 | 证据 |
|---|---|---|
| T06 M0 formal | **699/700 (99.86%)** gate pass | `m0-formal.json` |
| T06 M1 formal | **30/30** | `m1-formal.json` |
| T07 业务 15 步 / 50 步 | **pass / pass** | long-business-15 / long-seq-50 |
| T07 100 步 | 63 派发跑完，终态 inconclusive（已补收尾） | long-seq-100 |
| T08 M2 smoke | 可运行项通过；burst/ocr 明确门控 | m2-smoke-30b |
| T09 Actor | screen_state 映射 + factory（默认 off） | 单测 8/8 |
| T10 OCR | 适配器已实现；本机无引擎 → capability=false（诚实） | `ocr.py` |
| T11 经验检索 | App/build/goal/过期过滤 | 单测 4/4 |
| T12 Decider | **keep_shadow_only**（样本不足，不进 canary） | decider-rules-only.json |
| T13 文档 | `agent-quickstart.md` + `runbook-rollback.md` | docs/ |

离线全量：**809/809**。

## 验收结论

**核心 Direct 交付完成**（T00–T08 主路径 + T04 2/3 Agent 闭环）。  
**增强层 T09–T13**：实现与离线验证完成；OCR 真机、Decider canary、M2 300 次大批按计划留待后续设备窗口。

安全硬指标：0 未授权动作、0 假成功声明、0 unknown 盲重放。

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
