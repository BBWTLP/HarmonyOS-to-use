# 实施状态与证据

更新：2026-09-18。所有阶段仍未完成验收；此文件记录真实进展，不修改原计划范围。

## 当前验证

- 最新单元与协议测试：69/69 通过（16.259 秒）；依赖检查通过。下文各轮结果保留其当时范围。覆盖 FIFO 顺序与队列取消、暂停恢复请求失效、关闭会话保留在途租约、等待共用截止时间、截图结构一致性、显式屏幕状态解析与锁屏拒绝。
- 之前真机 MCP 只读探测返回 text/image，屏幕 1320×2848，首次观察 6281ms；该性能值来自修改采集核对前的版本，不能充当当前性能基准。
- 本轮系统服务明确返回 SLEEP 和 screenLocked=true；Runtime 真机只读探测在 1359ms 返回 screen_locked，保留观察为 0、动作日志为 0。该耗时是拒绝锁屏请求的耗时，不是页面采集基准。尚未验证解锁后写操作。
- 屏幕状态缺失、冲突或不识别时返回 screen_state_unknown；不推断为已解锁。采集前后及动作日志边界前检查状态。暂停后的旧采集结果不能回填观察缓存。
- Git 远程读取已成功；重新认证进程等待完成，写权限未验证。
- 仓库没有提交或推送本轮新增工程文件。

- 最新真机 stdio MCP 复测返回结构化 screen_locked，工具错误标志为真；这证明错误传递可用，不代表页面采集或操控验收成功。
- 新增独立设备进程，测试覆盖初始化卡住、读取取消、写入卡住、进程崩溃和关闭卡住。超时后隔离进程；写入结果未知时不重放。终止电脑进程不能撤回手机侧已收到的操作。
- 设备发现、进程启动、磁盘提交等尚不具备完整截止时间保证；已实现显式重连观察和未知写入跨重启阻断；读取隔离持久化、原文字后置条件的事件关闭已部分实现；未知写入可信对账仍待实施。

- 新增 `mobile_session(operation="recover")`：中断旧请求、串行关闭旧设备进程、重建连接并观察；若旧进程仍存活则拒绝替换。存在未知写入时返回 recovered_read_only，不允许新写入；纯读取故障恢复成功后可使用新观察执行。
- 动作日志在派发前原子保存请求与设备摘要的关联。重启发现未完成派发仍按 unknown 处理；旧版无设备关联的未知记录保守阻止所有设备的新写入。会话状态返回未决请求元数据，不返回过去页面内容。
- 目前尚无关闭未知写入事件的接口。这是恢复能力的未完成部分，不能将“重连可读”当作完整恢复验收。

## 逐任务状态

