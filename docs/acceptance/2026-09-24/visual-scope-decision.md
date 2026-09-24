# Task 5 — 视觉能力范围决策（2026-09-24）

**结论：tree-only 发布画像。** `ocr_capability=false`，`vlm_capability=false`。
M2 视觉任务**不得**计成功。

## 决策依据

| 检查项 | 结果 |
|---|---|
| 本机 OCR 引擎（tesseract/easyocr） | **无**（`detect_engine() = None`） |
| VLM 后端批准 / 凭据归属 | **未配置、未批准** |
| 适配器实现 | `harmony_runtime/ocr.py`、`harmony_agent/ocr.py`、`harmony_agent/vlm.py` 已实现 |
| 离线合同 | `tests/test_visual_providers.py` **36/36** 通过 |
| Visual Target Authority | ADR 已接受；无 raw coordinate 旁路 |
| 真机视觉全链路 | `blocked_dependency`（无引擎） |

计划 Task 5 允许在无后端批准时发布 tree-only 画像，并要求视觉失败显式为
capability/grounding 结果。本机没有可用引擎，**不安装、不伪造** OCR/VLM。

## 画像开关

| 能力 | 值 | 说明 |
|---|---|---|
| `capabilities.ocr` | `false` | Runtime session 报告已固定 false |
| `capabilities.webview` | `false` | 不承诺 WebView/Canvas |
| `ocr_tap` | unsupported | 不进入可派发目标 |
| `vlm_tap` | unsupported | 不进入可派发目标 |
| UI-tree 六工具 | **release-critical** | Direct 发布路径 |

## Provider 契约（保持，不放宽）

```text
OCR/VLM → regions only → GroundedTarget(crop_digest)
→ CandidateRegistry → Runtime revalidate (rotation/crop/freshness/risk)
→ journal → dispatch → verify
```

- 后端超时 / 畸形输出 / 错误旋转 / 缺引擎 → 显式 capability=false 或 `GroundingUnavailable`
- 任何 provider **不得**创建 action 或 raw coordinate
- 视觉区域只允许 tap/long_press，且 label 参与敏感词判定

## 后续若接入引擎（非本 run）

1. 明确批准的 OCR 后端（推荐 tesseract 本地，deadline_bounded=true）与 VLM（可选）
2. 配置归属与有界超时写入环境变量文档
3. 跑中文小字、旋转、icon-only、Canvas/WebView、malformed、timeout、unavailable 用例
4. 仅低风险视觉目标跑 `ground → revalidate → guard → dispatch → verify`
5. 新 run_id，不得把 M2 视觉任务改记成功而不重跑

## 门槛

- [x] 无 provider 可创建 action / raw coordinate（离线合同 + ADR）
- [x] 视觉失败显式 capability/grounding（adapter 异常隔离）
- [x] 无引擎时不把 M2 视觉任务计成功
- [ ] 真机视觉全链路（`blocked_dependency`，待批准引擎）
