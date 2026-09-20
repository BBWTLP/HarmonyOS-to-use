# v3.2 真机第二轮 · RC `b4049f5` · Stage 3–8

执行日期：2026-09-20（Asia/Shanghai）
被测构建：`b4049f50b851241817e0be577e080bcbcf8519a8`（`git rev-parse HEAD` 与之一致，工作树 clean）

## 1. 环境

| 项 | 值 |
|---|---|
| OS | Windows 11 专业版 10.0.26200 |
| Python | 3.13.14 |
| HDC | Ver 3.2.0f（`D:\DevEco Studio\sdk\default\openharmony\toolchains\hdc.exe`） |
| 设备 | SGT-AL10 · 6.1.0.135(SP8C00E120R3P11) · OpenHarmony-6.1.1.120 · API 24 · 1320×2848 |
| uitest | 6.0.2.3（protocol 2） |
| Decider | `local_off`（全程固定） |
| 状态目录 | `.runtime/acceptance-20260920-140508`（本地，不入库） |

## 2. Stage 3–7（全部 GREEN）

```text
Stage 3 离线回归
  reproduce.py           Ran 618 tests，exit 0
  unittest discover      Ran 618 tests，OK
  pip check              No broken requirements found.
Stage 5 只读门禁
  doctor                 ok，device_count=1
  baseline --execute     ok（见上表设备字段）
  protocol --execute     ok：protocol 2、localabstract:uitest_socket、session echo、port-forward
  tests/smoke_device.py  首次 not_ready（冷启动图树不一致），随后 ok/ok
Stage 6 常驻服务 + probe
  probe                  ok：phone_observation_verified=true，catalog 241，capture 6.2s
  观察稳定性采样         12/12 consistent，skew 636–765 ms
Stage 7 agent link
  check_agent_link       ok（14.2 s）：6 个 v1 工具、epoch 0、device_bound、
                         FAST/FULL actionable、图片尺寸匹配、unresolved=0
```

## 3. Stage 8 — M0 小样本（写路径）：NOT_READY

### 3.1 三次运行结果

| 运行 | 起始状态 | attempts | success | rate | status |
|---|---|---:|---:|---:|---|
| `m0-smoke-3.json` | 手机停在**桌面**（`com.ohos.sceneboard`） | 21 | 10 | 47.6% | not_ready |
| `m0-smoke-3-retry1.json` | 微博前台（先 launch 校正） | 21 | 16 | 76.2% | not_ready |
| `m0-smoke-back-input.json`（`--only back input`） | 微博前台 | 6 | 2 | 33.3% | not_ready |

第二次运行的逐原语结果：

| 原语 | 尝试 | 成功 | 成功率 | P50 |
|---|---:|---:|---:|---:|
| launch | 3 | 3 | 100% | 5730 ms |
| tree | 3 | 3 | 100% | 5370 ms |
| screenshot | 3 | 3 | 100% | 4735 ms |
| swipe | 3 | 3 | 100% | 7003 ms |
| tap | 3 | 3 | 100% | 5858 ms |
| input | 3 | 1 | 33% | 8722 ms |
| back | 3 | 0 | 0% | — |

### 3.2 失败分类（全部为 setup，不是写失败）

```text
back   0/3   search_editor_unavailable（setup 预算内没到达微博搜索编辑页）
input  1/3   search_editor_unavailable
launch 0/3（第一次运行）stale_observation —— 已核实那次手机停在桌面
       （观测到 catalog 全部为 com.ohos.sceneboard，foreground_bundle=null）；
       是「启动动画/桌面动态」还是别的机制导致观察过期，本轮未隔离
screenshot 0/3（第一次运行）screenshot_inconsistent —— 冷启动阶段图树括号不一致
```

### 3.3 根因（受控复现）

用与 `accept_m0_primitives.ensure_search_editor()` 完全相同的循环逐步复现：

```text
从「发现」页点搜索框：
  第 1 次 tap → refused: stale_observation
  第 2 次 tap → refused: stale_observation
  第 3 次 tap → ok/verified → 进入搜索编辑页

另一次同样从「发现」页起步：
  连续 7 次 tap → 全部 refused: stale_observation → 预算耗尽（7 次上限）
```

即：微博「发现」页是**持续变化的动态信息流**，观察指纹在 observe 与 dispatch 之间改变，
Runtime 因此按设计在**派发前**拒绝（`stale_observation`，`not_dispatched`）。
业务写入没有任何一次失败——被拒绝的是「还没发生的写」。

而 `back` 与 `input` 两个原语**必须先到达搜索编辑页**，路径恰好要经过这次在动态页面上的点击；
该点击的成功率低于 harness 的 7 步 setup 预算，于是两个原语被判失败。

### 3.4 安全侧证据（关键）

