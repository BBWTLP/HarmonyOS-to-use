# RSIAgent × Decider 计划落地 · 本轮增量证据

执行日期：2026-09-20（Asia/Shanghai）
仓库：`D:\music mv\HarmonyOS-to-use`（`feat/runtime-foundation`）
代码基线：`docs/acceptance/2026-09-20/baseline-freeze.json`
对应计划：`docs/architecture-rsi-decider-plan-2026-09-20.md`

本文件只记录本轮真实产出与真实失败分母，不修改计划范围，也不把 mock 结果当作真机证据。

## 1. 离线回归

```text
命令     .venv\Scripts\python.exe -m unittest discover -s tests -p "test*.py"
结果     Ran 710 tests … OK（本轮由 675 → 710，新增 35 项）
新增测试 tests/test_agent_protocol_v2.py        16 项（P2-01/02/04/05）
         tests/test_rsi_learning.py             13 项（P3-01…P3-06）
         tests/test_unknown_write_reconciliation.py 6 项（P1-04）
```

完整日志与摘要由 `scripts/reproduce.py` 写入本地 `.runtime/`（不提交）。

## 2. 本轮新增/修改代码

| 文件 | 内容 | 对应计划项 |
|---|---|---|
| `src/harmony_agent/contracts.py` | protocol 2.1：`Intent`、`CandidateSetRef`、`DecisionSuggestion`、`GuardedAction`、`EventEnvelope`、`admit_suggestion` | P2-01/P2-02 |
| `src/harmony_agent/actor.py` | `ActorProvider` 协议、`SubgoalProposal`/`ControlProposal`、`DeterministicActor` | P2-04 |
| `src/harmony_agent/verifier.py` | `VerifierProvider`、`CodeVerifier`、`assert_read_only`、`ReadOnlyVerifierMount` | P2-05 |
| `src/harmony_agent/supervisor.py` | 事件 envelope 写入与迁移、`GuardedAction` 派发前构造与拒绝、verifier 挂载、frozen memory 挂载、结果记录 verifier 证据 | P2-02/P2-03/P2-05/P3-06 |
| `src/harmony_agent/experience.py` | 经验 schema、脱敏扫描、staging/commit 门 | P3-03 |
| `src/harmony_agent/memory_store.py` | 版本化快照、父/自身 manifest hash、原子写、frozen 独立产物、只读挂载 | P3-04 |
| `src/harmony_agent/fake_device.py` | mock 设备适配器 + 故障注入（noop/unknown/lock）+ `open_practice_runtime` | P3-01 |
| `src/harmony_agent/rsi.py` | wave runner、barrier、固定顺序合并、PracticeSelector | P3-02/P3-05 |
| `src/harmony_agent/reporting.py` | 计划 §8.3 报告 schema | P0-03 |
| `src/harmony_runtime/journal.py` / `runtime.py` / `mcp_server.py` | unknown write 可信对账（`mobile_session(operation="reconcile")`），两种证据、拒绝过期/证据不足、永不重放 | P1-04 |
| `scripts/baseline_freeze.py` / `secret_scan.py` / `experiment_report.py` | 基线冻结、秘密扫描、批量报告 | P0-01…P0-03 |
| `tools/rsi/author_wave.py` / `run_practice.py` / `freeze_memory.py` / `evaluate_decider.py` | 计划 §6 的 `tools/rsi` 落位 | Phase 3 / Phase 5 |

## 3. 秘密与隐私扫描（P0-02）

```text
命令     python scripts/secret_scan.py --report docs/acceptance/2026-09-20/secret-scan.json
范围     git 索引 + 受控文件工作树内容（224 个文件）
结果     findings = 0
复核     reviewed_allowlisted = 8（均为 device-serial，位于 README/device-baseline/audit/acceptance 文档，
         理由为“记录被测真机身份”）
规则     binary-artifact / private-key / service-token / device-serial / embedded-blob / raw-tree
```

未发现被跟踪的截图、原始树 dump、私钥或服务 token。

## 4. 离线练习 wave（Phase 3 冒烟）

