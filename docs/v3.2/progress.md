# v3.2 进度台账（本实现仓库）

对照 `HarmonyOS_Agent_v3.2_完整项目开发计划.md` 与
`Codex_HarmonyOS_Agent_v3.2_无真机完整开发提示词.md`。

状态词汇（只使用这些值）：

```text
planned         已规划，未开始
in_progress     正在进行
implemented     代码已存在，未取得离线证据
verified_offline 离线测试/脚本通过
blocked_device  必须真机，当前无法执行
verified_device 真机证据通过（须绑定 revision / task-set / device build / run_id）
historical 旧 revision 或未绑定证据，只作历史记录，不进入 current 数字
deferred        明确推迟，不阻塞发布
```

本轮证据：`docs/v3.2/baseline-offline.md`、`.runtime/tests-current.json`
（本地，不提交）。真机待办：`docs/v3.2/blocked-device.md`。
**权威 current run：** `docs/acceptance/2026-09-24/`（`20260924T152342Z` @ `6b78c76`，
离线 809/809）。2026-09-22 设备窗口曾取得 M0 formal 699/700、M1 formal 30/30、
长任务与 M2 smoke，但 formal 产物未绑定 revision，状态为 `historical`，
详见 `docs/acceptance/2026-09-24/run-manifest.json` 与 `docs/acceptance/current-run/`。

## 1. 阶段总览

| Phase | 工作 | 状态 | 说明 |
|---:|---|---|---|
| 0 | Offline baseline | **verified_offline** | 变更前 346 / Phase 1-2 后 392 / 离线阶段全部完成后 562，全绿；见 `baseline-offline.md` |
| 1 | Decider 真正插件化 | **verified_offline** | 惰性 factory、协议类型依赖、泛化 diagnostics、故障矩阵 35 项 |
| 2 | Direct / Autonomous 边界冻结 | **verified_offline** | v1 六工具无宿主可用；宿主初始化失败降级；关闭宿主不影响 Runtime；11 项 |
| 3 | Observation / cache 语义审计 | **verified_offline** | `observation-cache-semantics.md`；14 项；性能 `blocked_device` |
| 4 | Grounding 离线 fixture 强化 | **verified_offline** | 23 个 fixture 用例；发现并记录 hint 限制 |
| 5 | Visual Target Authority ADR | **verified_offline** | ADR + Runtime 复核实现 + 19 项；真机执行 `blocked_device` |
| 6 | OCR adapter | **verified_offline（代码）** | `harmony_agent/ocr.py`；真机 OCR `blocked_device` |
| 7 | VLM visual grounding | **verified_offline（代码）** | `harmony_agent/vlm.py`；真机 VLM `blocked_device` |
| 8 | Risk Policy 统一 | **verified_offline** | `harmony_runtime/risk.py` 单一词表；17 项 |
| 9 | Burst / Wait / History 离线加固 | **verified_offline** | 10 项边界用例；真机时延 `blocked_device` |
| 10 | Autonomous Runner 离线收敛 | **verified_offline** | 15 项（记忆/Checker/任务级） |
| 11 | M2 任务规格（30 任务） | **verified_offline（规格）** | `evals/tasks/m2-30.json` + 13 项校验；执行 `blocked_device` |
| 12 | 离线故障注入矩阵 | **verified_offline** | 14 项；真机故障 `blocked_device` |
| 13 | 安装 / 迁移 / 回滚 | **verified_offline** | 9 项；干净环境安装与跨版本回滚仍待真机/新环境复跑 |
| — | 真机 Acceptance Sprint | **ready（ALLOW_DEVICE_TEST=YES）** | 见 `blocked-device.md`；用 `device-test-handoff.md` 的 NEW RC `b4049f5` 从 Stage 3 重新开始 |
| — | 三客户端对照（Codex/OpenCode/DeepSeek） | **deferred** | 非发布阻塞；Runtime 保持 client-agnostic |
| — | Pre-Device Release Gate（Gate A–G） | **verified_offline** | `pre-device-gate.md`；6 个真实缺陷已闭环，测试 562 → 607 |
| — | RC 重新冻结（OFFLINE-1 / OFFLINE-2） | **verified_offline** | `pre-device-gate.md` 第 9 节；终态与 result 原子可见、单测与机器本地 token 解耦，测试 607 → 618 |

