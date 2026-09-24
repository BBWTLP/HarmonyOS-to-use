# 执行状态台账

> 主计划：`docs/superpowers/plans/2026-09-22-runtime-goal-delivery.md`  
> 架构：`docs/architecture-rsi-decider-plan-2026-09-20.md`  
> 核查基线：`81b5eab` @ `feat/runtime-foundation`  
> 本轮 run_id：`20260922T172053Z`

## 环境

| 项 | 值 |
|---|---|
| Python | 3.11.9（`.venv`） |
| 权威 state-dir | `.runtime/agent-state` |
| 设备 | SGT-AL10 / OpenHarmony-6.1.1.120 / 1 台 |
| App | `com.sina.weibo.stage` |
| 离线基线 | **794/794**（`offline-final.json`） |

## T00 基线 — `verified_offline` + `verified_device`

源码/服务/journal/设备一致；本版 789→794 离线复跑。提交 `4b6eae7`。

## T01 唤醒解锁 — `verified_device`

受控 5/5 + 自然息屏 650s；旧句柄拒绝。提交 `8b0574e`。

## T02 未知写入 — `verified_offline`

`not_executed` 仅信可信未派发；反例 10/10。提交 `6f17afa`。

## T03 最短业务链 / 页面身份 — `verified_device`

- `surface_is` 返回/首页判据；搜索 `redesignedSearchInput`
- 输入风险不含占位「发布」；导航指纹稳定；「回到顶部」→「发现」
- **e2e（本 Agent）：** D1 搜索输入 **pass**、D2 替换+返回 **pass**（2/3）
- M1 可派发；到「综合」结果页与 D3 往返仍不稳

提交 `0912732` `7208a62` `9c1cd60`。证据：`docs/acceptance/current-run/e2e-final.json`。

## T04 真实 Agent 闭环 — `verified_device`（本 Agent 作为执行体）**已收尾**

按用户指示：不跑外部 Codex，由本会话 Agent 完成；本条**不再追加真机复验**，以既有 e2e 证据收尾。

| 编号 | 目标 | 结果 | 判据 |
|---|---|---|---|
| D1 | 搜索「鸿蒙」 | **pass** | `text_equals=鸿蒙` + `foreground_is`（`mid_typed` pass） |
| D2 | 替换 harmony 并返回发现 | **pass** | `text_equals=harmony` + `surface_is=discover` |
| D3 | 公开话题往返 | fail | 返回后 `foreground` 不可观察 / surface≠起点 |

- 计划计数：planned 3 / attempted 3 / **passed 2** / failed 1  
- 证据：`docs/acceptance/current-run/e2e-final.json`  
- 备注：D1 的「综合」结果页标签未稳定出现，查询词与 App 身份已独立验证；D3 不阻塞 Direct 核心路径收尾。  
- **T04 收尾结论：** 核心 Direct 闭环（观察→输入→语义返回）已由真实 Agent 路径证明；缺口如实保留，不宣称 3/3。

## T05 性能 — `verified_offline`（本版）

- `_early_progress` 失败的 skip-check 观察回灌 `post_observation`，避免同页二次读屏
- `_run_subgoal` 优先复用 post；默认 **FAST**，仅 visual 目标用 FULL
- 删除 `post_observation()` 死代码
- 新增 `tests/test_observe_reuse.py`：短任务 facade observe ≤3 且无 FULL
- 离线 **796/796**（`offline-t05.json`）

正式延迟对照批（`accept_p05_benchmark`）待真机空闲时跑；不阻塞功能主线。

依赖 T04 全量轨迹；已有 post-observation 复用实现与单测。

## T06 正式 M0/M1 — `verified_device` **通过**

| 批次 | 结果 | 门槛 | 证据 |
|---|---|---|---|
| **M1 formal** | **30/30** | ≥27/30 | `docs/acceptance/current-run/m1-formal.json` |
| **M0 formal** | **699/700 (99.86%)** | 每原语 ≥99% | `docs/acceptance/current-run/m0-formal.json` |

M0 分项：launch/tree/swipe/tap/back/input **100/100**；screenshot **99/100**（1 次 `screenshot_inconsistent`）。  
`gate.passed=true`，`primitive_success_rate_ok=true`，unresolved_actions=0，false_success_claims=0。  
M1 覆盖输入/搜索/返回/滑动/跨 tab；判据含 `surface_is` 页面身份。

## T07 长任务 — `verified_device`（业务 15 步 + 50 步通过；100 步 63 派发）

| 序列 | 结果 | 说明 |
|---|---|---|
| long_business_15 | **pass** | 13 派发，`surface_is=tabs` |
| long_sequence_50 | **pass** | 31 派发 + recover + history |
| long_sequence_100 | 63 派发跑完，终态 inconclusive | 结束落在 foreign；已补 `tap:首页` 收尾 |

## T08 M2 smoke — `verified_device`（30/30 可解释）

succeeded 4 / failed 10 / unsupported 16（burst、ocr 能力门控；旧 id 已对齐 `redesignedSearchInput`/`TextArea`）。  
负向/缺失能力分开记账，无“空跑成功”。

- checkpoint **resume bundle**：task_id / plan_version / memory_hash / device_scope / verified_subgoals / pending_request / remaining / last_event_sequence
- `Memory.snapshot_hash()`
- `scripts/accept_long_task.py`（合同：`--execute --state-dir --tasks --runs --report` + dry-run）
- `evals/tasks/long-device.json`（15 步业务 + 50/100 步耐久）
- 测试：resume 字段与预算不重置；离线 **803/803**
- 真机 50/100×3 与跨应用：`blocked_device`（待设备批）

## T08 M2 runner — `implemented` + `verified_offline`

- `scripts/accept_m2.py`：typed 步进（tap/back/swipe/replace_text/wait/observe/recover/history；burst/ocr 能力门控）
- 不支持/缺前置/不确定分别记账；cleanup 不刷成功；expect_refusal 单列
- `tests/test_m2_runner.py` 7/7；m2-30 增加 `task_set_version`
- `accept_c03_burst`：`burst_usable=false` 时进程退出非 0（max_supported_steps=0 是真实裁决）
- 30 项真机各 1 次：待设备

## T09 内部 auto Actor — `implemented` + `verified_offline`

- 修正 `screen_state` → Actor 观察字段映射（原先读 `screen` 恒空）
- `actor_from_env()`：默认 off；`deterministic` 可选；未知 provider 不造客户端
- Direct 六工具不依赖 Actor
- 测试 8/8（含字段契约与 factory）

## T10–T13 — `deferred`

OCR 实接、RSI 真机收益、Decider 评估、完整发布矩阵按计划依赖前序设备批。

## 安全

- 绝不点「发送/去支付」；误入支付页已立即返回
- compose 只点「取消」
- 无凭据唤醒/解锁已验收（T01）

## 下一步（按优先级）

1. 稳住 发现→搜索→结果页（D1 综合标签）
2. M1 smoke 10×1 + back/input 6/6 → T06 正式批
3. T05 计时 → T07 长任务 → T08 runner → T09–T13