```text
命令     python tools/rsi/author_wave.py --out .runtime/rsi/waves/default.json
         python tools/rsi/run_practice.py --approval-ref approval_0123456789abcdef01234567
结果     status=MERGED_WITH_BLOCKED
         branch_a 搜索闭环     verified
         branch_b 目标未达成   failed
         branch_c 脆弱成功     fragile_pass
         branch_d 未知写入     execution_unknown → blocked（未合并）
         merged = 3 experiences，memory v2 已冻结（manifest hash 见
         docs/acceptance/2026-09-20/offline-practice-wave-report.json）
```

该报告由 `harmony_agent.reporting` 转成计划 §8.3 结构，见
`docs/acceptance/2026-09-20/offline-practice-wave-report.json`（`kind=offline_practice_wave`，
`attempted=4 / success=2 / failed=1 / unknown=1 / safety_violations=0`）。
**这是 mock 设备结果，不是真机证据。**

## 5. 真机尝试（Phase 1 分级入口）

### 5.1 部署前置条件与失败模式（已修正，附复现实验）

真机分级入口 `scripts/accept_m0_primitives.py` 通过 stdio MCP 前端调用 Runtime，前端
（`harmony_runtime.cli mcp`）只连接**常驻服务**（`endpoint.json` + loopback HTTP）。
第一次尝试时没有常驻服务，只有 07:49 遗留的 `endpoint.json`：

```text
现象     AgentHarness.__aenter__ 1.5 s 成功；await open() 100 s 内无返回；
         M0 运行 25 分钟无 progress 文件、无派发、无 stderr
初版归因 前端读取到过期 endpoint.json 后长时间阻塞
修正归因 工具调用其实**很快失败**（`runtime_unavailable` / transport error）。真正的
         "静默停顿"来自**孤立的 stdio 子进程**：父进程在异常路径上没有关闭 stdio
         context，`harmony_runtime.cli mcp` 子进程继续存活并持有调用方的 stdout 管道，
         于是外部观察者看到的是"不返回、不输出、不退出"，而不是错误
复现     用相同 harness 在无服务目录下运行并把输出写入文件（不走管道）：
         `main_returning` 立刻写入，父进程却不退出；`Get-CimInstance` 仍看到
         `python -m harmony_runtime.cli mcp --state-dir .runtime\debug\no-service-dir` 存活
对照     hdc list targets 即时返回；doctor 全绿；常驻服务就绪后 MCP `open` 0.09 s；
         直接 Runtime（进程隔离 worker）open 0.02 s、FAST observe 5.7 s、catalog=242
```

本轮已修复（均为验收脚本侧，不触碰运行时安全策略）：

```text
scripts/agent_harness.py  AgentHarness.require_service()：启动前读 endpoint.json 并做
                          loopback TCP 存活探测；缺服务/端口已死分别给出明确错误与
                          启动命令，不再依赖 65 s 级超时
scripts/agent_harness.py  __aexit__：session context / stdio context / 日志各自 try，
                          保证 stdio 子进程一定被关闭，异常路径不再留下孤儿进程
tests/test_m0_setup_accounting.py  新增 3 项离线用例（缺 endpoint / 端口已死 / 服务可连）
```

含义：这是**部署顺序与异常退出**的失败模式（计划 §2.2 部署原则、P1-05 截止时间语义），
不是设备故障，也不会派发任何手机动作；但它会让一次真机批次无法诊断。

### 5.2 分级入口结果（`--only back input --per-primitive 3`）

```text
报告     docs/acceptance/2026-09-20/m0-staged-back-input-3.json
status   not_ready
duration 89.379 s
back     valid=3 success=3 rate=100%  setup_attempts=6 setup_stale_refusals=2 p50=5003 ms
input    valid=3 success=0 rate=0%    failure_code=input_field_missing ×3
         ("No single focused input field is observable", setup_actions=0)
session  unresolved_actions=0 recovery_required=false
gate     passed=false（primitive_success_rate_ok=false）
```

与 RC `19ee190` 的同一分级入口（back 3/3、input 3/3）相比，`back` 保持通过，`input`
本轮 3/3 失败且 setup 未发起任何动作。**这是必须继续定位的真机缺口**，当前不能声称
P1-02 通过；输入原语的有效样本仍为 0/3。

