# HarmonyOS Mobile Agent Runtime

通过通用 Agent 的 MCP 工具控制鸿蒙真机。当前为工程开发版，HAP 暂缓；开发、适配与首期验收均以用户当前真机及当轮实测系统构建为准（不固定为 6.0 或 6.1），不能视为已完成 Artemis 等效能力。

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

### 当前真机只读基线

验收对象是当前连接的真机，不预设必须运行 HarmonyOS 6.0 或 6.1。采集系统和工具白名单字段时使用：

```powershell
.\.venv\Scripts\python.exe -m harmony_runtime.cli baseline --execute
```

该命令只读取固定系统参数、`uitest --version` 和主机 HDC 版本；不会启动 UI 驱动、连接 Runtime 会话、唤醒/解锁手机、读取页面或执行动作。当前基线记录见 [device-baseline-20260919-batched.json](docs/acceptance/2026-09-19/device-baseline-20260919-batched.json)，说明见 [设备基准](docs/device-baseline.md)。当前记录设备为 `SGT-AL10`，软件发行版本为 `SGT-AL10 6.1.0.135(SP8C00E120R3P11)`，底层系统字段为 `OpenHarmony-6.1.1.120`；这些是当前真机的事实，不是项目固定版本要求。

### 当前真机 Agent 协议探活

如需执行一次只读设备端 Agent/RPC 探活，显式使用：

```powershell
.\.venv\Scripts\python.exe -m harmony_runtime.cli protocol --execute
```

本次当前真机结果为：v2 transport、`localabstract:uitest_socket`、session ID 回显和 HDC port-forward 均成功；但 `Driver.getDeviceInfo` 返回设备端 `exception` 结构，因此总体状态为 `partial`，不能宣称完整 Agent/RPC 语义验收通过。脱敏报告见 [protocol-agent-20260919.md](docs/acceptance/2026-09-19/protocol-agent-20260919.md) 和同目录 JSON。

### 当前真机有限观察

已用真实 stdio MCP → 常驻 Runtime → worker → 真机链路完成每组 3 次的有限观察：优化前 FAST **3/3**、FAST 带图 **2/3**、FULL **3/3**；诊断批处理后 FAST **3/3**、FAST 带图 **2/3**、FULL **2/3**。批处理后的失败样本保留了图树一致性、SoM、前台和图片检查字段；条件未受控，不能据此宣称稳定提速或正式 P95 达标。白名单报告见 [benchmark-observation-20260919.md](docs/acceptance/2026-09-19/benchmark-observation-20260919.md) 及同目录的六个 `benchmark-*.json` 文件。

测试报告和完整计划见 `docs/implementation-status.md`、`docs/development-plan.md`、`docs/development-tasks.csv`。

## 当前限制

已实现设备进程截止时间与中断隔离，故障测试覆盖初始化、读写和关闭卡住；终止电脑进程不能撤回手机已收到的操作。尚未完成全链路期限保证、持久化隔离、断连恢复、隐私保留与配额、可信人工批准、FULL 复杂场景真机验收、TEMPORAL 动态场景实测、OCR/视觉降级、历史回放、三客户端实测及长任务验收。危险目标目前采用临时拦截规则，不能视为完整安全策略。已实现保守前台身份识别、bundle 后置条件和 app_changed 等待；缺少可验证前台证据时拒绝相关操作。截图前后树一致只能证明采集期间没有检测到结构变化，不能证明动态画面完全一致，也不能证明锁屏截图可用。

已实现每台设备 FIFO 排队，以及采集前后和执行前的显式屏幕状态检查。检测到熄屏或锁屏时，Runtime 会通过 DevHelmKit 的无凭据唤醒与解锁路径尝试恢复，并重新读取状态；只有确认 `screen_on=true` 且 `screen_locked=false` 才会继续读屏或执行动作。该自动恢复已在 HarmonyOS 6.1 真机上通过休眠→Runtime observe→AWAKE/unlocked 的真实设备路径验证。状态未知、驱动不支持或恢复未确认时仍会拒绝操作。该检查不代表已具备安全窗口或应用防截图检测，也不能消除最后一次状态读取与实际点击之间的系统状态变化。

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

