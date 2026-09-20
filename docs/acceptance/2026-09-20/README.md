# 验收证据索引 · 2026-09-20

本目录保存本轮 HarmonyOS Agent v3.1 实施与验证的可提交证据。所有报告只含元数据：
不含 UI 文本、截图、输入内容、账号或设备序列号；原始图像与私有页面留在本地受控
`.runtime/` 目录，不进入版本库。

| 文件 | 对应任务 / 门槛 | 状态 |
|---|---|---|
| `baseline-manifest.json` | A01 工作树与依赖冻结 | 见文件 |
| `m0-primitives.json` | C04 / M0 原语矩阵（每原语 25 次） | **173/175 通过（98.86%）**，见下 |
| `m1-weibo.json` | B02/B03 / M1 十任务三十次（单批） | **22/30 成功**，未达 27/30，见下 |
| `m1-weibo-rerun.json` / `m1-weibo-rerun2.json` | 修复后按影响范围复验 | m1_06 3/3、m1_08 3/3 恢复；m1_05 仍未通过 |
| `decider-shadow.json` | F05 / DF3 影子运行 | **通过（零派发）**，12 个状态全部记录原生分数 |
| `decider-dataset.json` / `decider-calibration.json` | F04 / DF2 中文数据集与校准对照 | **45 个真实状态（20/13/12）**；规则覆盖 86.7%、放行错误 0；Decider 覆盖 0% → `keep_shadow_only` |
| `agent-client.json` | B01 真实客户端接入 | 见 `check_agent_link.py` 输出（本地） |
| `m1-weibo-batch2.json` | B03 修复后的正式批次（10 任务 × 3 次） | 见文件 |
| `m1-weibo-batch2b.json` | B03 续跑（中断后续验剩余 3 任务 × 3 次） | 9/9 成功 |
| `benchmark-c01-*.json` | C01 热基线（每模式 100 次） | 见文件；冷基线未产出（无受控冷启动流程） |
| `burst-arbitration.json` | C03 burst 能力裁决 | 见文件 |

## 本轮结论摘要

**M0（原语矩阵，`--per-primitive 25`）**

| 原语 | 尝试 | 成功 | 成功率 | P50 | 观察到的失败 |
|---|---:|---:|---:|---:|---|
| launch | 25 | 25 | 100% | 5011 ms | — |
| tree | 25 | 25 | 100% | 3890 ms | — |
| screenshot | 25 | 24 | 96% | 3790 ms | 1 次 `screenshot_inconsistent`（图树括号不一致） |
| swipe | 25 | 25 | 100% | 5589 ms | 4 次派发前 `stale_observation`（有界重观察后成功） |
| tap | 25 | 25 | 100% | 4457 ms | 1 次 `stale_observation` |
| back | 25 | 24 | 96% | 5008 ms | 1 次 `search_editor_unavailable`（进入搜索页的 setup 未在预算内完成） |
| input | 25 | 25 | 100% | 7598 ms | — |

**未达 99% 门槛**：screenshot 与 back 各 1 次失败，均为可解释的单点问题（动态信息流下的图树一致性、
搜索页 setup 预算），不是设备不可用。**样本量 25 也不足以证明 99%**；正式 M0 仍需每原语 100 次。

**M1（十任务各 3 次，单批 30 次）**：22/30 成功（5 blocked，3 failed）。

- 稳定通过：m1_01 打开搜索页、m1_02 中文输入、m1_03 英文输入、m1_04 清空后重输、
  m1_07 回首页、m1_09 信息流滑动、m1_10 标签往返（各 3/3）。
- m1_05 提交搜索：3/3 blocked。根因是**动作词汇缺口**——本机微博搜索编辑页右上角是「取消」，
  提交依赖输入法回车；Runtime 目前没有键位动作，因此该任务当前不可完成（不是脚本写错）。
- m1_06 返回发现页：3/3 failed，判定条件写得过强（要求输入框消失）。改为
  `page_changed` 后复验 **3/3 通过**。
- m1_08 打开消息页：1/3，原因是「消息」文本不唯一。改为按标签的可点击祖先（最紧容器）
  定位后复验 **3/3 通过**（其中 1 次因前一次中断导致 worker 隔离而重跑）。
- 修复后按影响范围复验的聚合为 **27/30**，但这是「原始批次 + 定向复验」的合并结果，
  不是同一批 30 次连续运行。**正式关闭 M1 仍需在冻结代码上重跑完整 30 次。**

**DF3 影子运行（12 个真实状态）**：零派发已验证（`dispatches_issued_by_shadow=0`）。

- 规则给出唯一候选并 `route=execute`；本机 Decider 在 6/6 个有候选的状态上选择「以上皆非」，
  confidence 0.84–0.89、certainty 0.37–0.49。
- 这说明**未校准的 Decider 不能进入执行路径**：与规则建议一致率为 0，维持 `local_shadow`。
- 延迟 P50 115 ms、P95 386 ms（含服务往返）；无 503/504，无回退。

**未完成**：F04 数据集与阈值拟合（工具已实现、未运行）、M2/M3、H02/H03/H06、OCR/VLM 接入。