## 2. Phase 0 — Offline Baseline

状态：`verified_offline`

```text
branch   feat/runtime-foundation
HEAD     8c401c07eede3686349eaa50ead3d0abbd135577（+ 未提交的 Phase 1/2 修改）
Python   3.11.9
tests    346（变更前）→ 392（Phase 1/2 后）→ 562（离线阶段全部完成后），
         0 failed / 0 error / 0 skip
pip      No broken requirements found.
```

已修正 `docs/agent-status.md` 中 336 / 346 的不一致口径。

## 3. Phase 1 — Decider Pluginization

状态：`verified_offline`

### 3.1 变更前后

| 维度 | before | after |
|---|---|---|
| 具体类型依赖 | `AgentHost` 顶层 `from .decision.providers.decider import …` | 只依赖 `DecisionProvider` Protocol |
| 默认构造 | `self.decider = decider or DeciderProvider(...)`（始终构造） | 惰性 factory，仅 `local_shadow` / `local_canary` 且请求时才构造 |
| Router 类型 | `decider: DeciderProvider \| None` | `fast_provider: DecisionProvider \| None`（`decider` 兼容别名） |
| diagnostics | 直接读 Decider 私有属性 `revision` / `breaker` | `provider_name` / `provider_revision` / `provider_health` / `provider_available` / `circuit_open` |
| 包导入 | `decision.providers` 顶层 import Decider | PEP 562 惰性 `__getattr__` |
| 默认 profile | `local_shadow` | `local_off`（当前开发默认，符合 v3.2 政策） |

### 3.2 组件

```text
src/harmony_agent/decision/factory.py   ProviderConfig / ProviderBuild /
                                        resolve_fast_provider / build_fast_provider /
                                        config_from_env
src/harmony_agent/decision/tokens.py    MAX_STATE_TOKENS / MAX_QUESTION_TOKENS /
                                        MAX_QUESTIONS / estimate_tokens（与 provider 无关）
```

Provider Profile 行为（未新增 `active`）：

```text
rules_only    rules only；不 import、不构造、不健康检查
local_off     rules only；同上（当前开发默认）
local_shadow  rules 决定执行；provider 可选调用；结果只记录、绝不派发
local_canary  provider 可选；需 calibration_version + 双阈值 + 低风险 +
              候选有效；最终仍走 Runtime Guard
```

### 3.3 失败隔离

`Router` 现在把三类 provider 故障统一降级为 rules 结论并记录原因码：

```text
ProviderUnavailable        -> fallback_<code>（如 model_busy / model_timeout）
其它异常（provider crash）  -> fallback_provider_error（fallback_reason=provider_error:<Type>）
返回值不符合 ProviderResult -> fallback_malformed_result
```

覆盖的故障：module missing、factory 返回 None、connection refused、timeout、503、
504、invalid/empty body、invalid schema、NaN、Inf、unknown candidate、expired
candidate、old epoch、late answer、revision mismatch、provider crash。

## 4. Phase 2 — Direct / Autonomous Boundary

状态：`verified_offline`

### 4.1 边界定义（冻结）

```text
Direct Runtime      = mobile_session / observe / act / wait / burst / history
                      不依赖 AgentHost / Planner / Supervisor / Memory / Checker / Decider
Autonomous Layer    = HARMONY_AGENT_TOOLS=1 才注册 mobile_run_task / task_* / mobile_decide
                      与 Direct 共用 Session / Observation / Epoch / Guard / Journal / Worker
```

### 4.2 本轮补齐的缺口

`service.serve` 过去在 `host_factory` 抛异常时会让整个常驻服务退出，等于“Agent 层坏了
顺手带走 v1”。现在：

```text
host_factory 抛异常
  -> HARMONY_AGENT_REQUIRED 未开启：host=None，stderr 记录一行，六工具继续服务
  -> HARMONY_AGENT_REQUIRED=1：fail fast，不静默降级
```

### 4.3 离线证据

