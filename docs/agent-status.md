# v3.1 任务状态台账（实现仓库侧）

对照 `harmonyos-design-v2/v3.1/tasks.json` 的 36 项任务，记录本实现仓库的实际状态、
证据位置和已知限制。状态含义与开发计划一致：`planned`、`in_progress`、`implemented`
（代码存在）、`verified`（相应证据通过）、`blocked`、`deferred`。

本轮证据目录：`docs/acceptance/2026-09-24/`（权威 current run `20260924T152342Z` @ `6b78c76`）。
测试入口：`scripts/reproduce.py`。历史批次见 `docs/acceptance/2026-09-20/` 与
`docs/acceptance/current-run/`（已标 historical）。

**总览与后续排期见 [v3.1 交付现状、剩余开发计划与验收计划](v3.1-progress-and-plan.md)
与 [2026-09-24 发布收口与能力扩展计划](superpowers/plans/2026-09-24-project-next-steps-assessment.md)。**

**v3.2 台账见 [v3.2 进度台账](v3.2/progress.md)、[离线基线](v3.2/baseline-offline.md)、
[真机待办](v3.2/blocked-device.md)。** 本表记录 v3.1 任务编号。状态词汇含
`historical`（旧 revision / 未绑定证据，不进入 current 数字）。

| 任务 | 状态 | 证据 / 说明 |
|---|---|---|
| A01 工作树与验收证据索引 | verified | `scripts/evidence_manifest.py`、`docs/acceptance/2026-09-20/baseline-manifest.json`；按文件哈希冻结含未提交变更的工作树 |
| A02 v1 兼容与 v2 契约定义 | verified | `src/harmony_agent/contracts.py`、`docs/agent-layer.md`；`tests/test_agent_contracts.py` 与 `test_agent_planner.McpToolVisibilityTests` 证明 v1 六工具不变、v2 契约可校验 |
| A03 可复现测试入口 | verified | `scripts/reproduce.py`；`python -m unittest discover -s tests` 于 `6b78c76` / run `20260924T152342Z` 实测 **809** 项通过；历史数量随代码变化，以当轮 `docs/acceptance/<date>/offline.json` 为准 |
| A04 RC blocker 闭环 | verified | OFFLINE-2 终态与 result 原子可见（`TaskStore.finalize_task()`）；OFFLINE-1 单测与机器本地 token 解耦（`agent_fakes.make_decider_provider()`）。详见 `docs/v3.2/pre-device-gate.md` 第 9 节 |
| A05 M0 setup 会计与动态页面 | verified | setup 失败不再计入原语分母（`SetupUnavailable` / `valid_attempts` / `evaluate_gate()`）；`setup_act()` 拒绝后重新 observe + 重新定位；`has_weibo_evidence()` 阻止桌面被当作微博页面。详见 `docs/v3.2/pre-device-gate.md` 第 10 节 |
| B01 首个真实 Agent 客户端接入 | implemented | `scripts/agent_harness.py`（真实 MCP stdio 客户端）、`scripts/check_agent_link.py`；真实调用记录见 `docs/acceptance/2026-09-20/agent-client.json`（生成命令见同目录 README） |
| B02 十个低风险任务与独立判定器 | implemented | `evals/tasks/m1-weibo.json`（10 任务，含中文输入、同名/缺树、返回、应用内跳转）、`scripts/accept_m1_weibo.py` 的程序化判定器 |
| B03 M1 三十次自主任务验收 | historical（formal 30/30，revision 未绑定） | `docs/acceptance/current-run/m1-formal.json`；产物无 code_revision。Task 2 绑定或重跑前不计入 current |
| C01 受控分段性能基准 | implemented | 既有 `scripts/accept_m0_primitives.py` 计时口径与观察阶段耗时；本轮 FAST/FULL 实测见 `agent-client.json` |
| C02 减少设备往返与观察复用 | in_progress | 观察缓存与失效规则沿用既有实现；本轮修复「设备发现持有运行时锁」的阻塞缺陷（`runtime.session`） |
| C03 burst 与动态控件能力裁决 | planned | 既有 burst 语义与回归保持；3 秒 burst 边界未在本轮真机专项验证 |
| C04 M0 原语矩阵 | in_progress | `m0-primitives.json`：每原语 25 次，173/175（98.86%）；screenshot 与 back 各 1 次失败；样本量不足以证明 99%，正式需各 100 次 |
| D01 控制代次与控制权交接 | implemented | 会话结果与观察暴露 `controller_epoch`；过代候选在派发前被拒（`tests/test_agent_tasks.py`） |
| D02 未知执行可信对账 | implemented | 未知写入进入 `RECONCILIATION_REQUIRED` 并保留 `resolution_required`；不自动重放（`tests/test_agent_tasks.py`） |
| D03 任务范围与可信批准 | implemented | `Scope`、风险分级、模型不能签批准（`contracts.py`、`tests/test_agent_contracts.py`） |
| D04 取消重启与存储故障整合 | in_progress | 任务重启不续跑、取消终态仍报告未知屏障已覆盖；断电式退出与写满专项矩阵未完成 |
| E01 通用结构定位与候选登记 | verified | `grounding.py`、`candidates.py`、Runtime v2 `target_ref` 解析；`tests/test_agent_grounding.py` |
| E02 OCR 与图像区域后备 | implemented | `grounding.py` 的可插拔 OCR/图像层；**未配置引擎时明确不可用**，真机专项未做 |
| E03 视觉目标与 VLM 可选接入 | deferred | 未实现，无视觉点击放行；`GroundedTarget` 已保留视觉几何字段 |
| E04 定位泛化与三十任务集 | in_progress | 10 任务集已定义；L1/L2/L3 各 10 任务未完成 |
| F01 决策 Provider 与路由契约 | verified | `decision/providers/*`、`decision/router.py`、`tests/test_agent_decision.py` |
| F02 Decider 本机部署与容量验证 | in_progress | 沿用部署证据；本轮以真实服务参与影子运行 |
| F03 Decider Runtime 适配器与上下文绑定 | implemented | `decision/providers/decider.py`：固定 revision、候选成员校验、非有限分数拒绝、503/504 熔断、晚到候选按 epoch 失效 |
| F04 中文鸿蒙数据集与校准对照 | in_progress | 已采集 **45 个真实状态（20/13/12）**：规则覆盖 86.7%、放行错误 0；Decider 覆盖 0% → `keep_shadow_only`（`decider-dataset.json`、`decider-calibration.json`）。距 300 状态目标仍有差距 |
| F05 Decider 影子运行与收益评估 | verified（首批） | `decider-shadow.json`：12 个真实状态，零派发已验证；Decider 6/6 选「以上皆非」，与规则一致率 0，维持 `local_shadow`；端到端收益需 canary |
| F06 故障降级与有限启用准入 | in_progress | 熔断与回退已实现并单测；GPU 故障注入、版本漂移与 canary 回滚演练未完成 |
| G01 TaskSupervisor 与持久任务状态 | implemented | `supervisor.py`：幂等提交、状态机、事件、暂停/取消/恢复；`tests/test_agent_tasks.py` |
| G02 规划与有限子目标循环 | implemented | `planner.py`：委派步骤语法、预算不随重规划重置、循环检测 |
| G03 事实记忆与可追溯压缩 | implemented | `memory.py`：事实/假设分离、压缩保留约束与未决事件、证据索引 |
| G04 只读 Checker 与最终结果 | implemented | `checker.py`：程序谓词由代码判定、`inconclusive` 不计成功、拒绝写工具 |
| G05 多客户端与低风险 canary | planned | 仅实现一个真实 MCP 客户端 harness；未做三客户端对照 |
| G06 一百步与资源稳定性 | planned | 未执行 |
| H01 证据保留配额与隐私出口 | implemented | `artifacts.py`：TTL、配额、路径防护、脱敏导出、未决记录不回收 |
| H02 M2 三百次正式任务验收 | planned | smoke 已跑 30 项×1（4/10/16）；300 次正式批待设备窗口（2026-09-24 Task 4） |
| H03 安装迁移与回滚 | planned | 未执行 |
| H04 CLI 任务查看与证据回放 | verified | `src/harmony_agent/cli.py` + `harmony-runtime agent tasks/task/artifacts/replay`；只读、分页有界、缺失工件显式标记、不开放公网（`tests/test_agent_cli.py` 9 项） |
| H05 发布前集中缺陷修复 | in_progress | 本轮修复设备发现持有运行时锁、工件存储跨线程、MCP 客户端无超时三处缺陷 |
| H06 发布裁决与交付归档 | planned | 未执行 |