`mobile_observe(mode="TEMPORAL")` 在 3 秒预算内采集最多五帧历史截图，返回实际采样时刻及末两帧比较。历史帧不能用于动作调用，操作前须重新观察。TEMPORAL 自身不启动实时 watch；burst 已有下文所述有界目标轮询。不能依赖历史序列点击已经消失的控件。


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
these limits in `evidence`. `app_changed` requires a fresh session-bound baseline `observation_id` and
verified foreground identity. Adapters without foreground support reject it.
Waits do not dispatch application actions (observation may wake/unlock the screen).

### Durable action history

`mobile_history(session_id, limit=20, before=None)` returns newest-first, device-bound action metadata (maximum 100 items). Pass `next_before` as `before` for the next page; null ends pagination. The owning session can query while paused without initializing the phone. Results include original execution/verification status and separate incident closure metadata, but omit stored UI, input and recovery text. History neither replays actions nor clears uncertain writes. It covers admitted actions, including burst children, not every rejected request or burst parent. This is metadata history, not complete evidence replay or retention/redaction management.

New action and burst journal results retain execution metadata only. Live responses still include observations, but deduplicated stored responses set `observation_retained=false` and omit observations; call `mobile_observe` for current UI before replanning. This also avoids persisting nested burst page snapshots. Existing database records are not migrated or securely erased by this change. New recovery text postconditions use independently salted exact-match digests and remain checkable after restart. These digests are not encryption: low-entropy labels can still be guessed by a database reader. Legacy plaintext conditions remain readable and are not migrated or securely erased. Caller-supplied request IDs should not contain secrets. Full retention and privacy management remains incomplete.


`harmony-runtime doctor` now reports Python, required package versions, HDC presence and bounded device discovery independently. It exits nonzero when an environment check fails; discovery is limited to 10 seconds and does not initialize a UI driver. Raw discovery errors and device serials are omitted. An `ok` report means these environment checks passed, not that the phone is unlocked or actions have been verified (`phone_operation_verified=false`).


New action and burst admission requires at least 64 MiB free on the journal volume. `storage_pressure` rejects new admission when below that threshold; `storage_unavailable` rejects it when free space cannot be inspected. Existing result persistence and metadata reads do not use this admission gate. This is a reserve check, not allocated storage: other processes may consume space afterward, and actual SQLite write failures, evidence quotas and retention still require further engineering and fault acceptance.


If an action completion cannot be saved because of a SQLite or OS storage error, the live response reports `execution_unknown`, `completion_persisted=false`, and `journal_write_failed`. The durable pre-dispatch record continues to block new writes and duplicate dispatch across restart. Restore storage and reconcile before proceeding; the response does not assert the physical action failed. Burst-parent persistence and startup storage failures still need dedicated handling.


### 实际 MCP 读屏诊断

在启动 `harmony-runtime serve` 后，运行 `harmony-runtime probe`；自定义数据目录时两个命令使用相同的 `--state-dir`。该命令连接已有 Runtime，经真实 stdio MCP 打开会话、读取树与截图并关闭会话。它不另启设备服务、不派发点击或输入，输出仅包含诊断元数据。

`doctor` 成功仅证明依赖和设备发现；`probe` 的 `phone_observation_verified=true` 才表示本次树/截图一致的读屏成功。`phone_write_verified` 始终为 false，读屏通过不证明写操作、复杂任务或指定 Agent 客户端已经验收。手机熄屏或锁屏时 Runtime 会先尝试无凭据恢复；只有恢复未确认时才返回 `screen_locked` 或 `screen_off`。由于桌面和动态页面可能在截图与图树采集之间变化，`probe` 仍可能返回 `not_ready`，这不等同于自动解锁失败。


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

Runtime 的导航页面指纹仅归一化指定时钟/电量文字、数值 Slider/Progress 以及无标签且不可点击的装饰图像边界。目标动作还要求目标目录项与目标子树指纹完全一致；其他页面结构、显示方向和前台身份仍参与检查。变化验证和图树一致性仍使用完整指纹。这不是对任意动态页面的安全保证，不能通过放宽校验强行点击 stale 目标。

2026-09-18 阶段结果及限制见 [真机验收记录](docs/acceptance/2026-09-18/README.md)。同日已补充自动唤醒/无凭据解锁的真机 Runtime 验证，记录见 [自动恢复验收记录](docs/acceptance/2026-09-18/automatic-wake-unlock.md)。微博搜索输入、实际 Agent 客户端、长任务与最终发布门槛仍未完成。

### 2026-09-19 真机进展