| ID | 任务 | 状态 | 证据或缺口 |
|---|---|---|---|
| M0-01 | 真机环境与驱动能力探测 | 部分实现 | 已验证 USB 发现、驱动连接、树和截图读取；输入和完整切换闭环待测。 |
| M0-02 | 基线采样与断连协议诊断 | 部分实现 | 已有一次首次观察 6281ms；200热/20冷及故障基准未完成。 |
| M0-03 | 冻结契约依赖及可行性评审 | 部分实现 | 依赖已锁定且安装通过；契约仍在开发，未冻结。 |
| M1-01 | Adapter及常驻Runtime | 部分实现 | 常驻服务、共享连接、四工具发现、FIFO 入队已有测试；设备进程期限与中断隔离已有故障测试；自动启动和恢复待做。 |
| M1-02 | 会话租约与最小安全策略 | 部分实现 | 租约争用、暂停恢复永久取消、关闭在途会话租约已有测试；完整状态机与安全策略待做。 |
| M1-03 | Journal幂等及未知执行恢复 | 部分实现 | 未知动作按设备持久保留；已执行动作的原文字后置条件可按新观察关闭事件；未知写入的可信对账待做。 |
| M2-01 | FAST与最小Catalog及Grounding | 部分实现 | FAST 目录、过期观察拒绝已实现；完整定位覆盖待测。 |
| M2-02 | 四工具及有界Wait | 部分实现 | 四工具及 stdio、等待队列共用截止时间有测试；设备进程超时终止已有故障测试，完整取消与恢复验收待完成。 |
| M2-03 | Codex接入及MVP验收 | 未验收 / 待实施 | 尚无满足原验收标准的完整执行证据。 |
| M3-01 | FULL与Catalog和SoM | 部分实现 / 未验收 | 完整树、原图、目录目标标注图已通过真实 stdio 协议测试；尚缺真机视觉质量与定位成功率验收。 |
| M3-02 | 分层Grounding及OCR和视觉降级 | 未验收 / 待实施 | 尚无满足原验收标准的完整执行证据。 |
| M3-03 | 验证Incident及可信高风险授权 | 未验收 / 待实施 | 尚无满足原验收标准的完整执行证据。 |
| M4-01 | TEMPORAL及本地watch | 未验收 / 待实施 | 尚无满足原验收标准的完整执行证据。 |
| M4-02 | 有界Burst与跨页重新定位 | 部分实现 / 未验收 | 共享截止时间、语义重定位、停止与持久子动作查询已实现；模拟设备验证，真机与中断故障覆盖尚未完成。 |
| M4-03 | 性能优化及分段基准 | 未验收 / 待实施 | 尚无满足原验收标准的完整执行证据。 |
| M5-01 | History和只读Replay及证据保留 | 未验收 / 待实施 | 尚无满足原验收标准的完整执行证据。 |
| M5-02 | 上下文压缩及历史召回 | 未验收 / 待实施 | 尚无满足原验收标准的完整执行证据。 |
| M5-03 | 50及100步跨应用任务验证 | 未验收 / 待实施 | 尚无满足原验收标准的完整执行证据。 |
| M6-01 | DeepSeekHarness和OpenCode适配 | 未验收 / 待实施 | 尚无满足原验收标准的完整执行证据。 |
| M6-02 | 安装诊断及连接可靠性 | 未验收 / 待实施 | 尚无满足原验收标准的完整执行证据。 |
| M6-03 | 故障注入及泛化回归 | 未验收 / 待实施 | 尚无满足原验收标准的完整执行证据。 |
| M7-01 | 发布打包与干净环境验收 | 未验收 / 待实施 | 尚无满足原验收标准的完整执行证据。 |
| M7-02 | 最终文档演示与发布收口 | 未验收 / 待实施 | 尚无满足原验收标准的完整执行证据。 |
| B1 | v1兼容及稳定性风险储备 | 未验收 / 待实施 | 尚无满足原验收标准的完整执行证据。 |
| P01 | 异步任务状态预算及模型配置 | 未验收 / 待实施 | 尚无满足原验收标准的完整执行证据。 |
| P02 | Planner和living-plan检查点 | 未验收 / 待实施 | 尚无满足原验收标准的完整执行证据。 |
| P03 | Operator与只读Checker | 未验收 / 待实施 | 尚无满足原验收标准的完整执行证据。 |
| P04 | 恢复召回与对照评测 | 未验收 / 待实施 | 尚无满足原验收标准的完整执行证据。 |
| P05 | 打包文档和发布回归 | 未验收 / 待实施 | 尚无满足原验收标准的完整执行证据。 |
| B2 | Pro风险储备 | 未验收 / 待实施 | 尚无满足原验收标准的完整执行证据。 |

## 后续优先顺序

1. 真机解锁后完成安全导航、输入、滑动及前台状态取证。
2. 设备执行队列、驱动硬超时、断连恢复和持久对账；补取消与重启故障测试。
3. 隐私、保留配额和可信批准，再推进 FULL、视觉降级和长任务闭环。
4. 按原计划完成 MVP、v1、三客户端、故障、性能和耐久门槛，再推进 v2 Pro；不以现有测试代替任何门槛。

## 关闭生命周期补充验证（2026-09-18）

