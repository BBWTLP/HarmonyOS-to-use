# 当前真机 Agent 协议验收（2026-09-19）

## 口径

本记录以当前连接真机为唯一开发、适配和验收基准，不把 HarmonyOS 6.0 或 6.1 作为固定前置条件。命令只执行两步只读语义探针：先用 `On.text` 构造 selector，再用 `Driver.findComponents` 查询；不会点击、输入、导航、截图或采集页面。命令可能部署/复用本地 agent、启动设备端 uitest daemon，并建立后清理 HDC port-forward。

## 命令

```powershell
cd "D:\music mv\HarmonyOS-to-use"
.\.venv\Scripts\python.exe -m harmony_runtime.cli protocol --execute
```

机器可读结果见 [protocol-agent-20260919.json](protocol-agent-20260919.json)。

## 结果

- 设备发现：1 台；预测 ABI `arm64-v8a`，预测协议 v2。
- agent 资产：本地选择 `agent_v2.so`，SHA-256 为 `0fee80fcc748ed6a76978771bc196d8e1c96a8e7025d238b720bf83731ab2403`。该哈希只证明选择的本地打包资产，不独立证明设备实际加载的二进制。
- 传输层：实际协议 v2，transport 为 `localabstract:uitest_socket`，session ID 回显为 true，HDC port-forward 建立成功。
- 语义探针：`On.text("__codex_protocol_probe_absent_20260919__", 0)` 构造 selector 成功；随后 `Driver.findComponents` 查询成功。真机返回只读结果 shape 为顶层 `pts`、`result`，`result_type` 为 `NoneType`；按 DevHelmKit 的 `ComponentFinder` 语义，非 list 结果按空引用列表处理。
- 总体状态：`ok`，退出码为 0。`transport_status=ok` 与 `rpc_probe.status=ok` 分开记录。

## 裁决

本次证明了当前真机上的 v2 协议帧、session 关联、HDC 转发，以及当前真机可用的最小只读 UI 查询契约。`Driver.getDeviceInfo` 并非当前真机可用的语义探针；此前设备端返回 exception 的报告已被本次契约核对替代，不能据此宣称设备信息业务 API 可用。设备信息基线继续来自 shell/设备基线命令；本协议验收只证明 transport 和只读 UI 查询，不证明点击、输入、导航、截图、微博搜索或首 Agent 客户端闭环。

设备端 exception 原文、selector 引用和页面内容均未写入报告，避免泄漏设备信息、页面内容或其他私有响应。
