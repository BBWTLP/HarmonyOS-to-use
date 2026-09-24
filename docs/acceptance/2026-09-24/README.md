# 验收 run `20260924T152342Z`（证据重冻结）

| 项 | 值 |
|---|---|
| run_id | `20260924T152342Z` |
| code_revision | `6b78c76c19a9517b975fe3c80227d49170eee348` |
| 分支 | `feat/runtime-foundation`（= `origin/feat/runtime-foundation`） |
| 核查日期 | 2026-09-24 |
| 计划 | `docs/superpowers/plans/2026-09-24-project-next-steps-assessment.md` Task 1 |

## 状态词汇

| 词汇 | 含义 |
|---|---|
| `verified_offline` | 离线测试/脚本通过，且绑定本 run_id 的 revision |
| `verified_device` | 真机证据通过，且绑定 revision / task-set / device build / run_id |
| `historical` | 旧 revision 或未绑定证据，只作历史记录，不进入 current 数字 |
| `blocked_device` | 必须真机，当前无法执行或未执行 |
| `deferred` | 明确推迟，不阻塞 Direct 发布 |

**current 数字**必须解析到同一 HEAD 与 run_id。分母：`planned = attempted + unattempted`。

## 本 run 当前结果

| 项 | planned | attempted | passed | failed | blocked | unknown | 绑定 |
|---|---:|---:|---:|---:|---:|---:|---|
| 离线单测 | 809 | 809 | **809** | 0 | 0 | 0 | 本 run 实测，`offline.json` |
| pip check | 1 | 1 | 1 | 0 | 0 | 0 | exit 0 |
| compileall | 1 | 1 | 1 | 0 | 0 | 0 | src/tests/scripts |
| secret_scan | 286 文件 | 286 | clean | 0 findings | — | — | `secret-scan.json` |
| evidence_manifest | 1 | 1 | 1 | 0 | 0 | 0 | 293 文件哈希 |

依赖锁：`requirements.lock` sha256 `18a991d5…126055`。  
设备：SGT-AL10 / OpenHarmony-6.1.1.120 / API 24；App `com.sina.weibo.stage`。  
权威 state-dir：`.runtime/agent-state`（**不新建 journal、不删屏障**）。

## 历史设备证据（未绑定 revision，不进入 current 数字）

以下文件保留在 `docs/acceptance/current-run/`，**只作历史**。产物内无 `code_revision`，
无法证明等于 `6b78c76`；Task 2 若不能以外部源哈希绑定，则重跑。

| 证据 | 文件 sha256（前 16） | 结果 | 分母 | 状态 |
|---|---|---|---|---|
| M0 formal | `51f03767264365a8…` | 699/700 (99.86%) | 7×100 | `historical` |
| M1 formal | `6ef805da277ea3ee…` | 30/30 | 10×3 | `historical` |
| Agent e2e | `957ecf223b0cd294…` | 2/3 | D1–D3 | `historical` |
| 旧 offline 789/794 | — | 789–794 | — | `historical`（`81b5eab`） |

旧 `current-run/README.md` 中的 `81b5eab` / 794 口径**不再修改**，仅在本 run 标注为 historical。

## unknown 写入台账

- 权威目录：`.runtime/agent-state`
- journal：`.runtime/agent-state/journal.sqlite3`
- **open incident：33**（本 run 实测；旧文「5 条」已过时）
- unresolved action rows：0
- 策略：无 `postcondition_verified` 或可信 `not_executed` 证据不得关闭；禁止盲重放；
  禁止换 state-dir 清 journal。清单见 `unknown-incidents.json`。

## 产物

| 文件 | 用途 |
|---|---|
| `run-manifest.json` | 修订绑定清单（HEAD / lock / task-set / device / gate） |
| `offline.json` | 本 run 离线全量 |
| `secret-scan.json` | 本 run 密钥扫描 |
| `evidence-manifest.json` | 工作树文件哈希 |
| `unknown-incidents.json` | open incident 台账 |

## Task 1 Gate

- [x] 本地 HEAD = `origin/feat/runtime-foundation`
- [x] 809 离线 + pip check + compileall + secret_scan + evidence_manifest，退出码与哈希已落盘
- [x] 仅 revision/分母匹配的结果记入 current；旧 81b5eab/794 与 unbound formal 标 historical
- [x] 记录 open incident 与权威 state-dir；未新建 journal、未删屏障
- [x] 顶层 README 与 v3.2 索引指向本 run，并说明状态词汇
- [x] 文档提交后 `git status` 干净；current 数字解析到同一 HEAD/run_id；无新增 secret-scan finding

## 复现

```powershell
$ProjectRoot = 'D:\music mv\HarmonyOS-to-use'
$Py = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
Set-Location $ProjectRoot
& $Py scripts/reproduce.py --report docs/acceptance/2026-09-24/offline.json --log .runtime/logs/2026-09-24-offline.log
& $Py scripts/secret_scan.py --report docs/acceptance/2026-09-24/secret-scan.json
& $Py scripts/evidence_manifest.py --report docs/acceptance/2026-09-24/evidence-manifest.json
```
