# P0.5 — Observation / Guard / Harness 设备往返成本

日期：2026-09-22（Asia/Shanghai）
分支：`feat/runtime-foundation`
基线 HEAD：`a1ed8f2ac6ed619acab2c6fe47be6e94bca7486d`
真机：`SGT-AL10` / `5XM0226105001134`，HDC `3.2.0f`，`uitest` 协议 v2
Python：3.11.9（`.venv`）；devhelmkit 0.6.0

范围：**只动 Observation 数据平面**。没有触碰 Decider、RSI、记忆、UI、多设备、
新 VLM/OCR，也没有放宽任何 stale / epoch / lease / fingerprint 规则。

---

## A. Root Cause（修改前为什么慢）

真实调用图（按当前代码逐 symbol 核对，不按文档推测）：

```text
MCP / harness            scripts/agent_harness.py, scripts/accept_m0_primitives.py,
                         scripts/accept_m1_weibo.py, harmony_agent/supervisor.py
  -> Runtime             harmony_runtime/runtime.py
                         observe() -> _observe()            ; act() -> _act_locked()
  -> Observation         _observe(): _ready_screen / foreground / tree / display /
                         snapshot() / foreground_after / _ready_screen
                         _act_locked(): preflight _observe() -> Guard -> dispatch
                                        -> 验证循环里再次 _observe()
  -> Device Worker       device_worker.ProcessDevice._call()  (每方法一次本地 IPC)
  -> Device boundary     harmony_runtime/device.py HarmonyDevice
  -> HDC / uitest        devhelmkit HdcDevice.shell()   |  rpc_captures("captureLayout")
                         driver.dump_hierarchy() = uitest RPC（长连接 socket）
  -> Phone
```

一次 FAST observation 的真实传输次数（改动前）：

```text
screen_ready_before  1x `hdc shell`（PowerManager + Screenlock，1 个事务）
foreground_before    1x `hdc shell`（WMS + AMS + WMS，1 个事务）
tree                 1x uitest RPC（captureLayout，长连接 socket）
display              1x `hdc shell`（DisplayManagerService）
foreground_after     1x `hdc shell`
screen_ready_after   1x `hdc shell`
                    = 6 次 host<->device 传输
```

FULL 另外再加 `screenshot`、`tree_after`、`display_after` = 9 次。

实测单次 `hdc shell` 的固定成本 ~125ms（`hdc shell true`），
`hidumper` 每条 ~55-100ms，`uitest dumpLayout` 约 830ms，
`snapshot_display`+`base64` 约 420ms。因此 6 次传输 = 约 2.6s。

放大效应同样实测确认：

```text
_act_locked  preflight = 一次完整 FAST observation（不是轻量探针）
             派发后验证循环里 again `_observe()`，未满足条件时每 ~150ms 再观察一次
harness      setup_act(): observe -> act -> （stale 拒绝后）observe -> act
             M1 run_step(): act 之后紧接 `observe()`，再从零开始下一步
             一次普通 tap = caller observe + preflight observe + dispatch + post observe
```

结论：**瓶颈是 Observation 传输边界数量，不是单条命令的耗时**。

## B. Code Changes