## 本轮已修复缺陷

1. `Runtime.session` 在持有运行时锁的情况下调用 HDC 发现设备，一次卡住会阻塞所有
   客户端；已移到锁外。
2. `ArtifactStore` 的 SQLite 连接未声明跨线程并缺少写锁，任务线程写证据时报
   `ProgrammingError`；已加锁并改为 `check_same_thread=False`。
3. 验收 harness 的 MCP 客户端没有读超时，服务端或设备卡住时永不返回；已加
   `read_timeout_seconds`，并把 MCP 服务端 stderr 落盘，避免管道写满阻塞。
4. `mobile_decide` 返回 pydantic 对象导致服务端 JSON 序列化抛错、客户端丢失响应；
   已改为 `model_dump()`，并加单测固定「建议结果可序列化」。
5. 设备 worker 被隔离（quarantined）时会话状态不体现，客户端只能反复收到
   `device_quarantined`；已新增 `device_state` / `worker_quarantined`，验收客户端
   打开会话时自动 `recover`。
6. 分层定位只按文本/身份匹配，未把可点击祖先登记为候选，真实页面上（文本标签
   不可点击）召回恒为 0；已按 `parent_action_id` 上溯到最近可点击祖先。

## 与设计不一致或被裁剪的部分

- TaskRunner 与服务同进程（仅通过 Runtime 公开接口提交动作），未实现独立进程隔离。
- OCR/VLM 未接入实际引擎，视觉点击未开放。
- 无云模型（`cloud_data_policy=disabled`），因此没有通用 LLM 规划基线对照。
- M2/M3 与发布相关任务未执行。