- Runtime 关闭时先停止接收请求、取消会话并等待在途操作完成日志记录，再关闭设备与 SQLite。并发或重复关闭共享同一次资源释放结果。
- 新增并发测试：一个写入在设备响应前中断、一个观察等待队列、两个关闭调用同时执行；验证只派发一次写入，等待请求被拒绝，设备只关闭一次，重新打开日志仍能读取未对账写入。
- 本轮完整单元/协议测试 41 项通过。该结果不等于原开发计划各阶段验收完成。
- 本轮真实 MCP 冒烟仍返回 screen_locked（退出码 1），未获得新的解锁态观察或操作成功证据。Git 浏览器认证进程仍在等待。
- 后续需完善 HTTP 服务停机顺序与请求读取超时；当前 Runtime 等待自定义适配器退出没有额外硬期限，生产设备操作由子进程预算限制。

## 本机 HTTP 服务补充验证（2026-09-18）

- 请求头与正文增加总计 5 秒读取期限；持续少量发送数据也不能延长该期限。响应写入另受连接超时限制，手机操作仍使用独立动作预算。
- 服务退出先取消并排空 Runtime 在途操作，再等待 HTTP 处理线程退出，最后移除连接信息文件。
- 新增真实 loopback socket 测试覆盖半截请求头、半截正文、持续缓慢发送请求头；另通过 HTTP 发起在途观察，验证关闭信号在连接线程等待之前到达设备工作流程。
- 当前仍未完成安装器、自动启动、断连后的证据对账与真机跨应用验收。

本轮上述修改后完整测试为 44 项通过（12.337 秒），依赖一致性检查通过；并未据此宣称计划整体交付。

## 驱动启动与发送边界补充验证（2026-09-18）

- 子进程启动后重新检查会话取消状态和操作截止时间；发送线程真正发送命令前再次检查，避免启动或线程调度消耗预算后仍派发命令。
- 两个故障测试在真实子进程启动返回时分别注入预算耗尽和暂停；除检查无写入标记外，直接监视 IPC send，确认没有发送命令。两种情况下工作进程均终止并隔离。
- 本轮完整测试 46 项通过（12.953 秒）。该检查没有消除检查完成到手机收到命令之间的竞态；执行前日志之后遇到异常仍按不确定结果处理。
- 真机只读 MCP 冒烟再次返回 screen_locked，未获得解锁态成功证据；浏览器 Git 认证进程仍在运行等待，未重新启动。

## 客户端连接故障契约（2026-09-18）

- 找不到或无法解析连接信息时，返回 runtime_unavailable；该阶段尚未发出动作请求。
- 发出动作请求后遇到连接失败、响应丢失或响应格式损坏时，返回 execution_unknown，明确要求先核对日志，禁止换请求号盲重试。连接失败未必意味着手机已执行；这里保守保留不确定性。
- 观察与会话调用的传输失败返回 runtime_transport_error。服务返回的有效业务错误仍保持原错误码；客户端不自动重试。
- 使用真实 loopback HTTP 服务覆盖直接断开、非法 JSON、错误响应形状等情况；逐项验证动作请求发送次数为一。MCP stdio 测试同时验证缺失服务能够返回结构化 runtime_unavailable。
- 连接恢复后的完整日志查询、事件对账与恢复授权尚未完成，本轮错误分类不等于恢复闭环验收。

- 最新完整验证：48 项单元/协议测试通过（13.204 秒），Python 依赖一致性检查通过。Git 远端 HEAD 可读取，HDC 当前识别到 1 台设备；这些结果不代表 Git 写权限或解锁态真机操作已验收。

## 按请求查询持久执行状态（2026-09-18）