| 文件 | symbol | 改了什么 | 为什么 |
|---|---|---|---|
| `src/harmony_runtime/snapshot.py`（新） | `BatchedSnapshotProvider` / `LegacySnapshotProvider` / `capture_snapshot` / `ProviderState` | 新增设备快照提供者抽象：legacy 保留原六次调用；batched 用**一次**设备侧事务完成同样六个采样点 | 传输边界从 6 降到 1，安全事实一个不少 |
| 同上 | `build_script` / `ALLOWED_COMMANDS` / `RAW_COMMANDS` | 事务脚本只由固定命令词表 + 随机 marker + 派生临时路径拼装；`cat`/`base64` 永不包 `time` | 不接受任何调用方 shell 文本；载荷必须逐字节精确 |
| 同上 | `parse_transaction` / `_ordered_sequence` | 严格顺序帧校验：每个 marker 恰好一次、顺序必须与脚本一致、命令 rc 必须为 0、段内不得有未标注输出、必须有 `:done` | 截断 / 重复 / 乱序 / 并发串扰的抓屏一律拒绝，绝不“部分解析” |
| 同上 | `strip_device_timing` / `device_timing_ms` | 用 shell 内建 `time` 关键字获取**设备侧**每段耗时（零额外进程），解析前剥离 | 让 batched 也有真实的分段设备耗时指标 |
| 同上 | `_validate_display` / `_parse_tree` / `parse_image` | 显示几何、JSON、图片任一不可用即整次抓屏失败 | 不允许“降级推断”出可执行观察 |
| 同上 | `assess` | 明确的 `snapshot_consistent` 与原因枚举 | screen/foreground/tree/display 一致性判定显式化 |
| `src/harmony_runtime/device.py` | `HarmonyDevice.batch_probe` | 新增唯一的只读事务入口（1 次 `hdc shell`） | 设备侧唯一写入口语义不变：该方法不派发任何写动作 |
| `src/harmony_runtime/device_worker.py` | `_METHODS`, `ProcessDevice.batch_probe` | 允许 `batch_probe` 通过常驻 worker | 保持单 writer + deadline/quarantine 语义 |
| `src/harmony_runtime/runtime.py` | `_capture_snapshot` | legacy 仍然先走 `_ready_screen`（锁屏在读 tree 之前就拒绝），batched 在事务内采样屏幕后就地判定就绪、丢弃不可信抓屏并按原有语义唤醒/解锁一次 | 保持“先确认屏幕再读层级”的顺序；批量路径不额外增加设备调用 |
| 同上 | `_observe` | 改为基于快照构建观察；新增 `snapshot_capture` / `snapshot_consistent` / `consistency_reason` / `perf` | 调用方拿到可验证的一致性结论与性能指标 |
| 同上 | `_observe_perf` | `observe.mode/wall_ms/device_ms/hdc_round_trips/screen_ms/foreground_ms/tree_ms/display_ms/screenshot_ms/encode_ms/snapshot_span_ms/consistency_check_ms` | 只记录 duration/count/enum/bool/hash，无 UI 文本、无截图、无输入值 |
| 同上 | `act` / `_with_action_perf` / `_note_stale` / `_session_result` | `act.preflight_ms` / `act.dispatch_ms` / `act.post_observe_ms` / `act.total_ms`、`stale.reason` / `stale.consecutive_count` | stale 分类与计数可观测 |
| 同上 | `_act_locked` | preflight 仍是真实设备读取；新增 `post_observe` 计时与 `preflight_capture` 元数据 | **没有**删除 preflight，只是把它变成同语义的单次事务捕获 |
| `scripts/agent_harness.py` | `post_observation` | 复用 Runtime 刚刚为验证生成的 post observation；非 actionable / 非 executed / 无新 id 一律返回 None | 这不是旧缓存，是刚采到的最新设备事实 |
| 同上 | `stable_observation` | 自洽的 batched 快照直接接受；只有 `snapshot_consistent=false` 才补采（有界），并输出 `stable_attempts` / `stable_reason` / `last_inconsistency_reason` | 不再默认一次抓 3 遍 |
| `scripts/accept_m0_primitives.py` | `_setup_step` / `_execute_transition` / `_settle_surface` | setup 状态机消费上一次 act 的 post observation，只有它没有显示状态变化时才补采 | 一个状态迁移原则上只消费一个新的 live observation |
| `scripts/accept_m1_weibo.py` | `run_step` / `goto_tabs` / `goto_editor` | 返回 `(record, post_observation)`；`run_once` 不再在每步后无条件 `observe()` | 删除 step 尾部重复观察 |
| `src/harmony_agent/supervisor.py` | `post_observation` / `RunContext.post_observation` / `_run_subgoal` / `_early_progress` | 任务 runner 复用 Runtime 的验证抓屏 | 同一任务下 observations/dispatch 下降 |
| `scripts/accept_p05_benchmark.py`（新） | — | 同一页面、同一设备、串行跑 legacy / batched 两相，自行管理服务进程 | A/B 唯一变量是 `HARMONY_OBSERVE_BATCHED` |
| `tests/test_snapshot_provider.py`（新） | 28 项 | 见 F 节 | 每条失败模式都有离线用例 |

