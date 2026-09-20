# 当前真机：严格输入与搜索闭环

日期：2026-09-19。当前真机 SGT-AL10 / API 24；系统与验收以连接真机为准，不固定到“6.0”。本轮复用既有 Runtime 和 journal，不重做基线，不启动竞争服务。

## 已完成的搜索验收

| 报告 | 结果 | 边界 |
|---|---|---|
| `weibo-search-followup-6.json` | 失败 | 同名搜索建议与用户卡片误定位；保留原证据 |
| `weibo-search-followup-7.json` | 失败 | 首帧尚无完整页签；后续只读观察到正确页面，也不回填为成功 |
| `weibo-search-followup-8.json` | service 1/1 轮通过 | 一次派发前 stale 刷新，未重放派发后的点击 |
| `weibo-search-stdio-current.json` | stdio MCP 3/3 轮通过 | 确定性脚本，非 Agent 自主规划；0 次派发前刷新 |

stdio 三轮发生于北京时间 15:12:46–15:15:09。轮耗时 43610 / 44453 / 46922 ms。23 次观察采样 P50=2312、P95=2422、max=2531 ms；16 个动作步骤周期 P50=9672、P95=10156、max=10156 ms。动作周期包含客户端前置观察、Runtime 执行及可能的只读 settling，不是纯驱动派发耗时。这些有限非受控样本不代表正式性能门槛。

### 定位和后验修复

- 搜索建议要求精确标签及当前应用目录里的 Text→可点击 Column→List；同名用户卡片使用 Row，不再按最小包围几何猜选。
- 页签依赖当前真机观察到的 `Tabs(resource_id=tab)` 页头结构；检查 Column 的 selected 状态、搜索词位于页头上方，以及各页必要语义。
- 视频页额外存在长文本节点只是较弱内容证据，不能据此声称视频播放已经验证；本场景验收的是搜索结果页签导航。
- settling 仅在动作 executed/verified 后重复只读观察。10 秒是进入下一次读取的等待预算，未统一为严格 RPC 共享截止时间，单次读取可能越界；不声称硬实时上限。

## 严格输入

驱动使用同控件身份检查：全选删除→观察真实空值→写入→观察真实最终值。输入节点的 hint/description 不再冒充 text。重复输入必须等值而非追加；缺失 text 属性不能当作空字符串。

`strict-replace-input.json` 记录先前 service 探针三步“鸿蒙→鸿蒙→空值”通过；它不是标准 schema 报告，缺少采集时间字段，不补造时间。

可重复入口（在仓库根运行）：

```powershell
$env:HARMONY_HDC='F:\DevEco Studio\sdk\default\openharmony\toolchains\hdc.exe'
.venv\Scripts\python.exe scripts/accept_weibo.py --execute --profile strict-input --rounds 1 --transport stdio --state-dir .runtime\acceptance --report .runtime\strict-input-new-run.json
```

只在已识别的微博搜索流程内操作。所有写入走 Runtime journal；三步严格绑定同一输入框，不自动恢复旧输入。FULL 树及图片只在内存校验，报告不保存 UI 内容、截图或设备序列号。失败停止，并关闭租约；后续只读校验失败也记录已经派发的结果，不隐藏写入。

新增入口前两次 stdio 尝试 `strict-input-stdio-current.json`、`strict-input-stdio-followup.json` 均失败在输入派发前：验收脚本没有解码 MCP 的 `flat_tree_v1`，误判无法取得原始输入身份。修正为无损还原父子关系，增加 bundle 继承、无效拓扑、完整三步流程测试；原失败不覆盖。两次只执行了启动/必要返回，没有派发 replace_text。

修正后的 `strict-input-stdio-followup-2.json` 于北京时间 15:26:01–15:26:55 **1/1 轮、3/3 输入步骤通过**。替换、同值替换、清空均为 executed/verified，且额外 FULL 原始树 `raw_value_verified=true`；无派发前 stale 刷新。三步耗时 15344 / 15265 / 14375 ms，包含前后 FULL 观察，不作为生产动作性能数字。轮结束时输入框为空。三份标准入口报告合计是两次预派发失败、一次通过，不能描述为三次连续成功。

## 部署与验收边界

本轮最终源码回归：`unittest discover -s tests -q` **224 项通过 / 30.450 秒**；`compileall -q src scripts tests`、`pip check`、`git diff --check` 通过。测试包含扁平树解码、缺失值/身份变化、完整三步输入和派发后只读失败不重放。日志位于本地 `.runtime/tests-current.log`。

- 旧常驻服务 PID 32688 已在受控窗口停止，并确认旧 launcher、service、worker 全部退出；首次尝试的 `python -m harmony_runtime` 因包没有 `__main__.py` 在服务启动前退出，未派发手机动作。随后用 `python -m harmony_runtime.cli serve --state-dir .runtime/acceptance` 启动新服务，保留两次启动日志。
- 新服务在线 FULL observe 已返回 `text_observed`、`hint`、`description`、`accessibility_id`、`host_window_id`、`hierarchy` 等输入身份字段；重启前后 250 条 durable action 逐行一致，未出现 recovery_required。证据：`service-reload-current.json`。
- 受控重启后的 `strict-input-stdio-service-reloaded.json` 为 **1/1 轮、3/3 步骤通过**；`weibo-search-stdio-service-reloaded.json` 为 **3/3 轮通过**。两者均是真实 stdio MCP 确定性脚本，不是 Agent 自主规划或 M1。
- 本节此前关于“常驻服务未重启”的文字属于历史状态，保留在旧报告中但不再代表当前部署状态。

## 首个 Agent 客户端接入 smoke（客户端协议层）

已新增项目级 `.codex/config.toml`，将 `harmony_mobile` 作为受信任项目的 stdio MCP 服务声明；只保存启动命令、工作目录和 HDC 路径，不保存 endpoint token。当前 Codex 任务不会动态重载该配置，因此不能把配置文件存在当作本任务已接入原生 Codex。

`agent-client-smoke-service-reloaded.json` 已在当前真机和新常驻服务上执行通过：

- 工具发现：6 个工具与契约一致；
- 结构化 JSON：通过；
- FULL 图片：1 张经 base64 与 Pillow 解码校验；
- 取消：取消一个只读等待后，服务将会话置为 paused；
- 状态查询：对未派发 request id 返回 `not_found`，未触碰手机写入。

该 smoke 是真实 MCP 客户端接入探活，不是通用 Agent 的自主规划，也不计入 M1 的 30 个端到端任务。
- 未完成 Agent 自主任务、完整 M1、正式 30/100/300 分组验收、未知写入可信对账、OCR/CV、回放及长任务。项目目标保持进行中。
