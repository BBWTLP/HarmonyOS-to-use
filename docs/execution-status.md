# 执行状态台账

> 主计划：`docs/superpowers/plans/2026-09-22-runtime-goal-delivery.md`  
> 后续计划：`docs/superpowers/plans/2026-09-24-project-next-steps-assessment.md`  
> 架构：`docs/architecture-rsi-decider-plan-2026-09-20.md`  
> 核查基线：`6b78c76` @ `feat/runtime-foundation`  
> 本轮 run_id：`20260924T152342Z`（证据重冻结）  
> 历史 run：`20260922T172053Z`（`81b5eab`，已标 historical）

## 环境

| 项 | 值 |
|---|---|
| Python | 3.11.9（`.venv`） |
| 权威 state-dir | `.runtime/agent-state` |
| 设备 | SGT-AL10 / OpenHarmony-6.1.1.120 / 1 台 |
| App | `com.sina.weibo.stage` |
| 离线基线 | **809/809**（`docs/acceptance/2026-09-24/offline.json`，绑定 `6b78c76`） |
| open unknown incident | **33**（见 `docs/acceptance/2026-09-24/unknown-incidents.json`） |

## Task 2 Direct v1 设备门禁 — `blocked_device`（USB 断连）

设备在线时已完成 probe/baseline/service/doctor（见 `task2-device-gate.md`）。  
C03/唤醒/M1/C01/M0 因 **USB/HDC 断连**（`hdc list targets`=`[Empty]`）未能跑完；
记 `blocked_device`，不记能力失败。恢复后按 `task2-device-gate.md` 重跑。

## Task 1 证据重冻结（2026-09-24 计划）— `verified_offline` **完成**

- run_id `20260924T152342Z` @ `6b78c76` = origin
- 离线 809/809 + pip check + compileall + secret_scan clean + evidence_manifest
- open incident 实测 33（旧「5 条」过时）；权威 state-dir 保留
- M0/M1/e2e formal 标 `historical`（无 revision 绑定）
- 权威清单：`docs/acceptance/2026-09-24/run-manifest.json`

## Task 5 视觉范围 — `verified_offline` **tree-only**

无批准 OCR/VLM 引擎（`detect_engine()=None`）。发布画像 `ocr/vlm capability=false`；
M2 视觉任务不计成功。见 `docs/acceptance/2026-09-24/visual-scope-decision.md`。

## Task 6 安全/留存/运维 — `verified_offline`（真机故障矩阵部分 open）

33 条 unknown incident 全部 leave open；R2/R3 `approval_required` 保持 disabled；
legacy 明文恢复条件 quarantine。离线 55/55。见 `docs/acceptance/2026-09-24/safety-retention-ops.md`。

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

## T06 正式 M0/M1 — `historical`（真机结果保留，revision 未绑定）

| 批次 | 结果 | 门槛 | 证据 |
|---|---|---|---|
| **M1 formal** | **30/30** | ≥27/30 | `docs/acceptance/current-run/m1-formal.json` |
| **M0 formal** | **699/700 (99.86%)** | 每原语 ≥99% | `docs/acceptance/current-run/m0-formal.json` |

产物内无 `code_revision`/task-set 哈希，不能证明等于当前 HEAD `6b78c76`。  
按 2026-09-24 计划 Task 2：若不能以外部源哈希绑定，则在设备窗口重跑。

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

## T10 OCR — `implemented` + `verified_offline`

`harmony_runtime/ocr.py`：tesseract/easyocr 探测；无引擎时 boxes=[]，不造假框。真机视觉闭环 `blocked_dependency`（无 OCR 引擎）。

## T11 经验检索 — `implemented` + `verified_offline`

`experience_retrieval.py`：App/build/goal/过期过滤；无匹配合法。单测 4/4。

## T12 Decider — `verified_offline` 结论 **keep_shadow_only**

`evaluate_decider.py --rules-only`：样本 0 → `keep_shadow_only`（insufficient_holdout / calibration missing）。**不进 canary。**

## T13 文档与发布 — `implemented`（核心交付）

- `docs/agent-quickstart.md`
- `docs/runbook-rollback.md`（已有）
- 当轮验收 README 已更新
- M2 300 次正式批：`deferred`（需更长设备窗口）

## 最终验收

**核心交付完成；增强层完成离线验证。** 本 run 离线 **809/809** 绑定 `6b78c76`。  
权威清单：`docs/acceptance/2026-09-24/run-manifest.json`。  
历史设备批次（M0/M1/e2e）见 `docs/acceptance/current-run/`（已标 historical）。

## 安全

- 绝不点「发送/去支付」；误入支付页已立即返回
- compose 只点「取消」
- 无凭据唤醒/解锁已验收（T01）

## 下一步（按 2026-09-24 计划）

1. **Task 2** Direct v1 设备门禁：绑定或重跑 M0/M1；C01/C03；唤醒续跑
2. **Task 3** D3 诊断 + 真 Agent 3×3 + 15/50/100 步 ×3 + 跨应用
3. **Task 4** M2 30 任务诊断 → 30×10 正式批
4. **Task 5–7** 视觉范围 / 安全运维 / 安装与客户端
5. **Task 8** Decider/Actor（最后；`keep_shadow_only`）
