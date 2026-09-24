# Task 6 — 安全 / 留存 / 运维缺口记录（2026-09-24）

## 1. Unknown incident 对账

| 项 | 值 |
|---|---|
| 权威 state-dir | `.runtime/agent-state` |
| open incident | **33**（`unknown-incidents.json`） |
| unresolved action rows | 0 |
| 关闭证据 | 仅 `postcondition_verified` 或可信 `not_executed` |
| 处置 | **全部显式保持 open**；不盲重放；不换 state-dir；不任意 attestation 关闭 |

每条 incident 的 next action：在设备可用且页面可独立取证时，用原始语义后置条件或
「派发前指纹逐字节一致 + 可信未派发记录」做 reconcile。当前没有新的可信对账证据，
因此 **33 条一律 leave open**，不作为发布阻塞的“已关闭”，但也不伪造成已解决。

清单：`docs/acceptance/2026-09-24/unknown-incidents.json`。

## 2. R2/R3 信任批准边界

**发布画像：敏感动作保持禁用。** Runtime 对敏感目标返回
`approval_required` / `Trusted approval flow is not implemented in this build.`
（`harmony_runtime/runtime.py`）。

| 风险类 | 处置 |
|---|---|
| payment / delete / send / permission / credential / session | **dispatch 禁止** |
| 词法风险扫描 | 预过滤，**不是**完整批准流 |
| 模型签批准 | 永不接受 |

词法 `risk_class_for` 只有 low/high；不存在可被 Agent 绕过的中风险放行。
在实现独立、可审计的人工批准通道之前，R2/R3 一律 disabled。

## 3. 明文恢复条件（legacy）

- 新 recovery 条件使用加盐 exact-match digest，重启后仍可校验
- **legacy 明文条件仍可读，未迁移、未安全擦除**（`tests/test_journal_privacy.py` 固定）
- 发布画像：不把 legacy 条件用于自动关闭；对账优先用 digest 路径
- 配额 / TTL / 脱敏导出在 `harmony_agent/artifacts.py`；未决记录不回收

限制（如实）：digest 不是加密，低熵标签可被库读者猜测；完整 retention 工程未完成。

## 4. 故障矩阵状态

| 场景 | 离线 | 真机 current |
|---|---|---|
| storage pressure / SQLite full | 4/4 通过 | 未测 |
| journal privacy | 5/5 通过 | 未测 |
| unknown reconcile 拒绝路径 | 10/10 通过 | 未测 |
| USB/HDC 断连 | 部分注入 | **待设备窗口** |
| worker hang / 隔离 | 有恢复路径 | **待设备窗口** |
| 服务受控重启 | runbook §1 | **已做过**（lease 卡住时重启，journal 保留） |
| artifact 配额 | 单测 | 未测 |

真机断连与 hang 演练归入 2026-09-24 计划 Task 2/3 的设备窗口；本 run 不伪称通过。

## 5. 发布门禁（Task 6）

- [x] 全部 open incident 显式登记并 leave open（有证据强度与 next action）
- [x] R2/R3 保持 disabled；词法拦截不是批准流
- [x] legacy 明文条件已文档化并 quarantine 用途
- [x] 离线故障/隐私/对账测试通过（55/55 相关子集）
- [ ] 真机 USB/HDC/worker hang/storage 压力矩阵（待设备窗口）

**Release checklist：** 无“已解决”的发布阻塞 incident 被伪造；未决 unknown 写入在
Direct 读路径可继续，写路径在 unresolved action rows=0 时放行。若出现新的
unresolved action rows，写路径按 journal barrier 阻塞。