### 5.2b `input` 缺口定位与修复（同日，真机复测通过）

先复现（同一入口、同一个 3/3 失败、`setup_actions=0`），再用 `PrimitiveRunner.ensure_search_editor()`
直接取回编辑器观察，得到设备端事实：

```text
surface        search_editor（single_field=true, scroll_container=true）
field          type=TextInput, bounds=[141,145,1081,267], enabled=true, clickable=true
               focused=false, resource_id=""（空）, accessibility_id=13871（自增计数器）
因此           focused_input() → None（未聚焦）
               find_node(resource_id="search_input") → None（resource_id 为空）
               ⇒ 旧实现的两次查找全部落空 → input_field_missing
```

修复（验收脚本侧，测量语义不变）：

```text
accept_m0_primitives.py  _editor_field()：解析顺序 focused → 声明的 resource_id → 结构
                         （顶部带唯一输入框 + 结果列表），并记录 source
accept_m0_primitives.py  _focus_editor_field()：字段未聚焦时，先用一次有界 setup tap
                         聚焦并重新观察；聚焦失败记 setup 失败（input_focus_unavailable），
                         不计入原语分母；测量仍是随后的 replace_text（expected 仍为 None）
tests/test_m0_setup_accounting.py  新增 4 项：结构回退、resource_id 优先、focused 优先、
                         歧义/缺失判 absent；外加 2 项：聚焦计入 setup 且不改变测量、
                         拒绝聚焦记 setup 失败
```

真机复测（`--only input --per-primitive 3`）：

```text
报告     docs/acceptance/2026-09-20/m0-input-focus-fix-3.json
status   ok
input    valid=3 success=3 rate=100% p50=7703 ms primitive_failures=0
setup    attempts=1 stale_refusals=0
notes    input: field resolved via structure and is not focused; focusing as setup
         input: editor field focused via focused
session  unresolved_actions=0 recovery_required=false
```

### 5.2c 全量 smoke（7 原语 × 3）

```text
报告     docs/acceptance/2026-09-20/m0-smoke-3.json
status   not_ready（3 个样本/原语不足以证明 ≥99%，gate 未通过）
有效样本 20/21
launch 3/3 · tree 3/3 · swipe 3/3 · tap 3/3 · back 3/3 · input 3/3
screenshot 2/3（1 次 screenshot_inconsistent：动态信息流下图树括号不一致）
setup    swipe 6 次(3 stale) / back 6 次(3 stale) / input 1 次
session  unresolved_actions=0 recovery_required=false
```

`screenshot` 的单点不一致保持**拒绝**，没有放宽图树一致性检查；这是已知的动态页面事实，
不是可以用重试掩盖的缺陷。

### 5.2d M0 放大到 25/原语

```text
报告     docs/acceptance/2026-09-20/m0-primitives-25-20260920.json
status   not_ready（gate 未通过）
duration 1596.491 s（26.6 分钟）
有效样本 175/175（每个原语 valid_attempts == requested_samples）
成功     174/175 = 99.43%
session  unresolved_actions=0 recovery_required=false

原语        success/valid  rate    p50 ms   p95 ms   setup attempts / stale / failures
launch      25/25          100%    5117     5245     0    / 0  / 0
tree        25/25          100%    3840     3944     0    / 0  / 0
screenshot  24/25           96%    3804     3920     0    / 0  / 0   ← screenshot_inconsistent ×1
swipe       25/25          100%    6046     6434     3    / 0  / 0
tap         25/25          100%    5220     5491     0    / 0  / 0
back        25/25          100%    5138     5299     44   / 30 / 7
input       25/25          100%    7642     8097     1    / 0  / 0
```

结论：

* `input` 的修复在 25 个样本上成立（1 次 setup 聚焦，0 原语失败）。
* `back` 的 setup 仍然昂贵（44 次 setup 尝试、30 次派发前 stale 拒绝、7 次 setup 预算耗尽），
  但**没有一次进入原语分母**——这正是 setup/measurement 分离要表达的事实。