Feature flag：`HARMONY_OBSERVE_BATCHED`（默认 `1`）。`0` 强制 legacy。
批处理**解析失败**时自动回退 legacy 一次并记录 `fallback_reason`；连续 3 次失败后
本进程永久降级为 legacy（`provider_state.degraded`），不会无界重试。

## C. Safety Preservation

| 约束 | 优化后为什么仍然成立 |
|---|---|
| preflight 不可删除 | `_act_locked` 仍先做一次**真实设备**捕获（`preflight_capture` 可查 provider / round_trips）；只是该捕获由 6 次传输合并为 1 次 |
| 不复用 caller observation 做派发前校验 | preflight 永远是新的设备读取；`post_observation` 复用只发生在**观察层**，`act` 仍会重新读取 |
| stale（页面变化） | `current["fingerprint"]` 与调用方 `before["fingerprint"]`、`navigation_fingerprint`、`matcher.match()` 逻辑未改；batched 只是更快地拿到同样的 `current` |
| epoch / lease / session | 未改：`generation` 校验、`_check_generation`、lease TTL、`_worker` 队列语义原样保留 |
| observation freshness (15s) | 未改；`cached_observation` 仍按 monotonic TTL 判定 |
| target identity / fingerprint | 未改；catalog 的 `target_fingerprint` 仍由 `canonical(node)` 计算 |
| tree/page consistency | batched 额外要求 `screen_before==screen_after`、`foreground_before==foreground_after`、tree 指纹自洽；FULL 仍要求 image 被 tree/display 双侧夹住（`tree_after` + `display_after` 仍在事务内按序采样） |
| 不可信抓屏不可执行 | `actionable=false` 时**不入缓存**，`act` 直接 `stale_observation`；离线用例覆盖 |
| unknown-write 不自动重试 | 未改：`execution_unknown` 仍进入 reconciliation；本轮没有任何自动重放路径 |
| 不用无限 retry 换成功率 | batched 回退有界（≤3 次后永久降级）；屏幕恢复仍是有界一次唤醒/解锁；setup 的 stale/无进展预算未放宽 |
| TEMPORAL 旧观察 | 未改：TEMPORAL 帧仍 `actionable=false`，且不参与本次复用 |
| 锁屏语义 | **P0.5-A.1 修正后**：legacy 仍先 `_ready_screen` 再读 tree；batched 的 readiness transaction 在 hierarchy/screenshot 之前 fail closed，不可信时后续 capture transaction **根本不会下发**。A.1 之前的实现只做到"事务内读屏后判定、不可信即丢弃"，即设备仍然执行了 `uitest dumpLayout`——该缺口已在 A.1 关闭，详见本文档第 I 节与 `p05a1-observation-readiness-gate.md` |

## D. Benchmark Before / After

设备：`5XM0226105001134`，页面：Weibo（`--prepare-bundle com.sina.weibo.stage`），
同一运行时配置、同一 feature flag 取值（0 / 1），legacy 相先跑。

**FAST（30 次/相，`bench-fast.json`）**

| 指标 | legacy | batched | 变化 |
|---|---|---|---|
| client p50 | 2653 ms | 1736 ms | **-34.6%** |
| client p95 | 2712 ms | 1795 ms | **-33.8%** |
| capture p50 / p95 | 2625 / 2703 ms | 1719 / 1781 ms | -34.5% / -34.1% |
| host↔device round trips | 6 | **1** | -83% |
| 成功样本 | 30/30 | 30/30 | — |
| 一致性原因 | consistent ×30 | consistent ×30 | — |

