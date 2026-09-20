# ADR: Visual Target Authority

状态：Accepted（离线实现与 fake-device 测试已完成；真机执行 `blocked_device`）
日期：2026-09-20
相关代码：`harmony_runtime/contracts.py`、`harmony_runtime/observation.py`、
`harmony_runtime/visual.py`、`harmony_runtime/runtime.py`、
`harmony_agent/grounding.py`、`harmony_agent/supervisor.py`
相关测试：`tests/test_visual_target_authority.py`

## 背景

Grounding 早就能产出视觉 `GroundedTarget`（`source=ocr/image`，带 bounds、
geometry、crop 指纹），但 Runtime 的 target resolve 只认 UI-tree catalog，
所以视觉候选实际上**永远无法派发**——这是安全的一侧。缺的是明确的权威模型：
OCR/VLM 只能提议区域，Runtime 必须自己复核，且不存在任何 raw coordinate 旁路。

## 决策

只有一条派发权威链：

```text
image
→ OCR / image matcher / VLM 产出 GroundedTarget（区域 + label + crop digest）
→ CandidateRegistry 登记（server-issued candidate_id，绑定 observation/epoch/TTL）
→ Runtime resolve 重新计算 crop digest 并核对 geometry
→ Runtime policy（label 参与敏感词判定；视觉区域只允许 tap/long_press）
→ Journal → dispatch → verification
```

视觉区域是**提议**，不是坐标授权。Runtime 不认识"点"，只认识"能被重新核验的区域"。

## 必须回答的问题

### 1. visual target 如何注册？

`CandidateRegistry` 与 UI-tree 候选走同一条登记路径，`candidate_id` 仍由服务端签发。
`GroundedTarget.local_fingerprint` 对视觉目标是 **Runtime 自己定义的 crop 内容摘要**
（`harmony_runtime.visual.region_digest`），不是 proposer 自报的哈希。

### 2. Runtime 如何识别 target_ref 的来源？

`Target.visual`（`VisualRegion`）存在即为视觉目标；它只能与 `target_ref` 同时出现，
且 `visual.crop_digest` 必须等于 `local_fingerprint`。缺少 `visual` 块时，
视觉区域会被当成普通 v2 句柄并在 UI-tree catalog 中找不到 → 拒绝。

### 3. action 前如何重新验证截图区域？

派发前，`_act_locked` 对视觉目标强制 `include_image=True` 重新观察，然后：

```text
重新计算 region_digest(current.image, region)
与 target.visual.crop_digest 比对
不等 -> RuntimeFault("target_not_revalidated")，不派发
```

摘要由 Runtime 计算；proposer 无法伪造，因为伪造的摘要必然与当前像素不符。

### 4. screenshot geometry 如何绑定？

`VisualRegion` 固定携带 `display_width` / `display_height` / `rotation`。
与派发前观察的 display 不一致即 `target_geometry_changed`。区域先按 image 尺寸裁剪，
因此 `region` 与截图分辨率必须来自同一次观察。

### 5. rotation / resize 如何处理？

旋转或分辨率变化都会改变页面指纹，同时显式命中 `target_geometry_changed`；
两种情况都拒绝，绝不按比例换算坐标。

### 6. 页面改变后视觉 target 是否立即失效？

是。三道闸门都会独立拒绝：

```text
页面指纹变化且 navigation 投影不一致 -> stale_observation
目标区域像素摘要变化                 -> target_not_revalidated
display 几何变化                     -> target_geometry_changed
```

### 7. 如何避免 raw coordinate bypass？

```text
协议里没有 x/y 字段；Target 只有 action_id / text / resource_id / target_ref(+visual)
"540,1200" 这类字符串只是普通文本选择器，永远不是坐标
视觉区域必须通过 resolve 复核才存在，且只允许 tap / long_press
```

`tests/test_visual_target_authority.py::RawCoordinateTests` 固定这条不变量。

### 8. 如何与 UI-tree target 共用 journal / policy？

共用。`resolve` 返回的视觉节点是一个普通 target 字典（含 `hit_bounds`、
`type="visual_region"`、`target_fingerprint`、`visual_source`），因此
`_policy`、journal begin/finish、unknown-write barrier、verification 全部沿用，
没有第二条写入路径。