- 四工具数量不变；mobile_session 增加 action_status 操作，要求当前会话和原 request_id，返回设备绑定的日志元数据。暂停状态也可查询；其他客户端不能凭 session_id 读取。
- 查询返回执行状态和当次记录的验证状态，不返回历史截图、页面树或输入文本，不触发设备初始化，不重放动作，不解除未知写入屏障。
- not_found 只表示未找到该设备绑定的记录，不证明动作未执行：原请求可能仍在排队，旧版无设备绑定记录也不会作为明确未执行的证据。原有未绑定未知记录仍保守阻止写入。
- 新增重启持久性、设备隔离、会话所有权、暂停态查询、元数据脱敏、在途/完成区分测试。完整测试 50 项通过（13.252 秒）；随后增加真实 stdio MCP 查询断言，3 项协议测试通过。
- 该入口是恢复证据的一部分。服务重启后的客户端重新握手、可信证据对账、事件关闭及恢复写入仍未完成，不把日志中的历史验证状态当作当前页面验证结果。

## 服务重启后的客户端恢复（2026-09-18）

- 已认证的本机服务返回 client_invalid 时，前端清除对应旧身份并返回 session_reopen_required；不自动重发被拒绝的调用。网络断开或结果丢失仍沿用 execution_unknown，不据此推断未执行。
- 下一次显式 open 会重新握手和取得新会话。Agent 应用原 request_id 查询持久日志，并重新观察；旧 observation_id 与旧 session_id 不可复用。
- 实际 HTTP 服务启动—停止—再启动测试验证：同一前端对象可恢复；旧动作未重放；设备未被查询流程初始化；未确认动作跨重启保留并继续阻止写入；旧会话拒绝访问。
- 完整测试 51 项通过（13.664 秒）。Git 原认证会话仍经实时轮询确认在等待。真实手机锁屏问题没有新的解决证据。
- 尚待实现可信证据对账与恢复写入；本轮恢复仅完成连接身份及日志读取，不等于复杂任务断点续跑验收。

## 前台应用验证能力核查（2026-09-18）

- 已核查锁定版本 devhelmkit 的 app_current：它以 WindowManager 焦点窗口编号匹配 AbilityManager 的 Mission 编号推断包名。当前真机上缺少该对应关系的验证证据，暂不接入为权威前台身份。
- Runtime 的 foreground_bundle 仍为不可用，session capabilities 明确返回 false。携带 expected.bundle 的新动作在日志写入及设备派发之前返回 unsupported_capability；按包名等待也立即报告不可用，不再伪装成一般等待超时。
- 原请求的持久去重结果仍优先返回。页面文字与变化验证继续可用；不能将文字命中等同于应用身份的强验证。前台身份、应用切换的完整验收仍未完成。
- 完整测试 53 项通过（13.948 秒），新增测试验证不支持的包名后置条件不会产生手机写入或执行日志，包名等待不会初始化设备。

## 页面几何与可见区域定位（2026-09-18）

- Catalog 保留原始 bounds，新增 hit_bounds 表示与当前屏幕的交集；实际点击和长按以 hit_bounds 中心派发。完全屏幕外节点不进入 Catalog，也不用于文字后置条件。
- 显式隐藏祖先抑制子树；禁用祖先下的子节点保留读取但不能解析为动作目标。边界仅接受完整整数矩形，非法显示尺寸明确报错。
- 不凭容器矩形推断裁剪，不宣称解决独立浮层遮挡；仍需要真实层级和视觉证据补齐遮挡判定。
- 新增 7 项几何测试，包含真实 HarmonyDevice 派发方法对模拟驱动的坐标检查。完整测试首次遇到 Windows socket 关闭返回 ConnectionAbortedError 的差异；测试补充接受此关闭类型，仍验证总截止时间及服务退出。复跑 60 项通过（13.686 秒）。这些测试没有替代真机触控验收。
- Git 浏览器认证会话仍在等待；尚未获得写权限成功证据。


## 验证失败事件持久化与 FULL 观察（2026-09-18）

