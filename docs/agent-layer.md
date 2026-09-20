# harmony_agent：任务层与决策层实现说明

本文件说明 v3.1 架构中「Runtime 之上」的新增实现。Runtime（`src/harmony_runtime`）
继续独占设备写入；`src/harmony_agent` 只通过 Runtime 的公开会话接口观察、登记候选、
决策、派发和核验，不直接持有 HDC、驱动或设备队列。

## 模块

| 模块 | 职责 |
|---|---|
| `contracts.py` | schema_version 2.0 的任务、候选、决策契约；结构之外的语义校验 |
| `grounding.py` | 分层定位：稳定身份 → 文本/结构 → OCR → 图像区域；产出 `GroundedTarget` |
| `candidates.py` | 服务端候选登记器：候选 id、epoch、参数引用、有效期绑定 |
| `state_builder.py` | 有界决策状态（不超过 1024 token）与独立问题（choice/noul） |
| `decision/providers/rules.py` | 确定性规则基线（唯一候选、唯一稳定身份、无候选重观察） |
| `decision/providers/decider.py` | 本机 Decider 2B 适配器：固定 revision、熔断、原生分数保留 |
| `decision/router.py` | 规则优先、影子/白名单 canary 路由、拒答与升级 |
| `planner.py` | 计划与子目标；委派步骤语法；循环检测 |
| `supervisor.py` | 任务状态机、预算、事件、暂停/取消/恢复、对账出口 |
| `memory.py` | 近期窗口、事实/假设分离、压缩与证据索引 |
| `checker.py` | 只读最终核验；`id:`/`a11y:`/`type:` 目标键与程序谓词 |
| `artifacts.py` | 工件配额、TTL、脱敏导出；未决记录不参与回收 |
| `cli.py` | 只读 CLI：`agent tasks / task / artifacts / replay`，分页有界、缺失工件显式标记 |
| `evals.py` | 离线评测：开发/校准/保留集切分、覆盖率与错误分母、Brier/ECE、阈值只在校准集拟合 |

## 安全边界

- 模型不持有设备权限。Provider 只能返回候选键与原生分数；坐标、包名和输入内容
  只能来自任务参数库或观察，不能由模型提供。
- 候选绑定观察 id、控制 epoch、参数引用与有效期；派发前由 Runtime Guard 复核
  `target_ref` 与局部指纹。
- 未校准不得执行：`DecisionResult` 在 `provider != rules` 且 `route=execute` 时
  要求非空 `calibration_version`；影子模式下建议只记录、不派发。
- epoch 失效：暂停、取消、显式接管、恢复都会递增 epoch；过代候选、排队动作与
  模型结果在派发前被拒绝。
- 未知写入：派发结果分 `execution_status` 与 `verification_status` 两轴；未知执行
  进入 `RECONCILIATION_REQUIRED`，不自动重放。

## v1 兼容

- v1 六个 MCP 工具（`mobile_session/observe/act/burst/wait/history`）语义不变；
  `Target` 新增 `target_ref` 分支，与 `action_id/text/resource_id` 互斥。
- v2 工具（`mobile_run_task`、`mobile_task_status/control/events/result`、
  `mobile_decide`）仅在 `HARMONY_AGENT_TOOLS=1` 时注册。
- 观察结果新增 `controller_epoch`；会话结果新增 `controller_epoch` 与
  `capabilities.grounded_target`。

## 已验证行为

`tests/test_agent_contracts.py`、`tests/test_agent_grounding.py`、
`tests/test_agent_decision.py`、`tests/test_agent_tasks.py` 覆盖：契约拒绝、候选与
epoch 绑定、目标指纹定位、影子零派发、未校准 canary 拒绝、503/504 熔断、非有限分
数拒绝、候选外回答拒绝、预算耗尽、未知写入阻断、重启后任务转 PAUSED、工件
TTL/配额/路径防护。

## 已知限制

- OCR 与图像匹配层是可选能力：未配置引擎时明确返回不可用，不算作已支持。
- `local_shadow` 下快速模型不参与控制；只有完成校准并显式选择 `local_canary`
  才可能执行低风险候选。
- TaskRunner 与服务同进程，通过 Runtime 公开接口提交动作；跨进程 Runner 隔离尚未
  实现，因此「同一 Windows 账号下的进程分离不构成权限隔离」依然成立。
- Runtime 的 v1 关键词风险拦截仍会拒绝标签中含「提交/发送/删除」等词的控件；
  验收应用微博的搜索页控件 id 不含这些词，但该规则对真实控件名敏感，属过渡行为。

## 决策层观测到的行为（DF2 预备）

- 合成两候选/单候选中文状态：本机 Decider 4/4 选择正确，confidence 0.95–0.99、ECE 0.036。
- 真实微博页面状态（影子运行）：12 个样本中 6 个有候选的状态全部选「以上皆非」，confidence 0.84–0.89。
- 结论：差异来自状态与候选的渲染，而不是模型能力；校准对象应包含状态裁剪策略、候选描述措辞
  和拒答阈值，且必须用真实数据拟合（合成样本不能替代）。