无密码锁屏自动唤醒、解锁和 FAST 读屏已连续三轮验证。当前 Runtime 新增前台应用确认和系统应用隐私密码拦截；小红书的独立应用保护仍阻止业务验收。微博启动和发现页导航已验证，动态搜索页及完整 Agent 长任务仍未通过。详细结果、可复现脚本和限制见 [验收记录](docs/acceptance/2026-09-19/foreground-and-authentication.md)。

## 当前设备基准与实现审计

用户于 2026-09-19 明确：**系统与验收均以当前真机为准**。历史 6.1 记录不等于本轮系统探测；不再把 6.0 专项验证作为首期发布的必要条件。设备升级后重新记录基线并复验受影响能力。见 [设备基准](docs/device-baseline.md) 和 [v2 设计对照审计](docs/project-audit-2026-09-19.md)。仓库原 v1 计划保留历史，最新设计来源为用户提供的 harmonyos-design-v2。

## 分段耗时与 MCP 观察基准

先显式启动常驻服务，以下命令不会代为启动它。使用同一虚拟环境，在项目目录运行；自定义状态目录时与服务端保持一致。


```powershell
.\.venv\Scripts\python.exe -m harmony_runtime.cli benchmark --execute --samples 30 --mode FAST
.\.venv\Scripts\python.exe -m harmony_runtime.cli benchmark --execute --samples 30 --mode FAST --include-image
.\.venv\Scripts\python.exe -m harmony_runtime.cli benchmark --execute --samples 30 --mode FULL
```

输出 JSON 仅保留计时和白名单状态，不保留序列号、截图、UI 树或页面文字。建议保存至被 Git 忽略的 `.runtime/` 目录，并单独附上当轮设备基线。此命令不执行应用点击、输入或导航，但观察可能触发自动唤醒和无凭据解锁，所以必须显式传入 `--execute`。

- 样本数 1–500；全部样本成功且会话关闭成功才退出 0，否则退出 1；参数错误退出 2。
- 失败样本计入 attempted/failed 与总体耗时；未采集样本单列 unattempted。认证、设备、会话及协议异常停止采样；图树不一致仍计失败，可继续下一次采集。
- FAST 带图和 FULL 必须收到真实 MCP 图片块且图树一致；FULL 还要求 SoM 可用。失败不能因返回图片或 HTTP 成功而算作通过。
- 总体与成功样本分别给出 nearest-rank P50/P95、最小值、最大值、样本量；空集为 null。first/subsequent 只表示顺序，**不代表受控冷/热启动**。
- client_ms 是每次 observe 的 MCP 往返，包含工具序列化/传输；不含常驻服务启动和 session open/close。Runtime timing 分解排队、worker 初始化、屏幕/前台检查、树、显示参数、截图、编码及标注；嵌套计时不能简单相加。
- 动作 timing 分解预检、屏幕守卫、日志准入、派发和后置验证；total_ms 截止到最终 journal.finish 之前，不含最终持久化、worker 清理与客户端传输。拒绝准入等错误不保证携带全部阶段；去重结果保留原执行计时。

本轮已通过真实本地 HTTP 服务和 stdio MCP 完成有限真机观察；但页面、热状态、网络、应用版本和启动条件未受控，**不能替代正式性能验收，也没有证明达到设计性能目标**。正式冷热采样还需受控启动、固定页面、当前真机系统/应用版本、模型与网络条件，以及完整失败分母。

## Agent 任务层与 Decider 接入（v3.1）

在既有 Runtime 之上新增 `src/harmony_agent`：候选登记、分层定位、规则/本机 Decider 路由、
任务监督、记忆、只读核验与工件存储。Runtime 仍独占设备写入，Agent 层只通过公开会话接口
提交观察与动作。详见 [Agent 层说明](docs/agent-layer.md) 与 [任务状态台账](docs/agent-status.md)。

启用 v2 工具（未启用时 MCP 客户端仍只看到 v1 六个工具）：

```powershell
$env:HARMONY_HDC='F:\DevEco Studio\sdk\default\openharmony\toolchains\hdc.exe'
$env:HARMONY_AGENT_TOOLS='1'          # 注册 mobile_run_task / task_* / mobile_decide
$env:HARMONY_AGENT_PROFILE='local_off'      # rules_only | local_off | local_shadow | local_canary
$env:HARMONY_AGENT_CALIBRATION='decider-cal-2026-09-20'   # 仅 canary 需要
.\.venv\Scripts\python.exe -m harmony_runtime.cli serve --state-dir .runtime/agent-state
```