* gate 的唯一失败原因是 `screenshot` 的一次图树括号不一致（96% < 99%），与 RC 记录的历史
  失败模式一致；不通过放宽检查来"修好"它。
* 100/原语长批（约 3–4 小时连续真机窗口）尚未执行，P1-02 仍未关闭。

### 5.3 仍然阻塞的真机门槛

```text
P1-02  M0 每原语 100 个有效样本（≥99%）
P1-03  M1 冻结代码单批 30 次（≥27/30）
P1-05  Burst/Wait 真机语义与取消/重启复跑
P1-06  真机隐私配额与磁盘压力演练
P4-05  真实客户端（新会话加载 MCP 配置）smoke
```

### 5.4 M1 单批 30 次尝试（2026-09-21 凌晨，未完成）

```text
报告     docs/acceptance/2026-09-20/m1-weibo-30-20260921.json
命令     python scripts/accept_m1_weibo.py --execute --runs 3 \
         --report docs/acceptance/2026-09-20/m1-weibo-30-20260921.json
结果     只完成 5/30 次运行，全部 blocked、dispatches=0
         m1_01 run1  stale_observation                499.773 s
         m1_01 run2  stale_observation                412.218 s
         m1_01 run3  stale_observation                  9.766 s
         m1_02 run1  editor_unavailable               198.558 s
         m1_02 run2  editor_unavailable               201.047 s
中止原因 批量脚本在第 6 次运行时抛出
         `screen_locked: A confirmed awake, unlocked screen is required;
          automatic wake/unlock did not confirm readiness`（手机屏幕熄灭并上锁），
         且脚本未捕获该错误 → 直接带 traceback 退出，已收集的 5 次证据差点丢失
环境复核 hdc list targets 正常；`power-shell wakeup` 能点亮屏幕；但设备仍为
         **密码锁屏**，Runtime 依策略拒绝（不读取、不输入任何锁屏凭据）
含义     两个独立问题：
         1. 真机环境：长批量运行需要保持屏幕解锁；密码锁屏必须由人工解锁
         2. 验收脚本：设备级失败必须记为该 run 的 blocked 并写报告，不能中止整批
```

已修复（验收脚本侧）：`scripts/accept_m1_weibo.py` 新增 `DeviceFailureTracker`，
设备级失败（screen_locked / runtime_unavailable / runtime_transport_error /
device_unavailable / device_quarantined / timeout 等）记为该 run 的 blocked 并在
连续 3 次后带 `blocked_reason.stopped` 干净收尾；任务级拒绝（`stale_observation`、
`editor_unavailable`）不计入设备失败。新增 4 项离线用例
`tests/test_m1_batch_controls.py`。

**P1-03 仍未关闭**：需要设备保持解锁的连续窗口重跑完整 30 次。

## 6. 本轮结论

```text
Phase 0  verified_offline
Phase 2  verified_offline（协议、事件、Actor、Verifier 全部有离线测试）
Phase 3  verified_offline（mock 设备；wave barrier / 经验门 / memory freeze / tools/rsi）
Phase 1  P1-04 代码闭环；P1-02 做完全量 smoke 3 与 25/原语长批（174/175，99.43%，
         screenshot 1 次图树不一致使 gate 未过）；P1-03 尝试 30 次单批但被密码锁屏中断，
         需人工解锁后重跑
Phase 5  工具就绪但数据不足，继续保持 local_shadow，canary 未开启
```

任何上层成功率提升都没有被用来覆盖底层安全门槛；本轮没有修改安全策略，也没有放宽
stale guard、TTL、journal barrier 或“不确定写入不重试”。

## 7. 本轮修复清单（都是验收/脚本层，不触碰运行时安全策略）

