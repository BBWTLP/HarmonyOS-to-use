# P0.5-A.1 — 观测就绪门（readiness gate）

日期：2026-09-22（Asia/Shanghai）
分支：`feat/runtime-foundation`
baseline SHA：`cbc815f`（P0.5-A 完成时）
真机：`SGT-AL10` / `5XM0226105001134`，HDC `3.2.0f`

本文档只回答一件事：**锁屏 / 熄屏 / 状态未知时，hierarchy capture 是否根本没有发生。**

---

## 1. 先决测量：设备侧 gate 在本技术栈上不可实现

需求是"device-side readiness gate"：设备侧读到 PowerManager / Screenlock 文本后自己判断，
不 READY 就跳过 tree。三次只读实验证明这条路走不通——**hidumper 的输出正文无法被任何
设备侧消费者读取**，只有 host 通过 hdc 通道能看到：

```text
POWER = hidumper -s PowerManagerService -a '-a'      （正文 ~15 KB）

实验                                                  结果
hidumper ... | grep -c 'Current State'               0        （管道里只有 banner）
hidumper ... | wc -c                                 163      （banner 长度，正文丢失）
X=$(hidumper ...); echo ${#X}                        0        （命令替换同样丢失正文）
hidumper ... > file; wc -c file                      0        （文件重定向同样丢失）
hidumper ... 1>/dev/null                             hdc 侧也收不到正文
hidumper ... 2>/dev/null                             hdc 侧仍收到正文（不是 stderr）
hidumper ... 1>/dev/null 2>/dev/null                 正文完全消失
exec 3>&1; exec 1>file; hidumper ...; exec 1>&3      file 0 字节
```

结论：正文只在 **stdout 直接就是 hdc 终端通道**时出现，任何重定向 / 管道 / 命令替换都会
让它消失。`grep` 在设备侧永远只看到 banner，因此"设备侧解析屏幕证据"在
HarmonyOS + hidumper + hdc shell 下不可能实现（除非自研设备侧二进制，超出本轮范围）。

## 2. 采用的机制：把 gate 放在传输边界上（更强，而不是更弱）

把 batched capture 拆成两次设备事务，中间由 host 用**与 legacy 完全相同的 parser 与策略**
判断：

```text
事务 1 (readiness)   只含屏幕证据：PowerManager + ScreenlockService
    ↓ host: parse_screen_state + Runtime._require_ready（与 legacy 同一策略）
    ├─ READY      → 事务 2 (capture)
    └─ 非 READY   → 结束，事务 2 根本不构造、不下发

事务 2 (capture)     foreground → display → tree → [image → tree_after → display_after]
                     → foreground_after → screen_after
```

为什么这比"事务内判定后丢弃"更强：

```text
旧 A 实现   host 只有在整条事务返回后才解析 screen_before；
            锁屏时 uitest dumpLayout / snapshot_display 已经真的在设备上执行过。
A.1         capture 事务是在 gate 通过之后才构造的字符串；
            非 READY 时设备从未收到任何 content-bearing 命令。
```

安全语义没有让步：

```text
READY 判定        只接受 Current State 全部为 AWAKE（且至少一条）且 screenLocked 全部为 false
                  （且至少一条）；其余一律 unknown → fail closed
unknown 语义      缺失 / 冲突 / 解析不确定一律不是"未锁"
tree 夹取          tree 仍在 foreground_before/after 与 screen_before/after 之间
preflight         act 前仍是一次新的真实设备读取（现在也是 gated 两阶段）
legacy            未改动：仍先 _ready_screen 再读 tree，仍是回归 oracle
```

TOCTOU：gate 与 capture 之间约 125 ms。该窗口与 legacy 同类（legacy 的
`_ready_screen → tree` 之间同样有窗口，而且更长）；若屏幕在 capture 窗口内变为不可用，
事务 2 末尾的 `screen_after` 会判定为不可信 → 该 observation 作废、不 action、不入缓存。
真正消除窗口需要设备侧 gate，即第 1 节证明的不可行路线。

## 3. 代码改动

| 文件 | symbol | 改动 |
|---|---|---|
| `src/harmony_runtime/snapshot.py` | `build_script(..., phase)` | 新增 `PHASE_READINESS`（只含屏幕证据）与 `PHASE_CAPTURE`（含全部 content 读取）；两个阶段共用同一 marker 帧格式与严格顺序校验 |
| 同上 | `BatchedSnapshotProvider.readiness()` | 事务 1：仅 `SCREEN_POWER` + `SCREEN_LOCK`，返回解析后的状态与设备侧 screen_ms |
| 同上 | `BatchedSnapshotProvider.capture(..., screen_before=)` | 事务 2：`screen_before` 为必填；缺省直接拒绝（`internal_error`），无法绕过 gate |
| 同上 | `capture_after_readiness()` / `readiness_probe()` / `provider_choice()` | 新的两阶段入口；`capture_snapshot()` 退化为 legacy/fallback 入口 |
| 同上 | `ProviderState` | 新增 `transactions`（实际下发的事务数）与 `gate_refusals`（在 content 之前被拒绝的次数） |
| `src/harmony_runtime/runtime.py` | `_require_ready` / `_screen_recoverable` | 把就绪策略从 `_ready_screen` 中抽出为共享实现（legacy 与 batched gate 同一份策略，legacy 行为不变） |
| 同上 | `_capture_snapshot` | batched：先 readiness_probe → `_require_ready` → 才允许 capture；不可恢复立即抛 `screen_locked`/`screen_off`/`screen_state_unknown`；可恢复才唤醒/解锁并重试（≤3 次） |
| 同上 | `_observe_perf` | 新增 `observe.readiness_gate` / `observe.readiness_gate_ms` / `observe.capture_transactions` |
| `tests/test_snapshot_provider.py` | 见第 4 节 | 44 项（含 13 项就绪门安全用例） |
| `scripts/accept_p05a1_safety_smoke.py`（新） | — | 真机 5 项安全冒烟 |