### 9. OCR / VLM 分歧如何处理？

每个候选都是独立 target，`CandidateRegistry` 保留全部候选与 target_ref；
分歧意味着候选集合不唯一，规则路由给出 `reobserve`/`escalate`，
不存在"取第一个"或"平均坐标"的合并策略。

### 10. 无法安全 revalidate 时如何 abstain？

```text
没有截图证据      -> target_not_revalidated
摘要无法计算      -> target_out_of_bounds
摘要不一致        -> target_not_revalidated
几何不一致        -> target_geometry_changed
句柄过期/未知     -> stale_observation / unknown_candidate
```

任何一种都是"拒绝派发"，不是"尽力点击"。

## 明确不做的

```text
不允许 OCR/VLM 直接 dispatch
不允许 VLM 返回 raw HDC 或屏幕坐标动作
不允许按比例换算坐标以适配旋转
不允许视觉区域接收 input_text / replace_text
不允许 Autonomous Agent 层执行未登记候选
不允许把 proposer 自报的 label 当作唯一风险证据
```

## 谁可以提议 target（v3.2 Gate B 结论）

原文写的"不允许未登记候选执行"过于笼统，与 Direct Runtime 的真实语义不符。
2026-09-20 的 Pre-Device Gate 审计确认并修正如下：

```text
Autonomous Agent 层（mobile_run_task / task_*）
  模型不能签发 candidate_id；执行目标必须先经 CandidateRegistry 登记，
  并且 supervisor 在派发前再次 registry.resolve()
  → 未登记 / 过期 / 旧 observation / 旧 epoch 一律 not_dispatched

Direct Runtime（mobile_act 等 v1/v2 调用）
  认证过的本地调用方本来就有权选择目标（v1 就允许按 action_id / text 指定）。
  因此调用方也可以提议一个视觉区域，但：
    - 区域必须携带 observation_id，且必须与本次请求引用的观察一致
    - 区域必须携带 crop_digest，Runtime 会用当前截图重新计算并比对
    - display 尺寸与旋转必须一致，越界区域直接拒绝
    - 风险判定同时使用调用方 label 与*设备侧*重叠文本（见下）
  → Runtime 复核 + Guard 才是权威，不是调用方的声明
```

### 风险证据不是只有 proposer 的 label

`_resolve_visual` 会读取当前 UI-tree 中与该区域重叠的节点文本、描述、资源 id，
写入 `visual_evidence`，并被 `harmony_runtime.risk.label_of` 纳入敏感词判定。
因此：

```text
label="继续" 但区域实际压在 "立即支付" 上 → approval_required，不派发
label 本身敏感 → approval_required，不派发
区域与任何 UI-tree 文本都不重叠 → 无独立语义证据（记录为空），
  此时仍只允许 tap/long_press 且受 blocking_dialog 与敏感词约束；
  这是已知边界，见 pre-device-gate.md 的剩余风险
```

### crop revalidation 证明什么

它证明的是"这块像素没有变"，**不是**"这个目标获得了授权"。授权来自：
观察句柄有效 + 几何一致 + 像素摘要一致 + 该区域没有命中风险策略 + journal 准入。
两者在实现里是分开的两步，ADR 不再把它们混为一谈。

## 后果

正向：

```text
视觉能力接入不会新增第二条设备写入路径
proposer 说谎必然失败（摘要与像素不符）
policy / journal / verification 自动覆盖视觉目标
```

代价与限制：

```text
视觉目标必须有截图证据，FAST（无图）观察不能派发视觉目标
每次视觉派发多一次带图 preflight 抓屏，真实开销待真机测量（blocked_device）
只支持 tap / long_press；输入仍必须落到可观察的输入框
```

## 真机待办

```text
真实 OCR 区域点击：中英文、小字号、旋转、动态页面
真实 VLM 视觉定位：icon-only / Canvas / WebView
带图 preflight 的 P50/P95 开销与失败分母
```

以上均为 `blocked_device`，本 ADR 不声称任何真机结论。