- 原后置条件与派发前指纹随动作预记录原子落盘；事件单独持久保存。修复 finish 的事务范围，完成结果和对应事件在同一事务提交，重启后不会把已保存结果误判为未完成派发。
- recover 的新观察只会关闭「已执行但未验证」且原文字后置条件满足的事件；仅 changed、缺少预期、应用身份条件或未知派发均不自动关闭。关闭记录保存观察编号、指纹和时间，不改写历史动作结果，也不重放动作。文字命中不等于业务目标或应用身份强验证。
- 中断派发在重启时创建稳定事件。未知执行仍阻止新写入，完整可信对账入口尚未实现。事件和历史证据的隐私保留、压缩及配额仍待实施。
- mobile_observe 新增 mode=FULL，返回原始层级、Catalog、原始截图及与观察编号绑定的目标标注图。默认 FAST 行为保留。标注来自 UI 树，不包含 OCR 或视觉猜测；禁用节点不标注为目标。
- 截图尺寸必须与设备显示尺寸一致；采集前后结构/显示指纹须一致且树图间隔在 0–1000ms 内。未验证的采集不进入动作缓存，也不生成目标标注。此检查不证明像素稳定、无遮挡或视频内容同步。
- MCP 通过两个独立 image 内容块交付原图与标注图；结构化结果不重复塞入图片 base64。真实 stdio 子进程—本机 HTTP 服务—模拟设备测试验证了 FULL 观察和按标注编号派发一次动作。此链路测试的设备是模拟设备，不能充当真实 Agent 或真机验收。
- 最新完整测试 69 项通过（16.259 秒），pip check 通过。真机 smoke 仍返回 screen_locked；Git 原浏览器认证进程实时轮询仍在等待。项目没有达到发布或端到端真机验收条件。


## 有界连续执行初步验证（2026-09-18）

- 新增 mobile_burst，第五个工具属于 v1 扩展；最多 5 步且共享最多 3000ms 的队列与执行预算。复用单步动作的观察校验、策略、派发日志与后置条件验证，整段持有同一设备 FIFO 执行位。
- 每步要求后置条件，目标只能按文字或资源编号重新解析，拒绝跨页沿用 action_id。未知执行、未验证执行、缺失目标或超时均停止后续步骤。
- 首次派发前持久保存父请求和所有计划子请求编号；重复父请求不重放，中断父请求不自动恢复。burst_status 返回设备绑定的子动作日志元数据，不返回页面内容。
- 新增 6 项测试：五页语义目标序列、重复请求与冲突、次步目标缺失、未知派发与新写入阻断、后置条件失败、契约边界和状态查询。完整 75 项测试通过（16.491 秒）。这些测试采用模拟设备；进程中断窗口、实际协议连续执行和真机性能仍待补齐验证。
- 同轮真机 smoke 返回 screen_locked。重新发起的 Git 浏览器登录进程仍在等待，写权限未证实。端到端目标保持未完成。


## 连续执行协议与重启记录验证（2026-09-18）

- 修复全局请求编号的并发准入问题：单步动作与连续执行在持久插入锁内统一检查编号是否已使用，防止不同设备在各自预查询后把同一编号写入两类记录。冲突发生在设备派发之前。
- 新增数据库重开测试，模拟子动作完成而父进度尚未保存的窗口：父序列变为 interrupted，子动作的 executed 证据仍可查询，未派发子动作返回 not_found（不将其解释为绝对未执行证明）。此测试不替代操作系统强杀或磁盘故障试验。
- 新增真实 stdio MCP 子进程经共享 HTTP 服务的连续执行测试：模拟设备三页顺序操作、验证、重复调用去重、父子状态查询均通过，实际派发次数保持三次。未连接真实模型客户端或手机，不能计入三客户端或真机任务成功率。
- 完整测试 78 项通过（18.703 秒）。修正 README 连续执行状态查询参数名为 operation；M4-02 仍为部分实现、未验收。


## TEMPORAL 历史采样基础实现（2026-09-18）

