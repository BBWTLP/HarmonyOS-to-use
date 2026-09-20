# Decider-2b 本机部署与后续接入计划

更新：2026-09-20 · 对应架构版本3.1 · 本文件保留原路径以维护历史引用

## 当前交付

**本机模型服务已部署并验证。** 当前 endpoint 为 http://127.0.0.1:8765，服务目录为 services/decider。采用自建 local_service.py 管理 HTTP 输入、鉴权、单并发、超时排空及真实预热；推理导入固定模型快照自带的 decider 包。未修改 harmony_runtime 的设备执行路径，未执行手机动作。

模型 Mapika/decider-2b 固定 revision：7789eb65d5cf519737608e218fa88819bddea0af。权重 SHA-256：49ca58057fd0cc34a70ffdbf5b8353659868a551fdcf1d3c395693041db1d588。已验证 Windows、Python3.11.9、RTX4070 SUPER、torch2.10.0+cu128、transformers5.17.0、BF16。

当前使用原生 Windows PyTorch reference kernels；causal_conv1d/flash-linear-attention 为未采用的优化依赖，缺少它们不表示本服务不可用。没有 FP8、compile、CUDA Graph 或 KV cache，不采用上游优化环境的时延承诺。

## 文件与运行

服务代码、安装/启停/检查脚本、requirements.in、requirements.lock.txt、模型元数据、逐文件清单、smoke_test.py 和 tests/test_deployment_contract.py 已建立。decider.env.example 是说明，不会自动载入；当前值由脚本及服务代码设置。

```powershell
Set-Location -LiteralPath 'D:\music mv\HarmonyOS-to-use\services\decider'
.\Start-Decider.ps1
.\Test-Decider.ps1
# 停止时使用
.\Stop-Decider.ps1
```

/health 无需鉴权；/v1/models 与 /v1/systemone 需 Bearer token。令牌保存在 .runtime/api-token，不进入版本库或交付包。仅绑定127.0.0.1，隐藏后台运行，无开机自启动。

独立 .venv 与进程提供依赖和故障分离；同一 Windows 账号仍可能访问账号有权限的文件，不能声称具备 Journal 的 OS 权限隔离。服务代码没有设备动作接口，未来手机动作必须由 Runtime 控制权、候选注册和 Guard 校验。

## 已通过检查

- [x] 固定模型下载及 SHA-256 完整性检查。
- [x] 实际 CUDA BF16 加载及 warmup 前向；ready 后提供健康响应。
- [x] 5项假模型边界单元测试：鉴权、输入、busy、超时排空、错误处理。
- [x] 41个包的依赖一致性检查。
- [x] 9次真实 GPU 合成请求：英文 choice、中文 choice、noul。
- [x] 实际 HTTP 拒绝：无令牌401、过长状态422、超大请求413。
- [x] Windows venv 启动器/实际子解释器的身份核验停止、端口释放及重启。

合成请求 HTTP 中位数50.71ms、最大234.08ms，输入54–78tokens。重启就绪8718.09ms，保留 OS 文件缓存。结果不代表任务准确率、中文泛化、持续负载或冷安装性能。Runtime 历史224项回归本轮未重跑。

## 剩余开发与准入

F02 保持 in_progress，DF0 本机服务验证已通过。依序完成：

1. F02/H03：128/512/900-token × 2/8/16候选 × 1/4问题容量矩阵、30分钟负载；干净环境重建与下载重试验证。
2. F01/F03：实现 Provider、StateBuilder、DeciderAdapter；候选、设备、观察、epoch、参数摘要和截止时间绑定。
3. F04：300状态的中文任务数据集，开发/校准/保留集为120/80/100，按应用与轨迹分组防泄漏；保留集仅用于准入评估。
4. F05/F06：真实任务影子、故障降级、超时取消及晚到结果丢弃；影子不取得写权限。
5. G05：完成 Guard 与故障门槛后低风险 canary，对比真实任务成功率及完整任务耗时/成本。

服务 project_calibration=null、mode=shadow_only；没有 Runtime Provider 集成，没有真实任务影子采集。服务标记本身不实现授权。未校准时不得打开模型驱动执行。执行未知的手机写入不自动重放。

所有36项任务的工作量为48人日加10人日缓冲，约12工作周；包含已开始部署，并非精确剩余工期。本次未提交、推送或发布仓库变更。

## 最新设计与证据

- [v3.1 文档入口](../../../../harmonyos-design-v2/v3.1/README.md)
- [架构设计](../../../../harmonyos-design-v2/v3.1/01_架构设计.md)
- [开发计划](../../../../harmonyos-design-v2/v3.1/02_开发计划.md)
- [部署与运维](../../../../harmonyos-design-v2/v3.1/04_Decider部署与运维.md)
- [脱敏部署证据](../../../../harmonyos-design-v2/v3.1/deployment-evidence.json)
- [服务 README](../../../services/decider/README.md)
