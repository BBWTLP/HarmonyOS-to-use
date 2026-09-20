# v3.2 真机测试交接单

交给"有 HarmonyOS 真机的那台电脑"执行。**只测本文指定的 RC SHA，不要测漂移中的分支 HEAD。**

## 0. 冻结信息

```text
repository   https://github.com/BBWTLP/HarmonyOS-to-use
branch       feat/runtime-foundation
RC SHA       <提交后填写>（见 pre-device-gate.md 第 8 节）
Python       3.11+
OS           Windows（脚本为 PowerShell）
test count   607 offline（0 failed / 0 error / 0 skip）
lock          requirements.lock（sha256 18a991d5…a126055）
```

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

期望：`Ran 607 tests` 且 `OK`。不通过就不要继续真机测试，先报告。

## 4. 真机只读起点

```powershell
.\.venv\Scripts\python.exe -m harmony_runtime.cli doctor
.\.venv\Scripts\python.exe -m harmony_runtime.cli baseline --execute
.\.venv\Scripts\python.exe -m harmony_runtime.cli protocol --execute
.\.venv\Scripts\python.exe tests/smoke_device.py
```

这些命令不执行导航或输入。记录设备型号、系统发行版本、HDC/uitest 版本、截图与树是否可用。

## 5. 然后按 blocked-device 顺序执行

顺序与门槛见 [真机待办](blocked-device.md)：

```text
D1 设备基线
D2 M0 每原语 100 次
D3 C01 受控冷/热基准
D4 C03 1/2/3 步 burst 仲裁
D5 M1 冻结版本单批 30 次
D6 真实 OCR / VLM 视觉执行（需先接入真实引擎）
D7 M2 30 任务 × 10 = 300 次（规格：evals/tasks/m2-30.json）
D8 100 步稳定性
D9 真机故障矩阵（USB/HDC 断连、重启、锁屏、worker hang）
```

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