| 缺陷 | 证据 | 修复 | 离线用例 |
|---|---|---|---|
| 缺常驻服务时 stdio 前端表现为静默停顿（孤儿子进程持有管道） | 复现实验：`main_returning` 已写入但父进程不退出、MCP 子进程仍存活 | `AgentHarness.require_service()` 预检 + `__aexit__` 逐步兜底关闭 | `tests/test_m0_setup_accounting.py::ServicePreflightTests`（3 项） |
| `input` 原语在 `resource_id` 为空且未聚焦时 0/3 | 真机观察：`resource_id=""`、`focused=false`、`accessibility_id=13871` | `_editor_field()` 三级解析 + `_focus_editor_field()` 有界 setup 聚焦 | `tests/test_m0_setup_accounting.py::EditorFieldResolutionTests`（6 项） |
| M1 批次遇到设备级失败直接中止、丢报告 | `screen_locked` traceback，已完成的 5 次运行未落入 totals | `DeviceFailureTracker`：记 blocked、连续 3 次后干净收尾 | `tests/test_m1_batch_controls.py`（4 项） |

离线回归：**723 项通过**（本轮 710 → 723）。`pip check`、`compileall`、
`git diff --check`、`secret_scan`（0 findings）均通过。

## 8. 设备锁定期间的离线推进（Phase 4 前半 + P1-04 收口 + P7-02）

手机处于密码锁屏期间，Runtime 依策略拒绝（不读、不输入锁屏凭据），因此把关键路径上
**不需要真机**的部分继续做完：

### 8.1 P1-04 收口：unknown write 可用任务自己的目标条件对账

缺口（第 9 节旧记录）：delegated/auto 计划生成的子目标 `expected` 为空，运行时只记录
「页面变了」，因此 agent 任务的未知写入只能以 `not_executed` 关闭，无法用
`postcondition_verified`。

```text
planner.py      Subgoal.recovery_expected + _bind_terminal_postcondition()：
                只把任务的语义终态条件（text_equals / foreground_is）绑定到**最后一步**；
                中间步骤保持 "page changed" 语义，避免把"目标还没达成"变成重复点击
runtime         ActRequest.recovery（可选、附加字段）：只作为 durable 恢复条件写入 journal，
                不改变 expected 的派发后校验；replace_text 仍按原规则不记录通用文本条件
supervisor      _dispatch 把 recovery 一并送进运行时；GuardedAction 增加 recovery 字段；
                派发事件记录 recovery_condition 布尔
supervisor      _failure_report()：子目标 blocked/failed 时补一次只读核验，失败任务不再
                报告空 conditions（"哪个条件没达成"必须可见）
tests           tests/test_agent_planner.py（3 项：只有最后一步绑定、非语义条件不绑定、
                参数引用保留）；tests/test_unknown_write_reconciliation.py（2 项：
                agent 任务的未知写入用 postcondition_verified 关闭且只派发 1 次；
                未达成的目标不会造成额外派发）
```

真机复述（离线证明）：agent 任务（`steps=tap:搜索`，终态 `text_equals 热搜榜`）的派发
响应丢失但动作已生效 → `reconcile(evidence_kind="postcondition_verified")` 关闭写屏障，
设备只收到 1 次派发。

### 8.2 P4-01 前半：Actor 接入任务循环（离线，走同一 guard）

```text
supervisor.py   TaskRun.actor / max_actor_replans（默认 2，可用
                HARMONY_AGENT_MAX_ACTOR_REPLANS 覆盖）/ actor_replans
                _block()：子目标 blocked 时给 Actor 一次有界重规划机会
                _actor_recovery()：构造**脱敏** ActorRequest（无 facade、无 catalog、
                无候选 id），提案经 Intent 契约校验后追加为新子目标，并写入新 plan version
host.py         AgentHost(actor=...) 注入；未注入时行为与之前完全一致
tests           tests/test_agent_actor_integration.py（6 项：恢复成功、请求脱敏、
                重规划次数上限、绕过 schema 的坐标提案不派发、Actor 故障降级、
                未注入 Actor 行为不变）
```

结论：Actor 只能提出结构化子目标；坐标/内联输入在契约层被拒；即使绕过 schema 也无法
派发（grounding 找不到语义目标）；重规划有硬上限且仍受 dispatch/时间预算约束。

### 8.3 P7-02：服务生命周期可诊断

