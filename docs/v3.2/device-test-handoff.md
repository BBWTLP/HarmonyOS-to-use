# v3.2 真机测试交接单

交给"有 HarmonyOS 真机的那台电脑"执行。**只测本文指定的 RC SHA，不要测漂移中的分支 HEAD。**

## 0. 冻结信息

```text
repository   https://github.com/BBWTLP/HarmonyOS-to-use
branch       feat/runtime-foundation
RC SHA       ef2731a6c1052781922d5386e9535ef3d678a86a
Python       3.11+（本轮已在 3.11.16 与 3.13.14 上验证）
OS           Windows（脚本为 PowerShell）
test count   643 offline（0 failed / 0 error / 0 skip）
gate         Pre-Device Gate 第三轮全绿（见 pre-device-gate.md 第 10 节）
lock          requirements.lock（sha256 18a991d5…a126055）
```

注意：RC 之后可能还有一个仅修改文档的提交。真机测试要 checkout 上面这个
**代码** SHA，而不是分支最新提交。

被替换的上一轮 RC（**不要再测**）：

```text
b4049f50b851241817e0be577e080bcbcf8519a8
状态：Stage 3–7 GREEN，但 M0 smoke 判定 NOT_READY（back 0/3、input 1/3）。
原因：acceptance harness 把 setup 失败计成原语失败；桌面被误判成微博发现页；
      微博发现页搜索入口的 accessibilityId 是自增计数器、子树哈希每次变化。
      已在本轮修复（见 pre-device-gate.md 第 10 节）。

再上一轮 RC：
80e28988c98e4f8cb75c3f4d976d637eaa31c22a
状态：DEVICE_BASELINE_READY，因 Stage 3 离线回归无法 fresh-clone 复现而被拒绝。
```

## 0.1 Python 兼容性（本轮实测）

```text
Python 3.13.14   fresh clone，pip check clean，Ran 618 tests，OK
Python 3.11.16   fresh clone，pip check clean，Ran 618 tests，OK
```

3.13 是受支持版本，不是特殊环境：fresh clone 上**不需要**
`services/decider/.runtime/api-token` 就能全绿。`local_off` 主测试路径不构造 Decider，
也不读取任何 token；只有 `local_shadow` / `local_canary` 才真正需要 token 文件，缺失时按
`token_unavailable` 安全回退。需要换路径时用 `HARMONY_DECIDER_TOKEN_FILE` 覆盖。

## 1. 取代码（必须精确到 SHA）

```powershell
git clone https://github.com/BBWTLP/HarmonyOS-to-use.git
cd HarmonyOS-to-use
git fetch --all --tags
git checkout <RC_SHA>            # 不要用分支名，不要 git pull
git rev-parse HEAD               # 必须打印与上面完全一致的 SHA
git status --porcelain           # 期望：只有未跟踪的 .codex/（或为空）
```

## 2. 环境

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock -e .
.\.venv\Scripts\python.exe -m pip check
```

环境变量（真机主测试必须用 `local_off`，Decider 不参与）：

```powershell
$env:HARMONY_HDC = "F:\DevEco Studio\sdk\default\openharmony\toolchains\hdc.exe"
$env:HARMONY_AGENT_TOOLS  = "1"          # 需要 v2 工具时
$env:HARMONY_AGENT_PROFILE = "local_off" # 主测试固定值；不要设 canary
# 不要设置 HARMONY_AGENT_CALIBRATION / HARMONY_AGENT_CALIBRATION_FILE
# 不要设置 HARMONY_DECIDER_URL（除非做 P2 影子采集）
```

## 3. 先跑离线回归（确认环境无损）

```powershell
.\.venv\Scripts\python.exe .\scripts\reproduce.py
.\.venv\Scripts\python.exe -m pip check
```

期望：`Ran 643 tests` 且 `OK`。不通过就不要继续真机测试，先报告。

本步必须在**全新 clone**（无 `services/decider/.runtime/api-token`、无 `.runtime/`）上通过；
如果它只在开发机上通过，说明测试又依赖了机器本地状态。

## 4. 真机只读起点

```powershell
.\.venv\Scripts\python.exe -m harmony_runtime.cli doctor
.\.venv\Scripts\python.exe -m harmony_runtime.cli baseline --execute
.\.venv\Scripts\python.exe -m harmony_runtime.cli protocol --execute
.\.venv\Scripts\python.exe tests/smoke_device.py
```

这些命令不执行导航或输入。记录设备型号、系统发行版本、HDC/uitest 版本、截图与树是否可用。

一次真机验收批次必须**显式复用同一个 `$stateDir`**，不要依赖脚本默认目录：

```powershell
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$stateDir = ".runtime\acceptance-$stamp"
.\.venv\Scripts\python.exe .\scripts\check_agent_link.py --state-dir $stateDir `
  --report "$stateDir\agent-link.json"
.\.venv\Scripts\harmony-runtime.exe --state-dir $stateDir probe
```

