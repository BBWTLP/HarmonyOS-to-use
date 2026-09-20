# v3.2 真机验收 · RC `80e2898` · Stage 0–7（只读部分）

执行日期：2026-09-20（Asia/Shanghai）
被测对象：`feat/runtime-foundation` 上的冻结 RC，**不是分支 HEAD**

## 0. 冻结与被测构建

```text
repository          https://github.com/BBWTLP/HarmonyOS-to-use
branch              feat/runtime-foundation
RC SHA              80e28988c98e4f8cb75c3f4d976d637eaa31c22a
分支 HEAD（取码时）  fdc4e63（docs-only，只写入了 RC SHA）
HEAD == RC SHA      true
working tree        clean（git status --porcelain 为空）
requirements.lock   sha256 18a991d5…a126055（与 Gate 记录一致）
```

RC 来源：`docs/v3.2/pre-device-gate.md` 第 8 节与 `docs/v3.2/device-test-handoff.md` 第 0 节。

## 1. 测试环境

| 项 | 值 |
|---|---|
| OS | Windows 11 专业版 10.0.26200（64-bit） |
| Python | **3.13.14**（本机无 `py -3.11`，按计划使用可用的 3.11+ 并记录真实版本；参考 Gate 为 3.11.9） |
| HDC | `D:\DevEco Studio\sdk\default\openharmony\toolchains\hdc.exe`，Ver 3.2.0f |
| 设备数量 | 恰好 1 台（脚本未自动挑第一台） |
| Decider profile | `local_off`（全程固定，未使用 `local_canary`） |

设备（已脱敏，不含序列号）：

| 字段 | 值 |
|---|---|
| model | SGT-AL10 |
| software_version | SGT-AL10 6.1.0.135(SP8C00E120R3P11) |
| os_full_name | OpenHarmony-6.1.1.120 |
| api_version | 24 |
| uitest | 6.0.2.3（predicted protocol 2） |
| display | 1320 × 2848，rotation 0 |
| screen_state | screen_on=true，screen_locked=false |

## 2. Stage 3 — 离线回归（未通过）

命令：

```powershell
.\.venv\Scripts\python.exe .\scripts\reproduce.py
.\.venv\Scripts\python.exe -m pip check
```

门槛：`failed=0 error=0 unexpected skip=0`。

| 运行 | 结果 | failures | errors | 说明 |
|---:|---|---:|---:|---|
| 1 | exit 1（Ran 607） | 12 | 2 | 14 项因缺少未跟踪的本地 Decider token 而失败 |
| 2 | exit 1（Ran 607） | 0 | 2 | `KeyError: 'result'` |
| 3 | exit 1（Ran 607） | 0 | 1 | `KeyError: 'result'`（换了一个测试） |

`pip check`：`No broken requirements found.`

### 2.1 失败分类

**OFFLINE-1 — 离线套件不是 fresh-clone 可复现的（14 项）**

`DeciderProvider` 的 `token_file` 默认值是**相对路径** `services/decider/.runtime/api-token`。
该文件被 gitignore，只在首次启动 Decider 服务时由 `Start-Decider.ps1` 生成。
全新 clone 中它不存在，于是 provider 在调用注入的假 transport **之前**就抛
`token_unavailable`，14 项 provider/decision 测试因此失败。
按 `Start-Decider.ps1` 同样的方式（32 字节随机数的 base64）补齐该本地文件后，这 14 项全部恢复。

含义：Gate 报告里“607 项全绿”依赖一个**未提交的本地工件**，不能在新机器上原样复现。

**OFFLINE-2 — 终态先于结果落库（读后终态竞态）**

`src/harmony_agent/supervisor.py:416` 在 run loop 返回 `reconciliation` 之前就把任务状态写成
`RECONCILIATION_REQUIRED`；而最终 `result` 要到 `_finish()` 才持久化。
因此“轮询到终态 → 立刻读 `task_result`”可能拿到 `available=false` 且没有 `result` 键。

证据：3/3 次全量运行失败，每次 1–2 项，且**每次落在不同的测试**
（`test_agent_tasks`、`test_agent_runner_boundaries`、`test_offline_fault_matrix`）；
同样的测试单独运行 2/2 通过。报错位置都是同一句
`self.host.task_result(...)["result"]`。

含义：这是“终态已发布、结果尚不可读”的顺序缺陷。对 release gate 而言它属于**可观测性契约**问题，
不是设备安全问题；它是本轮停止真机写测试的直接原因。

### 2.2 已应用的停止规则

按计划 Stage 3：“不通过 → **停止真机写测试**”。因此 Stage 8 及之后所有会产生设备写入的
阶段（M0 / M1 / C01 / C03 / OCR / VLM / M2 / 100-step / 故障矩阵）**未执行**。

## 3. Stage 4–7 — 只读门禁（不写入设备）

独立测试状态目录：`.runtime/acceptance-20260920-112150`（不复用开发机旧 `.runtime/agent-state`）。

