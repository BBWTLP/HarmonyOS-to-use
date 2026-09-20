# v3.2 Phase 0 — Offline Baseline

采集日期：2026-09-20（Asia/Shanghai）
设备条件：**当前没有可用的 HarmonyOS 真机**。本文件只记录离线事实。

## 1. 仓库与工作树

| 项目 | 值 |
|---|---|
| 仓库 | `https://github.com/BBWTLP/HarmonyOS-to-use` |
| 分支 | `feat/runtime-foundation` |
| HEAD | `8c401c07eede3686349eaa50ead3d0abbd135577` |
| 与远端关系 | 与 `origin/feat/runtime-foundation` 一致 |
| 工作树 | **dirty**：Phase 1/2 的修改尚未提交（文件清单见第 4 节） |
| Python | 3.11.9（`.venv`） |
| `requirements.lock` sha256 | `18a991d559cde14ec9d7d2e06dffd4d9140c4ac949ccf7e61b7719e62a126055` |

工作树在基线采集时只有一个未跟踪目录 `.codex/`（本机工具配置，不属于项目产物，
未提交、未修改）。Phase 1/2 完成后工作树包含以下变更：

```text
M src/harmony_agent/decision/providers/__init__.py
M src/harmony_agent/decision/providers/base.py
M src/harmony_agent/decision/providers/decider.py
M src/harmony_agent/decision/router.py
M src/harmony_agent/evals.py
M src/harmony_agent/host.py
M src/harmony_agent/memory.py
M src/harmony_agent/state_builder.py
M src/harmony_runtime/service.py
M tests/test_agent_tasks.py
?? src/harmony_agent/decision/factory.py
?? src/harmony_agent/decision/tokens.py
?? tests/test_agent_boundaries.py
?? tests/test_agent_provider_factory.py
```

因为工作树是 dirty 的，**唯一有效的代码快照标识是文件哈希而不是 HEAD**。
需要冻结时运行 `scripts/evidence_manifest.py`；原始报告写入 `.runtime/`（不提交）。

## 2. 执行命令与结果

```powershell
git status
git branch --show-current
git rev-parse HEAD

.\.venv\Scripts\python.exe .\scripts\reproduce.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip check
```

### 2.1 变更前基线（HEAD，未含 Phase 1/2 修改）

| 项目 | 值 |
|---|---|
| 测试总数 | **346** |
| 通过 | 346 |
| 失败 | 0 |
| 错误 | 0 |
| skip | 0 |
| 耗时 | 32.591s |
| `pip check` | `No broken requirements found.` |
| 报告 | `.runtime/tests-current.json` |

### 2.2 变更后基线（Phase 1/2 完成时的工作树）

| 项目 | 值 |
|---|---|
| 测试总数 | **392** |
| 通过 | 392 |
| 失败 | 0 |
| 错误 | 0 |
| skip | 0 |
| 耗时 | 36.932s |
| `pip check` | `No broken requirements found.` |
| 报告 | `.runtime/tests-current.json` |

新增 46 项：

```text
tests/test_agent_provider_factory.py   35 项（Phase 1 提供方隔离与故障矩阵）
tests/test_agent_boundaries.py         11 项（Phase 2 Direct/Autonomous 边界）
```

### 2.3 离线阶段全部完成后的最终基线

| 项目 | 值 |
|---|---|
| 测试总数 | **562** |
| 通过 | 562 |
| 失败 | 0 |
| 错误 | 0 |
| skip | 0 |
| 耗时 | 43.176s |
| `pip check` | `No broken requirements found.` |
| 报告 | `.runtime/tests-current.json` |

说明：verbose 日志内含每次运行的耗时，因此每轮的 log sha256 都不同；
比对基线时以"测试数量 + 退出码"为准，不要用日志哈希做跨轮相等判断。

新增 216 项（相对变更前的 346）：

```text
tests/test_agent_provider_factory.py            35  Phase 1 提供方隔离
tests/test_agent_boundaries.py                  11  Phase 2 Direct/Autonomous
tests/test_observation_authority.py             14  Phase 3 观察授权
tests/test_agent_grounding_fixtures.py          23  Phase 4 Grounding fixtures
tests/test_visual_target_authority.py           19  Phase 5 视觉目标权威
tests/test_visual_providers.py                  36  Phase 6/7 OCR + VLM
tests/test_risk_taxonomy.py                     17  Phase 8 风险词表统一
tests/test_burst_wait_history_boundaries.py     10  Phase 9 边界
tests/test_agent_runner_boundaries.py           15  Phase 10 运行器边界
tests/test_m2_specification.py                  13  Phase 11 M2 规格校验
tests/test_offline_fault_matrix.py              14  Phase 12 故障矩阵
tests/test_install_migration.py                  9  Phase 13 安装/迁移/回滚
```

### 2.4 device-required 测试

离线套件里**没有** device-required 用例，因此 skip 数为 0——这不是“真机已验证”，
而是“真机用例不在这个入口里”。真机只读冒烟是显式命令
`tests/smoke_device.py`，本轮**未运行**（无设备）。

## 3. 历史测试数量口径修正

文档中曾同时出现 336 / 346 两个数字。以本次实测为准：

| 位置 | 原值 | 修正后 | 依据 |
|---|---|---|---|
| `docs/agent-status.md` A03 行 | 336 | **562**（当前）/ 346（变更前） | `.runtime/tests-current.json` |
| `docs/v3.1-progress-and-plan.md` | 346（无日期限定） | 标注为 v3.1 收尾口径，并指向本文件 | 同上 |
| `docs/v3.1-progress-and-plan.md` 命令注释 | `# 346 项回归` | `# 回归测试（数量随代码变化）` | 同上 |

规则：文档引用测试数量时必须写明采集时间与对应代码状态；数量本身不构成完成证据。

## 4. 这份基线能证明什么

可以证明：

```text
既有 offline 回归在改动前后都是绿色
v1 六工具的工具可见性、语义与协议未被改动
Agent 层在无 Decider 包/服务/模型时功能完整
```

不能证明：

```text
任何真机行为
任何延迟、稳定性、P50/P95 结论
真机 OCR/VLM/视觉目标执行
M0/M1/M2 正式验收
```

Mock、fake、fixture 只能证明 offline semantics，不能证明 device behavior。
