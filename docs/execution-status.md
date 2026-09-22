# 执行状态台账

> 主计划：`docs/superpowers/plans/2026-09-22-runtime-goal-delivery.md`
> 架构：`docs/architecture-rsi-decider-plan-2026-09-20.md`
> 核查基线：`81b5eab8bc11b8141a71d9a81be02a06a9a5d7a9`（feat/runtime-foundation）
> 本轮 run_id：`20260922T172053Z`
> 状态分类：`implemented` / `verified_offline` / `verified_device` / `blocked_dependency` / `deferred`

## 环境快照（T00）

| 项 | 值 |
|---|---|
| UTC 生成时间 | 2026-09-22T17:20:53Z 起 |
| 本地时区 | +08:00 |
| git SHA | `81b5eab8bc11b8141a71d9a81be02a06a9a5d7a9` |
| 分支 | `feat/runtime-foundation` |
| dirty | false |
| 与远端 | 一致（fetch 后无超前/落后） |
| Python | 3.11.9（`.venv`） |
| requirements.lock sha256 | `18a991d559cde14ec9d7d2e06dffd4d9140c4ac949ccf7e61b7719e62a126055` |
| source content_sha256 | `008bebf8071663710fe3137648e0af17a7f938ae10906f428bff343cdaeac672` |
| 权威 state-dir | `D:\music mv\HarmonyOS-to-use\.runtime\agent-state` |
| 设备 | 1 台，SGT-AL10 / OpenHarmony-6.1.1.120 / API 24 |
| App 前台（观察时） | `com.sina.weibo.stage` |
| 服务 | 受控重启后 reachable（loopback） |
| 模型 profile | Direct 六工具；Decider `local_off`；内部 Actor 默认不加载 |

证据目录：`.runtime/execution-20260922T172053Z/`（本地 retention，不进 Git 明文 token/截图）。

## T00 — 基线、服务与证据一致性（P0）

**状态：** `verified_offline` + `verified_device`（读屏可用，动态页有明确一致性错误）

**本轮证据：**

- `manifest-source.json`：HEAD=`81b5eab…`，dirty=false，275 文件
- `offline.json`：789 tests，exit 0，115.4s（**本版复跑**，不引用历史 789 记录）
- `pip check`：No broken requirements
- `service`：`service_reachable`；启动前确认旧 PID 已退出
- `baseline.json`：status=ok，设备元数据完整
- `probe.json`：catalog=109，capture_ms≈4843，`image_tree_consistent=false`
- 手工 observe：`screen_on=true`，`screen_locked=false`，`foreground_evidence.status=verified`，catalog≈125
- journal：actions 全为 `executed`（866）；**5 条历史 open incident 保留**，`unresolved_actions=[]`
- 历史 incident request_id：`act_a93cd74…`、`act_611c5827…`、`act_f3edc569…`、`act_76c5c651…`、`act_c50da952…`（完整值在本地 journal）

**明确问题（记账，不绕过）：**

1. 动态微博页采集夹取不一致：`consistency_reason=tree_changed_during_capture`，导致 `actionable=false`。图尺寸、前台、skew(295ms) 均正常，是夹取窗口内树指纹变化。属设计内保守拒绝，后续 T03/T05 处理，不在此放宽。
2. `cli doctor` 未把 `--state-dir` 传给内部 `report()`（落在 `cli.py` else 分支）；`service --state-dir` 可正确指向权威目录。记为小缺陷，T00 不阻塞。
3. 受控启动 serve 时观察到父 venv 进程会再拉起一个系统 Python 子进程并持有锁；服务功能正常且能读到项目代码，但启动形态不干净。记为待查（不阻塞 T00），后续在服务生命周期中收紧。
4. 5 条 open incident 对应 action 状态已是 `executed`，当前 **不** 构成 `unresolved` 写屏障；禁止用删库/换 state-dir 消除。对账收紧属 T02。

**通过判定：** 源码与服务对齐、依赖健康、本版离线基线完成、设备读屏可用且错误明确 → **T00 通过**。

**提交：** （完成后填写）

**下一动作：** T01 自动唤醒、解锁、续跑。

---

## T01 — 自动唤醒、无凭据解锁与任务续跑（P0）

**状态：** `verified_device`

**改动：**
- `device.py`：新增 `screen_sleep_confirmed` / `screen_ready_confirmed`（明确 off 即算睡眠；锁标志如实记录不写死）
- `scripts/accept_wake_unlock.py`：去掉 `off+locked` 硬编码；新增 `--natural-idle-seconds`；恢复指标；旧句柄拒绝；低风险 continuation；长静置后租约过期则重开会话
- `scripts/agent_harness.py`：两个 transport 的 `recovery_log`
- `tests/test_device_state.py`、`tests/test_recovery.py`：睡眠前置三态、旧句柄失效

**本轮证据：** 受控 5/5 `wake-unlock.json` passed；自然息屏 `wake-unlock-natural.json` passed（650.015s，session_reopened）；continuation 至少 1 次 verified；离线 test_device_state 4/4、test_recovery 5/5、test_snapshot_provider 44/44

**通过判定：** **T01 通过**。

**下一动作：** T02 未知写入对账收紧。

## T02 — 未知写入对账收紧（P0）

**状态：** 未开始

## T03 — 最短业务链与可信验收（P0）

**状态：** 未开始

## T04 — 首个真实 Agent 闭环（P0）

**状态：** 未开始

## T05 — 端到端性能（P1）

**状态：** 未开始

## T06 — 正式 M0/M1（P1）

**状态：** 未开始

## T07 — 恢复与长任务（P1）

**状态：** 未开始

## T08 — M2 可执行与可信判定（P1）

**状态：** 未开始

## T09 — 内部 auto Actor（P2）

**状态：** 未开始

## T10 — 实际视觉兜底（P2）

**状态：** 未开始

## T11 — 经验复用与 RSI 收益（P2）

**状态：** 未开始

## T12 — Decider 数据评估（P3）

**状态：** 未开始

## T13 — 扩展兼容与完整发布（P3）

**状态：** 未开始

## 全局约束核对

- [x] Runtime 是唯一设备写入口
- [x] 过期必须重新观察
- [x] unknown 禁止盲重试
- [x] 无通用设备回滚
- [x] 原始树/截图/token 留在本地 retention，不进 Git
- [x] 不在在线任务中自动写入全局经验库
- [x] 无密码息屏由 Runtime 自动恢复（待 T01 真机验收）
- [x] 不修改系统息屏时间，不用保活点击掩盖恢复
