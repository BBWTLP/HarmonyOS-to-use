# 2026-09-19 当前真机观察基准

日期：2026-09-19（Asia/Shanghai）。本记录以当前 USB/HDC 连接的单台真机为开发、适配和验收基准；不把 HarmonyOS 6.0/6.1 写成固定验收目标。采样使用真实 stdio MCP → 常驻 Runtime → 独立设备 worker → 真机链路，未发出应用点击、输入或导航。

## 结论

- 批处理诊断已落地：把连续的只读诊断查询合并到有限的 HDC 往返中，同时保留前后屏幕、前台、图树和认证守卫。测试已证明顺序、帧完整性、隐私白名单、异常拒绝和不重试；当前三样本对照没有证明稳定提速。
- 最新批处理后 FAST 为 3/3 通过；FAST 带图为 2/3 通过；FULL 为 2/3 通过。FAST 带图与 FULL 的失败都保留在失败分母，不因图片存在或传输成功而算通过。
- FULL 第 3 次仅能定位为完整一致性/SoM 检查未通过：actionable=false、image_tree_consistent=false、som_available=false；image_present=true、image_dimensions_match=true、foreground_consistent=true。不输出页面内容，也不据此猜测具体控件变化。
- 所有计时均为小样本、非受控条件。热状态、页面动态、网络、应用版本和启动条件未冻结；first_attempt/subsequent_attempts 不是 cold/warm。不能把这些 P50/P95 当作正式性能验收。

## 环境与服务

- 关联基线：优化前 d2ac1dd37b4aabf7a30f6e5b8cd5349ceefb8ce9cab59327b893ca6638c5a239；批处理后 b2ea4e54d37fadec2c03acb012e51c3cdb4d4afbd0f7fd2766df373e62df4da1。两份基线的设备核心字段一致。
- 本轮在 .runtime/acceptance 对常驻服务做了一次受控重启：先确认独占租约、recovery_required=false、无未决动作，再停止旧 wrapper 及后代，启动加载批处理代码的新服务。未删除 journal 或日志，未并行启动第二服务。
- benchmark 报告字段只保留状态、计时、有限检查布尔值和错误码；不含设备序列号、页面文字、原始 UI 树、截图、账号、令牌或自由文本失败原因。
- 服务启动和 session open/close 不计入 client_ms；观察可能触发无凭据唤醒/解锁，因此 CLI 要求显式 --execute。

## 六组样本

| 报告 | 基线 | 样本结果 | client_ms（全部尝试，nearest-rank） | 备注 |
|---|---|---:|---:|---|
| [优化前 FAST](benchmark-20260919-fast.json) | 优化前 | 3/3 | P50 2562 / P95 6093 | 3 次：6093、2391、2562 |
| [优化前 FAST 带图](benchmark-20260919-fast-image.json) | 优化前 | 2/3 | P50 4063 / P95 7187 | 1 次 capture_unverified；旧报告没有新检查字段，不回填原因 |
| [优化前 FULL](benchmark-20260919-full.json) | 优化前 | 3/3 | P50 4375 / P95 4422 | 小样本，不代表正式门槛 |
| [批处理后 FAST](benchmark-20260919-batched-fast.json) | 批处理后 | 3/3 | P50 2532 / P95 4031 | checks 中 actionable、foreground_consistent 均为 true |
| [批处理后 FAST 带图](benchmark-20260919-batched-fast-image.json) | 批处理后 | 2/3 | P50 3734 / P95 4234 | 失败样本 image_tree_consistent=false，skew 695ms |
| [批处理后 FULL](benchmark-20260919-batched-full.json) | 批处理后 | 2/3 | P50 4484 / P95 4532 | 失败样本 capture_unverified，保留在分母 |

报告文件的本机 artifact time（Asia/Shanghai）为：优化前基线 11:23:50，批处理后基线 11:32:46；六组 benchmark 依次为 11:24:04、11:27:28、11:28:23、11:33:44、11:34:32、11:35:41。它们是文件修改时间，不冒充采样内部 started_at。

## 运行边界

- requested_samples=3、attempted_samples=3、unattempted_samples=0；失败样本计入 failed_samples。
- thermal_state=uncontrolled、service_startup_included=false、session_setup_included=false、application_actions_issued=false、screen_recovery_possible=true。
- 观察使用当前真机，但不是固定安全页面的业务任务验收，也不是 30/100/300 正式统计。FULL/SoM 在稳定页面的持续通过率仍未验收；微博动态搜索仍可能因 stale_observation 或完整图树变化而安全拒绝；应用隐私密码仍不能绕过。
- 不应从这六组样本得出“批处理提速”“Artemis 等效完成”或“任务成功率达标”的结论。

## 下一步

1. 在同一真机上选择不含私人内容的稳定安全页面，固定前台、屏幕、网络、应用版本和采样顺序，做批处理前后对照；仍保留完整失败分母。
2. 先定位图树/SoM 不一致与动态页面目标过期，再做受控优化；不得放宽指纹守卫来换取成功率。
3. 分开验收 FAST、FAST 带图、FULL/SoM、输入闭环和跨应用认证状态；之后才推进 v2 原语 100 次、M1、30 任务/100 步/300 次发布门槛。