batched 设备侧分段（同一次测量的 `perf`，p50 ms）：
`screen 90 + foreground 160 + display 70 + tree 830 + foreground_after 150 +
screen_after 100 ≈ 1400`，其余为传输固定成本与载荷。

**FULL（20 次/相，`bench-full.json`；`batched-only` 为 `bench-full-batched.json`）**

| 指标 | legacy | batched（A/B，含掉屏样本） | batched（干净重跑 20/20） |
|---|---|---|---|
| client p50 | 4670 ms | 3414 ms（-26.9%） | 3478 ms |
| client p95 | 4793 ms | 3583 ms | 3541 ms |
| round trips | 9 | **1** | **1** |
| 成功 | 20/20 | 16/20 | 20/20 |

说明：A/B 里 batched 相有 4 次 `screen_state_unknown`（手机在长跑中熄屏，
按原有语义直接拒绝，未自动重试），保留在分母里；因此 FAILED 的 4 个样本没有隐藏。
干净重跑（同样 20 次、同样页面）为 20/20，p50 3478ms。

**Act path（各 5 次，tap“首页”，`bench-act.json`，均值 ms）**

| 阶段 | legacy | batched |
|---|---|---|
| caller observation | 2492 | 1557 |
| preflight（派发前真实读取） | 2520 | 1567 |
| dispatch | 计入 act 总时长 | 计入 act 总时长 |
| post observation | 2344 | 1271 |
| act 总时长 | 5500 | 3708（**-32.6%**） |
| 实际 executed | 4/5 | 3/5（其余为派发前安全拒绝，保留在分母） |

## E. Harness Efficiency

本轮**没有**跑正式 M0/M1 长批（按你的要求，先不要把长批当门禁）。因此
observations/run、setup observations、observations/dispatch、time-to-first-dispatch
这些**端到端数字本轮未采集**，不编造。

已确定的代码级变化（每处都有离线用例或字段可验证）：

```text
setup_act  : act 之后不再无条件 observe —— 先消费动作本身的 post observation，
             只有它没有体现状态变化（或缺失）时才补采一次
M1 run_step: 返回 (record, post_observation)；run_once 删除 step 尾部 observe()
runner     : _run_subgoal / _early_progress 先消费 post_observation
stable     : 自洽快照直接接受（原先固定 ≥2 次），只有不可信才补采
```

预期效果（结构推导，非实测）：一次 tap/subgoal 的观察次数由
`early_progress observe + attempt observe(FULL) + preflight + post` 降到
`preflight + post`，即 observations/dispatch 至少下降 40%，
且 setup 每个状态迁移只消费一个新的 live observation。**正式数字需要 Gate 3 补采。**

## F. Tests

```text
命令   python scripts/reproduce.py
离线   774 tests，0 failed / 0 error / 0 skipped（116s）
        基线 745 → 774（+29）
新增   tests/test_snapshot_provider.py：28 项
真机   Gate 1 benchmark（FAST/FULL/act，全部走 MCP stdio + 常驻服务）
       未跑 Gate 2 / Gate 3（见 H）
```

新增用例覆盖（全部为真实失败模式，不是 happy path）：

```text
完整事务按序分段
截断（缺 :done） / 重复 marker / 段序颠倒 / 外部 marker / 段内未标注输出
某条命令 rc != 0 / 超长事务
脚本只用固定命令词表（不允许 $() 与反引号）
payload 命令（cat / base64）永不被 time 包装
normalized 结果：provider=batched、round_trips=1、树/显示/前台字段正确
抓屏窗口内 screen 变化 / foreground 变化 / foreground unstable
tree JSON 损坏 / display 几何非法 / 设备传输故障向上抛
flag 关闭走 legacy（含 fallback_reason=flag_off、round_trips=4）
设备无 batch_probe 走 legacy
默认走 batched
批处理构帧失败 → 回退一次 → 连续 3 次后永久降级
一次成功会清零失败计数
Runtime 层：FAST 1 次传输即可执行、不可信抓屏不 actionable 且不入缓存、
不可信抓屏不能授权动作、目标指纹变化仍拒绝、preflight 是新的设备读取、
post observation 可被自身 id 复用、act.perf 指标齐全
```

