# Decider 2B 本机决策服务

交付版本 3.1 · 2026-09-20 · Windows / Python 3.11 / NVIDIA CUDA

本服务已在当前 RTX 4070 SUPER 上加载固定模型并通过真实 GPU 合成推理、接口边界、启停和重启验证。服务仅提供 choice 与 noul 判断；Runtime Provider 适配器、中文任务校准和设备执行接入属于后续开发。当前返回 shadow_only，尚未开启实际任务影子采集。

## 运行

在 PowerShell 中执行：

```powershell
Set-Location -LiteralPath 'D:\music mv\HarmonyOS-to-use\services\decider'
.\Start-Decider.ps1
.\Test-Decider.ps1
# 需要停止时
.\Stop-Decider.ps1
```

默认地址为 http://127.0.0.1:8765。启动命令返回进程信息不表示就绪；Test-Decider.ps1 返回 ok=true 才表示模型加载及预热前向通过。脚本以隐藏进程运行；未设置开机启动。重启 Windows 后需重新启动。

## 固定模型与依赖

- 模型：Mapika/decider-2b
- revision：7789eb65d5cf519737608e218fa88819bddea0af
- 权重：3,763,692,048 bytes；SHA-256：49ca58057fd0cc34a70ffdbf5b8353659868a551fdcf1d3c395693041db1d588
- 已验证：Python 3.11.9、torch 2.10.0+cu128、transformers 5.17.0、BF16。
- 原生 Windows，使用快照自带 decider 包、PyTorch eager/reference kernels。未启用 FP8、compile、CUDA Graph、KV cache，未安装 causal_conv1d/flash-linear-attention。
- requirements.lock.txt 记录完整环境版本；snapshot-manifest.json 记录快照校验。模型缓存、虚拟环境、日志与令牌不进入版本库或文档 ZIP。

新环境需先有 Python 3.11、uv 与可用的 NVIDIA 驱动，再运行 Install-Decider.ps1。安装脚本保留已有虚拟环境，先从 PyTorch CUDA 12.8 索引安装 torch，再安装锁文件依赖并下载/校验固定快照。本机现有环境已验证；全新机器的完整重装仍在 F02/H03 验收范围。

## API

GET /health 不需令牌。GET /v1/models 与 POST /v1/systemone 需 Authorization: Bearer 本地令牌。令牌由首次启动生成并保存在 .runtime/api-token；适配器从该文件读取，不记录、不复制进文档。

```json
{
  "state": "当前在设置首页，可见 WLAN、蓝牙、显示和亮度。目标：进入 WLAN 页面。",
  "questions": {
    "action": {
      "type": "choice",
      "instructions": "选择最符合当前目标的可见候选，仅使用提供的事实。",
      "criteria": {
        "cand_wifi": "设置列表中的 WLAN 入口",
        "cand_bluetooth": "设置列表中的蓝牙入口",
        "cand_display": "设置列表中的显示和亮度入口"
      }
    }
  },
  "independent": true,
  "layout": "state_first"
}
```

choice 返回候选键、confidence、certainty 和 probabilities；noul 返回命题肯定分数。返回 deployment 中包含固定 revision、后端、精度、耗时、project_calibration=null 及 mode=shadow_only。服务的 mode 字段不提供设备权限控制；所有后续动作必须由 Runtime Guard 校验。

约束：64 KiB JSON、每请求 1–4 个独立问题、每 choice 2–16 候选、state 最多 1024 tokens、每个渲染问题最多 1536 tokens。仅支持 state_first；不支持 score、图像、聊天补全、schema_first 或问题链。超长输入拒绝，不能默默截断。并发 1、无等待队列；忙碌 503，15 秒推理期限后返回 504，实际 GPU 任务结束前保持 busy。没有自动 CPU 回退。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe .\smoke_test.py
uv pip check --python .\.venv\Scripts\python.exe
```

单元测试使用假模型，覆盖鉴权、请求边界、忙碌、超时排空及错误脱敏；smoke_test.py 调用真实本地模型，覆盖英文/中文 choice、noul 及 HTTP 边界，写入 .runtime/smoke-evidence.json，不执行手机动作。每次 smoke 会覆盖该本机证据文件；发布证据需单独归档。

2026-09-20 实测 9 个短输入合成请求全部通过，HTTP 中位耗时 50.71 ms，最大 234.08 ms，输入 54–78 tokens。它们不能证明中文任务准确率、长输入时延或持续吞吐。本次重启就绪 8718.09 ms，整卡显存停止时 1191 MiB、就绪时 5095 MiB；这包含桌面和其他进程，不是模型显存的精确测量。

## 运维与边界

Start-Decider.ps1 / Stop-Decider.ps1 管理 Windows venv 启动器及实际子解释器，并按 PID、创建时间、可执行路径和命令核对身份。端口冲突时拒绝启动；停止脚本身份不匹配时拒绝杀进程。

标准输出位于 .runtime/stdout.log，错误输出位于 .runtime/stderr.log；访问日志和 API 文档页关闭。decider.env.example 是配置说明，不会自动载入；真实限制由脚本与 local_service.py 实现。同一 Windows 账号下的独立进程提供依赖、资源和故障分离，不构成 OS 权限隔离。不要把同账号恶意进程纳入令牌能够防御的范围。

模型卡及推理源码随本地固定快照保留；所有上游许可与分数语义均以该快照为准。完整架构、开发计划及运维文档见 D:/music mv/harmonyos-design-v2/v3.1。