v3.2 起 `local_off` 是开发默认值：该模式下不 import、不构造、不健康检查任何模型
提供方，Agent 层只走确定性规则。需要收集影子对照时才切到 `local_shadow`。
可选提供方（Decider）不可用时，Runtime 与 v1 六工具不受影响；Agent 工具本身在
宿主构造失败时也会降级为“只提供六工具”，并在 stderr 记录一行原因。需要“宁可失败
也不静默降级”的部署可设置 `HARMONY_AGENT_REQUIRED=1`。

可选提供方的运维开关（仅 `local_shadow` / `local_canary` 使用）：

```text
HARMONY_AGENT_FAST_PROVIDER      0/off 表示不构造提供方（默认 auto）
HARMONY_DECIDER_URL              Decider 服务地址，默认 http://127.0.0.1:8765
HARMONY_DECIDER_REVISION         固定 revision，默认仓库内钉住值
HARMONY_DECIDER_TOKEN_FILE       token 文件路径，默认仓库相对路径
HARMONY_AGENT_PROVIDER_TIMEOUT   单次调用超时秒数，默认 15
```

解码器服务与真机验收脚本：

```powershell
Set-Location .\services\decider; .\Start-Decider.ps1; .\Test-Decider.ps1
Set-Location ..\..
.\.venv\Scripts\python.exe .\scripts\check_agent_link.py --state-dir .runtime\agent-state `
  --report .runtime\link.json
.\.venv\Scripts\python.exe .\scripts\accept_m0_primitives.py --execute --per-primitive 25 `
  --report .\docs\acceptance\2026-09-20\m0-primitives.json
.\.venv\Scripts\python.exe .\scripts\accept_m1_weibo.py --execute --runs 3 `
  --report .\docs\acceptance\2026-09-20\m1-weibo.json
.\.venv\Scripts\python.exe .\scripts\decider_shadow.py --execute --rounds 4 `
  --report .\docs\acceptance\2026-09-20\decider-shadow.json
.\.venv\Scripts\python.exe .\scripts\evidence_manifest.py      # A01 基线冻结
.\.venv\Scripts\python.exe .\scripts\reproduce.py              # A03 回归入口
```

本轮证据、口径与未完成项见 [验收证据索引](docs/acceptance/2026-09-20/README.md)。

## 交付现状与后续计划

**权威 current run：** [`docs/acceptance/2026-09-24/`](docs/acceptance/2026-09-24/README.md)
（`run_id=20260924T152342Z`，`code_revision=6b78c76`，离线 809/809）。

状态词汇：`verified_offline` / `verified_device`（须绑定 revision、task-set、设备构建、
run_id）/ `historical`（旧 revision 或未绑定证据，不进入 current 数字）/ `blocked_device`
/ `deferred`。`planned = attempted + unattempted`。

实现范围与剩余计划见
[v3.1 交付现状、剩余开发计划与验收计划](docs/v3.1-progress-and-plan.md)、
[2026-09-24 发布收口与能力扩展计划](docs/superpowers/plans/2026-09-24-project-next-steps-assessment.md)，
逐任务状态见 [任务状态台账](docs/agent-status.md) 与 [执行状态台账](docs/execution-status.md)。

v3.2 的离线收敛进度见 [v3.2 进度台账](docs/v3.2/progress.md)、
[离线基线](docs/v3.2/baseline-offline.md) 与
[真机待办](docs/v3.2/blocked-device.md)。2026-09-22 设备窗口曾取得 M0/M1 formal 与
长任务/M2 smoke，但 formal 未绑定 revision，记为 `historical`；current 真机门禁仍按
2026-09-24 计划 Task 2–4 执行。

v3.2 离线阶段的具体结论：

```text
观察授权语义      docs/v3.2/observation-cache-semantics.md
视觉目标权威      docs/v3.2/adr-visual-target-authority.md
OCR / VLM 适配器  docs/v3.2/visual-providers.md
风险词表统一      docs/v3.2/risk-taxonomy.md
离线加固与故障矩阵 docs/v3.2/offline-hardening.md
M2 30 任务规格    docs/v3.2/m2-benchmark-spec.md（evals/tasks/m2-30.json）
```
