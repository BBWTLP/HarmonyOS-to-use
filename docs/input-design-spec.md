# HarmonyOS Mobile Agent Runtime

> 2026-09-19 补充：本文件保留 v1 历史计划。最新参考为用户提供的 harmonyos-design-v2；系统与首期验收以[用户当前真机](device-baseline.md)为准，不固定为 6.0/6.1。当前实现及 v2 任务映射见[审计记录](project-audit-2026-09-19.md)。
## 对标 Google Artemis 的鸿蒙通用手机 Agent Runtime —— 完整设计规格与开发计划

> 文档版本：v1.0  
> 基准日期：2026-09-18  
> 目标平台：HarmonyOS 6.x  
> 核心设备驱动：`devhelmkit`  
> 对外协议：MCP（Model Context Protocol）  
> 主要客户端：Codex / DeepSeek Harness / OpenCode / Claude Code / 其他 MCP Agent  
> 核心目标：让通用 Agent 能够像真实用户一样观察、理解、操作 HarmonyOS 真机，并稳定完成跨页面、跨 App、长链路复杂任务。

---

## 目录

1. [项目目标](#1-项目目标)
2. [非目标](#2-非目标)
3. [参考项目与技术选型](#3-参考项目与技术选型)
4. [能力目标与 Artemis 对标矩阵](#4-能力目标与-artemis-对标矩阵)
5. [总体架构](#5-总体架构)
6. [核心设计原则](#6-核心设计原则)
7. [运行模式：Direct / Flash / Pro](#7-运行模式direct--flash--pro)
8. [设备层设计](#8-设备层设计)
9. [会话与设备生命周期](#9-会话与设备生命周期)
10. [Observation Engine](#10-observation-engine)
11. [屏幕与页面指纹](#11-屏幕与页面指纹)
12. [Action Catalog 与 Set-of-Marks](#12-action-catalog-与-set-of-marks)
13. [Grounding Engine](#13-grounding-engine)
14. [动作模型与 Action Primitive](#14-动作模型与-action-primitive)
15. [Semantic Action](#15-semantic-action)
16. [Fast Action Burst](#16-fast-action-burst)
17. [Wait / Watch 本地等待机制](#17-wait--watch-本地等待机制)
18. [执行前校验与 Safety Net](#18-执行前校验与-safety-net)
19. [Execution Incident 与恢复](#19-execution-incident-与恢复)
20. [Verification / Checker](#20-verification--checker)
21. [Session Ledger / History / Replay](#21-session-ledger--history--replay)
22. [长任务与上下文压缩](#22-长任务与上下文压缩)
23. [MCP Server 设计](#23-mcp-server-设计)
24. [MCP Tool 详细规格](#24-mcp-tool-详细规格)
25. [Codex / DeepSeek Harness / OpenCode 接入](#25-codex--deepseek-harness--opencode-接入)
26. [视觉与 VLM 降级](#26-视觉与-vlm-降级)
27. [WebView 支持](#27-webview-支持)
28. [任务模型与 Pro Runtime](#28-任务模型与-pro-runtime)
29. [数据模型](#29-数据模型)
30. [存储设计](#30-存储设计)
31. [日志、Trace 与可观测性](#31-日志trace-与可观测性)
32. [安全模型](#32-安全模型)
33. [性能设计与 SLO](#33-性能设计与-slo)
34. [项目目录设计](#34-项目目录设计)
35. [配置设计](#35-配置设计)
36. [测试策略](#36-测试策略)
37. [Benchmark 设计](#37-benchmark-设计)
38. [开发阶段与验收标准](#38-开发阶段与验收标准)
39. [MVP / v1 / v2 完成标准](#39-mvp--v1--v2-完成标准)
40. [风险与技术难点](#40-风险与技术难点)
41. [明确禁止的设计](#41-明确禁止的设计)
42. [最终技术路线](#42-最终技术路线)
43. [参考资料](#43-参考资料)

---

# 1. 项目目标

本项目的目标不是做一个针对某个 App 的自动化脚本，而是构建一个**通用 HarmonyOS Mobile Agent Runtime**。

最终使用体验应当是：

```text
用户
 ↓
Codex / DeepSeek Harness / OpenCode / Claude Code
 ↓
自然语言任务
 ↓
MCP
 ↓
HarmonyOS Mobile Agent Runtime
 ↓
真实 HarmonyOS 手机
```

例如用户对 Codex 输入：

> 打开华为视频，找到我最近看过的电视剧，继续播放，并切换到蓝光清晰度。

Agent 应能够自主完成：

```text
理解任务
→ 获取手机当前状态
→ 打开华为视频
→ 判断当前页面
→ 进入“我的”
→ 找到观看历史
→ 识别目标内容
→ 打开详情页
→ 开始播放
→ 点击画面唤出播放器浮层
→ 在浮层消失前打开清晰度菜单
→ 选择蓝光
→ 验证蓝光状态
→ 返回成功结果
```

关键要求：

- 不需要为华为视频单独写 Workflow。
- 不需要预先写 XPath。
- 不需要用户告诉 Agent 每一步点哪里。
- Agent 可以根据截图、UI Tree、OCR 和视觉信息实时判断。
- 支持跨页面、跨 App、长链路任务。
- 支持页面变化、弹窗、加载失败、误点击后的恢复。
- 支持瞬时 UI，例如播放器控制栏、Toast、短暂菜单。
- 支持 100+ step 的长任务。
- 支持任务回放、历史检索和证据追踪。
- 安全边界不能只依赖模型 Prompt。

一句话定义：

> **Agent 负责“想”，Runtime 负责“看、找、点、等、验证、恢复、记忆”。**

---

# 2. 非目标

第一阶段明确不做：

- 不重新实现 `devhelmkit` 已有能力。
- 不开发 Android Driver。
- 不依赖 Android ADB。
- 不做某一个 App 的专用自动化框架。
- 不把内部 Planner 当作第一优先级。
- 不在第一版开发复杂 Web 控制台。
- 不把所有设备原语拆成几十个 MCP Tool。
- 不开放任意 `hdc shell` 给外部 Agent。
- 不允许模型绕过 Safety Engine。
- 不要求模型每一步都调用 VLM。
- 不以“模型推理越多越智能”为设计目标。

---

# 3. 参考项目与技术选型

## 3.1 Google Artemis

项目：

https://github.com/google/artemis

主要参考：

- Flash Profile
- Pro Profile
- Planner / Operator / Checker
- UI hierarchy + OCR + vision 的 Progressive Grounding
- Fast-action burst / click sequence
- Pre-execution Safety Net
- Execution Incident
- Session transcript ledger
- History compression
- `search_history`
- `replay_steps`
- MCP 集成
- 100+ step 长任务设计

不直接复用：

- Android Driver
- ADB 命令体系
- Android Accessibility / UIAutomator2
- Android package / activity 语义
- 任意 raw shell 接口

---

## 3.2 devhelmkit

项目：

https://github.com/yabi-zzh/devhelmkit

定位：

> **唯一 HarmonyOS Device Driver。**

使用能力：

- HDC 真机连接
- App 启停
- UI Tree
- XPath
- 控件查询
- Screenshot
- Tap / Swipe / Long Press / Drag
- 文本输入
- OCR
- CV / Image Matching
- WebView
- 事件监听
- 屏幕录制
- 多种输入设备能力

架构约束：

```text
只有 device/harmony/devhelm_adapter.py
允许 import devhelmkit
```

其他模块只依赖本项目定义的 `DeviceAdapter`。

---

## 3.3 HarmonyOS MCP 项目

参考：

https://github.com/XixianLiang/HarmonyOS-mcp-server

用途仅限于参考：

- HarmonyOS + MCP 的连接模式
- MCP Tool 组织方式
- 外部 Agent 调用设备能力的接口经验

不作为运行依赖。

---

## 3.4 DeepSeek Harness

项目：

https://github.com/deepseek-ai/deepseek-harness

其 MCP Client 可以将外部 MCP Server 的 Tools 注册为 Harness 原生工具。

因此本项目无需针对 DeepSeek Harness 编写专用 Driver。

---

## 3.5 OpenCode

文档：

https://opencode.ai/v2/docs/mcp-servers

OpenCode 支持 MCP Server。

因此同一个 HarmonyOS Runtime 可以直接被 OpenCode Agent 使用。

---

# 4. 能力目标与 Artemis 对标矩阵

| 能力 | Artemis | 本项目目标 |
|---|---|---|
| Android 真机 | 是 | 否 |
| HarmonyOS 真机 | 否 | 是 |
| 通用 UI 操作 | 是 | 是 |
| MCP | 是 | 是 |
| Codex 接入 | 是 | 是 |
| DeepSeek Harness | 间接 | 是 |
| OpenCode | 可接 | 是 |
| Flash 响应式执行 | 是 | 是 |
| Pro 长任务 | 是 | 是 |
| UI Tree Grounding | 是 | 是 |
| OCR | 是 | 是 |
| Vision fallback | 是 | 是 |
| Set-of-Marks | 类似能力 | 是 |
| Fast Action Burst | 是 | 是 |
| Transient UI | 是 | 是 |
| Safety Net | 是 | 是 |
| Execution Incident | 是 | 是 |
| Checker | 是 | 是 |
| History Compression | 是 | 是 |
| Search History | 是 | 是 |
| Replay | 是 | 是 |
| 100+ step | 是 | 是 |
| WebView | Android侧能力 | 是 |
| Raw shell | Pro 可用 | 默认禁止 |
| 支付/敏感操作策略 | 通用 Safety | 更严格结构化策略 |

本项目不是复制 Artemis 的代码，而是实现：

> **Artemis 级别的 Agent-Device Runtime 体验，并针对 HarmonyOS 重构底层。**

---

# 5. 总体架构

```text
┌─────────────────────────────────────────────────────┐
│                    Generic Agent                    │
│                                                     │
│ Codex / DeepSeek Harness / OpenCode / Claude Code  │
└─────────────────────────┬───────────────────────────┘
                          │
                          │ MCP
                          ▼
┌─────────────────────────────────────────────────────┐
│                 Harmony MCP Gateway                 │
│                                                     │
│ session / observe / act / burst / wait             │
│ history / replay / inspect / run_task               │
└─────────────────────────┬───────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────┐
│              Harmony Mobile Agent Runtime           │
│                                                     │
│  Session Manager                                    │
│  Observation Engine                                 │
│  Grounding Engine                                   │
│  Action Engine                                      │
│  Execution Engine                                   │
│  Safety Engine                                      │
│  Verification Engine                                │
│  Incident Manager                                   │
│  History / Ledger                                   │
└─────────────────────────┬───────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────┐
│                    Device Adapter                   │
│                                                     │
│                   HarmonyDevice                     │
└─────────────────────────┬───────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────┐
│                     devhelmkit                      │
│                                                     │
│ UI Tree / XPath / Screenshot / Gesture / OCR / CV  │
│ WebView / Device / Event / Recording               │
└─────────────────────────┬───────────────────────────┘
                          │
                          ▼
                         HDC
                          │
                          ▼
                   HarmonyOS 6 真机
```

---

# 6. 核心设计原则

## 6.1 MCP-first

Agent Integration Surface 只有 MCP。

不要为：

- Codex
- DeepSeek Harness
- OpenCode

分别开发不同接口。

---

## 6.2 Device Driver 单一化

所有设备行为经过：

```text
HarmonyDeviceAdapter
→ devhelmkit
→ HDC
```

禁止旁路。

---

## 6.3 Persistent Runtime

MCP Server 和设备连接必须常驻。

禁止：

```text
每次 Tool Call
→ 重新连接手机
→ 操作
→ 断开
```

---

## 6.4 Model-independent

Runtime 本身不绑定具体模型。

Direct 模式甚至不需要 Runtime 内部模型。

---

## 6.5 Structured-first, Vision-fallback

优先：

```text
UI Tree
→ Text
→ XPath
→ Bounds
→ OCR
→ CV
→ Vision
→ Coordinate
```

而不是截图一上来全部交给 VLM。

---

## 6.6 Safety-by-construction

高风险动作在 Tool / Action Catalog 层受限。

不能只靠：

> “请不要点击支付按钮”

---

## 6.7 Evidence-first

所有动作必须产生：

```text
Before Observation
Action
Result
After Observation
Verification
```

形成可回放证据链。

---

# 7. 运行模式：Direct / Flash / Pro

## 7.1 Direct MCP Mode

最重要的模式。

外部 Agent 自己就是 Operator：

```text
Codex
→ mobile_observe
→ 思考
→ mobile_act
→ mobile_observe
→ 思考
→ ...
```

Runtime 不运行内部 Planner。

这是第一版必须优先完成的模式。

---

## 7.2 Flash Mode

可以由 Runtime 内部接模型，也可以由外部 Agent 模拟。

特点：

- 单 Agent 响应式循环
- 无复杂 Planner
- Token 少
- 延迟低
- 适合确定性任务
- 历史通过压缩与召回保持长程能力

流程：

```text
Observe
→ Decide
→ Validate
→ Act
→ Verify
→ Observe
```

---

## 7.3 Pro Mode

第二阶段开发。

组成：

```text
Planner
↓
Operator
↓
Checker
```

Planner：

- 把复杂任务拆成里程碑。
- 定义 `verify` / `assert` 条件。
- 可根据执行结果更新计划。

Operator：

- 观察手机。
- 执行动作。
- 处理异常。
- 恢复。
- 使用 History / Replay。

Checker：

- 只读。
- 不允许操作设备。
- 检查 checkpoint。
- 结束前验证最终目标。

---

# 8. 设备层设计

定义：

```python
class DeviceAdapter(Protocol):
    def connect(self): ...
    def disconnect(self): ...

    def get_device_info(self): ...

    def current_app(self): ...
    def launch_app(self, bundle_name: str): ...
    def stop_app(self, bundle_name: str): ...

    def screenshot(self): ...
    def dump_hierarchy(self): ...

    def tap(self, x: int, y: int): ...
    def long_press(self, x: int, y: int, duration_ms: int): ...
    def double_tap(self, x: int, y: int): ...
    def swipe(self, start, end, duration_ms: int): ...
    def drag(self, start, end, duration_ms: int): ...

    def input_text(self, text: str): ...
    def clear_text(self): ...

    def back(self): ...
    def home(self): ...

    def screen_size(self): ...
    def orientation(self): ...
```

上层永远只看 `DeviceAdapter`。

---

# 9. 会话与设备生命周期

核心实体：

```text
DeviceSession
```

字段：

```text
session_id
device_id
created_at
status

device_info
screen_size
orientation

current_app

last_observation_id
last_fingerprint
last_action_id

active_incident_id
active_task_id

step_counter
```

生命周期：

```text
OPEN
↓
ACTIVE
↓
PAUSED
↓
CLOSED
```

约束：

> 一个设备默认同一时刻只允许一个 Active Session。

防止两个 Agent 同时点击同一手机。

---

# 10. Observation Engine

Observation 是整个 Runtime 的核心。

统一实体：

```text
Observation
```

字段：

```text
observation_id
session_id
timestamp

profile

current_app
screen_width
screen_height
orientation

screenshot
ui_tree

fingerprint

elements

ocr

som

metadata
```

---

## 10.1 FAST Profile

目标：

> 为 Agent 提供最低延迟的当前状态。

包含：

- Screenshot
- UI Tree
- Fingerprint
- Actionable Elements

不包含：

- OCR
- VLM
- 大量视觉分析

用于：

```text
Observe → Act
```

高频控制循环。

---

## 10.2 FULL Profile

包含：

- Screenshot
- UI Tree
- Action Catalog
- Set-of-Marks
- OCR
- Fingerprint
- 基础 Page Hint
- 可选 Metadata

用于：

- 页面不熟悉
- UI Tree 信息不足
- Agent 需要完整视觉 Grounding

---

## 10.3 TEMPORAL Profile

用于：

- Player control overlay
- Toast
- Dialog
- Context Menu
- Animated Menu
- 自动消失控件

动作后以短窗口采样：

```text
T+50ms
T+150ms
T+300ms
T+700ms
T+1500ms
T+2500ms（可选）
```

每个采样点不一定都 dump UI Tree。

可以：

```text
Frame
Frame
Frame + Tree
Frame + Tree
Frame
```

降低成本。

返回：

```text
TemporalObservation
├─ frame_0
├─ frame_1
├─ frame_2
├─ ...
└─ stable_state
```

---

# 11. 屏幕与页面指纹

定义：

```text
ScreenFingerprint
```

由以下部分组成。

## 11.1 visual_hash

使用：

- dHash
- pHash

解决：

> 屏幕视觉上有没有显著变化？

---

## 11.2 structural_hash

从 UI Tree 提取：

```text
node roles
stable ids
tree topology
bounds buckets
```

解决：

> 页面结构是否相同？

---

## 11.3 semantic_signature

包含：

```text
bundle
重要文字
导航状态
主要控件角色
Page hint
```

解决：

> 产品意义是否仍然是同一个页面？

---

## 11.4 content_hash

可选。

用动态内容：

```text
title
card text
selected episode
```

区分同一页面不同内容状态。

---

# 12. Action Catalog 与 Set-of-Marks

每次 Observe 后构建：

```text
ActionCatalog
```

只收录**可安全暴露给 Agent 的操作对象**。

Element：

```json
{
  "action_id": 17,
  "element_id": "E-abc",
  "text": "我的",
  "description": "",
  "role": "button",
  "bounds": [980, 2200, 1180, 2380],
  "clickable": true,
  "scrollable": false,
  "selector": {
    "strategy": "stable_id",
    "value": "..."
  },
  "risk": "R1"
}
```

---

## 12.1 SoM 渲染

Screenshot 叠加 Action ID：

```text
┌───────────────────────┐
│       [1] 搜索         │
│                       │
│ [8] 内容卡片           │
│                       │
│              [17] 我的 │
└───────────────────────┘
```

SoM 中的 `[17]` 必须严格对应：

```text
ActionCatalog[17]
```

不能由视觉模型临时编号。

---

# 13. Grounding Engine

目标：

> 把 Agent 的“点这个”可靠地转换为真实设备动作。

定位层级：

```text
L0 Stable Resource ID
↓
L1 Exact Text / Description
↓
L2 UI Tree semantic selector
↓
L3 XPath
↓
L4 Bounds from current tree
↓
L5 OCR
↓
L6 CV / template / feature matching
↓
L7 Vision locator
↓
L8 coordinate fallback
```

每次解析保存：

```text
grounding_strategy
confidence
source_observation_id
```

用于分析定位稳定性。

---

# 14. 动作模型与 Action Primitive

统一动作模型：

```text
Action
```

第一版 primitive：

```text
tap
tap_point
double_tap
long_press

swipe
scroll
drag

input_text
clear_text

back
home

launch_app
stop_app

wait
```

---

## 14.1 不拆 MCP Tool

不要：

```text
mobile_tap
mobile_swipe
mobile_drag
mobile_type
mobile_back
...
```

而是统一：

```text
mobile_act
```

参数内部使用 discriminated union。

这样 Tool Schema 更集中，模型上下文成本更低。

---

# 15. Semantic Action

在 Primitive 上增加稳定语义层：

```text
SemanticAction
```

例如：

```text
REVEAL_CONTROLS
DISMISS_DIALOG
SCROLL_DOWN
SCROLL_UP
FOCUS_SEARCH
OPEN_NOTIFICATION_PANEL
```

例：

```text
REVEAL_CONTROLS
```

可转换为：

```text
tap safe_center_region
```

Agent 不需要知道具体坐标。

Semantic Action 是可选优化，不应该阻塞第一版。

---

# 16. Fast Action Burst

这是解决模型回合延迟的核心。

普通：

```text
Observe
→ Agent
→ Tap
→ Observe
→ Agent
→ Tap
```

如果 UI 只有 2 秒：

第二次 Agent 推理还没结束，控件就消失。

因此：

```text
mobile_burst
```

输入：

```json
{
  "observation_id": "OBS-101",
  "actions": [
    {
      "type": "tap",
      "target": {"action_id": 12}
    },
    {
      "type": "wait",
      "ms": 100
    },
    {
      "type": "tap",
      "target": {"action_id": 21}
    }
  ]
}
```

Runtime 本地执行。

---

## 16.1 Burst 限制

默认：

```text
max_actions = 5
max_duration_ms = 3000
```

Burst 中禁止：

- 支付
- 删除数据
- 注销
- 提交金融交易
- 跨 App Launch（默认）

Burst 必须通过 Safety Policy。

---

# 17. Wait / Watch 本地等待机制

不要让 Agent：

```text
observe
observe
observe
observe
```

等待加载。

提供：

```text
mobile_wait
```

条件：

```text
change
stable
text_present
text_absent
element_present
element_absent
app_changed
fingerprint_changed
```

例如：

```json
{
  "condition": {
    "type": "text_present",
    "value": "观看历史"
  },
  "timeout_ms": 5000
}
```

Runtime 本地轮询。

这会显著降低 Agent round-trip。

---

# 18. 执行前校验与 Safety Net

执行链固定：

```text
Action Request
↓
Schema Validation
↓
Session Validation
↓
Fresh Observation Check
↓
Risk Classification
↓
Policy Check
↓
Target Re-grounding
↓
Precondition Check
↓
Write-Ahead Log
↓
Execute
↓
Postcondition Verify
```

不能改变顺序。

---

## 18.1 Stale Observation

Agent 根据：

```text
OBS-100
```

决定点击。

但现在已经：

```text
OBS-103
```

如果页面明显变化：

返回：

```text
STALE_OBSERVATION
```

而不是盲点。

---

# 19. Execution Incident 与恢复

失败不能只是：

```text
ERROR
```

创建：

```text
ExecutionIncident
```

字段：

```text
incident_id

opened_at
closed_at

action_id
action_type

expected
actual

failure_type

before_observation
after_observation

consecutive_failures

recovery_steps

status
```

failure_type：

```text
TARGET_NOT_FOUND
STALE_OBSERVATION
NO_SCREEN_CHANGE
UNEXPECTED_SCREEN
APP_CRASH
TIMEOUT
PERMISSION_DIALOG
NETWORK_ERROR
SAFETY_BLOCKED
UNKNOWN
```

Incident 在后续上下文中持续可见。

当一次后续动作成功：

```text
incident resolved
```

---

# 20. Verification / Checker

## 20.1 Action Verification

Action 可以携带：

```json
{
  "expect": {
    "screen_change": true
  }
}
```

或：

```json
{
  "expect": {
    "text_present": "蓝光"
  }
}
```

Runtime 本地检查。

---

## 20.2 Pro Checker

只在 Pro Mode 使用。

Checker：

- 可 Observe
- 可读取 History
- 可 Replay
- 不可 Act

Verification Level：

```text
off
final
checkpoints
strict
```

默认：

```text
final
```

---

# 21. Session Ledger / History / Replay

每一步形成：

```text
StepRecord
```

字段：

```text
step_id
session_id
timestamp

observation_before
decision_summary

action
execution_result

observation_after
verification

incident_id
duration_ms
```

Ledger：

> append-only。

---

## 21.1 Replay

支持：

```text
mobile_replay
```

输入：

```text
step range
```

返回：

- Before screenshot
- Action
- After screenshot
- Verification
- Timing

---

# 22. 长任务与上下文压缩

目标：

支持 100+ step。

不能把所有 Screenshot 和 UI Tree 一直送给模型。

三层历史：

```text
Recent Window
Compressed Era
Raw Ledger
```

---

## 22.1 Recent Window

例如：

最近 8–12 Step。

保留完整细节。

---

## 22.2 Compressed Era

旧步骤压缩：

```text
T+00:00 ~ T+02:14
打开华为视频，从首页进入“我的”，进入观看历史。
期间出现一次网络重试，随后恢复。
```

包含：

```text
steps 1-23
apps
major pages
important actions
incidents
outcomes
```

---

## 22.3 Raw Ledger

永久保存。

需要时：

```text
search_history
replay_steps
```

召回。

---

# 23. MCP Server 设计

MCP 是本项目核心产品接口。

第一版只暴露少量高能力 Tool：

```text
mobile_session

mobile_observe

mobile_act

mobile_burst

mobile_wait

mobile_history

mobile_replay

mobile_inspect
```

Pro 阶段增加：

```text
mobile_run_task
mobile_task_status
mobile_task_cancel
```

---

# 24. MCP Tool 详细规格

## 24.1 mobile_session

Action：

```text
open
status
close
```

示例：

```json
{
  "action": "open",
  "device_id": "auto"
}
```

返回：

```json
{
  "session_id": "S-001",
  "device_id": "...",
  "resolution": [1216, 2688],
  "current_app": "...",
  "status": "ACTIVE"
}
```

---

## 24.2 mobile_observe

输入：

```json
{
  "session_id": "S-001",
  "profile": "FAST"
}
```

profile：

```text
FAST
FULL
TEMPORAL
```

返回：

```text
observation_id
current_app
screen_size
fingerprint
elements
screenshot
incident
```

FULL 额外：

```text
OCR
SoM
```

---

## 24.3 mobile_act

推荐 Schema：

```json
{
  "session_id": "S-001",
  "observation_id": "OBS-18",
  "action": {
    "type": "tap",
    "target": {
      "action_id": 7
    }
  },
  "expect": {
    "screen_change": true
  }
}
```

---

## 24.4 mobile_burst

输入：

```json
{
  "session_id": "S-001",
  "observation_id": "OBS-18",
  "actions": [
    {
      "type": "tap",
      "target": {"action_id": 13}
    },
    {
      "type": "wait",
      "ms": 120
    },
    {
      "type": "tap_point",
      "target": {
        "region": "top_right_safe"
      }
    }
  ]
}
```

---

## 24.5 mobile_wait

支持：

```text
change
stable
text_present
text_absent
element_present
element_absent
app_changed
```

---

## 24.6 mobile_history

操作：

```text
recent
search
eras
```

例如：

```json
{
  "action": "search",
  "query": "观看历史"
}
```

---

## 24.7 mobile_replay

```json
{
  "from_step": 21,
  "to_step": 28
}
```

---

## 24.8 mobile_inspect

用于只读诊断：

```text
session
device
current_screen
element
incident
performance
```

禁止 raw shell。

---

# 25. Codex / DeepSeek Harness / OpenCode 接入

## 25.1 Codex

使用 stdio MCP：

```text
Codex
↓
Harmony MCP Server
↓
Runtime
```

Runtime 不包含 Codex 特有逻辑。

---

## 25.2 DeepSeek Harness

DeepSeek Harness 的 MCP client 会将外部 Server Tool 注册为 Harness 原生 Tool。

推荐：

```text
transport = stdio
```

本机使用。

以后远程：

```text
streamable-http
```

---

## 25.3 OpenCode

配置概念：

```json
{
  "mcp": {
    "servers": {
      "harmony": {
        "type": "local"
      }
    }
  }
}
```

具体命令指向：

```text
python -m hma.mcp
```

---

# 26. 视觉与 VLM 降级

定义：

```text
VisionProvider
```

实现可以是：

```text
NoneVisionProvider
ExternalAgentVision
OpenAICompatibleVision
LocalQwenVL
```

默认 Direct Mode：

> 优先直接把截图交给外部 Agent。

Runtime 不强制调用自身 VLM。

---

## 26.1 Vision Locator

UI Tree / OCR 均无法定位时：

输入：

```text
Screenshot
Target Description
```

输出：

```text
normalized bounding box
confidence
```

然后 Runtime 做坐标安全校验。

---

# 27. WebView 支持

Observation 需要检测：

```text
Native
WebView
Hybrid
```

WebView 情况：

```text
DOM
→ element
→ click
```

优先于：

```text
Screenshot
→ vision
→ coordinates
```

这样性能和稳定性都更好。

---

# 28. 任务模型与 Pro Runtime

Pro 阶段定义：

```text
Task
```

字段：

```text
task_id
session_id

goal
constraints

plan
milestones

verification_level

max_steps

status
result
```

---

## 28.1 Planner

输入：

```text
Goal
Current Device State
Constraints
```

输出 Markdown Plan：

```markdown
- [ ] 打开华为视频
- [ ] 进入“我的”
  - verify: 页面存在“观看历史”
- [ ] 打开观看历史
- [ ] 打开最近内容
- [ ] 播放
- [ ] 切换蓝光
  - assert: 当前清晰度为蓝光
```

---

## 28.2 Operator

消费：

```text
Plan
Current Observation
History
Incident
```

并执行动作。

---

## 28.3 Checker

对：

```text
verify
assert
final goal
```

做只读验证。

---

# 29. 数据模型

核心实体：

```text
Device
Session
Observation
Fingerprint
Element
Action
Step
Incident
HistoryEra
Artifact
Task
Plan
Checkpoint
Verification
```

---

## 29.1 Observation

```text
observation_id
session_id

timestamp
profile

bundle_name

screenshot_artifact_id
ui_tree_artifact_id

fingerprint_id

element_count

ocr_status
som_status
```

---

## 29.2 Action

```text
action_id
session_id
source_observation_id

type
target
payload

risk_level

expected_postcondition
```

---

## 29.3 Step

```text
step_id

observation_before
action_id
execution_result
observation_after

verification_result

latency
```

---

# 30. 存储设计

第一版：

```text
SQLite
+
filesystem artifacts
```

数据库存结构数据。

Screenshot / UI Tree：

```text
artifacts/
```

保存。

Artifact：

```text
artifact_id
type
path
sha256
created_at
size
```

---

## 30.1 Session 目录

```text
data/
└── sessions/
    └── S-20260918-0001/
        ├── session.json
        ├── screenshots/
        ├── trees/
        ├── som/
        ├── video/
        └── exports/
```

---

# 31. 日志、Trace 与可观测性

日志必须区分：

```text
DEVICE
OBSERVE
GROUND
ACTION
SAFETY
VERIFY
INCIDENT
MCP
TASK
HISTORY
```

每次 Step 输出 Timing：

```text
observation_ms
grounding_ms
safety_ms
dispatch_ms
wait_ms
verify_ms
total_ms
```

只有有 Timing，性能优化才有意义。

---

# 32. 安全模型

风险等级：

```text
R0 观察

R1 普通导航

R2 修改数据/发送/删除/账号设置

R3 金融/支付/不可逆敏感操作
```

默认：

```text
R0 AUTO
R1 AUTO
R2 POLICY
R3 APPROVAL_REQUIRED
```

---

## 32.1 支付

默认：

> 支付确认类目标不进入可自动执行 Action Catalog。

如果以后需要测试环境：

必须显式：

```text
allow_financial_test_actions = true
test_account_confirmed = true
device_allowlisted = true
```

三项同时成立。

---

## 32.2 Raw Shell

MCP 永远不直接暴露：

```text
hdc shell <arbitrary command>
```

诊断必须结构化。

---

# 33. 性能设计与 SLO

性能来源：

```text
Total latency
=
Agent reasoning
+
MCP
+
Observation
+
Grounding
+
Device dispatch
+
UI response
```

Runtime 只能控制后五项中的大部分。

---

## 33.1 优化手段

### Persistent MCP Process

常驻。

### Persistent Device Connection

常驻。

### Observation Cache

保存最近 Observation。

### Lazy OCR

FAST 不 OCR。

### Lazy SoM

只有需要视觉编号时生成。

### Differential Tree

结构 hash 未变时避免重复完整解析。

### Local Wait

页面等待不经过模型。

### Burst

瞬时多步动作本地执行。

### Async Artifact Persistence

截图落盘与下一次部分操作解耦，但必须确保事实顺序一致。

---

## 33.2 初始 SLO

Runtime 层目标：

```text
MCP local overhead:
p50 < 30ms

Action dispatch:
p50 < 150ms
p95 < 300ms

FAST Observe:
p50 < 800ms
p95 < 1500ms

Burst inter-action gap:
尽量 < 100ms
```

FULL / TEMPORAL 单独统计。

这些指标：

> 不包含外部 LLM 推理时间。

---

# 34. 项目目录设计

```text
harmony-mobile-agent/
│
├── pyproject.toml
├── README.md
├── LICENSE
├── CHANGELOG.md
│
├── src/
│   └── hma/
│       │
│       ├── device/
│       │   ├── protocol.py
│       │   └── harmony/
│       │       └── devhelm_adapter.py
│       │
│       ├── session/
│       │   ├── manager.py
│       │   └── models.py
│       │
│       ├── observation/
│       │   ├── engine.py
│       │   ├── profile.py
│       │   ├── temporal.py
│       │   ├── fingerprint.py
│       │   └── som.py
│       │
│       ├── grounding/
│       │   ├── catalog.py
│       │   ├── resolver.py
│       │   ├── selector.py
│       │   ├── ocr.py
│       │   ├── cv.py
│       │   └── vision.py
│       │
│       ├── action/
│       │   ├── models.py
│       │   ├── semantic.py
│       │   └── burst.py
│       │
│       ├── execution/
│       │   ├── executor.py
│       │   ├── precondition.py
│       │   └── postcondition.py
│       │
│       ├── safety/
│       │   ├── policy.py
│       │   ├── risk.py
│       │   └── approval.py
│       │
│       ├── incident/
│       │   ├── manager.py
│       │   └── models.py
│       │
│       ├── verification/
│       │   ├── verifier.py
│       │   └── checker.py
│       │
│       ├── history/
│       │   ├── ledger.py
│       │   ├── compression.py
│       │   ├── search.py
│       │   └── replay.py
│       │
│       ├── storage/
│       │   ├── sqlite.py
│       │   └── artifact.py
│       │
│       ├── vision/
│       │   ├── protocol.py
│       │   ├── external.py
│       │   └── openai_compatible.py
│       │
│       ├── task/
│       │   ├── models.py
│       │   ├── planner.py
│       │   ├── operator.py
│       │   └── runner.py
│       │
│       ├── mcp/
│       │   ├── server.py
│       │   └── tools/
│       │
│       ├── diagnostics/
│       │
│       ├── config/
│       ├── models/
│       ├── errors/
│       └── cli.py
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── device/
│   ├── mcp/
│   └── scenarios/
│
├── benchmarks/
│   ├── observe_fast.py
│   ├── observe_full.py
│   ├── action_dispatch.py
│   ├── tree_dump.py
│   ├── screenshot.py
│   ├── burst.py
│   └── wait.py
│
├── examples/
│
└── docs/
    ├── architecture.md
    ├── mcp.md
    ├── safety.md
    ├── benchmark.md
    └── development.md
```

---

# 35. 配置设计

示例：

```yaml
runtime:
  data_dir: ./data

device:
  adapter: devhelmkit
  device_id: auto
  persistent_connection: true

observation:
  default_profile: FAST

  fast:
    screenshot: true
    tree: true
    ocr: false
    som: false

  full:
    screenshot: true
    tree: true
    ocr: true
    som: true

  temporal:
    sample_ms:
      - 50
      - 150
      - 300
      - 700
      - 1500

grounding:
  strategies:
    - stable_id
    - text
    - tree
    - xpath
    - bounds
    - ocr
    - cv
    - vision
    - coordinate

burst:
  max_actions: 5
  max_duration_ms: 3000

history:
  recent_steps: 10
  compress_after_steps: 20

safety:
  default_r2: ask
  default_r3: deny

mcp:
  transport: stdio
```

---

# 36. 测试策略

## 36.1 Unit Tests

覆盖：

- Fingerprint
- Action Schema
- Risk Classification
- Element Filtering
- Grounding Strategy
- Burst Validation
- History Compression
- Incident Lifecycle

---

## 36.2 Integration Tests

Mock Device：

```text
Observation
→ Action
→ State change
```

测试完整执行链。

---

## 36.3 Device Tests

真实 HarmonyOS：

- screenshot
- tree
- tap
- long_press
- drag
- input
- back
- launch
- orientation

---

## 36.4 Scenario Tests

至少：

### Settings

```text
打开设置
→ 搜索某设置
→ 进入
→ 返回
```

### Browser

```text
打开浏览器
→ 输入 URL
→ 页面加载
→ 点击链接
```

### Gallery

```text
打开图库
→ 进入相册
→ 打开图片
→ 返回
```

### Video

```text
打开视频
→ 播放
→ 唤出 Overlay
→ 打开清晰度
```

---

# 37. Benchmark 设计

每次 Release 运行。

## 37.1 Device Benchmark

```text
connect
screenshot
dump_hierarchy
tap
swipe
input
```

---

## 37.2 Runtime Benchmark

```text
FAST observe

FULL observe

SoM rendering

fingerprint

grounding

burst

wait_for_change
```

输出：

```text
count
mean
p50
p90
p95
p99
max
```

---

## 37.3 Agent Benchmark

构建 HarmonyOS Agent Test Set。

任务类型：

```text
启动 App
查找设置
搜索
文本输入
滚动
返回
Dialog
播放器
瞬时 UI
WebView
多 App
长任务
```

指标：

```text
Task Success Rate

Grounding Accuracy

Action Success Rate

Incident Recovery Rate

Average Steps

Agent Turns

Runtime Latency

Total Duration
```

---

# 38. 开发阶段与验收标准

## Phase 0 — Repository & Contracts

完成：

- 新仓库
- Python package
- DeviceAdapter Protocol
- Error Model
- Config System
- Logging
- CI
- License review

验收：

```text
package 可安装
tests 可运行
devhelmkit 只存在一个 import point
```

---

## Phase 1 — Persistent Device Runtime

完成：

- device discovery
- persistent connection
- launch / stop
- screenshot
- tree
- tap
- swipe
- input
- back
- home

验收：

连续执行大量基础操作无明显连接泄漏。

---

## Phase 2 — Observation Engine

完成：

- Observation
- FAST
- FULL
- visual hash
- structural hash
- element extraction

验收：

每次 Observe 有唯一：

```text
observation_id
```

且所有后续 Action 必须绑定 Observation。

---

## Phase 3 — MCP Direct Control

完成：

```text
mobile_session
mobile_observe
mobile_act
mobile_wait
```

验收：

至少一个通用 Agent 可以：

> 打开设置，进入指定页面，再回到桌面。

不使用 App-specific code。

这是第一个关键里程碑。

---

## Phase 4 — Action Catalog + SoM

完成：

- Action Catalog
- risk filtering
- bounds mapping
- SoM screenshot

验收：

SoM index → element 映射准确率：

```text
> 99%
```

---

## Phase 5 — Grounding Pipeline

完成：

```text
stable_id
text
tree
xpath
bounds
ocr
cv
vision
coordinate
```

验收：

结构树不完整场景仍能完成导航。

---

## Phase 6 — Temporal Observation + Burst

完成：

- TEMPORAL
- mobile_burst
- local wait
- transient UI state detection

验收任务：

```text
打开视频
→ 播放
→ 点击画面唤出浮层
→ 在浮层消失前打开清晰度菜单
```

必须稳定完成。

这是第二个关键里程碑。

---

## Phase 7 — Safety Net + Verification

完成：

- risk model
- stale observation
- precondition
- postcondition
- local verification

验收：

旧截图不能误点新页面。

高风险操作不能通过普通 Action 自动执行。

---

## Phase 8 — Ledger + Incident + Replay

完成：

- Step Ledger
- Artifact store
- Incident
- Replay

验收：

任意一次失败都可以恢复：

```text
Before
Action
After
Error
Recovery
```

完整链路。

---

## Phase 9 — History Compression

完成：

- Recent Window
- Era Compression
- Search History
- Replay Steps

验收：

执行 100+ step 后：

Agent 不需要完整原始历史即可继续任务。

---

## Phase 10 — Multi-Agent Client Compatibility

测试：

```text
Codex
DeepSeek Harness
OpenCode
```

全部使用：

> 同一个 MCP Server。

验收：

Runtime 不包含任何客户端专用逻辑。

---

## Phase 11 — Pro Runtime

最后开发：

- Planner
- Operator
- Checker
- mobile_run_task
- checkpoints
- final verification

验收：

输入自然语言复杂任务，Runtime 可以独立运行。

---

# 39. MVP / v1 / v2 完成标准

## MVP

必须有：

```text
HarmonyOS
↓
devhelmkit
↓
Persistent Runtime
↓
Observation
↓
Action Catalog
↓
MCP
↓
Generic Agent
```

成功标准：

用户在通用 Agent 中输入：

> 打开华为视频并进入我的观看历史。

Agent 可以自主完成。

---

## v1 —— HarmonyOS Artemis Flash 级能力

包括：

- Direct MCP
- FAST / FULL / TEMPORAL
- SoM
- Structured Grounding
- OCR / Vision fallback
- Burst
- Wait
- Safety
- Incident
- History
- Replay
- 100+ step

成功标准：

> 通用 Agent 能稳定执行复杂跨页面任务。

---

## v2 —— HarmonyOS Artemis Pro 级能力

增加：

- Planner
- Operator
- Checker
- Living Plan
- Checkpoint
- Final Verification
- Optional Report
- Long-running loop

成功标准：

用户只需要：

```text
mobile_run_task(goal)
```

Runtime 可以自主完成复杂流程。

---

# 40. 风险与技术难点

## 40.1 UI Tree 不完整

解决：

```text
OCR
→ CV
→ Vision
→ coordinate fallback
```

---

## 40.2 瞬时控件

解决：

```text
TEMPORAL
+
BURST
```

---

## 40.3 模型慢

解决：

- Runtime 本地等待
- Burst
- Persistent connection
- 不让模型处理低级重复动作

---

## 40.4 App 页面更新

解决：

> dynamic grounding，不写死 XPath。

---

## 40.5 坐标误操作

解决：

- observation_id
- fingerprint check
- normalized coordinates
- target region safety

---

## 40.6 长历史

解决：

```text
Ledger
→ compression
→ search
→ replay
```

---

## 40.7 Agent 越权

解决：

```text
Action Catalog
+
Risk Policy
+
Precondition
```

而不是 Prompt。

---

## 40.8 Device Connection Loss

必须有：

```text
DEVICE_LOST
```

Incident。

支持：

```text
reconnect
resume session
```

但不能偷偷重新执行高风险动作。

---

# 41. 明确禁止的设计

禁止：

```text
每个 App 写一套 Workflow
```

禁止：

```text
每一个 action 一个 MCP tool
```

禁止：

```text
模型直接生成 HDC 命令
```

禁止：

```text
模型任意 shell
```

禁止：

```text
所有截图强制跑 VLM
```

禁止：

```text
每次 tool call 重新连接设备
```

禁止：

```text
Agent 根据旧 screenshot 直接执行 coordinate
```

禁止：

```text
第一阶段先造内部 Planner
```

禁止：

```text
为了“智能”牺牲执行确定性
```

---

# 42. 最终技术路线

开发路线必须严格按照：

```text
Device
↓
Persistent Session
↓
Observation
↓
Direct MCP
↓
Action Catalog
↓
SoM
↓
Grounding
↓
Temporal + Burst
↓
Safety + Verification
↓
Incident
↓
Ledger + Replay
↓
History Compression
↓
Multiple Agent Clients
↓
Pro Planner / Operator / Checker
```

不要颠倒。

尤其不要：

```text
先做 Planner
```

因为 Codex、DeepSeek Harness、OpenCode 自己已经是强 Agent。

本项目真正的核心资产应该是：

```text
Observation

Grounding

Action

Burst

Wait

Safety

Verification

Incident

History

Replay

MCP
```

模型越强：

> Runtime 会自然变强。

这才是长期正确的架构。

---

# 43. 参考资料

## Google Artemis

https://github.com/google/artemis

重点阅读：

- README
- README_CN
- `mcp_server/`
- `mcp_server/rules.md`
- Flash / Pro
- Explorer
- Safety Net
- Execution Incident
- History / replay

---

## devhelmkit

https://github.com/yabi-zzh/devhelmkit

核心用途：

> HarmonyOS Device Driver。

重点阅读：

- README
- API Reference
- BaseDriver
- Harmony Driver
- UI Tree
- XPath
- OCR
- CV
- WebView
- events

---

## HarmonyOS MCP Reference

https://github.com/XixianLiang/HarmonyOS-mcp-server

仅参考 MCP integration pattern。

---

## DeepSeek Harness

https://github.com/deepseek-ai/deepseek-harness

MCP Client：

https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/mcp/mcp-client

---

## OpenCode MCP

https://opencode.ai/v2/docs/mcp-servers

---

# Appendix A — 第一版 MCP 最小接口

```text
mobile_session
mobile_observe
mobile_act
mobile_wait
```

只用这 4 个 Tool 就应该能验证架构。

只有验证 Direct MCP 成功之后：

再增加：

```text
mobile_burst
mobile_history
mobile_replay
mobile_inspect
```

最后才增加：

```text
mobile_run_task
```

---

# Appendix B — 第一批验收任务

## T01 设置页

> 打开设置，找到蓝牙页面，再回到桌面。

验证：

- structure grounding
- text
- navigation
- back/home

---

## T02 浏览器

> 打开浏览器，进入一个网页并点击一个链接。

验证：

- input
- WebView
- wait

---

## T03 图库

> 打开图库，进入最近照片，打开第一张图片，再返回。

验证：

- image-heavy UI
- CV / visual fallback

---

## T04 视频播放器

> 打开一段视频，开始播放，打开清晰度菜单。

验证：

- transient overlay
- TEMPORAL
- Burst

---

## T05 长任务

> 打开设置完成一个多层导航任务，再切换到另一个 App 执行第二个任务。

验证：

- cross-page
- cross-app
- history
- 50+ step

---

## T06 异常恢复

执行过程中制造：

- 网络延迟
- 错误页面
- Dialog

验证：

- Incident
- Recovery
- Replay

---

# Appendix C — Definition of Done

项目不能因为：

```text
“能点击手机”
```

就认为完成。

真正 DoD：

```text
通用 Agent
+
无 App-specific Script
+
真实 HarmonyOS 真机
+
动态 UI Grounding
+
跨页面任务
+
瞬时 UI
+
失败恢复
+
长历史
+
安全约束
+
完整 Trace
```

全部成立。

最终产品应达到：

> **一个通用 Agent 连接 MCP 后，就拥有可靠操作 HarmonyOS 真机的 Computer-Use 能力。**
