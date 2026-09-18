# 自动唤醒与无凭据解锁验收记录（2026-09-18）

当前状态：通过。该记录验证 HarmonyOS 6.1 真机在无锁屏密码时可以由 Runtime 自主恢复到可交互状态；不代表完整 Artemis 等效能力已经交付。

## 测试链路

1. HDC 识别真机。
2. 使用 `power-shell suspend` 主动让手机进入休眠。
3. 通过真实 stdio MCP 打开会话并调用 `mobile_observe(mode="FAST")`。
4. Runtime 读取屏幕状态，按顺序调用设备驱动的 `screen_on`、`wake_up_display`、`unlock`，随后重新读取状态。

## 结果

```text
observe_status: ok
screen_on: true
screen_locked: false
catalog_count: 137
HDC PowerManagerService: Current State: AWAKE
```

本链路不读取、保存或注入锁屏凭据。`probe` 仍可能因为动态图树和截图采集不同步而返回 `not_ready`；该现象属于一致性检查失败，不等于自动恢复失败。

## 后续缺口

微博搜索输入、小红书只读导航、Codex/OpenCode/DeepSeek Harness 客户端实测、跨应用长任务、OCR/CV grounding、前台应用身份、审批与可信对账、证据保留和最终发布验收仍需继续完成。
