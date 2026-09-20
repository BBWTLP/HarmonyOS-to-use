# 无密码锁屏自动恢复复测（2026-09-19）

结果：真机连续三轮通过。测试使用 USB 连接及正在运行的认证 Runtime 服务，不需要人工按键或滑动解锁。

## 验证方法

每轮先通过 HDC 的 `power-shell suspend` 进入休眠，再从 PowerManagerService 和 ScreenlockService 独立读取状态，确认 `screen_on=false`、`screen_locked=true`。随后通过 Runtime 的 `observe(mode="FAST")` 触发已有自动恢复逻辑，再独立读取系统状态，核对 `screen_on=true`、`screen_locked=false`，且 Runtime 返回有效 observation、非空控件目录和 `actionable=true`。

| 轮次 | 初始状态 | 恢复后状态 | 唤醒、解锁及读屏总耗时 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | 熄屏、锁屏 | 亮屏、未锁屏 | 3797 ms | 通过 |
| 2 | 熄屏、锁屏 | 亮屏、未锁屏 | 3531 ms | 通过 |
| 3 | 熄屏、锁屏 | 亮屏、未锁屏 | 3781 ms | 通过 |

以上为客户端调用耗时，并非纯解锁耗时。三轮平均 3703 ms；小样本不能代表长期成功率或性能分位数。

## 环境恢复与限制

正式三轮前，旧客户端会话曾返回 `lease_conflict`。会话释放后，旧设备工作进程返回 `device_quarantined`。调用已有 `session(operation="recover")` 后得到 `status=recovered`、`recovery_required=false`，随后才完成上述三轮。没有重放历史业务动作。

结论适用于当前无密码锁屏、USB 可调试且 Runtime 正常运行的环境；未验证重启后的首次解锁、USB 断连重连或密码锁屏。FAST 读屏通过不代表 FULL 截图一致性、应用导航或完整 Artemis 能力已验收。

## 重复运行

在仓库根目录执行：

```powershell
.\.venv\Scripts\python.exe scripts\accept_wake_unlock.py --execute --state-dir .runtime\acceptance --rounds 3 --report .runtime\automatic-unlock-retest.json
```

脚本必须显式传入 `--execute`，因为测试会主动使手机休眠。可使用 `--lease-wait-seconds 300` 等待旧会话租约到期，仅重试未成功取得设备的会话申请。遇到工作进程隔离或动作不确定状态会停止并报告错误，不自动重放动作。

报告只保留状态、计数、时间、脚本哈希和错误码，不保存界面文本、截图、账号内容、设备序列号或凭据。本次新增的是可复用验收脚本与本记录，自动恢复能力沿用已有 Runtime 实现。

## 当前连接再次验证

测试时间（UTC）：2026-09-18T20:33:33.036775+00:00。用户关闭锁屏密码后，在现有认证驻留服务上重新执行三轮主动休眠与自动恢复，三轮均通过；每轮独立确认熄屏且锁屏，再确认亮屏且未锁屏，并获得非空控件目录和可操作观察。

| 轮次 | 唤醒、解锁及读屏总耗时 | 结果 |
| --- | --- | --- |
| 1 | 3688 ms | 通过 |
| 2 | 3657 ms | 通过 |
| 3 | 3875 ms | 通过 |

本轮平均 3740 ms。会话已正常关闭，设备最后状态为亮屏、未锁屏。元数据报告位于 `.runtime/automatic-unlock-current.json`（不提交原始运行目录）。本次沿用已有恢复逻辑，没有更改业务动作或密码设置。

## 本次无密码解锁确认

测试时间（UTC）：2026-09-18T20:40:34.138901+00:00。通过现有认证驻留服务完成三轮主动休眠、自动唤醒和无密码解锁；所有轮次均通过独立系统状态检查，并返回有效、可操作的 FAST 页面观测。

| 轮次 | 唤醒、解锁及读屏总耗时 | 结果 |
| --- | --- | --- |
| 1 | 4062 ms | 通过 |
| 2 | 3796 ms | 通过 |
| 3 | 3469 ms | 通过 |

三轮平均 3776 ms。会话正常关闭。元数据报告：`.runtime/wake-unlock-recheck.json`。测试未使用密码，也未修改锁屏设置；未验证恢复前后应用页面完全相同。后续测试通过 Runtime 观测时可复用已有自动恢复路径；本结果不代表完整项目验收完成。


## 加载前台识别版本后的最终复测

测试时间（UTC）：2026-09-18T20:52:47.817198+00:00。当前服务新增前台身份采样后，重新完成三轮真机休眠与解锁复测，全部通过。验收脚本现在强制检查初始状态同时满足 `screen_on=false` 和 `screen_locked=true`，避免仅熄屏就计入锁屏验收。

| 轮次 | 唤醒、解锁及读屏总耗时 | 结果 |
| --- | --- | --- |
| 1 | 4594 ms | 通过 |
| 2 | 4265 ms | 通过 |
| 3 | 4329 ms | 通过 |

平均 4396 ms。每轮返回 68 个控件、有效 observation 和 `actionable=true`；独立系统检查确认亮屏且未锁屏。元数据报告：`.runtime/wake-unlock-final.json`。会话正常释放，未使用密码。

开始复测时，设备工作进程处于 `device_quarantined` 状态。先确认 `recovery_required=false`、未决动作数为 0，再显式调用恢复接口，返回 `recovered` 和可操作页面，随后执行上述完整三轮。未重放先前的应用启动操作。

独立的跨应用验收仍未通过，原因已定位：小红书启动后出现系统应用隐私密码弹窗，与屏幕锁屏密码是两项独立状态。新增识别后，Runtime 返回 `authentication_required`，保留 `executed/inconclusive`，不把弹窗视为启动成功。上述每轮的 68 个控件来自该弹窗，因此只证明屏幕锁已恢复，不证明小红书业务页面可操作。详情见 [前台与应用密码验收](foreground-and-authentication.md)。当前结果证明设备正常连接和服务可用时能自主恢复无密码锁屏，不承诺服务隔离、连接中断等异常后自动继续任务。

本轮元数据已脱敏保存为 [wake-unlock-metadata.json](wake-unlock-metadata.json)，包括原始验收脚本哈希；不包含原始界面或凭据。
