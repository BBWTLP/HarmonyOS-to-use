# v3.2 Phase 6/7 — OCR 与 VLM 视觉 Provider

## OCR（Phase 6）

不重建 `OcrEngine Protocol`，只加适配器：`src/harmony_agent/ocr.py`

```text
backend (系统 OCR / PaddleOCR / 托管服务)
   ↓ OcrAdapter
image_bytes → recognize() → 归一化区域
   ↓ Grounding 的 ocr 层
GroundedTarget(source=ocr) → CandidateRegistry → Runtime 复核
```

适配器职责（全部离线可测）：

```text
available / reason      引擎未配置或不可用时显式声明，不抛异常
with_observation()      绑定当次观察的 display 与 rotation
归一化                   list/tuple/dict 边界、裁剪、0 面积丢弃、越界丢弃
置信度                   clamp 到 [0,1]，低于下限丢弃，NaN/Inf 丢弃
去重                     (text, bounds) 相同只保留一次
预算                     max_regions（默认 64），超限记录原因
异常隔离                后端抛异常 → 返回空并记录 ocr_failed:<Type>
```

`grounding._ocr_layer` 只把归一化后的区域转换成候选，并额外做一次全层异常兜底；
OCR 后端崩溃永远不会传播到 Runtime。

## VLM（Phase 7）

VLM 是**视觉定位 provider**，不是控制器：`src/harmony_agent/vlm.py`

输入（`build_request()` 固化）：

```text
goal / subgoal / description
display（width, height, rotation）
tree_summary
existing_candidates
answer_contract = "regions_only"
timeout_seconds
```

输出契约：

```text
只允许 [{"bounds", "score", "method"}] 形式的区域
返回 {"action": "tap", "x": .., "y": ..} 之类 → 没有 bounds → 不产生任何候选
```

失败处理：

| 情况 | 结果 |
|---|---|
| 后端未配置 / 不可用 | `available=False`，grounding 记录 `image_layer_skipped` |
| 超时 | `GroundingUnavailable("vlm_timeout")` |
| 后端崩溃 | `GroundingUnavailable("vlm_failed")` |
| 回答不是区域列表 | `GroundingUnavailable("vlm_invalid_response")` |
| 区域全部非法/低分 | `GroundingUnavailable("vlm_low_confidence")` |
| 无匹配 | 空列表（不生成候选） |

`grounding._image_layer` 把这些统一记录为 `image_layer_failed:<code>` 备注，
层失败不会变成异常，也不会影响 UI-tree 层。

## 与 Runtime 的衔接

视觉候选走 Phase 5 的 Visual Target Authority：

```text
OCR/VLM 区域 → GroundedTarget(crop_digest) → CandidateRegistry
→ Target.visual → Runtime 派发前重新计算摘要
→ policy（label 参与敏感词判定）→ journal → dispatch → verify
```

所以 OCR/VLM 既不直接点击，也不能绕过 Guard、epoch、observation 新鲜度和 journal。

## 离线证据

`tests/test_visual_providers.py`（36 项）覆盖：

```text
区域归一化：list/tuple/dict、畸形值、0 面积、越界、旋转尺寸、置信度 clamp、去重
OCR：中英文、重复文本、低置信、越界、旋转、空结果、引擎不可用、引擎异常、区域预算
OCR 集成：产出带 geometry 与 crop_digest 的目标；无图时不产生候选；崩溃不抛异常
VLM：请求内容、区域校验、超时、崩溃、不可用、无匹配、非法 bounds、低分、
     非列表回答、纯坐标回答、区域预算、观察绑定
VLM 集成：经 image 层产出候选；失败只留备注；UI-tree 层优先
```

## 明确不做

```text
不新建 OCR 框架
不让 VLM 返回动作或 HDC 调用
不让视觉 provider 生成 candidate_id
不让 raw coordinate 进入派发路径
```

## 真机待办（`blocked_device`）

```text
真实 OCR 引擎接入与中文小字号/旋转识别质量
真实 VLM 服务接入与 icon-only / Canvas / WebView 定位
带图 preflight 的真实开销与失败分母
```