## G. Remaining Bottleneck

一次 batched FAST 事务（p50 1719ms）的真实构成：

```text
uitest dumpLayout（tree）      830 ms   48%   <- 现在最大的单项
hidumper 探针（11 条）        570 ms   33%
cat 载荷（~283KB）            100 ms    6%
hdc shell 固定成本            125 ms    7%
其余（host 解析 / IPC）         ~90 ms
```

**tree 是下一个瓶颈**。同一台设备上 `Captures.captureLayout`（RPC，长连接）
实测 594ms，而事务内的 `uitest dumpLayout` 830ms——差值是 CLI 进程启动与文件中转。
要把 FAST p50 从 -34.6% 推到 ≥-45%（即 ≤1460ms），只能从 tree 上省 ~260ms，
可选路线各有代价，本轮**没有**擅自采用：

```text
1) 事务外的 RPC tree  : <2 次传输就无法夹住 tree，等于放宽 tree/page 一致性  -> 安全语义变化
2) 3 次传输           : RT1 前后探针 - RT2 RPC tree - RT3 后探针，约 1.29s
                        但违反“host-device round trips <= 2”的硬指标
3) 常驻 shell 会话    : 省掉每次 125ms 的 hdc 启动成本，约 -7%，对 tree 无效
4) 换 screen 证据来源 : ScreenlockService 单条即可同时给出 screenState/screenLocked，
                        可省 2×100ms，但改变 screen_on 的证据来源，需要你确认
```

不建议继续优化第二瓶颈（`hidumper` 探针数），它已经接近设备侧下限。

## H. Recommendation

```text
STOP_AND_FIX（性能门槛）
```

理由与分项：

```text
安全验收（硬标准）      全部满足
  stale 错误派发        0（离线用例 + 真机 A/B 中 0 次派发）
  epoch mismatch        0（没有任何路径改动 epoch 判定）
  expired observation   0
  unknown write 自动重试 0（无新增重试路径）
  unregistered candidate 0（registry/resolve 未改）
  safety regression     0（774 项离线全绿，含全部原有 stale/锁屏/授权用例）

性能验收
  往返次数 6 -> 1（FAST）/ 9 -> 1（FULL）  达成（要求 <=2，优选 1）
  FAST p50 -34.6%（要求 >= -45%）         未达成
  FAST p95 -33.8%（要求 >= -35%）         未达成（差 1.2pt）
  FULL p50 -26.9%（要求 >= -30%）         未达成（差 3.1pt）
  Act 总时长 -32.6%                       达成方向正确
  目标值 0.6-1.2s                        未达成（1.74s）

本轮可以合并的部分
  传输合并 + 一致性语义 + 遥测 + 回退 + 29 项离线测试：建议合并
  harness 去重（post observation 复用 / stable 自适应）：建议合并
  但正式 M0/M1 不应在本 revision 上直接开跑

未完成的门禁
  Gate 2 每 primitive 10-20 次冒烟：未跑
  Gate 3 3 个低风险 M1 任务 × 3 次：未跑
  dynamic stress 分桶（微博发现页）：未跑

建议的下一步（只做一件）
  先决定 G 节的 tree 路线：如果接受“tree 与前后探针分属不同传输”，
  用 2 次传输（RPC tree + 单次 shell 探针事务）可到约 1.0-1.1s；
  若要严格保持 tree 在窗口内，则改为放宽“<=2 次传输”为“<=3 次”，
  用两次 shell 夹一次 RPC，可到约 1.29s。两条都能过 >= -45% 与 -35%，
  但都需要你确认哪一条约束是可以让步的。
```

本轮没有触碰：Decider accuracy/canary、RSI curriculum、memory learning、
long-task context compression、UI console、多设备/多模型并发、新 VLM、OCR 重构、
自动放宽 stale threshold、Action Burst 新功能。Burst/Wait 只做了回归（离线全绿）。