```text
journal.actions        36 条，state 全部 executed
incidents              0
unresolved_actions     0
recovery_required      false
execution_unknown      0
重复写 / 错误目标执行   0
```

三次 M0 运行都没有触发未知写屏障，也没有出现错误的成功宣告。

## 4. 判定与停止理由

```text
Stage 3 离线回归        GREEN（618 tests，exit 0，pip check clean）
Stage 4 状态目录隔离     GREEN
Stage 5 只读门禁         GREEN（记录冷启动 1 次图树不一致）
Stage 6 常驻服务 + probe GREEN
Stage 7 agent link       GREEN
Stage 8 M0 小样本        YELLOW→NOT_READY（back/input 系统性失败，根因明确）
正式 M0 25/100          NOT_RUN
Stage 9 及以后           NOT_RUN
```

按验收计划 Stage 9.1「存在系统性失败 → 先修 bug、重新冻结 RC、从 Stage 0/受影响最低 Stage 重来」，
本轮**没有**继续放大样本，也**没有在测试机上手改代码**（HEAD 始终等于 RC SHA，工作树 clean）。

## 5. 需要开发侧决定的修复方向（本轮不实施）

1. `accept_m0_primitives.py` 的 setup 会计与预算：`back`/`input` 的失败码是
   `search_editor_unavailable`（setup 未达成），却计入**原语成功率**——与该脚本自己文档里
   「页面状态问题属于 setup，不算原语失败」的口径不一致；同时 7 步预算对动态页面偏紧。
2. 动态页面上的可行动性：微博「发现」页的指纹在秒级内变化，导致合法点击几乎总是被
   `stale_observation` 拒绝。需要决定是「提高有界重观察次数/换更稳定的入口」还是
   「对特定只读导航动作放宽 staleness」（后者涉及安全语义，必须单独评审）。
3. 冷启动鲁棒性：服务刚起/界面刚切换时，`smoke_device` 与 `screenshot` 会出现
   `image_tree_consistent=false`（skew 636–765 ms，阈值 1000 ms 未改动）。

## 6. 未执行（不得声明通过）

```text
M0 25 / M0 100 正式矩阵
M1 10×1 / 10×3
C01 冷/热基准（cold baseline 仍未产出）
C03 burst 裁决
OCR / VLM 真机视觉链
M2 30 / 90 / 300
100-step 稳定性
设备故障矩阵
Decider P2 采集
```

证据目录（本地，不入库）：`.runtime/round2/`、`.runtime/acceptance-20260920-140508/`。
原始路径与命令见本文件；报告只含元数据，不含 UI 文本、截图、输入内容或设备序列号。

## 7. 根因（第三轮修复期间在真机上确认）

失败根因不是「动态信息流让整页指纹变化」这一笼统说法，而是两件可测量的事实：

### 7.1 桌面被误判成微博「发现」页（会话级）

第一次 M0 运行前手机停在桌面：观测到 `foreground_bundle=null`，
catalog 全部是 `com.ohos.sceneboard`。旧 `surface_kind()` 只用
「顶部一个可点击 Flex」这一条通用特征就判成 `discover`，
于是 setup 反复点桌面 UI，`back` 的 setup 烧掉 38–52 个动作仍到不了搜索编辑页。

### 7.2 目标自身不稳定（控件级，主导原因）

在「发现」页连续采样同一个搜索入口（`action_id=n1`，bounds `[47,154,1273,276]`，
`hierarchy` 与 `host_window_id` 完全相同，无 `resource_id`、无 text/hint/description）：

```text
sample 0  accessibility_id = 31985
sample 1  accessibility_id = 31995
sample 2  accessibility_id = 32000
```

即该控件的 `accessibilityId` 是**自增计数器**，不是语义标识；同时
`target_fingerprint`（对子树取哈希）每次采样都不同。
两者都会被派发前的目标比较命中，于是同一次「点搜索框」被
`stale_observation` 拒绝（受控复现中曾连续 7/7 次拒绝，另一次 2 次后成功）。

### 7.3 与之无关：Runtime 拒绝了不该拒绝的安全动作

从未发生。全部 `stale_observation` 都在**派发前**，`execution_status=not_dispatched`；
journal 36 条动作全部 `executed`，`incidents=0`，`unresolved_actions=0`。

### 7.4 结论

`back`/`input` 依赖的「进入搜索编辑页」在这台设备的这版微博上缺少稳定可操作目标：
方案 A（找一个更稳定、粒度更小的可点击节点）在本页不可行（该 Flex 下唯一子节点只出现 2/4 次采样），
方案 B（local action fingerprint）按定义仍包含 `accessibility_id`，对自增 ID 同样无效。
因此本轮**不修改 Runtime 的安全语义**，只修 harness 的会计、页面分类、setup 预算与重观察逻辑，
并把 setup 不稳定性如实报告为 `insufficient_valid_samples`。