```text
无宿主时六工具端到端可用（session/observe/act/wait/history）
无宿主时 agent_* RPC 名未注册（unknown method）
无宿主时 Runtime 生命周期独立可用
宿主初始化失败后六工具仍可用、writes=1（无重复派发）
关闭 AgentHost 不关闭 Runtime，之后仍可 observe/act
AgentHost 构造不打开设备
host_from_env 未开启返回 None；开启后默认 profile=local_off
```

## 5. Phase 3–13 摘要

详细说明分散在下列文件，避免本台账重复：

```text
Phase 3   docs/v3.2/observation-cache-semantics.md
Phase 4   tests/test_agent_grounding_fixtures.py（23 项 fixture 回归）
Phase 5   docs/v3.2/adr-visual-target-authority.md
Phase 6/7 docs/v3.2/visual-providers.md
Phase 8   docs/v3.2/risk-taxonomy.md
Phase 9/10/12/13  docs/v3.2/offline-hardening.md
Phase 11  docs/v3.2/m2-benchmark-spec.md
```

本阶段真正改动的**产品代码**（区别于纯测试/文档）：

```text
src/harmony_agent/decision/factory.py      新增：惰性 provider factory
src/harmony_agent/decision/tokens.py       新增：与 provider 无关的预算常量
src/harmony_agent/ocr.py                   新增：OCR 适配器
src/harmony_agent/vlm.py                   新增：VLM 视觉定位适配器
src/harmony_agent/visual_regions.py        新增：区域归一化
src/harmony_runtime/risk.py                新增：统一风险词表（唯一真源）
src/harmony_runtime/visual.py              扩展：区域裁剪与内容摘要
src/harmony_runtime/contracts.py           扩展：Target.visual / VisualRegion
src/harmony_runtime/observation.py         扩展：视觉目标复核
src/harmony_runtime/runtime.py             扩展：视觉 preflight/策略/运输异常归类
src/harmony_runtime/service.py             扩展：宿主构建失败降级
src/harmony_agent/grounding.py             扩展：可复核视觉候选 + 兜底
src/harmony_agent/supervisor.py            扩展：视觉句柄透传
src/harmony_agent/candidates.py            收敛：复用共享风险词表
```

## 6. 已知限制与风险

```text
真机：全部设备结论仍未取得（blocked_device）
视觉：链路已闭环（提议→复核→Guard→dispatch→verify），但真实 OCR/VLM 引擎与
      真机像素未验证；带图 preflight 的真实开销未知
视觉限制：只支持 tap/long_press；输入必须落到可观察输入框
风险策略：词表法只能防已知词，同义改写与多语言未覆盖；命中即拒绝（无可信批准流程）
Grounding 限制：输入框 placeholder（hint）不参与文本匹配，必须以 id/a11y 定位
观察缓存：Session.observations 仍是 authority cache，性能复用层未引入（有意）
任务级 model_profile：TaskSubmit 声明了该字段，runner 使用宿主 profile，未做差异审计
Phase 13：干净环境安装与跨版本回滚只在离线状态目录层面验证
```

## 7. Pre-Device Release Gate（2026-09-20）

在进入真机测试前做了一次反向审计，6 个 Gate 中 6 个都确认是**真实缺陷**并已闭环：

```text
A 派发前从不 registry.resolve()      → 现在派发关口复核签发/观察/epoch/TTL
B ADR 承诺与 Direct 真实语义不一致    → 分层契约写清，视觉句柄绑定 observation
C 视觉风险只信 proposer label         → 并入设备侧重叠文本证据（取并集）
D task.model_profile 被静默忽略       → 部署 profile 为权威，禁止任务提权
E provider 失败调用不计数             → RouterOutcome.provider_calls 显式计费
F OCR/VLM 无 deadline 所有权          → 有界调用 + 超时隔离 + 生产要求声明
G 任意 calibration 字符串即开 canary  → 必须完整 artifact，否则 fail closed
```

详见 [pre-device-gate.md](pre-device-gate.md) 与 [device-test-handoff.md](device-test-handoff.md)。
测试数 562 → 607（新增 adversarial 38 项 + soak 5 项 + 契约收紧 2 项）。

## 8. 下一步

离线部分已按计划推进到 Phase 13。真机恢复后按 `blocked-device.md` 顺序执行
Device Acceptance Sprint；在此之前不因缺少真机而跳到 Decider 优化
（Decider 重评估为 P2，见 `blocked-device.md` 第 9 节）。