## 4. 如何证明"没有 tree capture"

不是靠 `actionable == false`，也不是靠输出字符串匹配，而是三层直接证据：

```text
1) 传输层（离线，最直接）
   设备替身记录自己收到的每一个 script；锁屏/熄屏/unknown 的每种组合都断言：
     device.probes == 1                    只有一个事务被下发
     且该事务不含 uitest / snapshot_display / base64 / WMS / AMS / DMS

2) 脚本层（离线，script evaluator）
   executed_commands(script) 解析脚本里"设备真正会执行哪几条命令"：
     PHASE_READINESS → 只等于 [SCREEN_POWER, SCREEN_LOCK]
     PHASE_CAPTURE   → 含 uitest dumpLayout，但第一条不是屏幕读取
   并断言 capture 入口在 screen_before=None 时直接拒绝（无法绕过 gate）

3) 真机层（smoke）
   包装 device.batch_probe，记录真正下发到手机上的 script 文本：
     熄屏后 readiness probe 只下发 1 个事务，其命令正是那两条 hidumper，
     phase1_offenders == []（无任何 content 命令）
     策略判定 screen_locked → 抛错；随后普通 observe 才唤醒/解锁并 capture
   runtime 侧计数：gate_refusals >= 1，且 capture 事务只在 gate 通过后出现
```

## 5. 离线结果

```text
python scripts/reproduce.py
789 tests，0 failed / 0 error / 0 skipped（118s）
P0.5-A 后 774 → 789（+15），原有 774 项一条未减少
```

新增就绪门安全用例（`tests/test_snapshot_provider.py::ReadinessGateTests` 等）：

```text
locked             screen_on=true, screen_locked=true   → 无 content 命令，screen_locked
screen off         screen_on=false                      → 无 content 命令，screen_off
unknown lock       screenLocked 缺失                     → 无 content 命令，screen_state_unknown
unknown power      Current State 缺失                    → 无 content 命令，screen_state_unknown
conflicting power  AWAKE + SLEEP 同时出现                → 无 content 命令，screen_state_unknown
conflicting lock   false + true 同时出现                 → 无 content 命令，screen_state_unknown
ready              AWAKE + screenLocked false            → 才允许 tree（且恰好 1 次 dumpLayout）
FULL 同样受限      locked + include_image=True           → 无 dumpLayout / 无 snapshot_display
绕过 gate          capture_after_readiness(screen_before=None) → internal_error
帧不完整           截断 readiness 帧                      → fail closed
帧缺段             capture 帧缺 tree 段                   → 解析拒绝
抓屏中途变锁       screen_after=locked                    → snapshot 判为不可信、不 action
运行时计数         gate_refusals / transactions 只在正确分支递增
```

## 6. 真机安全冒烟（`scripts/accept_p05a1_safety_smoke.py`）

```text
1. unlocked observation       actionable=true  provider=batched  transactions=2  gate=confirmed
2. sleeping gate              transactions_issued=1
                              phase1_commands = [PowerManagerService, ScreenlockService]
                              phase1_offenders = []
                              screen={screen_on:false, screen_locked:true} → refusal=screen_locked
3. wake/unlock 恢复           由普通 observe 完成（runtime 自身恢复路径）
4. 恢复后 observation         actionable=true  provider=batched  screen_on=true/locked=false
5. FULL observation           actionable=true  transactions=2  som_available=true
6. 无 write dispatch          journal_entries=0
status = ok
```

## 7. 性能代价（保持 one-transport 与否）

```text
不再是一条 host→device 事务：ready 路径现在是 2 条（readiness + capture）。
追加成本 = 一次 hdc 事务固定开销（实测 `hdc shell true` ≈ 125 ms）+ readiness 的设备侧 ~60-90 ms，
但因为屏幕探针从 capture 事务中移出，净增约 125 ms 量级（不是翻倍）。

实测（10 次，非正式长批，`bench-fast-a1.json`）：
  batched FAST  client p50 1360 ms / p95 1593 ms，10/10 成功，round trips = 2
  设备侧分段 p50：screen 60 + foreground 120 + display 70 + tree 510 + cat + fg_after 120 + screen_after 90
  注意：该次 tree 只有 510 ms（页面/热态与 A 轮 830 ms 那次不同），
        因此 1360 ms 不能与 P0.5-A 的 1719 ms 直接比较；正式 A/B 需在 B 阶段重跑。

锁屏路径现在是**更便宜**的：只下发 readiness 事务（~200 ms）就结束，
不会再有 1.7 s 的无效 capture；`gate_refusals` 直接可观测。
```

## 8. 结论

```text
安全不变量   成立（三层证据 + 真机 smoke）
legacy       未改变（仍是 oracle / fallback / baseline）
离线         789 全绿，原有 774 项无减少
传输          ready 路径 2 条事务（A.1 的刻意取舍），非 READY 路径 1 条且无 content
建议         READY_FOR_P05_B
```

P0.5-B（Hybrid RPC：PRE live probe → persistent UITest RPC tree → POST live probe）
可以把 tree 从 shell `dumpLayout` 换成持久 RPC（实测 830 → 594 ms），
允许 3 次 physical interaction；本轮**没有**开始 B，也没有跑正式 M0/M1。