```text
diagnostics.py  service_report(state_dir)：endpoint 文件 / 端口 / 记录的 pid 分开报告，
                只做 loopback 连接探测，不发任何 Runtime 请求、不碰手机
cli.py          harmony_runtime.cli service --state-dir <dir>（只读；ok 退出 0，否则 1）
doctor          新增 service 检查项（设备探测仍是最后一项）
tests           tests/test_service_diagnostics.py（5 项：缺失/格式错误/端口已死/可连接/
                doctor 报告包含 service）
真机证据（本轮） service 命令在服务存活时 ok/service_reachable（exit 0）；
                服务停止后 not_ready/service_endpoint_stale（exit 1，报出端口与重启提示）
```

本轮合计离线回归 **739 项通过**（723 → 739），`pip check`、`compileall`、
`git diff --check`、`secret_scan`（0 findings）全部通过。

## 9. 设备锁定期间的第二批离线推进（P6-01 / P6-02 / P7-05）

设备仍为密码锁屏（第 3 个 goal 轮次），继续推进计划中不依赖真机的部分。

### 9.1 P6-01 子目标级 checkpoint

```text
supervisor.py  execute() 在每一步开始/结束写 checkpoint 事件：
               phase / subgoal_id / description / action_kind / status /
               blocked_reason / attempts / plan_version / replan_reasons(last 2) /
               outcome / remaining(dispatches,seconds,model_calls) /
               dispatches_used / observations
supervisor.py  task_status 增加 current_subgoal（live run 的下一个待执行子目标）
tests          tests/test_agent_long_task.py：两步任务产生 ≥4 条 checkpoint（每步子
               开始+结束），plan_version 与 remaining 齐全，结束后 current_subgoal 为 null
```

之前只有计划版本落库，运行中无法从日志重建"跑到哪一步、还剩多少预算、这一步为什么存在"。

### 9.2 P6-02 在线上下文压缩（真正触发 + 压缩后校验）

```text
memory.py      window_used_tokens() / over_window()；compression_questions() 与
               missing_after_compression()（供 runner 与测试共用）
supervisor.py  _compress_context_if_needed()：当 states ≥ compaction_states(默认 12) 且
               窗口超预算时压缩；写 memory_compressed 事件（states before/after、
               kept constraints/facts、unresolved incidents、evidence_refs、
               questions_checked、missing）
supervisor.py  压缩后立即用"任务目标 + 约束 + 未决 incident 代码"做 can_answer 校验；
               任何缺失都会写 context_loss 事件并把 context_loss:... 计入结果 limitations
host.py        memory_window_tokens / memory_compaction_states 可注入（便于有界验证与现场调参）
tests          4 项：每步 checkpoint、压缩确实触发且 missing 为空、校验能失败（丢失约束会被
               点名）、未决 incident 在压缩后仍可回答
```

此前 `Memory.compress()` 存在但**从未被在线路径调用**，属于"能力存在但未生效"。

### 9.3 P7-05 回滚 runbook（记忆层可执行）

```text
memory_store.py  rollback(to_version, reason)：以追加方式回滚——新版本条目为目标旧版本的
                 条目，parent_manifest_hash = 当前 tip；写 rollbacks/vNNNNNN.json 审查记录
                 （from_version / to_version / reason / manifest_hash / at）
                 rollbacks() 只读读取审查记录
docs/runbook-rollback.md  三层回滚：Runtime（服务停止→切 RC→复跑回归→只读冒烟）、
                 Decider（local_canary→local_shadow→local_off，附 canary gate 依据）、
                 memory snapshot（追加式回滚 + freeze + verify）；并明确回滚不撤销手机侧
                 已执行动作、未知写入必须先按 reconcile 对账
tests           tests/test_rsi_learning.py 2 项：回滚只追加且历史可读、冻结快照条目等于目标
                 旧版本；对当前 tip 或未知版本回滚被拒绝（already_at_version / snapshot_missing）
实跑演练         versions [1,2] → rollback(1) → v3（parent = v2）→ freeze(v3).verify() = True，
                 条目 = ["a"]，rollbacks() 记录 from=2/to=1，load(1)/load(2) 仍可读
```

本轮合计离线回归 **745 项通过**（739 → 745），`pip check`、`compileall`、
`git diff --check`、`secret_scan`（0 findings）全部通过。
