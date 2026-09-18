# HarmonyOS Mobile Agent Runtime

通过通用 Agent 的 MCP 工具控制鸿蒙真机。当前为工程开发版，HAP 暂缓；优先在已连接的 HarmonyOS 6.1 真机验证，不能视为已完成 Artemis 等效能力。

## 本地安装（Windows / PowerShell）

需要 Python 3.11+、Git，以及包含 HDC 的鸿蒙 SDK。开发版本锁定见 `requirements.lock`。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock -e .
$env:HARMONY_HDC = "F:\DevEco Studio\sdk\default\openharmony\toolchains\hdc.exe"
.\.venv\Scripts\harmony-runtime.exe doctor
```

`doctor` 检查依赖并列出设备数量，不创建手机控制连接。真机需开启调试并授权当前电脑。

## 启动

先在一个终端启动常驻服务：

```powershell
.\.venv\Scripts\harmony-runtime.exe serve
```

MCP 客户端使用本仓库虚拟环境下 `harmony-runtime.exe` 的绝对路径，参数为 `mcp`，传输方式为 stdio。所有前端连接同一常驻服务。自定义状态目录时，服务端和前端都传入相同的 `--state-dir`。暂未实现自动启动服务。

六个工具：`mobile_session`、`mobile_observe`、`mobile_act`、`mobile_wait`、`mobile_burst`、`mobile_history`。初始化和工具发现不连接手机；真实调用时才连接服务。先打开会话并观察，再使用观察编号和目录中的目标执行动作。输入文字需要先点击输入框，再观察确认焦点。

执行成功和后置条件验证成功是两个不同字段。执行结果未知时，复用请求编号查询，不生成新编号重复执行同一写操作。跨会话可查询持久动作状态；未知执行的可信对账与恢复写入仍待完成。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip check
# 以下命令主动连接真机并读取页面和截图，不执行导航或输入：
.\.venv\Scripts\python.exe tests/smoke_device.py
```

测试报告和完整计划见 `docs/implementation-status.md`、`docs/development-plan.md`、`docs/development-tasks.csv`。

## 当前限制

已实现设备进程截止时间与中断隔离，故障测试覆盖初始化、读写和关闭卡住；终止电脑进程不能撤回手机已收到的操作。尚未完成全链路期限保证、持久化隔离、断连恢复、隐私保留与配额、可信人工批准、FULL 复杂场景真机验收、TEMPORAL 动态场景实测、OCR/视觉降级、历史回放、三客户端实测及长任务验收。危险目标目前采用临时拦截规则，不能视为完整安全策略。前台应用验证尚未实现。截图前后树一致只能证明采集期间没有检测到结构变化，不能证明动态画面完全一致，也不能证明锁屏截图可用。

已实现每台设备 FIFO 排队，以及采集前后和执行前的显式屏幕状态检查。锁屏、熄屏或状态未知时拒绝生成可执行观察；需解锁后重新观察。该检查不代表已具备安全窗口或应用防截图检测，也不能消除最后一次状态读取与实际点击之间的系统状态变化。

运行日志可能包含私人页面内容；状态目录只应保留在本机，禁止把截图、UI 原始数据、令牌或设备凭据提交仓库。


### 中断后的恢复（开发中）

`mobile_session` 的 `recover` 操作在设备队列中重建连接并采集新观察。它不执行点击或输入，也不把旧动作判定为成功。会话的 `recovery_required` 和 `unresolved_actions` 会显示执行结果未知的请求；这些记录跨会话、跨 Runtime 重启保留。未知动作存在时，新请求编号也不能绕过写入阻断。

若只有读取故障，恢复成功后可基于新观察继续操作；若有未知写入，则返回 `recovered_read_only`。证据核对和事件关闭接口仍待实现，当前版本不能完成这类故障后的自动续跑。

## 服务重启后如何继续

如果工具返回 `session_reopen_required`，使用 `mobile_session(operation="open")` 取得新会话，然后用 `mobile_session(operation="action_status", session_id=新会话, request_id=原请求编号)` 查询之前动作的持久状态。之后重新观察手机页面。旧会话与旧页面观察不能继续使用。