## 9. RC 重新冻结（2026-09-20，第二轮）

真机测试机在 fresh clone 上重跑 Stage 3 时 3/3 失败，确认两个 release blocker：

```text
OFFLINE-2  任务状态先于 result 持久化 → RECONCILIATION_REQUIRED 可见但 result 读不到
OFFLINE-1  DeciderProvider 单测隐式依赖 gitignore 的 services/decider/.runtime/api-token
```

修复与回归：

```text
fix        TaskStore.finalize_task() 单事务写 state + result + 收尾事件；
           _finish() 改用它；删除 _run_subgoal() 提前发布终态的写操作
tests      test_task_finalize_atomicity.py（500 次终态竞态 + 10 次端到端）
           test_fresh_clone_semantics.py（local_off / local_shadow / 临时 token 三态）
           测试代码改用 agent_fakes.make_decider_provider() 的临时 token；
           生产 token 逻辑未改（默认仓库相对路径，HARMONY_DECIDER_TOKEN_FILE 可覆盖）
```

Gate 结果：

```text
fresh clone     Python 3.13.14 与 3.11.16 各 Ran 618 tests，OK，pip check clean
稳定性          修复后连续 6 次全量回归 exit 0（修复前 3/3 失败）
竞态敏感性      改回旧发布顺序后新测试在 attempt 0 抓到 2 次可见窗口，改回修复版后全绿
adversarial     58 项通过
fault matrix 等 93 项通过
soak            3000 周期 / 48 任务：48/48 SUCCEEDED，750 次派发，0 错误，线程 1 → 1
```

```text
previous RC  80e28988c98e4f8cb75c3f4d976d637eaa31c22a
             DEVICE_BASELINE_READY，但被拒绝继续验收（Stage 3 不可 fresh-clone 复现）
NEW RC       b4049f50b851241817e0be577e080bcbcf8519a8
ALLOW_DEVICE_TEST = YES
```

真机正式证据必须从 **Stage 3 Offline Regression** 重新开始，用新 RC，不要复用旧 RC 的数据。

## 10. RC 重新冻结（第三轮）：M0 动态页面 / acceptance harness

第二轮 RC `b4049f5` 的真机 M0 smoke 判 `NOT_READY`（back 0/3、input 1/3），
失败码全是 `search_editor_unavailable`。第三轮只修 harness，不动 Runtime 安全语义：

```text
根因      ① setup 失败被计成原语失败（分母被污染）
          ② foreground_bundle=null 时桌面被 surface_kind 判成 discover
          ③ target_fingerprint 对整棵子树取哈希（装饰性动画即漂移）
          ④ 主导：微博发现页搜索入口的 accessibilityId 是自增计数器
             （31985 → 31995 → 32000），目标比较命中它 → 派发前 stale
修改      agent_harness.py：SetupUnavailable / SetupBudget / SetupStats /
          setup_act()（拒绝后重新 observe + 重新定位）/ has_weibo_evidence /
          drift_metadata；surface_kind 无 App 证据时返回 unknown
          accept_m0_primitives.py：valid_attempts 与 setup_* 分离、
          evaluate_gate()、schema_version=2、setup 失败上限
测试      644（618 + 26）
newrc     19ee190503ccdedd284e31f1e3fb3b0867a13f1e

真机 smoke 验证（同一 RC）：
  M0 专项 back/input ×3   status=ok，valid=6 success=6（b4049f5 时为 back 0/3、input 1/3）
  全量 M0 smoke ×3        status=ok，valid=21 success=21，7 原语全部 3/3，
                          setup_stale_refusals=2，unresolved_actions=0
  说明：这是 smoke，不是正式 M0（正式需每原语 100 个有效样本且 ≥99%）

注：本轮曾先冻结候选 ef2731a，第一次真机专项暴露 setup 统计未接入 per-primitive
记录的报告缺陷，修复后重新冻结为 19ee190。
```

安全侧：stale Guard / TTL / journal / unknown-write 语义全部未改。
下一阶段的真机顺序：Stage 3 → 5–7 → M0 专项 back/input ×3 → 全量 M0 ×3 → 25 → 100。