- mobile_observe 新增 TEMPORAL：单个 FIFO 执行窗口内，按调用开始后的 50/150/300/700/1500ms 候选点最多采五帧，总预算 3000ms。每帧记录计划、实际起始和完成时间；采集慢时不伪造达到候选采样点。当前逐帧截图并以树前后夹取检查一致性，未实施轻量帧缓存优化。
- 所有采样均为历史证据，不写入可操作观察缓存；即使最后两帧相同也不能作为动作观察编号使用。stable_state 仅报告末两帧树指纹和 PNG 摘要是否相同，明确不证明连续稳定、无遮挡或业务成功。
- MCP 为各帧输出独立图片内容块，结构化帧元数据不重复包含 base64。暂停中断采样，未知设备状态仍拒绝采集。实时 watch 尚未实现，capabilities.temporal_watch=false；该历史采样功能不能替代计划中瞬时目标命中即执行的验收。
- 初轮测试发现 Windows 等待提前唤醒；改为唤醒后重新核对单调时间再采集。修复后完整 81 项测试通过（23.124 秒），包括历史凭证拒绝执行、像素变化而树不变、暂停取消，以及 stdio MCP 多图片返回验证。均非真机瞬时控件成功率证据。


## Local target watch implementation (2026-09-18)

- BurstStep now supports optional watch_timeout_ms (0 disables; maximum 3000ms). Parent request persistence precedes arming; duplicate and interrupted parents never rearm automatically.
- Detection runs inside the device FIFO slot, followed by the existing fresh regrounding, policy, screen and durable dispatch checks. Only target_not_found is polled; ambiguity and other faults stop. Deadline covers detection, regrounding, dispatch admission and verification and shares the burst total budget.
- temporal_watch capability now denotes this tree-based bounded watch. Historical TEMPORAL observations still cannot authorize actions. No OCR/CV, lightweight frame ring or verified transient-video performance is implied.
- Full suite: 84 tests passed in 23.393s. Added delayed appearance, disappearance during regrounding, and expiry/dedup tests, all using simulated devices. Real phone smoke still returned screen_locked. Git browser authentication session was polled and remains live awaiting completion; remote write permission remains unverified.
- Real MCP watch execution, cancellation/slow-device deadline edge cases and actual phone transient-target acceptance remain outstanding. Project-wide acceptance remains incomplete.


## Watch device deadline and protocol validation (2026-09-18)

- Found and fixed a deadline propagation gap: the device subprocess previously retained the outer burst deadline while watch logic used a shorter deadline. Each step now installs its deadline on ProcessDevice; nested scopes cannot extend an outer deadline and restore the previous budget on exit.
- Added a spawned-process fault test: after initial observation, the fake driver blocks for 20 seconds. A 150ms watch within a 3000ms burst interrupts the read, quarantines the worker, reports not_dispatched and produces no write marker; the test enforces completion within 1.5 seconds including host cleanup. This is a host fault-injection bound, not a real-phone performance result.
- Added pause-during-watch verification and duplicate-after-resume verification: no dispatch or rearming occurs. Existing real stdio subprocess/HTTP integration now executes three watch-enabled steps and checks deduplication and journal status against a simulated device.
- Full suite: 87 tests passed in 24.825 seconds. Physical phone, real model clients, sensitive-action approvals, privacy/history policies and full acceptance gates remain incomplete.


## Typed wait conditions (2026-09-18)

- Added typed condition to mobile_wait while retaining legacy expected; exactly one must be supplied. Text and semantic element presence/absence, baseline-bound change/fingerprint_changed, and sampled tree stability now execute locally with the existing timeout, cancellation and device queue.
- Change conditions require a fresh observation in the same session; handles invalidated by writes cannot serve as baselines. Element presence includes disabled nodes but never grants actionability. Absence is scoped to the UI catalog, not visual absence.
- Stable uses repeated equal tree/display fingerprints over stable_ms; changes reset the interval. Evidence explicitly denies continuous/pixel stability proof. app_changed fails before device initialization because foreground identity remains unsupported.
- Added six wait tests and expanded real stdio/HTTP protocol coverage for typed conditions and stale baseline rejection. Full suite: 93 tests passed in 25.368 seconds, simulated devices only. No phone or Agent acceptance claims follow from these tests.