动作返回 `execution_unknown` 时，不要换一个请求编号重新点击。查询返回 `not_found` 也不代表可以重试：原调用可能尚在排队。当前版本会保留未确认写入限制；可信对账和恢复写入尚未完成。


观察模式支持默认 `FAST` 和 `FULL`。`mobile_observe(session_id=..., mode="FULL")` 会采集完整页面树、原始截图和带目录目标编号的标注图；只有结构、尺寸及时序检查通过才生成可用编号。标注图与原图分别作为 MCP 图片返回，编号必须配合当次 observation_id 使用。该模式已通过模拟设备及实际 stdio 协议测试，真机视觉质量和耗时仍待验收；没有实现 OCR 或任意视觉坐标点击；TEMPORAL 历史帧接口见下文。

恢复操作会按新页面中的原文字后置条件关闭对应已执行动作的验证失败事件；不会改写原动作结果。执行未知、只有页面变化条件或缺少预期的事件不会自动关闭，未知执行屏障仍保留。

连续执行：`mobile_burst` 接受 1–5 步，整段共享最多 3000ms 的预算，每步必须指定可观察后置条件。点击目标用文字或资源编号，在每步派发前重新定位。结果不确定或验证失败立即停止；重复请求只返回原结果，中断序列不自动续跑。可用 `mobile_session(operation="burst_status", request_id=...)` 查询各子动作持久状态。该能力尚未通过真机跨页面验收。

`mobile_observe(mode="TEMPORAL")` 在 3 秒预算内采集最多五帧历史截图，返回实际采样时刻及末两帧比较。历史帧不能用于动作调用，操作前须重新观察。当前不提供实时 watch，不能依赖此历史序列点击已经消失的控件。


### Bounded local target watch

A `mobile_burst` step may set `watch_timeout_ms` (1–3000; default 0 disables watch).
The runtime polls for that semantic target while holding the device execution slot,
then takes a fresh observation before dispatch. Missing targets may be polled;
ambiguous targets, page changes during regrounding, cancellation and expiry stop the
sequence. The watch deadline also bounds dispatch and post-verification, within the
shared 3000ms burst budget. Parent persistence prevents retries from rearming an
expired/interrupted watch. Historical TEMPORAL frames remain non-actionable.
This initial watch uses the UI tree with a candidate 50ms polling gap; it makes no
claim of 50ms end-to-end latency or successful video/transient-control handling.


### Typed local waits

`mobile_wait` accepts exactly one of legacy `expected` or a typed `condition`:

```json
{"session_id":"<session>","condition":{"type":"text_absent","value":"Loading"},"timeout_ms":5000,"poll_ms":200}
```

Supported conditions: `text_present`, `text_absent` (exact catalog text),
`element_present`, `element_absent` (semantic `target` by text or resource_id),
`change` / `fingerprint_changed` (fresh session-bound `observation_id`), and
`stable` (`stable_ms`, default 300). Changes use tree/display fingerprints.
Element presence includes disabled catalog nodes; it does not authorize clicking.
Absence only means absence from the observed catalog; inaccessible visual content
may still be present. Stability means equal sampled tree/display fingerprints,
not pixel stability or continuous stability between samples. The response labels
these limits in `evidence`. `app_changed` explicitly returns unsupported until
foreground app identity is verified. Waits do not dispatch actions.

### Durable action history

`mobile_history(session_id, limit=20, before=None)` returns newest-first, device-bound action metadata (maximum 100 items). Pass `next_before` as `before` for the next page; null ends pagination. The owning session can query while paused without initializing the phone. Results include original execution/verification status and separate incident closure metadata, but omit stored UI, input and recovery text. History neither replays actions nor clears uncertain writes. It covers admitted actions, including burst children, not every rejected request or burst parent. This is metadata history, not complete evidence replay or retention/redaction management.

