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

## T05 性能 — `in_progress`（T04 收尾后启动）

依赖 T04 全量轨迹；已有 post-observation 复用实现与单测。

## T06 正式 M0/M1 — `blocked_device`

M1 smoke 未达 9/9（发现/搜索栏导航不稳）。正式 10×3 与 M0×100 待 smoke 稳定后冻结版本再跑。

## T07 长任务 — `deferred`

checkpoint/unknown 屏障已有；50/100 步与跨应用待 T03/T06。

## T08 M2 runner — `deferred`

规格在 `evals/tasks/m2-30.json`；typed 步进 runner 待实现。

## T09–T13 — `deferred`

Actor 字段映射、OCR、RSI、Decider、发布矩阵按计划依赖前序。

## 安全

- 绝不点「发送/去支付」；误入支付页已立即返回
- compose 只点「取消」
- 无凭据唤醒/解锁已验收（T01）

## 下一步（按优先级）

1. 稳住 发现→搜索→结果页（D1 综合标签）
2. M1 smoke 10×1 + back/input 6/6 → T06 正式批
3. T05 计时 → T07 长任务 → T08 runner → T09–T13