## M1 修复后复验（batch2 + batch2b）

修复 m1_05（改为点击搜索建议行）、m1_06（判定改为 `page_changed`）、m1_08（按标签的
可点击祖先定位）之后，重新执行 10 任务 × 3 次：

- `m1-weibo-batch2.json`：跑到第 22 次时被一次设备 worker 隔离中断（
  `device_quarantined`），已修复为客户端自动 `recover` 后重试未派发的调用。
- `m1-weibo-batch2b.json`：接续执行剩余 3 个任务 × 3 次，9/9 成功。
- 合并去重后覆盖 **30 个 (任务, 运行) 组合，全部 30 次成功**，无未决写入、
  无错误完成声明；门槛 27/30 达标。

**口径说明**：30 次成功分布在两个批次（batch2 因基础设施缺陷中断），不是同一次
连续 30 次；正式冻结版本前仍建议重跑一次不中断的 30 次。批次内的每次运行都是独立
任务、独立判定，不存在用成功样本补足分母。

## F04/DF2 预备观察（合成对照，不构成校准结论）

### 真实数据结果（45 状态，2026-09-20）

| 组 | 样本 | 覆盖率 | 放行错误 | Brier | ECE | 结论 |
|---|---:|---:|---:|---:|---:|---|
| 规则基线 | 45 | 86.7%（39 放行） | 0 | 0.0 | 0.0 | 当前可用路径 |
| Decider（未校准） | 45 | **0%**（全部拒答） | — | — | — | 无阈值可拟合 |
| Decider（校准集拟合阈值后） | 45 | 0% | — | — | — | 保留集判定 `keep_shadow_only` |

- 6 个状态本身没有候选（`tap_absent` 与采集末尾的设备不可用），规则与 Provider 都按「重观察」处理，
  其中 6 次对模型的调用被正确拒绝（`invalid_request`：零候选不调用模型）。
- 结论：**本机 Decider 在真实中文鸿蒙/微博状态上尚不具备可用覆盖**，保持 shadow；
  合成对照（下表）说明差距来自状态与候选的渲染方式，而不是模型能力。

在校准数据采集之前，先做了一次**合成对照**（状态与候选都是人工构造的两候选/单候选中文状态，
经本机 Decider 实跑）：

| 状态形态 | 样本 | 选择正确 | 置信度 | 说明 |
|---|---:|---:|---|---|
| 单候选 + 「以上皆非」 | 2 | 2/2 | 0.951 / 0.985 | 正确选择目标标签 |
| 两候选 + 「以上皆非」 | 2 | 2/2 | 0.971 / 0.949 | 正确选择目标标签 |

同一模型在**真实页面影子运行**中 6/6 选「以上皆非」（confidence 0.84–0.89）。
差异出现在状态与候选的渲染方式，而不是模型是否具备该判断能力。这直接指向 F04 的校准对象：
状态裁剪、候选描述措辞与拒答阈值必须用真实数据拟合，合成样本不能替代。

实验数据与报告保存在本地 `.runtime/evals/decision/`（含真实页面文本，不进入版本库）。

## 复现方式

```powershell
# 1 基线冻结（只读，不触发设备动作）
.\.venv\Scripts\python.exe .\scripts\evidence_manifest.py

# 2 回归入口（不连接手机）
.\.venv\Scripts\python.exe .\scripts\reproduce.py

# 3 需要真机与常驻服务的验收（显式 --execute）
# 一次验收批次必须显式复用同一个 $stateDir（不要依赖脚本默认的 .runtime/agent-state）
.\.venv\Scripts\python.exe .\scripts\check_agent_link.py --state-dir $stateDir `
  --report ".runtime\acceptance-$stamp\agent-link.json"
.\.venv\Scripts\python.exe .\scripts\accept_m0_primitives.py --execute --per-primitive 100 `
  --report .\docs\acceptance\2026-09-20\m0-primitives.json
.\.venv\Scripts\python.exe .\scripts\accept_m1_weibo.py --execute --runs 3 `
  --report .\docs\acceptance\2026-09-20\m1-weibo.json
.\.venv\Scripts\python.exe .\scripts\decider_shadow.py --execute --rounds 4 `
  --report .\docs\acceptance\2026-09-20\decider-shadow.json
```

## 前置条件

1. 真机通过 HDC 连接，`hdc list targets` 恰好一台。
2. 决策服务运行：`services/decider/Start-Decider.ps1`，`Test-Decider.ps1` 返回 `ok=true`。
3. Runtime 服务以 v2 工具启用：

```powershell
$env:HARMONY_HDC='F:\DevEco Studio\sdk\default\openharmony\toolchains\hdc.exe'
$env:HARMONY_AGENT_TOOLS='1'
$env:HARMONY_AGENT_PROFILE='local_shadow'
.\.venv\Scripts\python.exe -m harmony_runtime.cli serve --state-dir .runtime/agent-state
```

`HARMONY_AGENT_TOOLS` 未启用时，MCP 客户端仍只看到 v1 的六个工具（A02 兼容要求）。