New action and burst journal results retain execution metadata only. Live responses still include observations, but deduplicated stored responses set `observation_retained=false` and omit observations; call `mobile_observe` for current UI before replanning. This also avoids persisting nested burst page snapshots. Existing database records are not migrated or securely erased by this change. New recovery text postconditions use independently salted exact-match digests and remain checkable after restart. These digests are not encryption: low-entropy labels can still be guessed by a database reader. Legacy plaintext conditions remain readable and are not migrated or securely erased. Caller-supplied request IDs should not contain secrets. Full retention and privacy management remains incomplete.


`harmony-runtime doctor` now reports Python, required package versions, HDC presence and bounded device discovery independently. It exits nonzero when an environment check fails; discovery is limited to 10 seconds and does not initialize a UI driver. Raw discovery errors and device serials are omitted. An `ok` report means these environment checks passed, not that the phone is unlocked or actions have been verified (`phone_operation_verified=false`).


New action and burst admission requires at least 64 MiB free on the journal volume. `storage_pressure` rejects new admission when below that threshold; `storage_unavailable` rejects it when free space cannot be inspected. Existing result persistence and metadata reads do not use this admission gate. This is a reserve check, not allocated storage: other processes may consume space afterward, and actual SQLite write failures, evidence quotas and retention still require further engineering and fault acceptance.


If an action completion cannot be saved because of a SQLite or OS storage error, the live response reports `execution_unknown`, `completion_persisted=false`, and `journal_write_failed`. The durable pre-dispatch record continues to block new writes and duplicate dispatch across restart. Restore storage and reconcile before proceeding; the response does not assert the physical action failed. Burst-parent persistence and startup storage failures still need dedicated handling.


### 实际 MCP 读屏诊断

在启动 `harmony-runtime serve` 后，运行 `harmony-runtime probe`；自定义数据目录时两个命令使用相同的 `--state-dir`。该命令连接已有 Runtime，经真实 stdio MCP 打开会话、读取树与截图并关闭会话。它不另启设备服务、不派发点击或输入，输出仅包含诊断元数据。

`doctor` 成功仅证明依赖和设备发现；`probe` 的 `phone_observation_verified=true` 才表示本次树/截图一致的读屏成功。`phone_write_verified` 始终为 false，读屏通过不证明写操作、复杂任务或指定 Agent 客户端已经验收。手机锁屏会返回 `screen_locked`，请在真机解锁后重试。


## 微博真机跨页面验收

启动常驻服务并解锁手机，在微博首页、发现页或热搜列表上运行：

```powershell
.\.venv\Scripts\python.exe -m harmony_runtime.cli serve --state-dir .runtime/acceptance
# 在另一个终端执行，会实际导航手机：
.\.venv\Scripts\python.exe scripts/accept_weibo.py --execute --rounds 10
```

路径：发现 → 更多热搜 → 科技 → 当前列表中的话题结果 → 返回科技 → 热搜 → 返回发现 → 首页。
每步验证准确页面文案，话题从当次 UI 中选取；不执行发布、关注、点赞或评论。
只有 Runtime 明确在派发前返回 `stale_observation` 时，才重新观察并重新定位，最多三次。
其他错误、已执行但验证失败、执行未知都会停止；禁止盲目重试。
报告默认保存到被忽略的 `.runtime/weibo-acceptance.json`，只保留步骤状态、耗时和汇总，不保存话题、账户、原始树或截图。
该验收证明指定版本微博的同应用跨页面路径，不证明任意任务规划、跨应用长任务或三个 Agent 客户端已通过。


视频验收：进入微博视频页或主页面后执行 `python scripts/accept_weibo.py --execute --profile video --rounds 10 --report .runtime/weibo-video.json`。每轮验证同一视频的数值进度前进，再上滑并确认内容标识变化。该验证不覆盖音频或画面解码。

Runtime 仅对 back/home/swipe 的过期检查忽略数值 Slider 进度及指定时钟、电量节点的文字变化；点击和输入仍要求完整页面指纹匹配，变化验证也仍使用完整指纹。

2026-09-18 阶段结果及限制见 [真机验收记录](docs/acceptance/2026-09-18/README.md)。真机验收已按用户要求暂停；已通过的微博场景不代表完整项目验收完成。