## Resource-only wait targets (2026-09-18)

- Fixed catalog omission of visible, screen-intersecting nodes carrying only a resource ID. Previously element_absent could match even while such a node existed; resource-only progress indicators can now be observed and waited on.
- Disabled nodes remain observable but are rejected by action target resolution. Explicitly hidden subtrees and completely offscreen nodes remain excluded; absence still means catalog absence, not proof of visual absence.
- Added regression coverage for resource-only presence/absence, disabled resolution, hidden ancestors and offscreen bounds. Full suite: 95 tests passed in 25.027 seconds, using simulated devices.
- Physical-device smoke returned screen_locked again. Git browser login session 31481 was confirmed live with no completion output; write authorization remains unverified. Neither condition establishes phone/client acceptance.


## Device-bound metadata history (2026-09-18)

- Added mobile_history through stdio MCP, authenticated loopback service and Runtime. Owner-bound session access works while paused without constructing a device. Device-scoped descending row cursors page 1–100 admitted action records; burst children are included, parent summaries remain available via burst_status.
- Returned fields exclude stored observation, input and recovery content. Original verification and subsequent incident closure remain separate. Reading unknown records does not replay or clear them. This does not implement evidence replay, at-rest redaction, retention, disk quotas or complete rejected-request history.
- Full suite passed 99 tests in 25.411s. After adding real stdio/HTTP pagination assertions, all 5 protocol tests passed in 8.652s. Device activity is simulated; these results do not establish real phone/client acceptance.


## Minimize newly persisted action results (2026-09-18)

- Action and burst writes now persist an allowlisted metadata projection; nested live observations, arbitrary payloads and messages are omitted. Live responses remain unchanged. Deduplicated stored responses explicitly set observation_retained=false; current UI must be observed again.
- Added action reopen/dedup and nested burst privacy tests proving sentinel UI content is absent from stored result JSON without mutating live replies. Updated the incident immutability assertion for the explicit metadata marker. Full suite: 101 tests passed in 25.392s.
- Scope limits: existing records/WAL/free pages are not sanitized; recovery postcondition text is still stored, identifiers may contain caller text, and evidence replay, retention and disk quotas remain outstanding. This is data minimization for new result writes, not comprehensive at-rest privacy. Real phone and external Agent acceptance remain unproven.


## 恢复条件摘要与格式验证（2026-09-18）

- 新恢复条件不再保存原文字，改存每条记录独立随机盐和精确匹配摘要；重启后仍可按新观察核对原条件。摘要不是加密，低熵文字仍可能被持库者猜测。
- 已验证相似文字不匹配、要求变化时原指纹不关闭事件、重启后精确匹配可关闭对应已执行事件。未知执行屏障不受影响。
- 未知摘要版本、非法盐或摘要、错误 changed 类型和多余字段均拒绝匹配。旧明文条件继续兼容读取；没有迁移或安全擦除旧记录。
- 本轮完整单元与协议测试 104 项通过（25.546 秒），依赖一致性检查通过。以上不代表真机或客户端端到端验收通过。
- 本轮真机探测仍返回 screen_locked；Git 浏览器认证进程仍等待完成，写权限未验证。


## 分项环境诊断（2026-09-18）

- doctor 支持逐项依赖与 HDC 检查、独立子进程设备发现、10 秒发现超时和失败退出码。发现失败不会输出原始错误或序列号，也不初始化 UI 驱动。
- 本机实际执行通过：Python 3.11.9、devhelmkit 0.6.0、MCP 2.2.0、Pydantic 2.13.5、Pillow 12.3.0、HDC 路径存在、设备发现数为 1。此结果不证明解锁或可操作，报告明确 phone_operation_verified=false。
- 完整测试 107 项通过（25.655 秒），新增覆盖发现超时、缺少 HDC、失败子进程输出不外泄。Git 认证进程仍在等待完成。


## 磁盘余量准入（2026-09-18）