| 检查 | 命令 | 结果 |
|---|---|---|
| Doctor | `harmony-runtime.exe doctor` | **ok**；依赖齐全，device_count=1；`phone_operation_verified=false`（doctor 不证明可操作） |
| Baseline | `harmony_runtime.cli baseline --execute` | **ok**；见上表设备字段；`acceptance_verified=false` |
| Protocol | `harmony_runtime.cli protocol --execute` | **ok**；protocol 2、`localabstract:uitest_socket`、session echo=true、port-forward=true、只读 RPC `Driver.findComponents` 返回（`result` 为 null） |
| Device smoke | `tests/smoke_device.py` | **ok**；`phone_observation_verified=true`，catalog 241，`image_tree_consistent=true` |
| 常驻服务 | `serve --state-dir …` | 正常启动（endpoint.json + journal 建立） |
| MCP Probe | `harmony-runtime.exe probe` | **最终 ok**；首次 2 次 `not_ready`，之后 18/18 连续通过 |
| Agent link | `check_agent_link.py --state-dir … --report …` | **ok**（第二次尝试，14.3 s）；6 个 v1 工具可见；session epoch 0、device_bound；FAST/FULL 均 actionable；图片 1320×2848 且图树一致；`unresolved=0` |

注意：计划文档 Stage 7 给出的命令**没有传 `--state-dir`**，而脚本默认 `.runtime/agent-state`。
按计划 Stage 4“后续所有必须使用同一个 `$stateDir`”的要求，本轮显式传入了同一状态目录。

### 3.1 观察一致性余量偏小（记录，非结论）

`image_tree_consistent` 要求：截图前后 UI-tree 指纹相同、`image_tree_skew_ms ≤ 1000`、图像尺寸匹配。
实测 skew 稳定落在 **747–976 ms**，即距阈值只有 2%–20% 余量；失败的观测 catalog 只有 99–140 个
节点（正常 241–259）。服务刚启动后的前 2 次观测失败，之后 18 次连续通过。

### 3.2 一次未复现的停顿（记录，需复现后才能定性）

`check_agent_link.py` 首次按正确状态目录运行时，`connect` 与 `session_open` 成功（epoch 0、
device_bound=true），但紧接着的第一次 `observe` 超过 9 分钟未返回。同一时刻独立 `probe`
仍能在 7.4 s 内返回 ok；随后用等价调用序列（initialize/list_tools/open/observe FAST/FULL/
FAST+image/status/close）复跑 15 s 全部成功，`check_agent_link.py` 再跑也 14.3 s 通过。
该停顿**未复现**，暂不作为缺陷结论，列为需要复现的观察项。

## 4. 判定

```text
Stage 0  冻结与版本         GREEN
Stage 1  依赖环境           GREEN（Python 版本偏离，已记录）
Stage 2  HDC/设备唯一性     GREEN
Stage 3  离线回归           RED（写测试停止条件）——失败原因已定位，非设备问题
Stage 4  状态目录隔离       GREEN
Stage 5  只读设备门禁       GREEN（记录 skew 余量小）
Stage 6  常驻服务 + probe   GREEN（服务冷启动前 2 次 not_ready）
Stage 7  Agent link         GREEN（含一次未复现停顿）
Stage 8+ 写路径             NOT_RUN（按 Stage 3 停止规则阻断）
```

## 5. 局限

- 本轮**没有**任何 v3.2 写路径真机证据：M0/M1/C01/C03/视觉/M2/100-step/故障矩阵全部未执行。
- 离线回归在有真机的测试机上**没有一次**全绿，因此无法声明“离线基线在本机复现”。
- Python 版本偏离参考 Gate（3.13.14 vs 3.11.9），未做双版本对照。
- `image_tree_consistent` 的 skew 余量偏小，冷启动阶段已观测到一致性失败；本轮的
  受控冷/热基准与 Stage 11 的 cold baseline 结论无关（无受控冷启动流程，`cold_baseline=not_produced`）。
- 本文件与 `environment.json` 只含元数据；截图、原始 UI 树、输入内容、账号与设备序列号
  均留在本地 `.runtime/`，不进入版本库。

## 6. 建议的下一步（由开发侧执行）

1. 修 OFFLINE-2：让“终态可见”与“结果可读”成为同一原子事件（先落库 result 再置终态），
   或让验收读取方等待 `available=true`，并补一条针对该顺序的回归测试。
2. 修 OFFLINE-1：测试不应依赖仓库内未跟踪的本地 token；由测试注入临时 token 路径，
   或在测试 setup 中生成该文件。
3. 重新跑 Pre-Device Gate，冻结**新的** RC SHA，然后从 Stage 3 重新开始；
   不要用修好之前的旧分母继续 M0/M1。
4. 若要继续设备侧工作：`check_agent_link.py` 的命令行应显式包含 `--state-dir`。