## 5. 然后按 blocked-device 顺序执行

顺序与门槛见 [真机待办](blocked-device.md)：

```text
D1 设备基线
D2a M0 专项：--only back input --per-primitive 3（先跑这个）
D2 M0 每原语 100 次
D3 C01 受控冷/热基准
D4 C03 1/2/3 步 burst 仲裁
D5 M1 冻结版本单批 30 次
D6 真实 OCR / VLM 视觉执行（需先接入真实引擎）
D7 M2 30 任务 × 10 = 300 次（规格：evals/tasks/m2-30.json）
D8 100 步稳定性
D9 真机故障矩阵（USB/HDC 断连、重启、锁屏、worker hang）
```

M0 专项命令（先确认 back/input 的 setup 健康，再放大样本）：

```powershell
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$stateDir = ".runtime\acceptance-$stamp"
.\.venv\Scripts\python.exe .\scripts\accept_m0_primitives.py --execute `
  --only back input --per-primitive 3 `
  --state-dir $stateDir --report "$stateDir\m0-back-input.json"
# 通过后再跑全量 3/primitive，最后才是 25 / 100
```

M0 正式门槛（报告 `schema_version=2`）：

```text
valid_attempts == requested_samples
AND 每原语 success_rate >= 0.99
AND unresolved_actions == 0 / recovery_required == false
setup 单独报告：setup_attempts / success / failures / stale_refusals / elapsed_ms
```

注意：`back` / `input` 的 setup 依赖微博「发现」页的搜索入口，该入口在本机微博构建上
`accessibilityId` 是自增计数器（31985 → 31995 → 32000），子树哈希每次采样都变。
若 setup 被反复拒绝导致 `valid_attempts` 不足，结论是
`M0 = NOT_READY / insufficient_valid_samples`，不要用「只算测到的样本」的方式宣称通过。

## 6. 本轮明确不要做的事

```text
不要开启 local_canary（本轮没有可用的 calibration artifact）
不要在验收中途强杀进程；一旦强杀，先重启 Runtime 服务再继续
不要用 mock/fake 结果代替真机结论
不要为了让任务通过而放宽 Runtime Guard 或修改验收分母
不要更新分支代码；发现问题只记录现象 + RC SHA + 复现步骤
```

## 7. 证据与回报格式

每项记录：

```text
RC SHA
设备与系统版本
命令 / 脚本
attempted / failed / 失败分类（observation/grounding/planning/decision/guard/action/verification/infrastructure）
P50 / P95（如适用）
原始报告路径（docs/acceptance/<日期>/）
```

真机报告不得改写历史文件；新证据放新目录。

## 8. 已知限制（真机前必读）

```text
canary 不可用：没有 calibration artifact 时 local_canary 一定 fail closed
视觉：区域必须先有截图（FAST 无图观察不能派发视觉目标）；只允许 tap / long_press
视觉风险证据：完全无 UI-tree 文本的纯视觉区域只有 proposer label 作为语义证据
风险词表：词法匹配，同义改写可能绕过
未知写入：一旦出现，必须人工对账，系统不会自动重放
```

来自上一轮真机的**观察项**（本轮未修，属于下一 RC 的采集任务，不是放行条件）：

```text
image_tree skew 接近 1000ms 阈值：实测 747–976 ms，冷启动阶段曾出现图树一致性失败
                                （catalog 掉到 99–140，正常 241–259）
                                → 不改阈值；下一轮真机做 20–30 次 observe/probe 稳定性采样
check_agent_link 一次性停顿：约 9 分钟未复现；同刻独立 probe 正常、等价链路 15 秒通过
                                → 下一轮真机连续跑 10–20 次，若再现再开 stdio/subprocess/lifecycle 专项
foreground_known=false：设备端 getDeviceInfo 仍不返回可用结构，前景判定不可用
cold baseline 未产出：没有受控冷启动流程，first sample 不等于 cold
```