- 新动作与 burst 建立持久派发记录前检查日志所在卷；不足 64 MiB 返回 storage_pressure，无法检查返回 storage_unavailable。既有结果保存和元数据查询不受此准入门槛阻止。
- 完整测试 110 项通过（25.754 秒）。故障注入覆盖低空间时真实 Runtime 调用未触发模拟设备派发、没有新动作记录、burst 未建立记录、已有动作结果仍可保存和查询，以及容量检查失败。
- 这是余量检查，不是实际预分配；无法防止检查后其他进程耗尽空间。实际 SQLite 磁盘写满、证据配额、保留清理和发布门槛尚未完成，不能视为磁盘故障验收通过。
- Git 认证会话本轮轮询仍在运行，写权限未确认。


## 动作后 SQLite 写满（2026-09-18）

- 动作完成日志发生 SQLite/OSError 时，Runtime 返回 execution_unknown、completion_persisted=false、journal_write_failed，不报告已验证成功。此前提交的派发记录继续阻止未知写入重放。
- 使用 SQLite max_page_count 与测试专用触发器在完成记录更新时产生真实容量错误；模拟设备派发一次，重复请求无第二次派发，移除故障触发器后重开日志仍保留未知写入屏障。
- 完整测试 111 项通过（25.928 秒）。这是数据库容量故障路径验证，不是物理磁盘写满或真机故障验收。burst 父记录保存失败的结构化结果、启动期间存储故障恢复仍待实现。


## 批量进度保存失败（2026-09-18）

- burst 中间进度或最终完成保存发生 SQLite/OSError 时，返回 interrupted、journal_write_failed、completion_persisted=false，立即停止后续步骤；保留全部预先登记的子动作编号及已知子动作结果。
- SQLite 容量触发器验证父记录更新失败时只派发一次，重复请求不重放；重开日志后父批次为 interrupted，首个子动作仍是 executed/verified，其他子动作无记录（无记录不是未执行的普遍证明）。另覆盖最终完成保存失败，不错误报告 completed。
- 完整测试 113 项通过（26.333 秒）。使用模拟设备验证 Runtime 和真实 SQLite 容量错误；尚不代表真机、物理磁盘耗尽或客户端端到端验收完成。


## 实际 MCP 读屏诊断命令（2026-09-18）

- 新增 `harmony-runtime probe`，经 stdio MCP 连接指定的已有 Runtime；不启动竞争设备服务，检查后释放会话租约。诊断输出不含界面文字、截图、设备序列号或连接凭据。
- 原真机 smoke 复用同一实现；本次真机执行结果为 not_ready/screen_locked，观察与写入验证均为 false。唤醒并触发系统解锁入口后，屏幕状态仍为 locked，截图没有可识别密码键盘；真机验收需用户在手机上手动解锁。
- 116 项测试通过（29.245 秒），包含真实 stdio/本机服务链路配合模拟设备的读屏与租约释放、无服务时不自动启动，以及图树不一致/没有图片时不能通过。此结果不代表 Codex、OpenCode、DeepSeek Harness 的实际客户端验收。


## 微博真机验收与暂停交接（2026-09-18）

- 真机已实际解锁并执行：热搜跨页面路径 10/10 轮，视频播放进度与滑动切换 10/10 轮。此前章节的 screen_locked 与认证等待是历史状态，不代表本次结果。
- 新增可重复的真实 MCP 验收脚本和三项导航指纹回归、四项验收脚本测试；完整回归累计 123 项。
- 修复数值播放进度导致非目标导航被拒绝的问题；点击和输入保留完整页面过期保护。
- 热搜成功测试早于最终导航修复，最新版本复测尚待进行。当前动作周期约 4–5 秒，仍需性能优化。
- 用户已要求暂停真机验收，控制服务已停止。搜索输入、实际 Agent 客户端、长任务与最终发布门槛未完成。
- 详细阶段证据、失败记录与待办见 [验收记录](acceptance/2026-09-18/README.md)。本次同步为阶段进展，不是最终发布。
